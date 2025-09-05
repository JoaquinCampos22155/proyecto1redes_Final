# host/gui_app.py
from __future__ import annotations
import os
import json
import traceback
from typing import Any, List, Optional

from dotenv import load_dotenv
from anthropic import Anthropic

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QLineEdit, QTextEdit,
    QHBoxLayout, QVBoxLayout, QFileDialog, QTableWidget,
    QTableWidgetItem, QFrame, QMessageBox, QSplitter, QSizePolicy
)

from .tool_schemas import TOOLS
    # seguimos usando el mismo schema (add_song, list_playlists, ... ya expuesto)
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


# ---------------- Drop Area para archivos (solo drop) ----------------
class DropArea(QFrame):
    pathDropped = Signal(str)  # absoluta

    def __init__(self):
        super().__init__()
        self.setFrameStyle(QFrame.StyledPanel | QFrame.Plain)
        self.setStyleSheet("QFrame { border: 2px dashed #888; border-radius: 8px; }")
        self.setAcceptDrops(True)
        lbl = QLabel("Arrastra aquí una canción (MP3/WAV/FLAC/M4A/OGG)")
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
        first = urls[0].toLocalFile()
        if not first:
            return
        self.pathDropped.emit(first)


# ---------------- Ventana principal ----------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Setlist Architect — Chat + MCP")
        self.resize(1100, 740)

        load_dotenv()
        self.api_key = os.environ.get("ANTHROPIC_API_KEY")
        self.model = os.environ.get("ANTHROPIC_MODEL") or os.environ.get("MODEL") or "claude-3-5-sonnet-20240620"
        if not self.api_key:
            QMessageBox.critical(self, "Falta API Key", "Define ANTHROPIC_API_KEY en tu .env")
        self.client = Anthropic(api_key=self.api_key) if self.api_key else None

        # Conectar MCP al iniciar (timeout infinito si MCP_TIMEOUT_S no está)
        self.adapter: Optional[MCPAdapter] = None
        self._connect_mcp()

        # Historial de conversación
        self.history: list[dict[str, Any]] = []
        self._last_user_text: str = ""

        # UI
        self._build_ui()
        self.update_hints()

        # Animación "pensando…"
        self._thinking_timer: Optional[QTimer] = None
        self._thinking_dots: int = 0

    # ---------- Conexión MCP ----------
    def _connect_mcp(self):
        try:
            cmd = strip_quotes(os.environ.get("MCP_SERVER_CMD") or "")
            args = env_list("MCP_SERVER_ARGS")
            cwd = norm_path(os.environ.get("MCP_CWD"))
            extra_env = {}
            if os.environ.get("MCP_PYTHONPATH"):
                extra_env["PYTHONPATH"] = norm_path(os.environ["MCP_PYTHONPATH"]) or ""
            # Si MCP_TIMEOUT_S no está → None = espera infinita
            tval = os.environ.get("MCP_TIMEOUT_S")
            timeout = float(tval) if tval not in (None, "",) else None
            self.adapter = MCPAdapter(cmd, args, cwd=cwd, env=extra_env, timeout_s=timeout)
            tools = [t.get("name") for t in getattr(self.adapter, "tools", [])]
            self.mcp_status = f"Conectado (tools: {tools})"
        except Exception as e:
            self.adapter = None
            self.mcp_status = f"Error MCP: {e}"

    # ---------- Hints (comandos MCP) ----------
    def _escape_path(self, p: str) -> str:
        return p.replace("\\", "\\\\").strip()

    def _build_hints_text(self) -> str:
        # usa la última canción dropeada para prellenar
        song = getattr(self, "_last_dropped_song", "") or "C:\\\\ruta\\\\a\\\\cancion.mp3"
        song = self._escape_path(song)
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
        self.hints_text.setMaximumHeight(120)

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
        self.input_edit.setPlaceholderText('Escribe tu mensaje… (ej: Run the tool add_song with {"path":"C:\\\\...\\\\file.mp3"})')
        self.send_btn = QPushButton("Enviar")
        self.send_btn.clicked.connect(self.on_send)

        # Indicador de “pensando…”
        self.thinking_label = QLabel("")
        self.thinking_label.setStyleSheet("color: #777;")

        chat_box = QVBoxLayout()
        chat_box.addWidget(QLabel("Chat"))
        chat_box.addWidget(self.chat_view, stretch=1)
        chat_box.addWidget(self.thinking_label)

        input_row = QHBoxLayout()
        input_row.addWidget(self.input_edit, stretch=1)
        input_row.addWidget(self.send_btn)
        chat_box.addLayout(input_row)

        chat_panel = QWidget()
        chat_panel.setLayout(chat_box)

        # -------- Panel Derecho minimalista --------
        tools_box = QVBoxLayout()
        tools_box.addWidget(QLabel("Arrastra una canción"))

        self.drop_area = DropArea()
        self.drop_area.setMinimumHeight(110)
        self.drop_area.pathDropped.connect(self.on_song_dropped)
        tools_box.addWidget(self.drop_area)

        # Resultados
        self.notes_label = QLabel("")
        self.results_table = QTableWidget(0, 9)
        self.results_table.setHorizontalHeaderLabels(
            ["path", "title", "artist", "duration", "bpm", "key", "mode", "energy", "brightness"]
        )
        self.results_table.horizontalHeader().setStretchLastSection(True)
        self.results_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        tools_box.addWidget(QLabel("Notas / Métricas"))
        tools_box.addWidget(self.notes_label)
        tools_box.addWidget(QLabel("Resultados"))
        tools_box.addWidget(self.results_table, stretch=1)

        tools_panel = QWidget()
        tools_panel.setLayout(tools_box)

        # Splitter
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(chat_panel)
        splitter.addWidget(tools_panel)
        splitter.setSizes([620, 480])

        # Layout raíz
        main_box = QVBoxLayout()
        main_box.addWidget(self.hints_title)
        main_box.addWidget(self.hints_text)
        main_box.addLayout(hints_row)
        main_box.addWidget(self.status_label)
        main_box.addWidget(splitter, stretch=1)

        root.setLayout(main_box)

    # ---------- Animación “pensando…” ----------
    def _thinking_start(self):
        self._thinking_dots = 0
        if self._thinking_timer is None:
            self._thinking_timer = QTimer(self)
            self._thinking_timer.timeout.connect(self._thinking_tick)
        self._thinking_timer.start(450)
        self._thinking_tick()  # pinta inmediato
        self.send_btn.setEnabled(False)
        self.input_edit.setEnabled(False)

    def _thinking_tick(self):
        self._thinking_dots = (self._thinking_dots + 1) % 6
        self.thinking_label.setText("assistant pensando" + "." * self._thinking_dots)

    def _thinking_stop(self):
        if self._thinking_timer:
            self._thinking_timer.stop()
        self.thinking_label.setText("")
        self.send_btn.setEnabled(True)
        self.input_edit.setEnabled(True)

    # ---------- Slots (chat) ----------
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

        self._last_user_text = text
        self.append_chat("you", text)
        self.input_edit.clear()

        self._thinking_start()
        worker = ChatWorker(self.client, self.model, self.history, self.adapter, text)
        worker.done.connect(self.on_chat_done)
        worker.fail.connect(self.on_chat_fail)
        worker.start()
        self._chat_worker = worker  # mantener referencia

    def on_chat_done(self, assistant_text: str, assistant_blocks: list):
        self._thinking_stop()
        self.append_chat("assistant", assistant_text)
        # Actualiza history correctamente (evita bloques vacíos)
        self.history.extend([
            {"role": "user", "content": [{"type": "text", "text": self._last_user_text}]},
            {"role": "assistant", "content": assistant_blocks},
        ])

    def on_chat_fail(self, err: str):
        self._thinking_stop()
        self.append_chat("assistant", f"[ERROR]\n{err}")

    # ---------- Helper tabla ----------
    def _row_to_dict(self, row_idx: int) -> dict:
        get = self.results_table.item
        def _val(c):
            it = get(row_idx, c)
            return "" if it is None else it.text()
        return {
            "path": _val(0),
            "title": _val(1),
            "artist": _val(2),
            "duration": float(_val(3) or 0),
            "bpm": float(_val(4) or 0),
            "key": _val(5),
            "mode": _val(6),
            "energy": float(_val(7) or 0),
            "brightness": float(_val(8) or 0),
        }

    def _set_table(self, rows: List[dict]):
        self.results_table.setRowCount(0)
        for r in rows:
            i = self.results_table.rowCount()
            self.results_table.insertRow(i)
            self.results_table.setItem(i, 0, QTableWidgetItem(str(r.get("path", ""))))
            self.results_table.setItem(i, 1, QTableWidgetItem(str(r.get("title", ""))))
            self.results_table.setItem(i, 2, QTableWidgetItem(str(r.get("artist", ""))))
            self.results_table.setItem(i, 3, QTableWidgetItem(f'{float(r.get("duration", 0)):.2f}'))
            self.results_table.setItem(i, 4, QTableWidgetItem(f'{float(r.get("bpm", 0)):.2f}'))
            self.results_table.setItem(i, 5, QTableWidgetItem(str(r.get("key", ""))))
            self.results_table.setItem(i, 6, QTableWidgetItem(str(r.get("mode", ""))))
            self.results_table.setItem(i, 7, QTableWidgetItem(f'{float(r.get("energy", 0)):.3f}'))
            self.results_table.setItem(i, 8, QTableWidgetItem(f'{float(r.get("brightness", 0)):.3f}'))

    # ---------- Drop → prellenar comando en chat ----------
    def on_song_dropped(self, path: str):
        # Guarda para hints
        self._last_dropped_song = path
        self.update_hints()

        # Prellenar el comando en el input del chat (el usuario decide si enviar)
        escaped = path.replace("\\", "\\\\")
        cmd = f'Run the tool add_song with {{"path":"{escaped}"}}'
        self.input_edit.setText(cmd)
        self.append_chat("assistant", "Comando sugerido prellenado para analizar la canción (pulsa Enviar).")


def main():
    app = QApplication([])
    win = MainWindow()
    win.show()
    app.exec()

if __name__ == "__main__":
    main()
