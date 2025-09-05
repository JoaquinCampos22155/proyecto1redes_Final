# host/utils.py
from __future__ import annotations
import json
import os
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional
from pathlib import Path
from rich.console import Console

console = Console()

# ---------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------

@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    tool_use_id: str


# ---------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------

def pretty(obj: Any) -> str:
    """Devuelve el objeto serializado en JSON 'bonito' (utf-8, con indentación)."""
    return json.dumps(obj, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------
# Entorno (.env) helpers
# ---------------------------------------------------------------------

def getenv(name: str, default: str | None = None) -> str | None:
    """Equivalente a os.environ.get; se mantiene por compatibilidad con tu main."""
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    """Lee un booleano de entorno (true/false/1/0/yes/no)."""
    val = os.environ.get(name)
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def strip_quotes(s: str | None) -> str | None:
    """Elimina comillas iniciales/finales si existen (útil para rutas en .env)."""
    if s is None:
        return None
    s = s.strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    return s


def env_list(name: str, sep: str = ",") -> list[str]:
    """
    Parsea una variable de entorno separada por comas.
    Ej: MCP_SERVER_ARGS="-m,setlist_architect.server" -> ["-m", "setlist_architect.server"]
    Soporta tokens entrecomillados.
    """
    raw = os.environ.get(name, "")
    if not raw:
        return []
    out: list[str] = []
    for item in (part.strip() for part in raw.split(sep)):
        if not item:
            continue
        s = strip_quotes(item)
        if s:
            out.append(s)
    return out


# ---------------------------------------------------------------------
# Paths helpers
# ---------------------------------------------------------------------

def norm_path(p: str | Path | None) -> Optional[str]:
    """Normaliza una ruta (expande ~ y variables de entorno). Devuelve str o None."""
    if p is None:
        return None
    s = strip_quotes(str(p))
    if not s:
        return s
    s = os.path.expandvars(os.path.expanduser(s))
    return os.path.normpath(s)


def ensure_dir(path: str | Path) -> str:
    """Crea el directorio si no existe y retorna la ruta normalizada."""
    p = Path(norm_path(path) or ".")
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def path_exists(path: str | Path) -> bool:
    """Retorna True si la ruta existe."""
    p = Path(norm_path(path) or "")
    return p.exists()


def escape_win_path_for_json(p: str | Path) -> str:
    """Duplica backslashes para imprimir rutas Windows dentro de JSON/texto de prompt."""
    return str(p).replace("\\", "\\\\")


# ---------------------------------------------------------------------
# Debug helpers
# ---------------------------------------------------------------------

SENSITIVE_KEYS = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"}

def redact(val: str, keep: int = 4) -> str:
    """Enmascara valores sensibles dejando visibles los últimos 'keep' caracteres."""
    if val is None:
        return ""
    if len(val) <= keep:
        return "*" * len(val)
    return "*" * (len(val) - keep) + val[-keep:]


def print_env(keys: Iterable[str]) -> None:
    """Imprime variables de entorno (enmascara las sensibles)."""
    rows: List[str] = []
    for k in keys:
        v = os.environ.get(k)
        if k in SENSITIVE_KEYS and v:
            v = redact(v)
        rows.append(f"{k} = {v}")
    if rows:
        console.print("[dim]Entorno:[/dim]\n" + "\n".join(rows))


def banner(msg: str) -> None:
    console.rule(f"[bold cyan]{msg}[/bold cyan]")
