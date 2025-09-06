# host/gui_app.py
from __future__ import annotations
import sys, json, traceback, time
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QLineEdit, QTextEdit,
    QHBoxLayout, QVBoxLayout, QTableWidget, QTableWidgetItem, QMessageBox,
    QSplitter, QSizePolicy, QComboBox, QDialog, QDialogButtonBox, QHeaderView
)

from anthropic import Anthropic
from dotenv import load_dotenv
import os

from host.mcp_adapter import MCPAdapter, MCPNeedsConfirmation, MCPServerError
from host.tool_schemas import TOOLS as GET_TOOLS  # schemas dinámicos (con fallback)
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

# -------------------------------------------------------------------
# Diálogo de confirmación de candidatos (add_song)
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

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["#", "Título", "Artistas", "Duración (s)", "Confianza", "Preview"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table, 1)

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
# Worker de chat LLM (hilo): LLM → tool_use → tools → tool_result → LLM
# -------------------------------------------------------------------

SYSTEM_PROMPT = (
    "Eres Setlist Architect Host. Tienes herramientas MCP para gestionar música: "
    "add_song, list_playlists, get_playlist, export_playlist, clear_library. "
    "Usa las tools cuando ayuden a cumplir la petición del usuario. "
    "Sé conciso y, cuando corresponda, muestra datos útiles."
)

