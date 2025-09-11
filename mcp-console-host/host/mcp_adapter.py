# host/mcp_adapter.py
"""
MCP Adapter (fachada de alto nivel para el host/chatbot).

- Descubre tools en runtime (con caché).
- Inyecta 'workspace' automáticamente SOLO si la tool lo acepta.
- Para el server de música (setlist), inyecta 'workspace' siempre (fallback seguro).
- Normaliza errores del servidor MCP.
- Wrappers convenientes para las tools conocidas (add_song, list_playlists, etc.).

Dependencias internas:
  - host.mcp_client.MCPClient
  - host.settings (DEFAULT_WORKSPACE, etc.)
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import time

from host.mcp_client import MCPClient
from host.settings import DEFAULT_WORKSPACE

# --------- Errores de alto nivel ---------

class MCPAdapterError(Exception):
    """Error genérico del adapter."""

class MCPServerError(MCPAdapterError):
    """El MCP devolvió un error JSON-RPC (top-level)."""

class MCPNeedsConfirmation(MCPAdapterError):
    """
    Flujo de confirmación para add_song:
    - Guarda candidates y un mensaje, para que la GUI pregunte al usuario.
    """
    def __init__(self, candidates: List[Dict[str, Any]], message: str, original_args: Dict[str, Any]):
        super().__init__(message)
        self.candidates = candidates
        self.message = message
        self.original_args = original_args  # args que originaron la búsqueda


# --------- Modelos útiles para la GUI ---------

@dataclass
class CandidateView:
    id: str
    title: str
    artists: str
    duration_sec: Optional[float]
    confidence: Optional[float]
    source_url: str
    preview_url: Optional[str]

    @staticmethod
    def from_raw(c: Dict[str, Any]) -> "CandidateView":
        return CandidateView(
            id=str(c.get("id") or ""),
            title=str(c.get("title") or ""),
            artists=str(c.get("artists") or ""),
            duration_sec=(c.get("duration_sec") if isinstance(c.get("duration_sec"), (int, float)) else None),
            confidence=(c.get("confidence") if isinstance(c.get("confidence"), (int, float)) else None),
            source_url=str(c.get("source_url") or ""),
            preview_url=(c.get("preview_url") if c.get("preview_url") else None),
        )

@dataclass
class AddSongOK:
    status: str            # "ok"
    chosen: Dict[str, Any] # incluye playlist asignada

@dataclass
class AddSongConfirmation:
    status: str                    # "needs_confirmation"
    candidates: List[CandidateView]
    message: str


# --------- Adapter principal ---------

class MCPAdapter:
    """
    Fachada sobre MCPClient con:
    - get_tools_schema() con caché y normalización a snake_case esperado por LLMs
    - call_tool() inyectando workspace solo cuando procede
    - helpers de alto nivel para cada tool
    """
    def __init__(self, workspace: Optional[str] = None):
        self.workspace = (workspace or DEFAULT_WORKSPACE)
        self._client = MCPClient()
        self._client.start()
        self._tools_cache: Tuple[float, List[Dict[str, Any]]] = (0.0, [])
        # Mapa: name -> bool indicando si el schema DECLARA 'workspace'
        self._tools_accept_workspace: Dict[str, bool] = {}

        # Fallback seguro para el servidor de música por si no declara 'workspace'
        self._always_inject_ws = {
            "add_song", "list_playlists", "get_playlist", "export_playlist", "clear_library"
        }

    # ---- Infra ----
    def set_workspace(self, ws: str) -> None:
        self.workspace = ws or DEFAULT_WORKSPACE

    def get_client(self) -> MCPClient:
        return self._client

    def shutdown(self) -> None:
        try:
            self._client.stop()
        except Exception:
            pass

    # ---- Normalización de tools ----
    def _normalize_tool_schema(self, t: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convierte una tool del formato del server (a menudo 'inputSchema') al formato
        esperado por Anthropic/OpenAI ('input_schema'), asegurando:
        - type: "object"
        - properties: {}
        NO añade 'workspace' si el server no lo declara (para evitar 400 en servers como filesystem).
        """
        name = str(t.get("name") or "")
        desc = str(t.get("description") or "")

        # Algunos servers devuelven 'inputSchema' en camelCase
        schema = t.get("input_schema")
        if not isinstance(schema, dict):
            schema = t.get("inputSchema", {}) or {}
        if not isinstance(schema, dict):
            schema = {}

        # Asegurar forma básica de JSON Schema
        if "type" not in schema or not isinstance(schema.get("type"), str):
            schema["type"] = "object"
        props = schema.get("properties")
        if not isinstance(props, dict):
            props = {}
            schema["properties"] = props

        # ¿Declara workspace?
        accepts_ws = "workspace" in props

        # Si lo declara, aseguramos metadatos mínimos
        if accepts_ws and isinstance(props.get("workspace"), dict):
            props["workspace"].setdefault("type", "string")
            props["workspace"].setdefault("description", "Workspace/session id")

        # Guardamos si acepta workspace de verdad
        self._tools_accept_workspace[name] = accepts_ws

        return {
            "name": name,
            "description": desc,
            "input_schema": schema,
        }

    # ---- Tools schema (descubrimiento con caché) ----
    def get_tools_schema(self, *, ttl_sec: float = 60.0) -> List[Dict[str, Any]]:
        ts, cached = self._tools_cache
        now = time.time()
        if cached and (now - ts) < ttl_sec:
            return cached

        resp = self._client.tools_list()
        if "error" in resp:
            raise MCPServerError(str(resp["error"]))

        raw_tools = list(resp.get("result", {}).get("tools", []))
        tools: List[Dict[str, Any]] = []
        self._tools_accept_workspace.clear()

        for t in raw_tools:
            norm = self._normalize_tool_schema(t)
            tools.append(norm)

        # Cache
        self._tools_cache = (now, tools)
        return tools

    # ---- Llamada genérica ----
    def call_tool(self, name: str, args: Optional[Dict[str, Any]] = None, *, workspace: Optional[str] = None) -> Dict[str, Any]:
        arguments = dict(args or {})

        # Inyectar workspace solo si:
        #   a) la tool lo declara en su schema, o
        #   b) está en la lista de fallback del server de música.
        accept_ws = self._tools_accept_workspace.get(name, False)
        if (accept_ws or name in self._always_inject_ws) and "workspace" not in arguments:
            arguments["workspace"] = workspace or self.workspace

        resp = self._client.tools_call(name, arguments)
        if "error" in resp:
            # Normaliza el error para la capa superior/GUI
            raise MCPServerError(str(resp["error"]))
        return resp.get("result", {})

    # ---- Wrappers de alto nivel (setlist) ----

    # add_song con manejo de needs_confirmation
    def add_song(
        self,
        title: str,
        artists: str = "",
        *,
        candidate_index: Optional[int] = None,
        candidate_id: Optional[str] = None,
        workspace: Optional[str] = None
    ) -> AddSongOK:
        args: Dict[str, Any] = {"title": title}
        if artists:
            args["artists"] = artists
        if candidate_index is not None:
            args["candidate_index"] = int(candidate_index)
        if candidate_id:
            args["candidate_id"] = str(candidate_id)

        result = self.call_tool("add_song", args, workspace=workspace)
        status = result.get("status")
        if status == "ok":
            return AddSongOK(status="ok", chosen=result.get("chosen", {}))

        if status == "needs_confirmation":
            candidates = [CandidateView.from_raw(c) for c in (result.get("candidates") or [])]
            msg = str(result.get("message") or "Se requiere confirmación del candidato.")
            original = dict(args)
            raise MCPNeedsConfirmation([c.__dict__ for c in candidates], msg, original)

        # Estado inesperado
        raise MCPAdapterError(f"add_song devolvió estado desconocido: {status}")

    def list_playlists(self, *, workspace: Optional[str] = None) -> List[Dict[str, Any]]:
        result = self.call_tool("list_playlists", {}, workspace=workspace)
        return list(result.get("playlists", []))

    def get_playlist(self, name: str, *, workspace: Optional[str] = None) -> Dict[str, Any]:
        result = self.call_tool("get_playlist", {"name": name}, workspace=workspace)
        return result

    def export_playlist(self, name: str, *, workspace: Optional[str] = None) -> Dict[str, Any]:
        result = self.call_tool("export_playlist", {"name": name}, workspace=workspace)
        # { "uri": "file://...", "rows": N }
        return result

    def clear_library(self, *, workspace: Optional[str] = None) -> Dict[str, Any]:
        result = self.call_tool("clear_library", {}, workspace=workspace)
        return result

    # ---- Utilidad: transformar schema para un LLM (opcional) ----
    def as_llm_tools(self) -> List[Dict[str, Any]]:
        """
        Devuelve las tools descubiertas tal cual (JSON Schema normalizado a 'input_schema'),
        listas para registrarse en un LLM que soporte "tools" con JSON Schema.
        """
        return self.get_tools_schema(ttl_sec=60.0)
