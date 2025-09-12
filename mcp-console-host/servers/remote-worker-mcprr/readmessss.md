# En una terminal vs la otra MODO INSTPECTOR
wrangler.cmd dev --port=8788
npx @modelcontextprotocol/inspector
https://remote-mcp-jjcam.jjcampos2003.workers.dev/sse

wrangler.cmd dev --port=8788

Wireshark: 

MCP_SERVER_URL=http://127.0.0.1:8788/sse

MCP_SERVER_URL=https://remote-mcp-jjcam.jjcampos2003.workers.dev/sse



# modo gui

powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\use_env.ps1 remote.env

.\.venv\Scripts\Activate.ps1 

python -m host.gui_app

tú: dime la hora

tú: repite: "Quiero jugar futbol"

tú: repite: "quiero añadir blinding lights a la playlsit"

tú: genera el sha 256 de esa misma frase