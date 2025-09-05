# host/mcp_adapter.py
from __future__ import annotations
import json
import os
import threading
import subprocess
from collections import deque
from typing import Any, Dict, Optional


class MCPError(RuntimeError):
    pass


class MCPAdapter:
    """
    Cliente MCP mínimo por STDIO usando framing newline-delimited (NDJSON):
      • Cada mensaje es un objeto JSON-RPC en UNA sola línea terminada en '\n'
      • Sin cabeceras "Content-Length"

    Implementa:
      - initialize + notifications/initialized
      - tools/list (descubrimiento)
      - tools/call (invocación)

    Compatible con servidores FastMCP (mcp>=1.2) ejecutados con transport='stdio'.
    """

    def __init__(
        self,
        command: str,
        args: list[str],
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        timeout_s: float | None = None,
    ) -> None:
        if not command:
            raise MCPError("MCPAdapter requiere 'command' (ruta a python.exe u otro ejecutable)")

        # Timeout (permite override por env; análisis de audio puede tardar)
        if timeout_s is None:
            try:
                timeout_s = float(os.environ.get("MCP_TIMEOUT_S", "60"))
            except Exception:
                timeout_s = 60.0

        # Fusiona entorno (preserva PYTHONPATH)
        merged_env = os.environ.copy()
        if env:
            if env.get("PYTHONPATH") and merged_env.get("PYTHONPATH"):
                env["PYTHONPATH"] = env["PYTHONPATH"] + os.pathsep + merged_env["PYTHONPATH"]
            merged_env.update(env)

        # Guarda para diagnóstico
        self._cmdline = [command] + list(args)
        self._cwd = cwd or None

        # Arranca el proceso MCP
        try:
            self.proc = subprocess.Popen(
                self._cmdline,
                cwd=self._cwd,
                env=merged_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError as e:
            raise MCPError(
                "No se pudo lanzar el servidor MCP.\n"
                f"command={command}\nargs={args}\n"
                f"cwd={cwd}\n\n"
                "Sugerencias:\n"
                "- Quita comillas en MCP_SERVER_CMD dentro de .env (usa ruta cruda sin \").\n"
                "- Verifica que MCP_CWD exista.\n"
                "- Prueba en tu terminal:\n"
                f'  "{command}" {" ".join(args)}'
            ) from e

        if not self.proc.stdin or not self.proc.stdout:
            raise MCPError("No se pudo abrir stdin/stdout del proceso MCP")

        # Infra de sincronización
        self._timeout = float(timeout_s)
        self._id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, threading.Event] = {}
        self._results: dict[int, Any] = {}

        # Tail de stderr para debug
        self._stderr_tail: deque[str] = deque(maxlen=200)

        # Hilos de lectura
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._stderr_loop, daemon=True)
        self._stderr_reader.start()

        # Para que .tools exista aunque falle el listado
        self.tools: list[dict[str, Any]] = []

        # --- Handshake MCP ---
        _ = self._request(
            method="initialize",
            params={
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "mcp-console-host",
                    "title": "Setlist Architect Console Host",
                    "version": "0.1",
                },
            },
        )
        # Notificación post-handshake
        self._notify("notifications/initialized", {})

        # Descubre tools
        try:
            listing = self._request("tools/list", params={})
            if isinstance(listing, dict):
                self.tools = listing.get("tools", []) or []
        except Exception:
            # deja self.tools = []
            pass

    # -------------------- API pública --------------------

    def list_tools(self) -> list[dict[str, Any]]:
        return list(self.tools) if isinstance(self.tools, list) else []

    def call(self, name: str, arguments: Dict[str, Any]) -> str:
        """
        Invoca `tools/call` y devuelve texto amigable para el LLM.
        Si el server retorna `{"content":[{"type":"text","text":"..."}]}`, lo concatena.
        Si retorna otra estructura, se devuelve el JSON completo serializado.
        """
        self._assert_alive()
        result = self._request("tools/call", {"name": name, "arguments": arguments})

        # FastMCP suele envolver en {"content":[...]} o devolver payload directo
        if isinstance(result, dict) and "content" in result:
            content = result.get("content")
            if isinstance(content, list):
                texts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        t = item.get("text", "")
                        if isinstance(t, str):
                            texts.append(t)
                if texts:
                    return "\n".join(texts)
                return json.dumps(result, ensure_ascii=False)
            if isinstance(content, str):
                return content
            return json.dumps(result, ensure_ascii=False)

        if isinstance(result, str):
            return result

        return json.dumps(result, ensure_ascii=False)

    def close(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass

    # -------------------- Internos (framing newline) --------------------

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def _write_msg(self, obj: dict) -> None:
        """Escribe UNA línea JSON (sin saltos embebidos) + '\\n'."""
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assert self.proc.stdin is not None
        self.proc.stdin.write(data + b"\n")
        self.proc.stdin.flush()

    def _read_msg(self) -> Optional[dict]:
        """Lee UNA línea de stdout y parsea JSON; None en EOF."""
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            return None  # EOF
        try:
            return json.loads(line.decode("utf-8"))
        except Exception:
            # línea inválida → ignora y sigue leyendo
            return {}

    def _reader_loop(self):
        while True:
            try:
                msg = self._read_msg()
                if msg is None:
                    break  # EOF
                if not isinstance(msg, dict):
                    continue

                rid = msg.get("id")
                if rid is not None and ("result" in msg or "error" in msg):
                    ev = self._pending.get(int(rid))
                    if ev is not None:
                        self._results[int(rid)] = msg
                        ev.set()
                # Requests/notifications desde el server se ignoran en este cliente mínimo
            except Exception:
                break

    def _stderr_loop(self):
        if not self.proc.stderr:
            return
        while True:
            line = self.proc.stderr.readline()
            if not line:
                break
            try:
                self._stderr_tail.append(line.decode("utf-8", errors="replace").rstrip())
            except Exception:
                continue

    def _request(self, method: str, params: dict) -> Any:
        self._assert_alive()
        req_id = self._next_id()
        ev = threading.Event()
        self._pending[req_id] = ev

        self._write_msg({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})

        if not ev.wait(timeout=self._timeout):
            tail = "\n".join(self._stderr_tail)
            raise MCPError(
                f"Timeout esperando respuesta a '{method}'.\n"
                f"cmdline={self._cmdline}\n"
                f"cwd={self._cwd}\n"
                f"stderr_tail:\n{tail}"
            )

        msg = self._results.pop(req_id, None)
        self._pending.pop(req_id, None)

        if msg is None:
            tail = "\n".join(self._stderr_tail)
            raise MCPError(f"Respuesta perdida para '{method}'. stderr_tail:\n{tail}")

        if "error" in msg:
            err = msg["error"]
            tail = "\n".join(self._stderr_tail)
            raise MCPError(f"Error MCP en '{method}': {err}\n\nstderr_tail:\n{tail}")

        return msg.get("result")

    def _notify(self, method: str, params: dict) -> None:
        self._write_msg({"jsonrpc": "2.0", "method": method, "params": params})

    def _assert_alive(self):
        code = self.proc.poll()
        if code is not None:
            tail = "\n".join(self._stderr_tail)
            raise MCPError(
                f"El proceso MCP terminó con código {code}.\n"
                f"cmdline={self._cmdline}\n"
                f"cwd={self._cwd}\n"
                f"stderr_tail:\n{tail}"
            )
