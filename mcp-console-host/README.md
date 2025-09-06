# mcp-console-host

Host/chatbot que se comunica por **MCP** (JSON-RPC 2.0 vía STDIO) con el servidor **setlist-architect-mcp**.  
Incluye:

- **CLI** con subcomandos (listar tools, añadir canción, exportar playlist, etc.)
- **GUI** en PySide6 que muestra todas las canciones (título, artistas, BPM, key, mode, energy, brightness, duración y playlist) y permite filtrar por playlist.
- Descubrimiento **dinámico** de tools (`tools/list`) — no duplicamos schemas.

> Este repo es el **cliente**. El servidor vive en otro repo: `setlist-architect-mcp` (tu MCP local).

---

## Requisitos

- Python **3.10+** (recomendado 3.11/3.12)
- Sistema Windows/macOS/Linux
- Tener el repo del **servidor MCP** disponible en disco (ver sección de configuración)

---

## Instalación (venv recomendado)

```bash
# 1) crear y activar venv
python -m venv .venv
py -3.12 -m venv .venv

# macOS/Linux
source .venv/bin/activate
# Windows PowerShell
# .\.venv\Scripts\Activate.ps1

# 2) instalar dependencias del host
pip install -r requirements.txt
Configuración (enlazar con el servidor MCP)
El host lanza el servidor MCP como proceso hijo. Indica el comando completo con la variable de entorno MCP_SERVER_CMD.

Ejemplos:

Windows PowerShell

powershell
Copy code
# RUTA al python DEL SERVIDOR + RUTA al server.py
$env:MCP_SERVER_CMD = "C:\ruta\al\setlist-architect-mcp\.venv\Scripts\python.exe C:\ruta\al\setlist-architect-mcp\server.py"
macOS/Linux

bash
Copy code
export MCP_SERVER_CMD="$HOME/proyectos/setlist-architect-mcp/.venv/bin/python $HOME/proyectos/setlist-architect-mcp/server.py"
Opcionalmente, crea un .env en la raíz del host:

ini
Copy code
# .env
MCP_SERVER_CMD="C:/Users/tuuser/proyectos/setlist-architect-mcp/.venv/Scripts/python.exe C:/Users/tuuser/proyectos/setlist-architect-mcp/server.py"
MCP_WORKSPACE="user-miusuario-host"
MCP_LOG_FILE="logs/mcp_host.jsonl"
MCP_DEBUG=1
settings.py carga .env automáticamente si python-dotenv está instalado.

Ejecutar
CLI
Listar tools:

bash
Copy code
python -m host.main tools
Listar playlists:

bash
Copy code
python -m host.main playlists
Añadir canción:

bash
Copy code
python -m host.main add --title "Blinding Lights" --artists "The Weeknd"
# si devuelve needs_confirmation, confirma por índice:
python -m host.main add --title "Blinding Lights" --artists "The Weeknd" --confirm 0
Ver playlist:

bash
Copy code
python -m host.main show --name "Workout"
Exportar playlist (genera XLSX, el servidor devuelve un file:// URI):

bash
Copy code
python -m host.main export --name "Workout"
Limpiar librería:

bash
Copy code
python -m host.main clear
REPL de pruebas:

bash
Copy code
python -m host.main repl
--ws permite cambiar de workspace en cualquier subcomando, p.ej.: --ws demo.

GUI (PySide6)
bash
Copy code
python -m host.gui_app
Características:

Panel de chat (izquierda) con comandos simples:

add "titulo" -a "artistas"

playlists

show "Nombre de Playlist"

export "Nombre de Playlist"

clear

o directo: run the tool add_song with {"title":"...", "artists":"..."}

Si add_song requiere confirmación, aparece un diálogo con candidatos.

Panel derecho: tabla con todas las canciones (o filtradas por playlist). Columnas:

Título, Artistas, BPM, Key, Mode, Energy, Brightness, Duración (s), Playlist

Botones:

Refrescar (relee playlists + tabla)

Exportar playlist (para la seleccionada en el combo)

Limpiar librería (elimina canciones; conserva nombres de playlists)

Estructura del repo (host)
graphql
Copy code
mcp-console-host/
├─ host/
│  ├─ __init__.py
│  ├─ settings.py        # configuración del host y MCP (lee .env)
│  ├─ mcp_client.py      # cliente STDIO JSON-RPC (subprocess)
│  ├─ mcp_adapter.py     # fachada de alto nivel (descubre tools, llama, maneja confirmación)
│  ├─ tool_schemas.py    # schemas dinámicos (con fallback)
│  ├─ main.py            # entrypoint CLI
│  └─ gui_app.py         # GUI (PySide6)
├─ logs/                 # JSONL de req/resp host↔MCP
├─ requirements.txt
└─ README.md
Cómo funciona (alto nivel)
El host descubre las tools con tools/list (MCP) → evita duplicar schemas.

Llama tools con tools/call y inyecta workspace automáticamente (desde settings.py).

Respuestas de add_song:

json
Copy code
{"status":"ok","chosen":{...,"playlist":"..."}}
json
Copy code
{"status":"needs_confirmation","candidates":[...],...}
→ la GUI/CLI muestra opciones y reintenta con candidate_index.

Variables de entorno relevantes
MCP_SERVER_CMD : comando completo para lanzar el servidor MCP (OBLIGATORIO).

MCP_WORKSPACE : workspace por defecto (si no, settings.py genera uno).

MCP_LOG_FILE : ruta del JSONL de logs del host.

MCP_DEBUG : 1 para incluir el stderr del MCP en el log (útil para depurar).

Opcionales: MCP_REQ_TIMEOUT_SEC, MCP_STARTUP_TIMEOUT, MCP_MAX_RETRIES.

Consejos / Troubleshooting
No responde el host: revisa MCP_SERVER_CMD — debe apuntar al python del servidor y a su server.py.

Windows muestra “?” por guiones/acentos:

powershell
Copy code
chcp 65001
$env:PYTHONIOENCODING="utf-8"
Exportar XLSX: el archivo lo genera el servidor y el host muestra el file://... devuelto.

Audio/FFmpeg: esto es del lado del servidor (features.py); asegurarse que tenga FFmpeg (ver README del servidor).

Roadmap sugerido
Integrar LLM (opcional) usando los schemas dinámicos de tool_schemas.py.

Soporte multi-usuario: mapear workspace a user-id/chat-id.

Más vistas GUI: detalles por canción, edición de metadatos, arrastrar/soltar para re-clasificar.