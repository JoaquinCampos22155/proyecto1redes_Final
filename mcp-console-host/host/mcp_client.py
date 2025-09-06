# host/mcp_client.py
from __future__ import annotations
import json, subprocess, sys, threading, queue, time, os, shlex, traceback
from typing import Any, Dict, Optional, List
from host.settings import (
    MCP_SERVER_CMD,
    LOG_FILE,
    REQUEST_TIMEOUT_SEC,
    STARTUP_TIMEOUT_SEC,
    MAX_RETRIES,
    DEBUG,
)

class MCPClient:
    """
    Cliente STDIO para un servidor MCP (JSON-RPC 2.0).
    - Transport: newline-delimited JSON (el server responderá en el mismo modo).
    - Demultiplexing por id: permite varias llamadas concurrentes.
    - Reintentos opcionales en errores transitorios (proceso caído).
    - Logging JSONL: req/resp (y stderr del MCP si DEBUG=True).
    """

    def __init__(self,
                 server_cmd: Optional[List[str] | str] = None,
                 log_path: str = LOG_FILE,
                 request_timeout: float = REQUEST_TIMEOUT_SEC,
                 startup_timeout: float = STARTUP_TIMEOUT_SEC,
                 max_retries: int = MAX_RETRIES) -> None:
        self.server_cmd = self._normalize_cmd(server_cmd or MCP_SERVER_CMD)
        self.log_path = log_path
        self.request_timeout = float(request_timeout)
        self.startup_timeout = float(startup_timeout)
        self.max_retries = int(max_retries)

        self.proc: Optional[subprocess.Popen] = None
        self._rid = 0
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None

        # Map de requests en curso: id -> Queue para recibir respuesta
        self._pending: dict[int, "queue.Queue[dict]"] = {}
        self._pending_lock = threading.Lock()

        # buffer de líneas "huérfanas" (si llegan responses que nadie espera)
        self._orphan_q: "queue.Queue[dict]" = queue.Queue()

        # asegurar carpeta de logs
        try:
            log_dir = os.path.dirname(self.log_path) or "."
            os.makedirs(log_dir, exist_ok=True)
        except Exception:
            pass

    # ------------- Utilidades internas -------------

    @staticmethod
    def _normalize_cmd(cmd: List[str] | str) -> List[str]:
        if isinstance(cmd, list):
            return cmd
        # En Windows usar posix=False para conservar rutas con espacios
        posix = os.name != "nt"
        return shlex.split(cmd, posix=posix)

    def _log(self, kind: str, payload: Dict[str, Any]) -> None:
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "kind": kind, **payload}, ensure_ascii=False) + "\n")
        except Exception:
            # No frena el flujo si falla el log
            pass

    def _next_id(self) -> int:
        self._rid += 1
        return self._rid

    def _reader_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                # Si llegó algo que no es JSON válido (no debería), lo anotamos y seguimos
                self._log("stdout_noise", {"line": line})
                continue

            # despachar por id
            rid = obj.get("id")
            if isinstance(rid, int):
                with self._pending_lock:
                    q = self._pending.get(rid)
                if q:
                    q.put(obj)
                else:
                    self._orphan_q.put(obj)
            else:
                self._orphan_q.put(obj)

    def _reader_stderr(self) -> None:
        # Captura stderr del servidor (útil para depurar). Se loguea solo si DEBUG=True
        if not DEBUG:
            # Aún así, drena para evitar bloquear si el buffer se llena
            assert self.proc and self.proc.stderr
            for _ in self.proc.stderr:
                pass
            return

        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            try:
                self._log("mcp_stderr", {"line": line.rstrip("\n")})
            except Exception:
                pass

    def _ensure_running(self) -> None:
        if self.proc and (self.proc.poll() is None):
            return
        # (Re)lanzar proceso
        self._spawn()

    def _spawn(self) -> None:
        # Inicia el proceso MCP
        self._log("spawn", {"cmd": self.server_cmd})
        try:
            self.proc = subprocess.Popen(
                self.server_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # line-buffered
                encoding="utf-8",
                errors="replace",
            )
        except Exception as e:
            raise RuntimeError(f"No se pudo lanzar el MCP: {e}")

        # Hilos de lectura
        self._stdout_thread = threading.Thread(target=self._reader_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._reader_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

        # Pequeña espera de arranque
        t0 = time.time()
        while (time.time() - t0) < self.startup_timeout:
            if self.proc and self.proc.poll() is None:
                return
            time.sleep(0.05)
        # Si llegó aquí, igual consideramos "arrancado"; la verificación real ocurre en el primer _send.

    def _send_once(self, obj: Dict[str, Any]) -> Dict[str, Any]:
        if not self.proc:
            raise RuntimeError("MCP process no iniciado")
        if not self.proc.stdin:
            raise RuntimeError("stdin del MCP no disponible")

        rid = obj.get("id")
        if not isinstance(rid, int):
            raise ValueError("Request JSON-RPC debe tener 'id' entero")

        # crear queue para esta respuesta
        q: "queue.Queue[dict]" = queue.Queue()
        with self._pending_lock:
            self._pending[rid] = q

        data = json.dumps(obj, ensure_ascii=False)
        self._log("req", {"payload": obj})

        try:
            self.proc.stdin.write(data + "\n")
            self.proc.stdin.flush()
        except Exception as e:
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise RuntimeError(f"Error escribiendo a MCP: {e}")

        # esperar respuesta en su queue (o timeout)
        try:
            resp = q.get(timeout=self.request_timeout)
        except queue.Empty:
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise TimeoutError(f"Timeout esperando respuesta del MCP (>{self.request_timeout}s)")

        self._log("resp", {"payload": resp})
        with self._pending_lock:
            self._pending.pop(rid, None)
        return resp

    def _send(self, obj: Dict[str, Any]) -> Dict[str, Any]:
        """
        Envia un request JSON-RPC al MCP con reintentos si el proceso murió.
        """
        attempts = 0
        last_exc: Optional[BaseException] = None

        while attempts <= self.max_retries:
            attempts += 1
            try:
                self._ensure_running()
                return self._send_once(obj)
            except (BrokenPipeError, TimeoutError, RuntimeError) as e:
                last_exc = e
                # Intento de reinicio limpio
                try:
                    self.stop()
                except Exception:
                    pass
                time.sleep(0.2)  # backoff corto
                continue
            except Exception as e:
                # Errores no transitorios: burbujear
                raise

        # agotados reintentos
        if last_exc:
            raise last_exc
        raise RuntimeError("Fallo al enviar request al MCP")

    # ------------- API pública -------------
    def start(self) -> "MCPClient":
        self._ensure_running()
        return self

    def stop(self) -> None:
        if not self.proc:
            return
        try:
            if self.proc.stdin:
                try:
                    self.proc.stdin.flush()
                except Exception:
                    pass
            self.proc.terminate()
            # si no muere, forzar
            try:
                self.proc.wait(timeout=1.5)
            except Exception:
                self.proc.kill()
        except Exception:
            pass
        finally:
            self.proc = None

    def __enter__(self) -> "MCPClient":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # ---- JSON-RPC helpers ----
    def ping(self) -> Dict[str, Any]:
        req = {"jsonrpc": "2.0", "id": self._next_id(), "method": "ping"}
        return self._send(req)

    def tools_list(self) -> Dict[str, Any]:
        req = {"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list"}
        return self._send(req)

    def tools_call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(name, str) or not name:
            raise ValueError("name inválido para tools/call")
        if not isinstance(arguments, dict):
            raise ValueError("arguments debe ser un objeto")
        req = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        return self._send(req)
