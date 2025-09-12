# host/settings.py
"""
Configuración del host/chatbot que habla con el servidor MCP (server.py).
- Sin dependencias obligatorias. Si existe python-dotenv, cargará .env automáticamente.
- Permite definir el comando del servidor MCP por variable de entorno o heurísticas.
- Estrategia de workspace por defecto (usuario + nombre repo/carpeta + fecha opcional).
- Timeouts, reintentos y logging del host → MCP.

Variables de entorno principales (opcional):
  MCP_SERVER_CMD        Comando completo para lanzar el MCP, p.ej.:
                        "C:/ruta/python.exe C:/ruta/setlist-architect-mcp/server.py"
  MCP_SERVER_PY        Ruta al intérprete Python a usar con server.py (si prefieres separar).
  MCP_SERVER_PATH      Ruta al server.py del MCP (si no usas MCP_SERVER_CMD).
  MCP_SERVER_URL       URL de un servidor MCP remoto por SSE (p.ej. https://.../sse).
  MCP_SERVER_TRANSPORT Tipo de transporte: "sse" (si hay URL) o "stdio" (local). Por defecto
                        se asume "sse" si MCP_SERVER_URL está definido; en caso contrario "stdio".
  MCP_WORKSPACE        ID de workspace por defecto.
  MCP_REQ_TIMEOUT_SEC  Timeout de petición (host esperando respuesta), por defecto 30.
  MCP_STARTUP_TIMEOUT  Timeout para que el proceso inicie, por defecto 8.
  MCP_MAX_RETRIES      Reintentos de tools_call en errores transitorios (por defecto 0).
  MCP_LOG_FILE         Ruta del log JSONL del host (por defecto logs/mcp_host.jsonl).
  MCP_DEBUG            1/0, activa trazas en stderr del MCP dentro del log también.
"""

from __future__ import annotations
import os, sys, getpass, datetime, shutil

# --- Dotenv (opcional) ---
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# --- Utilidades internas ---
def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip())
    except Exception:
        return default

def _slug(s: str) -> str:
    import re
    s = (s or "").strip()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^A-Za-z0-9._\\-]", "_", s)
    return s[:64] or "default"

def _detect_repo_name() -> str:
    """Nombre de carpeta del repo actual (para el workspace)."""
    try:
        return os.path.basename(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    except Exception:
        return "host"

# --- Workspaces ---
def default_workspace() -> str:
    """
    Estrategia por defecto:
      user-<usuario>-<repo>
    Puedes agregar fecha si quieres 'user-<usuario>-<repo>-YYYYMMDD'
    """
    user = _slug(getpass.getuser())
    repo = _slug(_detect_repo_name())
    # Si quieres fecha, descomenta:
    # today = datetime.date.today().strftime("%Y%m%d")
    # return f"user-{user}-{repo}-{today}"
    return f"user-{user}-{repo}"

DEFAULT_WORKSPACE = os.environ.get("MCP_WORKSPACE", default_workspace())

# --- Timeouts y reintentos ---
REQUEST_TIMEOUT_SEC = float(os.environ.get("MCP_REQ_TIMEOUT_SEC", "30"))
STARTUP_TIMEOUT_SEC = float(os.environ.get("MCP_STARTUP_TIMEOUT", "8"))
MAX_RETRIES = _env_int("MCP_MAX_RETRIES", 0)

# --- Logging ---
LOG_FILE = os.environ.get("MCP_LOG_FILE", "logs/mcp_host.jsonl")
DEBUG = _env_bool("MCP_DEBUG", False)

# Asegurar carpeta de logs
try:
    log_dir = os.path.dirname(LOG_FILE) or "."
    os.makedirs(log_dir, exist_ok=True)
except Exception:
    pass

# --- Config remoto opcional ---
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL")  # p.ej. https://.../sse
MCP_SERVER_TRANSPORT = os.environ.get(
    "MCP_SERVER_TRANSPORT",
    "sse" if MCP_SERVER_URL else "stdio",
).lower()

# --- Resolución del comando para el servidor MCP (solo si local/stdio) ---
# Prioridad:
#  1) MCP_SERVER_CMD (cadena completa)
#  2) MCP_SERVER_PY + MCP_SERVER_PATH
#  3) Heurística: <python actual> ../setlist-architect-mcp/server.py
def _build_server_cmd() -> str:
    cmd = os.environ.get("MCP_SERVER_CMD")
    if cmd:
        return cmd

    py = os.environ.get("MCP_SERVER_PY")  # ruta a python para el MCP
    srv = os.environ.get("MCP_SERVER_PATH")  # ruta absoluta a server.py
    if py and srv:
        return f'"{py}" "{srv}"'

    # Heurística: buscar un server.py típico al lado del repo actual
    #   host repo: <...>/mcp-console-host/host/settings.py
    # asumimos MCP en: <...>/setlist-architect-mcp/server.py
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    guess_srv = os.path.abspath(os.path.join(base, "..", "setlist-architect-mcp", "server.py"))
    python_bin = os.environ.get("PYTHON") or sys.executable

    if os.path.exists(guess_srv):
        return f'"{python_bin}" "{guess_srv}"'

    # Último recurso: si hay un server.py en el cwd/padre
    local_srv = os.path.abspath(os.path.join(base, "server.py"))
    if os.path.exists(local_srv):
        return f'"{python_bin}" "{local_srv}"'

    # Si nada de lo anterior aplica, devolvemos un comando claramente inválido para que el error sea explícito.
    return "python server.py"

MCP_SERVER_CMD = _build_server_cmd()

# --- Validaciones amistosas en arranque (no detienen, solo ayudan) ---
def print_startup_banner():
    if _env_bool("MCP_BANNER", True):
        print("[host] MCP settings:")
        if MCP_SERVER_URL:
            print(f"  - MCP_SERVER_URL     : {MCP_SERVER_URL}")
            print(f"  - MCP_TRANSPORT      : {MCP_SERVER_TRANSPORT}")
        else:
            print(f"  - MCP_SERVER_CMD     : {MCP_SERVER_CMD}")
        print(f"  - DEFAULT_WORKSPACE  : {DEFAULT_WORKSPACE}")
        print(f"  - REQUEST_TIMEOUT_SEC: {REQUEST_TIMEOUT_SEC}")
        print(f"  - STARTUP_TIMEOUT_SEC: {STARTUP_TIMEOUT_SEC}")
        print(f"  - MAX_RETRIES        : {MAX_RETRIES}")
        print(f"  - LOG_FILE           : {LOG_FILE}")
        print(f"  - DEBUG              : {DEBUG}")

# --- Opcional: mejoras de encoding en Windows (para UTF-8) ---
def apply_windows_utf8_console():
    """
    En Windows/PowerShell, fuerza UTF-8 para evitar caracteres ‘?’ en algunos nombres.
    Llama esto al iniciar tu host si lo deseas.
    """
    try:
        if os.name == "nt":
            # Solo afecta a la salida del proceso actual
            import sys
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
            os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    except Exception:
        pass
