# host/gui_app.py
from __future__ import annotations
import os
import json
import traceback
from typing import Any, List, Optional, Iterable, Set

from dotenv import load_dotenv
from anthropic import Anthropic

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QLineEdit, QTextEdit,
    QHBoxLayout, QVBoxLayout, QFileDialog, QComboBox, QSpinBox, QTableWidget,
    QTableWidgetItem, QFrame, QMessageBox, QSplitter, QSizePolicy, QTableWidgetSelectionRange
)

from .tool_schemas import TOOLS  # <- cuando actualices el schema del LLM, usa las nuevas tools aquí
from .mcp_adapter import MCPAdapter
from .utils import env_list, norm_path, strip_quotes


# ---------------- Helpers Anthropic blocks ----------------
def _block_type(b):
    return getattr(b, "type", None) if not isinstance(b, dict) else b.get("type")

def blocks_to_text(blocks: list[dict | object]) -> str:
    parts = []
    for b in blocks:
        if _block_type(b) == "text":
            txt = getattr(b, "text", None) if not isinstance(b, dict) else b.get("text")
            if isinstance(txt, str):
                parts.append(txt)
    return "\n".join(parts)

class ToolCall:
    def __init__(self, name: str, arguments: dict[str, Any], tool_use_id: str):
        self.name = name
        self.arguments = arguments
        self.tool_use_id = tool_use_id

def extract_tool_uses(blocks: list[dict | object]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for b in blocks:
        if _block_type(b) == "tool_use":
            name = getattr(b, "name", None) if not isinstance(b, dict) else b.get("name")
            input_args = getattr(b, "input", None) if not isinstance(b, dict) else b.get("input")
            tu_id = getattr(b, "id", None) if not isinstance(b, dict) else b.get("id")
            calls.append(ToolCall(name=name, arguments=input_args or {}, tool_use_id=tu_id))
    return calls


SYSTEM_PROMPT = (
    "Eres Setlist Architect Host. Tienes herramientas para analizar audio local y sugerir setlists. "
    "Usa herramientas cuando el usuario lo pida explícitamente o cuando mejore la respuesta. "
    "Responde conciso y devuelve JSON cuando el usuario lo solicite."
)


# ---------------- Worker de chat (hilo) ----------------
class ChatWorker(QThread):
    done = Signal(str, list)   # (assistant_text, assistant_blocks)
    fail = Signal(str)

    def __init__(self, client: Anthropic, model: str, history: list[dict[str, Any]], adapter: MCPAdapter, user_text: str):
        super().__init__()
        self.client = client
        self.model = model
        self.history = history
        self.adapter = adapter
        self.user_text = user_text

    def run(self):
        try:
            # 1ª ronda
            msg = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=self.history + [{"role": "user", "content": self.user_text}],
            )
            tool_calls = extract_tool_uses(msg.content)
            if tool_calls:
                results_blocks: list[dict[str, Any]] = []
                for tc in tool_calls:
                    try:
                        result_str = self.adapter.call(tc.name, tc.arguments)
                    except Exception as e:
                        result_str = json.dumps({"error": str(e)}, ensure_ascii=False)
                    results_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": tc.tool_use_id,
                        "content": result_str,
                    })
                follow = self.client.messages.create(
                    model=self.model,
                    max_tokens=1024,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=self.history
                    + [{"role": "user", "content": self.user_text}]
                    + [{"role": "assistant", "content": msg.content}]
                    + [{"role": "user", "content": results_blocks}],
                )
                assistant_text = blocks_to_text(follow.content)
                self.done.emit(assistant_text, follow.content)
            else:
                assistant_text = blocks_to_text(msg.content)
                self.done.emit(assistant_text, msg.content)
        except Exception as e:
            self.fail.emit(f"{type(e).__name__}: {e}\n{traceback.format_exc()}")


# ---------------- Drop Area para ARCHIVOS ----------------
class DropArea(QFrame):
    filesDropped = Signal(list)  # lista de rutas absolutas

    def __init__(self):
        super().__init__()
        self.setFrameStyle(QFrame.StyledPanel | QFrame.Plain)
        self.setStyleSheet("QFrame { border: 2px dashed #888; border-radius: 8px; }")
        self.setAcceptDrops(True)
        lbl = QLabel("Arrastra aquí tus canciones (MP3/WAV/FLAC/M4A/OGG)")
        lbl.setAlignment(Qt.AlignCenter)
        font = QFont()
        font.setPointSize(10)
        lbl.setFont(font)
        layout = QVBoxLayout(self)
        layout.addWidget(lbl)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        if not urls:
            return
        paths = []
        for u in urls:
            p = u.toLocalFile()
            if p:
                # si es carpeta, ignora (nuevo flujo: per-song)
                if os.path.isfile(p):
                    paths.append(p)
        if paths:
            self.filesDropped.emit(paths)


