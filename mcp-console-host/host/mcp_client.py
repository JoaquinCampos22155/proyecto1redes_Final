# host/mcp_client.py
from __future__ import annotations
import json, subprocess, sys, threading, queue, time, os, shlex, traceback
from typing import Any, Dict, Optional, List
from urllib import request as urlrequest
from urllib import parse as urlparse
import re  
from urllib import error as urlerror

from host.settings import (
    MCP_SERVER_CMD,
    LOG_FILE,
    REQUEST_TIMEOUT_SEC,
    STARTUP_TIMEOUT_SEC,
    MAX_RETRIES,
    DEBUG,
    MCP_SERVER_URL,
    MCP_SERVER_TRANSPORT,
)

class MCPClient:
    """
    Cliente para un servidor MCP (JSON-RPC 2.0).

    Transportes soportados:
      - "stdio": lanza un proceso local (server.py) y habla por STDIN/STDOUT (líneas JSON).
      - "sse":   abre un stream SSE a MCP_SERVER_URL (Cloudflare Worker) y envía requests
                 vía POST /sse/message con connectionId. Recibe responses por el stream.

    API pública:
      - start(), stop(), __enter__/__exit__
      - ping(), tools_list(), tools_call(name, arguments)
    """

    def __init__(self,
                 server_cmd: Optional[List[str] | str] = None,
                 log_path: str = LOG_FILE,
                 request_timeout: float = REQUEST_TIMEOUT_SEC,
                 startup_timeout: float = STARTUP_TIMEOUT_SEC,
                 max_retries: int = MAX_RETRIES) -> None:

        # Modo de transporte
        self.mode = (MCP_SERVER_TRANSPORT or "stdio").lower()
        self.is_sse = (self.mode == "sse" and bool(MCP_SERVER_URL))

        # ---- Config común
        self.log_path = log_path
        self.request_timeout = float(request_timeout)
        self.startup_timeout = float(startup_timeout)
        self.max_retries = int(max_retries)

        # Estado común
        self._rid = 0
        self._pending: dict[int, "queue.Queue[dict]"] = {}
        self._pending_lock = threading.Lock()
        self._orphan_q: "queue.Queue[dict]" = queue.Queue()

        # asegurar carpeta de logs
        try:
            log_dir = os.path.dirname(self.log_path) or "."
            os.makedirs(log_dir, exist_ok=True)
        except Exception:
            pass

        # ---- STDIO
        self.server_cmd = self._normalize_cmd(server_cmd or MCP_SERVER_CMD)
        self.proc: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None

        # ---- SSE
        self.sse_url: Optional[str] = MCP_SERVER_URL if self.is_sse else None
        self._sse_resp = None  # HTTPResponse (file-like)
        self._sse_thread: Optional[threading.Thread] = None
        self._sse_stop = threading.Event()
        self._sse_connected = threading.Event()
        self._sse_connection_id: Optional[str] = None

    # ------------- Utilidades internas -------------

    @staticmethod
    def _normalize_cmd(cmd: List[str] | str) -> List[str]:
        if isinstance(cmd, list):
            return cmd
        posix = os.name != "nt"
        return shlex.split(cmd, posix=posix)

    def _log(self, kind: str, payload: Dict[str, Any]) -> None:
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "kind": kind, **payload}, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _next_id(self) -> int:
        self._rid += 1
        return self._rid

    # ===========================
    #       STDIO  (local)
    # ===========================
    def _reader_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                self._log("stdout_noise", {"line": line})
                continue
            self._dispatch_response(obj)

    def _reader_stderr(self) -> None:
        if not DEBUG:
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

    def _ensure_running_stdio(self) -> None:
        if self.proc and (self.proc.poll() is None):
            return
        self._spawn_stdio()

    def _spawn_stdio(self) -> None:
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

        self._stdout_thread = threading.Thread(target=self._reader_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._reader_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

        t0 = time.time()
        while (time.time() - t0) < self.startup_timeout:
            if self.proc and self.proc.poll() is None:
                return
            time.sleep(0.05)

    def _send_once_stdio(self, obj: Dict[str, Any]) -> Dict[str, Any]:
        if not self.proc:
            raise RuntimeError("MCP process no iniciado")
        if not self.proc.stdin:
            raise RuntimeError("stdin del MCP no disponible")

        rid = obj.get("id")
        if not isinstance(rid, int):
            raise ValueError("Request JSON-RPC debe tener 'id' entero")

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

    # ===========================
    #       SSE (remoto)
    # ===========================
    def _open_sse(self) -> None:
        assert self.sse_url
        req = urlrequest.Request(self.sse_url, headers={
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "User-Agent": "mcp-host/0.1",
        })
        self._log("sse_open", {"url": self.sse_url})
        try:
            self._sse_resp = urlrequest.urlopen(req, timeout=self.startup_timeout)
        except Exception as e:
            raise RuntimeError(f"No se pudo abrir SSE en {self.sse_url}: {e}")

    def _sse_reader(self) -> None:
        """
        Lee eventos SSE y enruta JSON-RPC responses a las colas por id.
        Espera eventos con estructura como:
          {"type":"connect","payload":{"connectionId":"..."}}
          {"type":"message","payload":{"message": {...jsonrpc response...}}}
        """
        fp = self._sse_resp
        if not fp:
            return
        data_lines: List[str] = []
        event_type: Optional[str] = None

        def handle_event(evt_type: Optional[str], data_text: str):
            if not data_text:
                return

            # Caso 1: Cloudflare template manda solo la ruta con sessionId
            m = re.search(r"/sse/message\?sessionId=([A-Za-z0-9_-]+)", data_text)
            if m:
                self._sse_connection_id = m.group(1)
                self._sse_connected.set()
                self._log("sse_connect", {"sessionId": self._sse_connection_id})
                return

            # Caso 2: JSON
            try:
                payload = json.loads(data_text)
            except Exception:
                self._log("sse_noise", {"data": data_text})
                return

            # Algunos templates envían directamente el JSON-RPC como data
            if isinstance(payload, dict) and payload.get("jsonrpc") == "2.0":
                self._dispatch_response(payload)
                return

            # Formatos alternos (poco comunes)
            t = payload.get("type") or evt_type or "message"
            if t == "connect":
                cid = payload.get("payload", {}).get("connectionId")
                if isinstance(cid, str) and cid:
                    self._sse_connection_id = cid
                    self._sse_connected.set()
                    self._log("sse_connect", {"connectionId": cid})
                return
            if t == "message":
                msg = payload.get("payload", {}).get("message")
                if isinstance(msg, dict) and msg.get("jsonrpc") == "2.0":
                    self._dispatch_response(msg)
                    return

            self._log("sse_unhandled", {"payload": payload})


        try:
            for raw in fp:
                if self._sse_stop.is_set():
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line == "":
                    # Fin de evento
                    data_text = "\n".join(data_lines).strip()
                    handle_event(event_type, data_text)
                    data_lines.clear()
                    event_type = None
                    continue
                if line.startswith(":"):
                    # comentario SSE, ignorar
                    continue
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                    continue
                # Cualquier otra línea la guardamos como data bruta
                data_lines.append(line)
        except Exception as e:
            if not self._sse_stop.is_set():
                self._log("sse_reader_error", {"error": str(e)})

    def _ensure_running_sse(self) -> None:
        if self._sse_thread and self._sse_thread.is_alive() and self._sse_connected.is_set():
            return
        self._sse_stop.clear()
        self._open_sse()
        self._sse_thread = threading.Thread(target=self._sse_reader, daemon=True)
        self._sse_thread.start()

        # Esperar connect con connectionId
        ok = self._sse_connected.wait(timeout=self.startup_timeout)
        if not ok or not self._sse_connection_id:
            raise RuntimeError("No se recibió 'connect' desde el servidor SSE (sin connectionId).")

    def _send_once_sse(self, obj: Dict[str, Any]) -> Dict[str, Any]:
        """
        Envía el request JSON-RPC crudo por POST a:
        <base>/sse/message?sessionId=<id>
        La respuesta llega por el stream SSE y se enruta por id.
        """
        if not self.sse_url:
            raise RuntimeError("Falta MCP_SERVER_URL para transporte SSE")
        if not self._sse_connection_id:
            raise RuntimeError("SSE no conectado (sin connectionId)")

        # Registrar queue por id ANTES de enviar
        rid = obj.get("id")
        if not isinstance(rid, int):
            raise ValueError("Request JSON-RPC debe tener 'id' entero")

        q: "queue.Queue[dict]" = queue.Queue()
        with self._pending_lock:
            self._pending[rid] = q

        # Construir URL de POST esperada por el Worker
        base = self.sse_url.rstrip("/")
        if not base.endswith("/sse"):
            base = base + "/sse"
        post_url = f"{base}/message?sessionId={urlparse.quote(self._sse_connection_id)}"

        # Cuerpo: JSON-RPC crudo (no envuelto)
        body = json.dumps(obj).encode("utf-8")
        req = urlrequest.Request(
            post_url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "mcp-host/0.1"},
            method="POST",
        )
        self._log("req", {"payload": obj, "via": "sse"})

        try:
            # El Worker responde 202; la respuesta real llegará por SSE
            _ = urlrequest.urlopen(req, timeout=self.request_timeout)
        except urlerror.HTTPError as he:
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise RuntimeError(f"HTTP {he.code} al enviar a {post_url}: {he.reason}")
        except Exception as e:
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise RuntimeError(f"Error enviando POST SSE: {e}")

        # Esperar la response por el stream (enrutada por _dispatch_response)
        try:
            resp = q.get(timeout=self.request_timeout)
        except queue.Empty:
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise TimeoutError(f"Timeout esperando respuesta SSE (>{self.request_timeout}s)")

        self._log("resp", {"payload": resp, "via": "sse"})
        with self._pending_lock:
            self._pending.pop(rid, None)
        return resp


    # ===========================
    #   Despacho común de resp
    # ===========================
    def _dispatch_response(self, obj: Dict[str, Any]) -> None:
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

    # ------------- API pública -------------
    def start(self) -> "MCPClient":
        if self.is_sse:
            self._ensure_running_sse()
        else:
            self._ensure_running_stdio()
        return self

    def stop(self) -> None:
        if self.is_sse:
            try:
                self._sse_stop.set()
                if self._sse_resp:
                    try:
                        self._sse_resp.close()
                    except Exception:
                        pass
                # no hay cierre "gracioso" del stream; con cerrar resp basta
            except Exception:
                pass
            finally:
                self._sse_resp = None
                self._sse_thread = None
                self._sse_connected.clear()
                self._sse_connection_id = None
        else:
            if not self.proc:
                return
            try:
                if self.proc.stdin:
                    try:
                        self.proc.stdin.flush()
                    except Exception:
                        pass
                self.proc.terminate()
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
    def _send(self, obj: Dict[str, Any]) -> Dict[str, Any]:
        """
        Envía un request JSON-RPC usando el transporte activo con reintentos básicos
        cuando tiene sentido (stdio). En SSE, reintentos de conexión se hacen en start().
        """
        attempts = 0
        last_exc: Optional[BaseException] = None

        while attempts <= (self.max_retries if not self.is_sse else 0):
            attempts += 1
            try:
                if self.is_sse:
                    self._ensure_running_sse()
                    return self._send_once_sse(obj)
                else:
                    self._ensure_running_stdio()
                    return self._send_once_stdio(obj)
            except (BrokenPipeError, TimeoutError, RuntimeError) as e:
                if self.is_sse:
                    # En SSE no relanzamos automáticamente aquí; dejamos burbujear
                    raise
                last_exc = e
                try:
                    self.stop()
                except Exception:
                    pass
                time.sleep(0.2)
                continue
            except Exception:
                raise

        if last_exc:
            raise last_exc
        raise RuntimeError("Fallo al enviar request al MCP")

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
