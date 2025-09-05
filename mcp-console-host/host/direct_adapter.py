# host/direct_adapter.py
#INNECESARIO 
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

# Importa el paquete del servidor local (modo DIRECTO requiere poder importarlo)
try:
    from setlist_architect.audio_features import scan_folder, analyze_file
    from setlist_architect.setlist_logic import suggest_rampa, suggest_ola
    from setlist_architect.io_utils import export_csv
except ModuleNotFoundError as e:
    raise ModuleNotFoundError(
        "No se pudo importar 'setlist_architect'.\n"
        "Si vas a usar MODE=direct, instala el paquete del servidor en este venv:\n"
        "  pip install -e <RUTA_A_TU_REPO>/setlist_architect\n"
        "Si vas a usar MODE=mcp, no necesitas este adapter."
    ) from e


def _norm_path(p: str | os.PathLike[str]) -> str:
    """Normaliza rutas (expande ~ y variables de entorno)."""
    s = os.fspath(p)
    s = s.strip().strip('"').strip("'")
    s = os.path.expandvars(os.path.expanduser(s))
    return os.path.normpath(s)


class DirectAdapter:
    """
    Ejecuta las tools llamando directamente a funciones Python del paquete `setlist_architect`.
    La salida imita el JSON que devolvería el servidor MCP para facilitar la integración.

    Tools soportadas:
      - batch_analyze(folder)
      - suggest_setlist(curve, max_gap_bpm=6)
      - export_setlist(csv_path)
    """

    def __init__(self) -> None:
        self._tracks: List[Dict[str, Any]] = []
        self._order: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # API pública invocada por el host
    # ------------------------------------------------------------------
    def call(self, name: str, arguments: Dict[str, Any]) -> str:
        if name == "batch_analyze":
            return self._batch_analyze(arguments)
        if name == "suggest_setlist":
            return self._suggest_setlist(arguments)
        if name == "export_setlist":
            return self._export_setlist(arguments)

        return json.dumps({"error": f"Tool desconocido: {name}"}, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Implementaciones de tools
    # ------------------------------------------------------------------
    def _batch_analyze(self, arguments: Dict[str, Any]) -> str:
        folder = arguments.get("folder")
        if not folder or not isinstance(folder, str):
            return json.dumps({"error": "Falta argumento 'folder' (string)."}, ensure_ascii=False)

        folder = _norm_path(folder)
        if not os.path.isdir(folder):
            return json.dumps({"error": f"Carpeta no encontrada: {folder}"}, ensure_ascii=False)

        try:
            paths = scan_folder(folder)
        except Exception as e:
            return json.dumps({"error": f"Fallo escaneando carpeta: {e}"}, ensure_ascii=False)

        rows: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []

        for p in paths:
            try:
                rows.append(analyze_file(p))
            except Exception as ex:
                errors.append({"path": str(p), "error": str(ex)})

        self._tracks = rows
        self._order = []

        payload: Dict[str, Any] = {"tracks": rows, "count": len(rows)}
        if errors:
            payload["errors"] = errors  # útil para depurar archivos corruptos o formatos no soportados

        return json.dumps(payload, ensure_ascii=False)

    def _suggest_setlist(self, arguments: Dict[str, Any]) -> str:
        if not self._tracks:
            return json.dumps(
                {"error": "No hay análisis cargado. Ejecuta batch_analyze primero."},
                ensure_ascii=False,
            )

        curve = arguments.get("curve")
        if curve not in {"rampa", "ola"}:
            return json.dumps(
                {"error": "Argumento 'curve' inválido. Usa 'rampa' u 'ola'."},
                ensure_ascii=False,
            )

        try:
            max_gap = int(arguments.get("max_gap_bpm", 6))
        except Exception:
            max_gap = 6

        # Pequeño acotado de seguridad
        if max_gap < 0:
            max_gap = 0
        if max_gap > 40:
            max_gap = 40

        if curve == "rampa":
            order, notes = suggest_rampa(self._tracks, max_gap)
        else:
            order, notes = suggest_ola(self._tracks, max_gap)

        self._order = order
        return json.dumps(
            {"order": order, "notes": notes, "count": len(order)},
            ensure_ascii=False,
        )

    def _export_setlist(self, arguments: Dict[str, Any]) -> str:
        if not self._order:
            return json.dumps(
                {"error": "No hay setlist sugerido. Ejecuta suggest_setlist primero."},
                ensure_ascii=False,
            )

        out = arguments.get("csv_path")
        if not out or not isinstance(out, str):
            return json.dumps({"error": "Falta argumento 'csv_path' (string)."}, ensure_ascii=False)

        out = _norm_path(out)
        try:
            # export_csv crea directorios padre si hace falta (según nuestra implementación),
            # pero si tu versión no lo hace, puedes garantizar aquí:
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            n = export_csv(self._order, out)
        except Exception as e:
            return json.dumps({"error": f"Fallo exportando CSV: {e}"}, ensure_ascii=False)

        return json.dumps({"csv_path": out, "rows": n}, ensure_ascii=False)
