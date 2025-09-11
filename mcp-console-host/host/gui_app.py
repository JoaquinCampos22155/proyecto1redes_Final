# host/gui_app.py
from __future__ import annotations
import sys, json, traceback
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QLineEdit, QTextEdit,
    QHBoxLayout, QVBoxLayout, QTableWidget, QTableWidgetItem, QMessageBox,
    QSplitter, QSizePolicy, QDialog, QDialogButtonBox, QHeaderView
)

from anthropic import Anthropic
from dotenv import load_dotenv
import os

from host.mcp_adapter import MCPAdapter, MCPNeedsConfirmation
from host.settings import DEFAULT_WORKSPACE, apply_windows_utf8_console, print_startup_banner

# -------------------------------------------------------------------
# Utilidades
# -------------------------------------------------------------------

def _fmt_num(v: Any, ndigits: int = 2) -> str:
    try:
        if v is None: return ""
        return f"{float(v):.{ndigits}f}"
    except Exception:
        return ""

def _blocks_to_text(blocks: list[dict | object]) -> str:
    def _type(b): return getattr(b, "type", None) if not isinstance(b, dict) else b.get("type")
    parts = []
    for b in blocks:
        if _type(b) == "text":
            txt = getattr(b, "text", None) if not isinstance(b, dict) else b.get("text")
            if isinstance(txt, str):
                parts.append(txt)
    return "\n".join(parts).strip()

def _extract_tool_uses(blocks: list[dict | object]) -> list[dict]:
    def _type(b): return getattr(b, "type", None) if not isinstance(b, dict) else b.get("type")
    uses = []
    for b in blocks:
        if _type(b) == "tool_use":
            name = getattr(b, "name", None) if not isinstance(b, dict) else b.get("name")
            input_args = getattr(b, "input", None) if not isinstance(b, dict) else b.get("input")
            tu_id = getattr(b, "id", None) if not isinstance(b, dict) else b.get("id")
            uses.append({"name": name, "arguments": input_args or {}, "id": tu_id})
    return uses

def _normalize_blocks(blocks: list[dict | object]) -> list[dict]:
    """Convierte bloques Anthropic a dicts 'limpios' para guardar en history."""
    out: list[dict] = []
    for b in blocks:
        if isinstance(b, dict):
            out.append(b); continue
        typ = getattr(b, "type", None)
        if not typ: continue
        d: dict = {"type": typ}
        if typ == "text":
            d["text"] = getattr(b, "text", "") or ""
        elif typ == "tool_use":
            d["id"] = getattr(b, "id", None)
            d["name"] = getattr(b, "name", None)
            d["input"] = getattr(b, "input", {}) or {}
        elif typ == "tool_result":
            d["tool_use_id"] = getattr(b, "tool_use_id", None)
            content = getattr(b, "content", "")
            if isinstance(content, list):
                d["content"] = content
            else:
                d["content"] = [{"type": "text", "text": str(content) if content is not None else ""}]
        out.append(d)
    return out

# -------------------------------------------------------------------
# (Opcional) Diálogo para confirmación de candidatos en add_song
# -------------------------------------------------------------------

