# mcp-console-host
Chatbot de consola que conversa con Claude (Anthropic) y ejecuta herramientas de **Setlist Architect**.

## Modos
- **direct** (por defecto): llama funciones Python del paquete `setlist_architect`.
- **mcp** (pendiente): conectará por STDIO a servidores MCP.

## Requisitos
- Python 3.10+
- API Key de Anthropic (`ANTHROPIC_API_KEY`).

## Instalación
```bash
python -m venv venv
# Windows
venv\Scripts\activate
# Linux/macOS: source venv/bin/activate

pip install -r requirements.txt

# instala Setlist Architect local en editable para modo DIRECTO
pip install -e ..\setlist_architect   # ajusta la ruta a tu repo