# host/tool_schemas.py
"""
Proveedor de schemas de tools **dinámico** para el host MCP.

- Intenta descubrir las tools reales con `tools/list` (MCPAdapter).
- Inyecta el campo opcional `workspace` en todos los schemas (UX).
- Mantiene caché en memoria con TTL para evitar consultas repetidas.
- Incluye un FALLBACK estático 100% compatible con el servidor MCP de setlist-architect,
  por si el proceso no arranca o hay un problema temporal.

Uso típico:
    from host.tool_schemas import TOOLS, TOOLS_PRELOADED
    schemas = TOOLS()              # dinámico (con caché)
    # o si tu SDK exige una lista inmutable al arranque:
    schemas = TOOLS_PRELOADED      # snapshot a import-time

Importante: nuestro MCP exporta playlist a **XLSX** (no CSV) y `add_song` usa `artists`.
"""

from __future__ import annotations
from typing import List, Dict, Any
import time

from host.mcp_adapter import MCPAdapter

# ---------------- Fallback estático (coincide con server.py) ----------------
FALLBACK_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "add_song",
        "description": "Busca, extrae features y agrega a playlist por heurística.",
        "input_schema": {
            "type": "object",
            "required": ["title"],
            "properties": {
                "title": {"type": "string", "description": "Título de la canción"},
                "artists": {"type": "string", "description": "Artista(s), opcional"},
                "candidate_index": {"type": "integer", "description": "Índice del candidato a confirmar"},
                "candidate_id": {"type": "string", "description": "ID del candidato a confirmar"},
                "workspace": {"type": "string", "description": "Workspace/session id"},
            },
            "additionalProperties": True,
        },
    },
    {
        "name": "list_playlists",
        "description": "Lista playlists del workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "Workspace/session id"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_playlist",
        "description": "Devuelve canciones y metadatos de una playlist.",
        "input_schema": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "description": "Nombre exacto de la playlist"},
                "workspace": {"type": "string", "description": "Workspace/session id"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "export_playlist",
        "description": "Exporta playlist a XLSX (canción, bpm) y devuelve file:// URI.",
        "input_schema": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "description": "Nombre exacto de la playlist"},
                "workspace": {"type": "string", "description": "Workspace/session id"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "clear_library",
        "description": "Vacía canciones y asociaciones; mantiene nombres de playlists.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "Workspace/session id"},
            },
            "additionalProperties": False,
        },
    },
]

# ---------------- Caché simple ----------------
_CACHE = {"ts": 0.0, "tools": None}

def _ensure_workspace_prop(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for t in tools:
        schema = t.setdefault("input_schema", {})
        props = schema.setdefault("properties", {})
        if "workspace" not in props:
            props["workspace"] = {"type": "string", "description": "Workspace/session id"}
    return tools

def TOOLS(cache_ttl_sec: float = 60.0) -> List[Dict[str, Any]]:
    """
    Devuelve la lista de tools (JSON Schema) descubierta desde el MCP.
    Usa caché con TTL; si falla, retorna el FALLBACK.
    """
    now = time.time()
    cached = _CACHE.get("tools")
    ts = _CACHE.get("ts", 0.0)
    if cached and (now - ts) < cache_ttl_sec:
        return cached

    try:
        adapter = MCPAdapter()
        tools = adapter.get_tools_schema(ttl_sec=0.0)  # sin caché del adapter aquí
        adapter.shutdown()
        tools = _ensure_workspace_prop(list(tools))
        _CACHE["tools"] = tools
        _CACHE["ts"] = now
        return tools
    except Exception:
        # Fallback estático
        tools = _ensure_workspace_prop([dict(t) for t in FALLBACK_TOOLS])
        _CACHE["tools"] = tools
        _CACHE["ts"] = now
        return tools

# Snapshot para frameworks que requieren una lista inmutable en el arranque
TOOLS_PRELOADED: List[Dict[str, Any]] = TOOLS(cache_ttl_sec=0.0)