# ---------------- Ventana principal ----------------
SUPPORTED_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".ogg"}

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Setlist Architect — Chat + MCP")
        self.resize(1200, 760)

        load_dotenv()
        self.api_key = os.environ.get("ANTHROPIC_API_KEY")
        self.model = os.environ.get("ANTHROPIC_MODEL") or os.environ.get("MODEL") or "claude-3-5-sonnet-20240620"
        if not self.api_key:
            QMessageBox.critical(self, "Falta API Key", "Define ANTHROPIC_API_KEY en tu .env")
        self.client = Anthropic(api_key=self.api_key) if self.api_key else None

        # Conectar MCP al iniciar
        self.adapter: Optional[MCPAdapter] = None
        self._connect_mcp()

        # Historial de conversación
        self.history: list[dict[str, Any]] = []

        # Canciones pendientes de agregar (paths)
        self.pending_files: Set[str] = set()

        # UI
        self._build_ui()

    # ---------- Conexión MCP ----------
    def _connect_mcp(self):
        try:
            cmd = strip_quotes(os.environ.get("MCP_SERVER_CMD") or "")
            args = env_list("MCP_SERVER_ARGS")
            cwd = norm_path(os.environ.get("MCP_CWD"))
            extra_env = {}
            if os.environ.get("MCP_PYTHONPATH"):
                extra_env["PYTHONPATH"] = norm_path(os.environ["MCP_PYTHONPATH"]) or ""
            self.adapter = MCPAdapter(cmd, args, cwd=cwd, env=extra_env)
            tools = [t.get("name") for t in getattr(self.adapter, "tools", [])]
            self.mcp_status = f"Conectado (tools: {tools})"
        except Exception as e:
            self.adapter = None
            self.mcp_status = f"Error MCP: {e}"

    # ---------- Hints (comandos MCP) ----------
    def _escape_path(self, p: str) -> str:
        return p.replace("\\", "\\\\").strip()

    def _build_hints_text(self) -> str:
        # usa un path de ejemplo o el último ingresado en el input
        example = "C:\\\\ruta\\\\a\\\\cancion.mp3"
        user_one = ""
        if hasattr(self, "file_input"):
            txt = self.file_input.text().strip()
            if txt:
                user_one = self._escape_path(txt)
        song = user_one or example
        csv_path = self._escape_path(os.path.join(os.path.expanduser("~"), "setlist.csv"))

        lines = [
            "Comandos MCP sugeridos:",
            f'• Añadir canción → Run the tool add_song with {{"path":"{song}"}}',
            '• Ver playlists → Run the tool list_playlists with {}',
            '• Ver una playlist → Run the tool get_playlist with {"name":"Pop 100–130"}',
            f'• Exportar playlist → Run the tool export_playlist with {{"name":"Pop 100–130","csv_path":"{csv_path}"}}',
            '• Limpiar librería → Run the tool clear_library with {}',
            "(Copia y pega estos comandos en el chat de la izquierda.)",
        ]
        return "\n".join(lines)

    def update_hints(self):
        self.hints_text.setPlainText(self._build_hints_text())

    def copy_hints(self):
        QApplication.clipboard().setText(self.hints_text.toPlainText())
        QMessageBox.information(self, "Copiado", "Comandos copiados al portapapeles.")

    # ---------- UI ----------
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)

        # --- Hints de comandos MCP (banner superior) ---
        self.hints_title = QLabel("Cómo usar el MCP (comandos sugeridos)")
        font = QFont()
        font.setBold(True)
        self.hints_title.setFont(font)

        self.hints_text = QTextEdit()
        self.hints_text.setReadOnly(True)
        self.hints_text.setMaximumHeight(140)

        self.hints_refresh = QPushButton("Actualizar")
        self.hints_refresh.clicked.connect(self.update_hints)
        self.hints_copy = QPushButton("Copiar")
        self.hints_copy.clicked.connect(self.copy_hints)

        hints_row = QHBoxLayout()
        hints_row.addWidget(self.hints_refresh)
        hints_row.addWidget(self.hints_copy)
        hints_row.addStretch(1)

        # Estado MCP
        self.status_label = QLabel(self.mcp_status)
        self.status_label.setWordWrap(True)

        # -------- Panel Chat --------
        self.chat_view = QTextEdit()
        self.chat_view.setReadOnly(True)

        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("Escribe tu mensaje… (o usa: Run the tool ...)")
        self.send_btn = QPushButton("Enviar")
        self.send_btn.clicked.connect(self.on_send)

        chat_box = QVBoxLayout()
        chat_box.addWidget(QLabel("Chat"))
        chat_box.addWidget(self.chat_view, stretch=1)

        input_row = QHBoxLayout()
        input_row.addWidget(self.input_edit, stretch=1)
        input_row.addWidget(self.send_btn)
        chat_box.addLayout(input_row)

        chat_panel = QWidget()
        chat_panel.setLayout(chat_box)

        # -------- Panel Herramientas (per-song) --------
        tools_box = QVBoxLayout()
        tools_box.addWidget(QLabel("Canciones a añadir (arrastra o examina)"))

        # Input + Browse
        self.file_input = QLineEdit()
        self.file_input.setPlaceholderText("Pega la ruta de una canción (MP3/WAV/FLAC/M4A/OGG)")
        browse_btn = QPushButton("Examinar…")
        browse_btn.clicked.connect(self.on_browse_files)

        input_row2 = QHBoxLayout()
        input_row2.addWidget(self.file_input, stretch=1)
        input_row2.addWidget(browse_btn)
        tools_box.addLayout(input_row2)

        # Drop area
        self.drop_area = DropArea()
        self.drop_area.setMinimumHeight(90)
        self.drop_area.filesDropped.connect(self.on_files_dropped)
        tools_box.addWidget(self.drop_area)

        # Pending files status + Add button
        self.pending_label = QLabel("0 archivos pendientes")
        add_btn = QPushButton("➕ Añadir canción(es)")
        add_btn.clicked.connect(self.on_add_songs)

        row_add = QHBoxLayout()
        row_add.addWidget(self.pending_label)
        row_add.addStretch(1)
        row_add.addWidget(add_btn)
        tools_box.addLayout(row_add)

        # Playlists panel
        tools_box.addWidget(QLabel("Playlists"))
        self.playlists_table = QTableWidget(0, 2)
        self.playlists_table.setHorizontalHeaderLabels(["name", "count"])
        self.playlists_table.horizontalHeader().setStretchLastSection(True)
        self.playlists_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
        tools_box.addWidget(self.playlists_table)

        row_pl_buttons = QHBoxLayout()
        refresh_pl = QPushButton("🔄 Refrescar playlists")
        refresh_pl.clicked.connect(self.on_list_playlists)
        self.playlist_name_edit = QLineEdit()
        self.playlist_name_edit.setPlaceholderText('Nombre exacto (p.ej. "Pop 100–130")')
        show_pl = QPushButton("👁️ Ver playlist")
        show_pl.clicked.connect(self.on_get_playlist)
        export_pl = QPushButton("💾 Exportar playlist…")
        export_pl.clicked.connect(self.on_export_playlist)
        clear_btn = QPushButton("🧹 Limpiar librería")
        clear_btn.clicked.connect(self.on_clear_library)

        row_pl_buttons.addWidget(refresh_pl)
        row_pl_buttons.addWidget(self.playlist_name_edit, stretch=1)
        row_pl_buttons.addWidget(show_pl)
        row_pl_buttons.addWidget(export_pl)
        row_pl_buttons.addWidget(clear_btn)
        tools_box.addLayout(row_pl_buttons)

        # Resultados (tracks en memoria / playlist mostrada)
        self.notes_label = QLabel("")
        self.results_table = QTableWidget(0, 9)
        self.results_table.setHorizontalHeaderLabels(
            ["path", "title", "artist", "duration", "bpm", "key", "mode", "energy", "brightness"]
        )
        self.results_table.horizontalHeader().setStretchLastSection(True)
        self.results_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        tools_box.addWidget(QLabel("Resultados"))
        tools_box.addWidget(self.results_table, stretch=1)

        tools_panel = QWidget()
        tools_panel.setLayout(tools_box)

        # Splitter
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(chat_panel)
        splitter.addWidget(tools_panel)
        splitter.setSizes([600, 600])

        # Layout raíz
        main_box = QVBoxLayout()
        main_box.addWidget(self.hints_title)
        main_box.addWidget(self.hints_text)
        main_box.addLayout(hints_row)

        main_box.addWidget(self.status_label)
        main_box.addWidget(splitter, stretch=1)

        root.setLayout(main_box)

        # Inicializa el texto del banner
        self.update_hints()

    # ---------- Slots ----------
    def append_chat(self, who: str, text: str):
        self.chat_view.append(f"<b>{who}:</b> {text}")

    def on_send(self):
        text = self.input_edit.text().strip()
        if not text:
            return
        if not self.client or not self.api_key:
            QMessageBox.warning(self, "Sin API", "Configura ANTHROPIC_API_KEY en .env")
            return
        if not self.adapter:
            QMessageBox.warning(self, "MCP no conectado", "Revisa variables MCP_* en .env")
            return

        self.append_chat("you", text)
        self.input_edit.clear()

        worker = ChatWorker(self.client, self.model, self.history, self.adapter, text)
        worker.done.connect(self.on_chat_done)
        worker.fail.connect(self.on_chat_fail)
        worker.start()
        self._chat_worker = worker  # mantener referencia

    def on_chat_done(self, assistant_text: str, assistant_blocks: list):
        self.append_chat("assistant", assistant_text)
        # Actualiza history con la última interacción
        self.history.extend([
            {"role": "user", "content": self.chat_view.toPlainText().split("assistant:")[0] if self.history else [{"type":"text","text":""}]},
            {"role": "assistant", "content": assistant_blocks},
        ])

    def on_chat_fail(self, err: str):
        self.append_chat("assistant", f"[ERROR]\n{err}")

    # ---------- Files helpers ----------
    def _is_audio(self, path: str) -> bool:
        ext = os.path.splitext(path)[1].lower()
        return ext in SUPPORTED_EXTS

    def _add_files(self, paths: Iterable[str]):
        added = 0
        for p in paths:
            if os.path.isfile(p) and self._is_audio(p):
                if p not in self.pending_files:
                    self.pending_files.add(p)
                    added += 1
        self.pending_label.setText(f"{len(self.pending_files)} archivos pendientes (+{added})")
        # sugerencia en input con el último
        if paths:
            last = next(iter(reversed(list(paths))))
            self.file_input.setText(last)
        self.update_hints()

    def on_browse_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Selecciona canciones",
            os.path.expanduser("~"),
            "Audio (*.mp3 *.wav *.flac *.m4a *.ogg)"
        )
        if files:
            self._add_files(files)

    def on_files_dropped(self, paths: List[str]):
        self._add_files(paths)

    # ---------- Table helpers ----------
    def _append_track_row(self, r: dict):
        i = self.results_table.rowCount()
        self.results_table.insertRow(i)
        def setc(col, val):
            self.results_table.setItem(i, col, QTableWidgetItem(str(val)))
        setc(0, r.get("path", ""))
        setc(1, r.get("title", ""))
        setc(2, r.get("artist", ""))
        setc(3, f'{float(r.get("duration", 0)):.2f}')
        setc(4, f'{float(r.get("bpm", 0)):.2f}')
        setc(5, r.get("key", ""))
        setc(6, r.get("mode", ""))
        setc(7, f'{float(r.get("energy", 0)):.3f}')
        setc(8, f'{float(r.get("brightness", 0)):.3f}')

    def _set_tracks_table(self, rows: List[dict]):
        self.results_table.setRowCount(0)
        for r in rows:
            self._append_track_row(r)

    def _set_playlists_table(self, playlists: List[dict]):
        self.playlists_table.setRowCount(0)
        for pl in playlists:
            i = self.playlists_table.rowCount()
            self.playlists_table.insertRow(i)
            self.playlists_table.setItem(i, 0, QTableWidgetItem(str(pl.get("name", ""))))
            self.playlists_table.setItem(i, 1, QTableWidgetItem(str(pl.get("count", 0))))

    # ---------- MCP tool actions (direct) ----------
    def on_add_songs(self):
        if not self.adapter:
            QMessageBox.warning(self, "MCP no conectado", "Revisa variables MCP_* en .env")
            return

        # también toma el texto del input como una sola canción (opcional)
        text_path = self.file_input.text().strip()
        if text_path and os.path.isfile(text_path):
            self.pending_files.add(text_path)

        if not self.pending_files:
            QMessageBox.information(self, "Nada que añadir", "Arrastra o selecciona al menos una canción.")
            return

        added = 0
        errors = []
        for path in list(self.pending_files):
            try:
                result_str = self.adapter.call("add_song", {"path": path})
                try:
                    data = json.loads(result_str)
                except Exception:
                    data = {"raw": result_str}
                if "track" in data:
                    self._append_track_row(data["track"])
                    added += 1
            except Exception as e:
                errors.append(f"{os.path.basename(path)}: {e}")
            finally:
                # consumimos la cola
                self.pending_files.discard(path)

        self.pending_label.setText(f"{len(self.pending_files)} archivos pendientes")
        msg = f"{added} canción(es) añadidas."
        if errors:
            msg += f" Errores: {len(errors)}"
        self.append_chat("assistant", msg)
        if errors:
            self.append_chat("assistant", "\n".join(errors))

    def on_list_playlists(self):
        if not self.adapter:
            QMessageBox.warning(self, "MCP no conectado", "Revisa variables MCP_* en .env")
            return
        try:
            result_str = self.adapter.call("list_playlists", {})
            data = json.loads(result_str) if result_str else {}
            pls = data.get("playlists", [])
            self._set_playlists_table(pls)
            self.append_chat("assistant", f"{len(pls)} playlist(s).")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def _selected_playlist_name(self) -> str:
        # Usa el textbox si tiene algo, si no, intenta tomar la fila seleccionada
        name = self.playlist_name_edit.text().strip()
        if name:
            return name
        items = self.playlists_table.selectedItems()
        if items:
            # la primera columna tiene el nombre
            row = items[0].row()
            it = self.playlists_table.item(row, 0)
            if it:
                return it.text().strip()
        return ""

    def on_get_playlist(self):
        if not self.adapter:
            QMessageBox.warning(self, "MCP no conectado", "Revisa variables MCP_* en .env")
            return
        name = self._selected_playlist_name()
        if not name:
            QMessageBox.information(self, "Playlist requerida", "Escribe o selecciona una playlist.")
            return
        try:
            result_str = self.adapter.call("get_playlist", {"name": name})
            data = json.loads(result_str) if result_str else {}
            rows = data.get("tracks", [])
            self._set_tracks_table(rows)
            self.notes_label.setText(f'Playlist "{name}" — {len(rows)} pistas')
            self.append_chat("assistant", f'Playlist "{name}" mostrada ({len(rows)} pistas).')
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def on_export_playlist(self):
        if not self.adapter:
            QMessageBox.warning(self, "MCP no conectado", "Revisa variables MCP_* en .env")
            return
        name = self._selected_playlist_name()
        if not name:
            QMessageBox.information(self, "Playlist requerida", "Escribe o selecciona una playlist.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Guardar CSV de playlist", os.path.expanduser(f"~/{name}.csv"), "CSV (*.csv)"
        )
        if not path:
            return
        try:
            result_str = self.adapter.call("export_playlist", {"name": name, "csv_path": path})
            data = json.loads(result_str) if result_str else {}
            if data.get("error"):
                QMessageBox.warning(self, "No exportado", data["error"])
                return
            rows = int(data.get("rows", 0))
            self.append_chat("assistant", f'CSV exportado: {data.get("csv_path", path)} ({rows} filas).')
            QMessageBox.information(self, "Exportado", f'Archivo guardado:\n{data.get("csv_path", path)}')
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def on_clear_library(self):
        if not self.adapter:
            QMessageBox.warning(self, "MCP no conectado", "Revisa variables MCP_* en .env")
            return
        try:
            result_str = self.adapter.call("clear_library", {})
            data = json.loads(result_str) if result_str else {}
            if data.get("ok"):
                self.results_table.setRowCount(0)
                self.playlists_table.setRowCount(0)
                self.pending_files.clear()
                self.pending_label.setText("0 archivos pendientes")
                self.notes_label.setText("")
                self.append_chat("assistant", "Librería limpiada.")
            else:
                QMessageBox.warning(self, "Atención", "No se pudo limpiar la librería.")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))


# ---------------- Entrypoint ----------------
def main():
    app = QApplication([])
    win = MainWindow()
    win.show()
    app.exec()

if __name__ == "__main__":
    main()
