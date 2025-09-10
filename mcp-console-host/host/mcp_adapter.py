# host/mcp_adapter.py
"""
MCP Adapter (fachada de alto nivel para el host/chatbot).

- Descubre tools en runtime (con caché).
- Inyecta 'workspace' solo cuando la tool lo declara o si está whitelisteada (setlist).
- Normaliza errores del servidor MCP.
- Wrappers convenientes para las tools de setlist (add_song, list_playlists, etc.).
- Listo para conectar a una GUI/LLM.

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
    - get_tools_schema() con caché
    - call_tool() con inyección condicional de workspace
    - helpers de alto nivel para cada tool de setlist
    """
    def __init__(self, workspace: Optional[str] = None):
        self.workspace = (workspace or DEFAULT_WORKSPACE)
        self._client = MCPClient()
        self._client.start()
        self._tools_cache: Tuple[float, List[Dict[str, Any]]] = (0.0, [])

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

    # ---- Tools schema (descubrimiento con caché) ----
    def get_tools_schema(self, *, ttl_sec: float = 60.0) -> List[Dict[str, Any]]:
        """
        Devuelve la lista de tools tal como las reporta el servidor (sin mutarlas).
        Estructura esperada (MCP): result.tools[].{name, description, inputSchema|input_schema}
        """
        ts, cached = self._tools_cache
        now = time.time()
        if cached and (now - ts) < ttl_sec:
            return cached

        resp = self._client.tools_list()
        if "error" in resp:
            raise MCPServerError(str(resp["error"]))
        tools = list(resp.get("result", {}).get("tools", []))
        self._tools_cache = (now, tools)
        return tools

    # ---- Utilidades internas ----
    def _find_tool_schema(self, tool_name: str) -> Optional[Dict[str, Any]]:
        try:
            for t in self.get_tools_schema():
                if t.get("name") == tool_name:
                    return t
        except Exception:
            return None
        return None

    def _input_schema_dict(self, tool_obj: Dict[str, Any]) -> Dict[str, Any]:
        """
        Devuelve el objeto schema de entrada indiferente a snake/camel:
        - 'input_schema' (snake) o 'inputSchema' (camel)
        """
        if not isinstance(tool_obj, dict):
            return {}
        schema = tool_obj.get("input_schema")
        if isinstance(schema, dict):
            return schema
        schema = tool_obj.get("inputSchema")
        if isinstance(schema, dict):
            return schema
        return {}

    def _tool_accepts_workspace(self, tool_name: str) -> bool:
        """
        True si el JSON Schema declara la propiedad 'workspace'.
        """
        tool = self._find_tool_schema(tool_name)
        if not tool:
            return False
        schema = self._input_schema_dict(tool)
        props = schema.get("properties") or {}
        return "workspace" in props

    def _always_workspace_tools(self) -> set[str]:
        """
        Whitelist para tu servidor local 'setlist-architect-mcp', por
        compatibilidad si el schema no declara 'workspace'.
        """
        return {
            "add_song",
            "list_playlists",
            "get_playlist",
            "export_playlist",
            "clear_library",
        }

    # ---- Llamada genérica ----
    def call_tool(
        self,
        name: str,
        args: Optional[Dict[str, Any]] = None,
        *,
        workspace: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Llama tools/call con inyección condicional de 'workspace':
        - Se inyecta si la tool lo declara en su schema (propiedad 'workspace'), o
        - si el nombre está en el whitelist de setlist.
        """
        arguments = dict(args or {})

        should_inject = self._tool_accepts_workspace(name) or (name in self._always_workspace_tools())
        if should_inject:
            arguments.setdefault("workspace", workspace or self.workspace)

        resp = self._client.tools_call(name, arguments)
        if "error" in resp:
            # Normaliza el error para la capa superior/GUI
            raise MCPServerError(str(resp["error"]))
        return resp.get("result", {})

    # ---- Wrappers de alto nivel (setlist) ----

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
            # Proveer a la GUI/LLM el contexto para relanzar con candidate_index/id
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
        Devuelve las tools descubiertas tal cual (JSON Schema), listas para
        registrarse en un LLM que soporte "tools" con JSON Schema.
        """
        return self.get_tools_schema(ttl_sec=60.0)