class LLMWorker(QThread):
    done = Signal(str, list)   # assistant_text, assistant_blocks
    fail = Signal(str)
    refresh_hint = Signal()    # para refrescar tabla tras tool calls

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
                # si add_song pidió confirmación (flujo especial)
                if isinstance(result_obj, dict) and result_obj.get("status") == "needs_confirmation":
                    # devolvemos tal cual para que el LLM pida confirmación textual al usuario
                    payload = json.dumps(result_obj, ensure_ascii=False)
                else:
                    payload = json.dumps(result_obj, ensure_ascii=False)
                results_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": payload
                })
            except MCPNeedsConfirmation as cf:  # por si usaste wrappers directamente
                payload = json.dumps({
                    "status": "needs_confirmation",
                    "candidates": cf.candidates,
                    "message": cf.message
                }, ensure_ascii=False)
                results_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": payload
                })
            except Exception as e:
                err = json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
                results_blocks.append({"type":"tool_result","tool_use_id":tu_id,"content": err})
        # Si ejecutamos tools que alteran estado, pide refrescar tabla
        if any(u["name"] in ("add_song","clear_library") for u in uses):
            self.refresh_hint.emit()
        return results_blocks

    def run(self):
        try:
            tools_schema = GET_TOOLS()  # dinámico (llama tools/list al MCP o usa fallback)
            # Ronda 1
            msg1 = self.client.messages.create(
                model=self.model,
                system=SYSTEM_PROMPT,
                tools=tools_schema,
                max_tokens=1024,
                messages=self.history + [{"role":"user","content": self.user_text}],
            )
            uses = _extract_tool_uses(msg1.content)
            if not uses:
                text = _blocks_to_text(msg1.content)
                self.done.emit(text, msg1.content)
                return

            # Ejecutar tools y seguir
            tool_results = self._call_tools(uses)
            msg2 = self.client.messages.create(
                model=self.model,
                system=SYSTEM_PROMPT,
                tools=tools_schema,
                max_tokens=1024,
                messages=self.history
                    + [{"role":"user","content": self.user_text}]
                    + [{"role":"assistant","content": msg1.content}]
                    + [{"role":"user","content": tool_results}],
            )
            uses2 = _extract_tool_uses(msg2.content)
            if uses2:
                # (loop simple 2ª vuelta por si el modelo encadena otra tool)
                tool_results2 = self._call_tools(uses2)
                msg3 = self.client.messages.create(
                    model=self.model,
                    system=SYSTEM_PROMPT,
                    tools=tools_schema,
                    max_tokens=1024,
                    messages=self.history
                        + [{"role":"user","content": self.user_text}]
                        + [{"role":"assistant","content": msg1.content}]
                        + [{"role":"user","content": tool_results}]
                        + [{"role":"assistant","content": msg2.content}]
                        + [{"role":"user","content": tool_results2}],
                )
                text = _blocks_to_text(msg3.content)
                self.done.emit(text, msg3.content)
                return

            text = _blocks_to_text(msg2.content)
            self.done.emit(text, msg2.content)

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

        self._build_ui()
        self._thinking_timer: Optional[QTimer] = None
        self._thinking_dots: int = 0

        self._refresh_playlists()
        self._refresh_library_table()

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
        self.input_edit.setPlaceholderText('Habla natural: "añade Blinding Lights de The Weeknd y muéstrame las playlists"...')
        self.input_edit.returnPressed.connect(self.on_send)
        self.send_btn = QPushButton("Enviar"); self.send_btn.clicked.connect(self.on_send)
        self.thinking_label = QLabel(""); self.thinking_label.setStyleSheet("color:#777;")

        chat_box = QVBoxLayout()
        chat_box.addWidget(QLabel("Chat"))
        chat_box.addWidget(self.chat_view, 1)
        io = QHBoxLayout(); io.addWidget(self.input_edit, 1); io.addWidget(self.send_btn); chat_box.addLayout(io)
        chat_box.addWidget(self.thinking_label)
        chat_panel = QWidget(); chat_panel.setLayout(chat_box)

        # --- Panel derecho: filtros + referencia + tabla ---
        right_box = QVBoxLayout()

        # Fila de controles (solo filtro + refrescar)
        ctrl_row = QHBoxLayout()
        self.playlist_filter = QComboBox(); self.playlist_filter.addItem("Todas")
        self.playlist_filter.currentIndexChanged.connect(self._refresh_library_table)
        self.btn_refresh = QPushButton("Refrescar"); self.btn_refresh.clicked.connect(self._refresh_all)
        ctrl_row.addWidget(QLabel("Playlist:")); ctrl_row.addWidget(self.playlist_filter, 1)
        ctrl_row.addStretch(1); ctrl_row.addWidget(self.btn_refresh)
        right_box.addLayout(ctrl_row)

        # Referencia de funciones (para prompts)
        self.ref_text = QTextEdit(); self.ref_text.setReadOnly(True); self.ref_text.setMaximumHeight(200)
        self.ref_text.setPlainText(self._build_reference_text())
        right_box.addWidget(QLabel("Referencia de funciones MCP (para guiar tus prompts)"))
        right_box.addWidget(self.ref_text)

        # Tabla principal
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(["Título","Artistas","BPM","Key","Mode","Energy","Brightness","Duración (s)","Playlist"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        right_box.addWidget(self.table, 1)

        right_panel = QWidget(); right_panel.setLayout(right_box)

        # Splitter
        splitter = QSplitter(Qt.Horizontal); splitter.addWidget(chat_panel); splitter.addWidget(right_panel)
        splitter.setSizes([520, 680])

        main = QVBoxLayout(); main.addWidget(self.banner); main.addWidget(splitter, 1)
        root.setLayout(main)

    def _build_reference_text(self) -> str:
        return (
            "Pídele al asistente cosas como:\n"
            "• “Añade ‘Blinding Lights’ de The Weeknd y dime en qué playlist cayó.”\n"
            "• “Lista las playlists y cuántas canciones tienen.”\n"
            "• “Enséñame la playlist Workout y exporta la Chill.”\n"
            "• “Limpia la librería.”\n"
            "\n"
            "Herramientas disponibles (nombres exactos):\n"
            "• add_song  → args: {title: str, artists?: str}\n"
            "• list_playlists → args: {}\n"
            "• get_playlist  → args: {name: str}\n"
            "• export_playlist → args: {name: str} (genera .xlsx y devuelve file://...)\n"
            "• clear_library → args: {}\n"
            "\n"
            "Notas:\n"
            "• El asistente decide cuándo llamar a cada tool.\n"
            "• El workspace se inyecta automáticamente desde el host.\n"
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

    # ---------------- Acciones de alto nivel ----------------

    def _refresh_all(self):
        self._refresh_playlists()
        self._refresh_library_table()

    def _refresh_playlists(self):
        try:
            pls = self.mcp.list_playlists()
        except Exception as e:
            QMessageBox.warning(self, "Error", f"No se pudo listar playlists:\n{e}")
            return
        current = self.playlist_filter.currentText() if self.playlist_filter.count() else "Todas"
        self.playlist_filter.blockSignals(True)
        self.playlist_filter.clear(); self.playlist_filter.addItem("Todas")
        for p in sorted(pls, key=lambda x: x["name"].lower()):
            self.playlist_filter.addItem(p["name"])
        idx = self.playlist_filter.findText(current)
        self.playlist_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self.playlist_filter.blockSignals(False)

    def _collect_all_songs(self) -> List[Dict[str, Any]]:
        songs_by_id: Dict[str, Dict[str, Any]] = {}
        pls = self.mcp.list_playlists()
        for p in pls:
            name = p["name"]
            data = self.mcp.get_playlist(name)
            for s in data.get("songs", []):
                sid = str(s.get("song_id"))
                if sid not in songs_by_id:
                    sc = dict(s); sc["playlist"] = name
                    songs_by_id[sid] = sc
        return list(songs_by_id.values())

    def _refresh_library_table(self):
        try:
            songs = self._collect_all_songs()
        except Exception as e:
            QMessageBox.warning(self, "Error", f"No se pudo leer la librería:\n{e}")
            return

        flt = self.playlist_filter.currentText()
        if flt and flt != "Todas":
            songs = [s for s in songs if s.get("playlist") == flt]

        self.table.setRowCount(0)
        for s in songs:
            r = self.table.rowCount(); self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(str(s.get("title", ""))))
            self.table.setItem(r, 1, QTableWidgetItem(str(s.get("artists", ""))))
            self.table.setItem(r, 2, QTableWidgetItem(_fmt_num(s.get("bpm"), 2)))
            self.table.setItem(r, 3, QTableWidgetItem(str(s.get("key", ""))))
            self.table.setItem(r, 4, QTableWidgetItem(str(s.get("mode", ""))))
            self.table.setItem(r, 5, QTableWidgetItem(_fmt_num(s.get("energy"), 3)))
            self.table.setItem(r, 6, QTableWidgetItem(_fmt_num(s.get("brightness"), 3)))
            self.table.setItem(r, 7, QTableWidgetItem(_fmt_num(s.get("duration_sec"), 1)))
            self.table.setItem(r, 8, QTableWidgetItem(str(s.get("playlist", ""))))
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
        worker.refresh_hint.connect(self._refresh_all)
        worker.start()
        self._llm_worker = worker

    def _on_llm_done(self, assistant_text: str, blocks: list):
        self._thinking_stop()
        self.append_chat("assistant", assistant_text or "(sin texto)")
        # Actualizar history (preserva bloques originales)
        self.history.extend([
            {"role": "user", "content": self._last_user_text},
            {"role": "assistant", "content": blocks},
        ])
        # refrescar por si el modelo pidió listados
        self._refresh_all()

    def _on_llm_fail(self, err: str):
        self._thinking_stop()
        self.append_chat("assistant", f"[ERROR]\n<pre>{err}</pre>")

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
