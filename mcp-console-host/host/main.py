# host/main.py
from __future__ import annotations
import os
import json
from typing import Any

from dotenv import load_dotenv
from rich.console import Console
from anthropic import Anthropic

from .tool_schemas import TOOLS
from .mcp_adapter import MCPAdapter
from .utils import ToolCall, env_list, norm_path, strip_quotes

# --- Helpers para bloques de Anthropic ---
def _block_type(b):
    # Soporta objetos (pydantic) y dicts
    return getattr(b, "type", None) if not isinstance(b, dict) else b.get("type")

def blocks_to_text(blocks: list[dict | object]) -> str:
    parts = []
    for b in blocks:
        if _block_type(b) == "text":
            txt = getattr(b, "text", None) if not isinstance(b, dict) else b.get("text")
            if isinstance(txt, str):
                parts.append(txt)
    return "\n".join(parts)

def extract_tool_uses(blocks: list[dict | object]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for b in blocks:
        if _block_type(b) == "tool_use":
            name = getattr(b, "name", None) if not isinstance(b, dict) else b.get("name")
            input_args = getattr(b, "input", None) if not isinstance(b, dict) else b.get("input")
            tu_id = getattr(b, "id", None) if not isinstance(b, dict) else b.get("id")
            calls.append(ToolCall(name=name, arguments=input_args or {}, tool_use_id=tu_id))
    return calls

def _tool_names_from_adapter(adapter) -> list[str]:
    # MCPAdapter expone .tools
    if hasattr(adapter, "tools") and isinstance(getattr(adapter, "tools"), list):
        return [t.get("name") for t in adapter.tools if isinstance(t, dict)]
    # Fallback (por si se ejecuta en DIRECT sin MCP)
    return ["add_song", "list_playlists", "get_playlist", "export_playlist", "clear_library"]

def format_welcome(names: list[str]) -> str:
    # Construye mensaje de ayuda con ejemplos de comandos "Run the tool ..."
    example_song = "C:\\\\ruta\\\\a\\\\cancion.mp3"
    example_csv  = "C:\\\\Users\\\\tuusuario\\\\setlist.csv"
    lines = ["Opciones MCP disponibles:"]
    if "add_song" in names:
        lines.append(
            f'• Añadir canción → Run the tool add_song with {{"path":"{example_song}"}}'
        )
    if "list_playlists" in names:
        lines.append(
            '• Ver playlists → Run the tool list_playlists with {}'
        )
    if "get_playlist" in names:
        lines.append(
            '• Ver una playlist → Run the tool get_playlist with {"name":"Pop 100–130"}'
        )
    if "export_playlist" in names:
        lines.append(
            f'• Exportar playlist → Run the tool export_playlist with {{"name":"Pop 100–130","csv_path":"{example_csv}"}}'
        )
    if "clear_library" in names:
        lines.append(
            '• Limpiar librería → Run the tool clear_library with {}'
        )
    lines.append("Tip: escribe help para volver a ver estas opciones.")
    return "\n".join(lines)


SYSTEM_PROMPT = (
    "Eres Setlist Architect Host. Tienes herramientas para analizar audio local y sugerir setlists. "
    "Usa herramientas cuando el usuario lo pida explícitamente o cuando mejore la respuesta. "
    "Responde conciso y devuelve JSON cuando el usuario lo solicite."
)

console = Console()


def run_chat_loop():
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        console.print("[red]Falta ANTHROPIC_API_KEY en .env[/red]")
        return

    model = (
        os.environ.get("ANTHROPIC_MODEL")
        or os.environ.get("MODEL")
        or "claude-3-5-sonnet-20240620"
    )
    mode = os.environ.get("MODE", "mcp").lower()  # por defecto mcp para el nuevo flujo

    client = Anthropic(api_key=api_key)

    # --- Adapter selection
    if mode == "mcp":
        cmd = strip_quotes(os.environ.get("MCP_SERVER_CMD") or "")
        args = env_list("MCP_SERVER_ARGS")  # coma-separado, p.ej. "-m,setlist_architect.server"
        cwd = norm_path(os.environ.get("MCP_CWD"))
        extra_env = {}
        if os.environ.get("MCP_PYTHONPATH"):
            extra_env["PYTHONPATH"] = norm_path(os.environ["MCP_PYTHONPATH"]) or ""
        adapter = MCPAdapter(cmd, args, cwd=cwd, env=extra_env)
        console.print(
            f"[cyan]MCP conectado. Tools detectadas: "
            f"{[t.get('name') for t in getattr(adapter, 'tools', [])]}[/cyan]"
        )
    else:
        console.print("[yellow]MODE=direct: el flujo per-song (add_song, playlists) requiere MODE=mcp.[/yellow]")
        # En DIRECT no tenemos implementación equivalente a per-song.
        cmd = strip_quotes(os.environ.get("MCP_SERVER_CMD") or "")
        args = env_list("MCP_SERVER_ARGS")
        cwd = norm_path(os.environ.get("MCP_CWD"))
        adapter = MCPAdapter(cmd, args, cwd=cwd)  # intenta igualmente usar MCP

    history: list[dict[str, Any]] = []

    # --- Mensaje inicial como "assistant" con los comandos MCP
    names = _tool_names_from_adapter(adapter)
    welcome = format_welcome(names)
    print(f"assistant> {welcome}")
    history.append({"role": "assistant", "content": [{"type": "text", "text": welcome}]})

    console.print("[bold green]Setlist Architect CLI[/bold green] — escribe 'exit' para salir")
    try:
        while True:
            user = input("you> ").strip()
            if user.lower() in {"exit", "quit", ":q"}:
                break
            if user.lower() in {"help", "ayuda"}:
                welcome = format_welcome(names)
                print(f"assistant> {welcome}")
                history.append({"role": "assistant", "content": [{"type": "text", "text": welcome}]})
                continue

            # 1ª ronda: el modelo decide si usa tools
            msg = client.messages.create(
                model=model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,  # <-- asegúrate de que definen las 5 tools nuevas
                messages=history + [{"role": "user", "content": user}],
            )

            tool_calls = extract_tool_uses(msg.content)
            if tool_calls:
                # Ejecuta cada tool y regresa sus resultados como tool_result
                results_blocks: list[dict[str, Any]] = []
                for tc in tool_calls:
                    try:
                        result_str = adapter.call(tc.name, tc.arguments)
                    except Exception as e:
                        result_str = json.dumps({"error": str(e)}, ensure_ascii=False)
                    results_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tc.tool_use_id,
                            "content": result_str,
                        }
                    )

                # 2ª ronda: el modelo integra los resultados y responde al usuario
                follow = client.messages.create(
                    model=model,
                    max_tokens=1024,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=history
                    + [{"role": "user", "content": user}]
                    + [{"role": "assistant", "content": msg.content}]  # incluye tool_use
                    + [{"role": "user", "content": results_blocks}],
                )
                assistant_text = blocks_to_text(follow.content)
                print(f"assistant> {assistant_text}")
                history.extend(
                    [
                        {"role": "user", "content": user},
                        {"role": "assistant", "content": follow.content},
                    ]
                )
            else:
                # Sin tools: respuesta directa
                assistant_text = blocks_to_text(msg.content)
                print(f"assistant> {assistant_text}")
                history.extend(
                    [
                        {"role": "user", "content": user},
                        {"role": "assistant", "content": msg.content},
                    ]
                )
    finally:
        # Cierra MCP si aplica
        try:
            if isinstance(adapter, MCPAdapter):
                adapter.close()
        except Exception:
            pass


if __name__ == "__main__":
    run_chat_loop()
