# host/direct_adapter.py
from __future__ import annotations
import json
from typing import Any, Dict, List

# Importa el paquete local de Setlist Architect (instalado en editable)
from setlist_architect.audio_features import analyze_file
from setlist_architect.setlist_logic import classify_playlists
from setlist_architect.io_utils import export_csv


class DirectAdapter:
    """Modo DIRECTO: ejecuta tools llamando funciones Python del paquete.
    Emula el JSON del servidor MCP.
    """

    def __init__(self) -> None:
        self._tracks: List[Dict[str, Any]] = []
        self._playlists: dict[str, List[Dict[str, Any]]] = {}
        self._by_path: dict[str, Dict[str, Any]] = {}

    # ------------ helpers internos ------------
    def _add_to_playlists(self, row: Dict[str, Any], playlists: List[str]) -> None:
        for pl in playlists:
            lst = self._playlists.setdefault(pl, [])
            if not any(r.get("path") == row.get("path") for r in lst):
                lst.append(row)

    # ------------- tools públicas -------------
    def call(self, name: str, arguments: Dict[str, Any]) -> str:
        if name == "add_song":
            path = arguments["path"]
            row = analyze_file(path)
            pls = classify_playlists(row)
            self._by_path[row["path"]] = row
            if not any(r.get("path") == row.get("path") for r in self._tracks):
                self._tracks.append(row)
            self._add_to_playlists(row, pls)
            return json.dumps({"track": row, "playlists": pls}, ensure_ascii=False)

        if name == "list_playlists":
            out = [{"name": k, "count": len(v)} for k, v in sorted(self._playlists.items())]
            return json.dumps({"playlists": out}, ensure_ascii=False)

        if name == "get_playlist":
            name_ = arguments["name"]
            rows = self._playlists.get(name_, [])[:]
            return json.dumps({"name": name_, "tracks": rows, "count": len(rows)}, ensure_ascii=False)

        if name == "export_playlist":
            name_ = arguments["name"]
            csv_path = arguments["csv_path"]
            rows = self._playlists.get(name_, [])
            if not rows:
                return json.dumps({"error": f"Playlist vacía o inexistente: {name_}"}, ensure_ascii=False)
            n = export_csv(rows, csv_path)
            return json.dumps({"playlist": name_, "csv_path": csv_path, "rows": n}, ensure_ascii=False)

        if name == "clear_library":
            self._tracks.clear()
            self._playlists.clear()
            self._by_path.clear()
            return json.dumps({"ok": True}, ensure_ascii=False)

        return json.dumps({"error": f"Tool desconocido: {name}"}, ensure_ascii=False)