class CandidateDialog(QDialog):
    def __init__(self, candidates: List[Dict[str, Any]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Confirma la canción")
        self.resize(700, 360)
        self._selected_index: Optional[int] = None

        layout = QVBoxLayout(self)
        info = QLabel("Se encontraron múltiples coincidencias. Elige una:")
        layout.addWidget(info)

        table = QTableWidget(0, 6)
        table.setHorizontalHeaderLabels(["#", "Título", "Artistas", "Duración (s)", "Confianza", "Preview"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(table, 1)
        self.table = table

        for i, c in enumerate(candidates):
            self.table.insertRow(i)
            self.table.setItem(i, 0, QTableWidgetItem(str(i)))
            self.table.setItem(i, 1, QTableWidgetItem(str(c.get("title", ""))))
            self.table.setItem(i, 2, QTableWidgetItem(str(c.get("artists", ""))))
            self.table.setItem(i, 3, QTableWidgetItem(_fmt_num(c.get("duration_sec"), 1)))
            self.table.setItem(i, 4, QTableWidgetItem(_fmt_num(c.get("confidence"), 2)))
            self.table.setItem(i, 5, QTableWidgetItem("sí" if c.get("preview_url") else "no"))

        self.table.doubleClicked.connect(self._accept)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _accept(self):
        idxs = self.table.selectionModel().selectedRows()
        if not idxs:
            QMessageBox.information(self, "Selecciona uno", "Elige un candidato.")
            return
        self._selected_index = idxs[0].row()
        self.accept()

    @property
    def selected_index(self) -> Optional[int]:
        return self._selected_index

# -------------------------------------------------------------------
# Prompt dinámico según tools disponibles
# -------------------------------------------------------------------

def _build_system_prompt_for_tools(tools_schema: List[Dict[str, Any]]) -> str:
    names = {t.get("name","") for t in tools_schema}

    # Heurística: Filesystem server
    fs_tools = {"list_allowed_directories", "write_file", "read_text_file", "create_directory"}
    is_fs = fs_tools.issubset(names)

    # Heurística: Setlist server
    music_tools = {"add_song", "list_playlists", "get_playlist", "export_playlist", "clear_library"}
    is_music = music_tools.issubset(names)

    base = (
        "Eres el Host MCP. Puedes llamar herramientas MCP cuando ayuden a cumplir la petición del usuario.\n"
        "- Devuelve respuestas claras y concisas.\n"
        "- Si llamas a una tool, espera su resultado y explica brevemente lo hecho.\n"
    )

    fs_part = (
        "\n[Instrucciones para sistema de archivos]\n"
        "1) Llama primero a list_allowed_directories para obtener la(s) carpeta(s) permitida(s).\n"
        "2) Usa la PRIMERA ruta permitida como raíz por defecto.\n"
        "3) Resuelve rutas relativas dentro de esa raíz (p.ej., raíz + '/docs/README.md').\n"
        "4) Si no existe la carpeta destino, crea la jerarquía con create_directory antes de escribir/mover.\n"
        "5) Para crear/actualizar un archivo: write_file. Para mostrar contenido: read_text_file.\n"
        "6) Nunca intentes acceder fuera de los directorios permitidos.\n"
        "Ejemplos:\n"
        "• “Crea README.md con 'Hola mundo' en docs y muéstrame su contenido”.\n"
        "• “Lista los archivos de la raíz permitida”.\n"
    )

    music_part = (
        "\n[Instrucciones para música / playlists]\n"
        "Tienes tools: add_song, list_playlists, get_playlist, export_playlist, clear_library.\n"
        "- Usa add_song para añadir; si hay confirmación, presenta candidatos.\n"
        "- Usa list_playlists / get_playlist para listar/consultar; export_playlist para XLSX.\n"
        "- clear_library limpia la librería.\n"
        "Ejemplos:\n"
        "• “Añade 'Blinding Lights' de The Weeknd y dime a qué playlist fue”.\n"
        "• “Enséñame la playlist Chill”.\n"
    )

    prompt = base
    if is_fs: prompt += fs_part
    if is_music: prompt += music_part
    if not is_fs and not is_music:
        prompt += "\nTools disponibles: " + ", ".join(sorted(names)) + ". Úsalas cuando convenga.\n"
    return prompt

# -------------------------------------------------------------------
# Worker de chat LLM (hilo): LLM → tool_use → tools → tool_result → LLM
# -------------------------------------------------------------------

class LLMWorker(QThread):
    # ✅ Incluimos el patch completo para guardarlo en history
    done = Signal(str, list, list)      # assistant_text, assistant_blocks_final, patch_msgs
    fail = Signal(str)
    song_added = Signal(dict)           # emite dict con datos de la canción añadida
    library_cleared = Signal()          # emite cuando se limpia la librería

    def __init__(self, client: Anthropic, model: str, history: list[dict], user_text: str, mcp: MCPAdapter):
        super().__init__()
        self.client = client
        self.model = model
        self.history = history
        self.user_text = user_text
        self.mcp = mcp

    def _call_tools(self, uses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        results_blocks: List[Dict[str, Any]] = []
        for u in uses:
            name = u["name"]; args = u["arguments"]; tu_id = u["id"]
            try:
                result_obj = self.mcp.call_tool(name, args or {})
                payload = json.dumps(result_obj, ensure_ascii=False)

                # Señales específicas para GUI (música)
                if name == "add_song":
                    if isinstance(result_obj, dict) and "chosen" in result_obj and isinstance(result_obj["chosen"], dict):
                        self.song_added.emit(result_obj["chosen"])
                elif name == "clear_library":
                    self.library_cleared.emit()

                results_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": [{"type": "text", "text": payload}]
                })
            except MCPNeedsConfirmation as cf:
                payload = json.dumps({
                    "status": "needs_confirmation",
                    "candidates": cf.candidates,
                    "message": cf.message
                }, ensure_ascii=False)
                results_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": [{"type": "text", "text": payload}]
                })
            except Exception as e:
                err = json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
                results_blocks.append({
                    "type":"tool_result",
                    "tool_use_id":tu_id,
                    "content": [{"type": "text", "text": err}]
                })
        return results_blocks

    def run(self):
        try:
            tools_schema = self.mcp.get_tools_schema(ttl_sec=10.0)
            system_prompt = _build_system_prompt_for_tools(tools_schema)

            patch: List[Dict[str, Any]] = []
            patch.append({"role": "user", "content": self.user_text})

            final_blocks = []
            final_text = ""
            MAX_TOOL_HOPS = 8  # seguridad: admite cadenas largas (FS suele necesitar varias)

            for _ in range(MAX_TOOL_HOPS):
                # 1) Modelo razona y (opcionalmente) pide tools
                msg = self.client.messages.create(
                    model=self.model,
                    system=system_prompt,
                    tools=tools_schema,
                    max_tokens=1024,
                    messages=self.history + patch
                )
                patch.append({"role": "assistant", "content": msg.content})

                uses = _extract_tool_uses(msg.content)
                if not uses:
                    final_blocks = msg.content
                    final_text = _blocks_to_text(msg.content)
                    break

                # 2) Ejecutamos todas las tools pedidas y entregamos resultados
                tool_results = self._call_tools(uses)

                # ¡IMPORTANTE!: el siguiente mensaje DEBE ser user con SOLO tool_result
                patch.append({"role": "user", "content": tool_results})

            else:
                # Salimos por límite de hops: dejamos la conversación en estado válido
                # (el último assistant(tool_use) ya recibió su user(tool_result))
                # No hay respuesta final del modelo; mostramos algo corto.
                final_blocks = patch[-1]["content"] if patch and patch[-1]["role"] == "assistant" else []
                if not final_text:
                    final_text = "Hecho. (Se alcanzó el límite de pasos de herramientas; si necesitas, di “sigue”)."

            self.done.emit(final_text, final_blocks, patch)

        except Exception as e:
            self.fail.emit(f"{type(e).__name__}: {e}\n{traceback.format_exc()}")

# -------------------------------------------------------------------
# Ventana principal (GUI)
# -------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Setlist Architect — Host MCP (LLM)")
        self.resize(1200, 780)

        # LLM
        load_dotenv()
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            QMessageBox.critical(self, "Falta ANTHROPIC_API_KEY", "Define ANTHROPIC_API_KEY en .env")
            raise SystemExit(2)
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-20241022")
        self.client = Anthropic(api_key=api_key)

        # MCP
        self.workspace: str = DEFAULT_WORKSPACE
        self.mcp = MCPAdapter(workspace=self.workspace)

        # Estado de chat
        self.history: List[Dict[str, Any]] = []
        self._last_user_text = ""

        # Solo canciones agregadas EN ESTA SESIÓN (si está el server de música)
        self._session_songs: List[Dict[str, Any]] = []

        self._build_ui()
        self._thinking_timer: Optional[QTimer] = None
        self._thinking_dots: int = 0

    # ---------------- UI ----------------

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)

        # Banner & estado
        self.banner = QLabel(f"Workspace: <b>{self.workspace}</b>  •  Modelo: <b>{self.model}</b>")
        self.banner.setWordWrap(True)

        # --- Panel Chat (izquierda) ---
        self.chat_view = QTextEdit(); self.chat_view.setReadOnly(True)
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText('Pide cosas naturales: ej. "crea README en docs y muéstrame su contenido" • "añade Blinding Lights"...')
        self.input_edit.returnPressed.connect(self.on_send)
        self.send_btn = QPushButton("Enviar"); self.send_btn.clicked.connect(self.on_send)
        self.thinking_label = QLabel(""); self.thinking_label.setStyleSheet("color:#777;")

        chat_box = QVBoxLayout()
        chat_box.addWidget(QLabel("Chat"))
        chat_box.addWidget(self.chat_view, 1)
        io = QHBoxLayout(); io.addWidget(self.input_edit, 1); io.addWidget(self.send_btn); chat_box.addLayout(io)
        chat_box.addWidget(self.thinking_label)
        chat_panel = QWidget(); chat_panel.setLayout(chat_box)

        # --- Panel derecho: referencia + tabla (solo música) ---
        right_box = QVBoxLayout()

        self.ref_text = QTextEdit(); self.ref_text.setReadOnly(True); self.ref_text.setMaximumHeight(180)
        self.ref_text.setPlainText(self._build_reference_text())
        right_box.addWidget(QLabel("Referencia rápida"))
        right_box.addWidget(self.ref_text)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["Título","Artistas","BPM","Key","Mode","Energy","Brightness","Duración (s)"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        right_box.addWidget(self.table, 1)

        right_panel = QWidget(); right_panel.setLayout(right_box)

        splitter = QSplitter(Qt.Horizontal); splitter.addWidget(chat_panel); splitter.addWidget(right_panel)
        splitter.setSizes([520, 680])

        main = QVBoxLayout(); main.addWidget(self.banner); main.addWidget(splitter, 1)
        root.setLayout(main)

    def _build_reference_text(self) -> str:
        return (
            "Filesystem (ejemplos):\n"
            "• “Crea README.md con 'Hola mundo' en la carpeta docs y muéstrame su contenido”.\n"
            "• “Lista los archivos de la raíz permitida”.\n"
            "• “Edita README.md: reemplaza 'Hola' por 'Hola 👋' y enséñame el diff”.\n"
            "\n"
            "Música (ejemplos):\n"
            "• “Añade ‘Blinding Lights’ de The Weeknd.”\n"
            "• “Lista las playlists y cuántas canciones tienen.”\n"
            "• “Enséñame la playlist Chill.”\n"
            "• “Limpia la librería.”\n"
        )

    # ---------------- Utilidades de UI ----------------

    def append_chat(self, who: str, text: str):
        self.chat_view.append(f"<b>{who}:</b> {text}")

    def _thinking_start(self):
        self._thinking_dots = 0
        if not hasattr(self, "_thinking_timer") or self._thinking_timer is None:
            self._thinking_timer = QTimer(self)
            self._thinking_timer.timeout.connect(self._thinking_tick)
        self._thinking_timer.start(450); self._thinking_tick()
        self.send_btn.setEnabled(False); self.input_edit.setEnabled(False)

    def _thinking_tick(self):
        self._thinking_dots = (self._thinking_dots + 1) % 6
        self.thinking_label.setText("pensando" + "." * self._thinking_dots)

    def _thinking_stop(self):
        if getattr(self, "_thinking_timer", None):
            self._thinking_timer.stop()
        self.thinking_label.setText(""); self.send_btn.setEnabled(True); self.input_edit.setEnabled(True)

    # ---------------- Tabla (solo música) ----------------

    def _table_clear_session(self):
        self._session_songs.clear()
        self.table.setRowCount(0)

    def _table_add_song(self, song: Dict[str, Any]):
        self._session_songs.append(song)
        r = self.table.rowCount()
        self.table.insertRow(r)

        def s(k, default=""): return str(song.get(k, default) if song.get(k, default) is not None else "")
        self.table.setItem(r, 0, QTableWidgetItem(s("title")))
        self.table.setItem(r, 1, QTableWidgetItem(s("artists")))
        self.table.setItem(r, 2, QTableWidgetItem(_fmt_num(song.get("bpm"), 2)))
        self.table.setItem(r, 3, QTableWidgetItem(s("key")))
        self.table.setItem(r, 4, QTableWidgetItem(s("mode")))
        self.table.setItem(r, 5, QTableWidgetItem(_fmt_num(song.get("energy"), 3)))
        self.table.setItem(r, 6, QTableWidgetItem(_fmt_num(song.get("brightness"), 3)))
        self.table.setItem(r, 7, QTableWidgetItem(_fmt_num(song.get("duration_sec"), 1)))
        self.table.resizeRowsToContents()

    # ---------------- Chat (LLM) ----------------

    def on_send(self):
        text = self.input_edit.text().strip()
        if not text: return
        self.append_chat("tú", text)
        self.input_edit.clear()
        self._last_user_text = text
        self._thinking_start()

        worker = LLMWorker(self.client, self.model, self.history, text, self.mcp)
        worker.done.connect(self._on_llm_done)
        worker.fail.connect(self._on_llm_fail)
        worker.song_added.connect(self._on_song_added)
        worker.library_cleared.connect(self._on_library_cleared)
        worker.start()
        self._llm_worker = worker  # evita GC del thread

    def _on_llm_done(self, assistant_text: str, blocks_final: list, patch: List[Dict[str, Any]]):
        self._thinking_stop()
        self.append_chat("assistant", assistant_text or "(sin texto)")
        # ✅ Guardar TODO el patch de mensajes (incluye tool_result intermedios)
        #    Esto evita `tool_use` huérfanos en el siguiente turno.
        #    Además, normalizamos los bloques assistant para que queden “serializables”.
        patched_history: List[Dict[str, Any]] = []
        for msg in patch:
            role = msg.get("role")
            content = msg.get("content")
            if role == "assistant":
                content = _normalize_blocks(content)
            patched_history.append({"role": role, "content": content})
        self.history.extend(patched_history)

    def _on_llm_fail(self, err: str):
        self._thinking_stop()
        self.append_chat("assistant", f"[ERROR]\n<pre>{err}</pre>")

    def _on_song_added(self, chosen: dict):
        self._table_add_song(chosen)

    def _on_library_cleared(self):
        self._table_clear_session()

# -------------------------------------------------------------------
# Entry point
# -------------------------------------------------------------------

def main():
    apply_windows_utf8_console()
    print_startup_banner()
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
