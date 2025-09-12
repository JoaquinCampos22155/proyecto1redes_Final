# host/main.py
from __future__ import annotations
import argparse, json, sys, shlex, os, time, traceback
from typing import Any, Dict, Optional, List
from collections import deque

from dotenv import load_dotenv
from anthropic import Anthropic

from host.settings import (
    DEFAULT_WORKSPACE,
    apply_windows_utf8_console,
    print_startup_banner,
)
from host.mcp_adapter import MCPAdapter, MCPNeedsConfirmation, MCPServerError, MCPAdapterError
from host.tool_schemas import TOOLS as GET_TOOLS  

# ------------------------------------------------------------------------------
# Utilidades comunes
# ------------------------------------------------------------------------------

def jprint(obj: Any) -> None:
    try:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    except Exception:
        print(str(obj))

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
    """Convierte bloques Anthropic a dicts 'limpios' para guardar en history y logs."""
    out: list[dict] = []
    for b in blocks:
        if isinstance(b, dict):
            # homogeneiza tool_result.content si fuera string
            if b.get("type") == "tool_result" and isinstance(b.get("content"), str):
                b = {**b, "content": [{"type": "text", "text": b["content"]}]}
            out.append(b); continue
        typ = getattr(b, "type", None)
        if not typ:
            continue
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

def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")

def _ensure_dir(p: str):
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)

def jsonl_log(path: str, record: dict):
    _ensure_dir(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

# ------------------------------------------------------------------------------
# Subcomandos existentes (tus funciones originales)
# ------------------------------------------------------------------------------

def cmd_tools(args) -> int:
    mcp = MCPAdapter(workspace=args.ws)
    try:
        jprint(mcp.get_tools_schema())
        return 0
    finally:
        mcp.shutdown()

def cmd_playlists(args) -> int:
    mcp = MCPAdapter(workspace=args.ws)
    try:
        out = mcp.list_playlists()
        jprint(out)
        return 0
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    finally:
        mcp.shutdown()

def cmd_add(args) -> int:
    mcp = MCPAdapter(workspace=args.ws)
    try:
        if args.confirm is not None:
            ok = mcp.add_song(args.title, args.artists or "", candidate_index=int(args.confirm))
            jprint({"status":"ok", "chosen": ok.chosen})
            return 0

        try:
            ok = mcp.add_song(args.title, args.artists or "")
            jprint({"status":"ok", "chosen": ok.chosen})
            return 0
        except MCPNeedsConfirmation as cf:
            payload = {
                "status":"needs_confirmation",
                "message": cf.message,
                "candidates": cf.candidates,
                "hint": f'Vuelve a ejecutar con --confirm <idx>, p. ej.: '
                        f'add --ws {args.ws!s} --title {shlex.quote(args.title)}'
                        + (f' --artists {shlex.quote(args.artists)}' if args.artists else '')
                        + ' --confirm 0'
            }
            jprint(payload)
            return 10
    except MCPServerError as e:
        print(f"[server-error] {e}", file=sys.stderr)
        return 3
    except MCPAdapterError as e:
        print(f"[adapter-error] {e}", file=sys.stderr)
        return 4
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    finally:
        mcp.shutdown()

def cmd_confirm(args) -> int:
    mcp = MCPAdapter(workspace=args.ws)
    try:
        ok = mcp.add_song(args.title, args.artists or "", candidate_index=int(args.index))
        jprint({"status":"ok","chosen": ok.chosen})
        return 0
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    finally:
        mcp.shutdown()

def cmd_show(args) -> int:
    mcp = MCPAdapter(workspace=args.ws)
    try:
        out = mcp.get_playlist(args.name)
        jprint(out)
        return 0
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    finally:
        mcp.shutdown()

def cmd_export(args) -> int:
    mcp = MCPAdapter(workspace=args.ws)
    try:
        out = mcp.export_playlist(args.name)
        jprint(out)
        return 0
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    finally:
        mcp.shutdown()

def cmd_clear(args) -> int:
    mcp = MCPAdapter(workspace=args.ws)
    try:
        out = mcp.clear_library()
        jprint(out)
        return 0
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    finally:
        mcp.shutdown()

def cmd_schema(args) -> int:
    mcp = MCPAdapter(workspace=args.ws)
    try:
        tools = mcp.as_llm_tools()
        jprint(tools)
        return 0
    finally:
        mcp.shutdown()

def cmd_repl(args) -> int:
    """
    REPL de prueba minimal (no es GUI; solo útil para debug).
    Comandos:
      tools
      playlists
      add "<title>" [-a "<artists>"]
      confirm <idx> (usa el último title/artists usados en esta sesión)
      show "<playlist>"
      export "<playlist>"
      clear
      quit/exit
    """
    mcp = MCPAdapter(workspace=args.ws)
    print(f"[repl] workspace={args.ws}")
    last_title, last_artists = None, None
    try:
        while True:
            try:
                raw = input("host> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not raw:
                continue
            if raw.lower() in ("quit","exit"):
                return 0
            if raw == "tools":
                jprint(mcp.get_tools_schema()); continue
            if raw == "playlists":
                jprint(mcp.list_playlists()); continue
            if raw.startswith("add "):
                parts = shlex.split(raw)
                title = None; artists = ""
                i = 1
                while i < len(parts):
                    if parts[i] == "-a" and (i+1) < len(parts):
                        artists = parts[i+1]; i += 2; continue
                    if title is None:
                        title = parts[i]
                    else:
                        title += " " + parts[i]
                    i += 1
                title = title or ""
                try:
                    ok = mcp.add_song(title, artists)
                    jprint({"status":"ok","chosen": ok.chosen})
                    last_title, last_artists = title, artists
                except MCPNeedsConfirmation as cf:
                    jprint({"status":"needs_confirmation","message":cf.message,"candidates":cf.candidates})
                    last_title, last_artists = title, artists
                continue
            if raw.startswith("confirm "):
                if last_title is None:
                    print("No hay add previo en esta sesión.")
                    continue
                try:
                    idx = int(raw.split()[1])
                except Exception:
                    print("Uso: confirm <idx>"); continue
                ok = mcp.add_song(last_title, last_artists or "", candidate_index=idx)
                jprint({"status":"ok","chosen": ok.chosen})
                continue
            if raw.startswith("show "):
                name = raw[5:].strip().strip('"')
                jprint(mcp.get_playlist(name)); continue
            if raw.startswith("export "):
                name = raw[7:].trip().strip('"')  # (typo a propósito del usuario? corregimos abajo)
                jprint(mcp.export_playlist(name)); continue
            if raw == "clear":
                jprint(mcp.clear_library()); continue
            print("Comando no reconocido. Usa: tools | playlists | add | confirm | show | export | clear | exit")
    finally:
        mcp.shutdown()

# ------------------------------------------------------------------------------
# NUEVO: subcomando `chat` (CLI con LLM + contexto + tool-use)
# ------------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "Eres Setlist Architect Host (CLI). Tienes herramientas MCP para gestionar música: "
    "add_song, list_playlists, get_playlist, export_playlist, clear_library. "
    "Usa las tools cuando ayuden a cumplir la petición del usuario. "
    "Sé conciso y, cuando corresponda, muestra datos útiles."
    "Eres un asistente CLI con herramientas MCP dinámicas. "
    "Usa las tools disponibles cuando ayuden a cumplir la petición del usuario. Sé conciso."
)

CHAT_HELP = """Comandos dentro de chat:
  /help                   Muestra esta ayuda
  /quit                   Salir
  /tools                  Lista herramientas disponibles
  /call <tool> <json>     Invoca una tool con argumentos JSON
                          ej: /call add_song {"title":"Man in the Mirror","artists":"Michael Jackson"}
"""

def _call_tools_cli(mcp: MCPAdapter, uses: List[Dict[str, Any]], chat_log: str) -> List[Dict[str, Any]]:
    """Ejecuta tools y devuelve bloques tool_result con content=[{text}]."""
    results_blocks: List[Dict[str, Any]] = []
    for u in uses:
        name = u["name"]; args = u["arguments"]; tu_id = u["id"]
        try:
            result_obj = mcp.call_tool(name, args or {})
            payload = json.dumps(result_obj, ensure_ascii=False)
            results_blocks.append({
                "type": "tool_result",
                "tool_use_id": tu_id,
                "content": [{"type": "text", "text": payload}]
            })
            jsonl_log(chat_log, {"t":"tool_result","ts":_ts(),"tool":name,"args":args,"result":result_obj})
        except MCPNeedsConfirmation as cf:
            payload = json.dumps({"status":"needs_confirmation","candidates":cf.candidates,"message":cf.message}, ensure_ascii=False)
            results_blocks.append({
                "type":"tool_result",
                "tool_use_id":tu_id,
                "content":[{"type":"text","text": payload}]
            })
            jsonl_log(chat_log, {"t":"tool_result","ts":_ts(),"tool":name,"args":args,"result":{"status":"needs_confirmation"}})
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            results_blocks.append({
                "type":"tool_result",
                "tool_use_id":tu_id,
                "content":[{"type":"text","text": json.dumps({"error": err}, ensure_ascii=False)}]
            })
            jsonl_log(chat_log, {"t":"tool_error","ts":_ts(),"tool":name,"args":args,"error":err})
    return results_blocks

def cmd_chat(args) -> int:
    """
    Chat con LLM Anthropic + tool-use MCP (hasta 2 rondas de tools).
    Usa .env: ANTHROPIC_API_KEY y ANTHROPIC_MODEL.
    """
    load_dotenv()
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: Falta ANTHROPIC_API_KEY en .env", file=sys.stderr)
        return 2
    model = os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-20241022")
    client = Anthropic(api_key=api_key)

    mcp = MCPAdapter(workspace=args.ws)
    history = deque(maxlen=16)
    chat_log = f"logs/cli-{int(time.time())}.chat.jsonl"

    # Tools schema (puede ser dict o lista)
    schema = {"tools": mcp.as_llm_tools()}
    tools = schema["tools"]


    print(f"[chat] modelo={model} | workspace={args.ws}")
    print("Escribe /help para ver comandos.\n")

    try:
        while True:
            try:
                text = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not text:
                continue

            # Comandos
            if text in ("/q","/quit","exit"):
                return 0
            if text in ("/h","/help"):
                print(CHAT_HELP); continue
            if text == "/tools":
                names = [t.get("name") for t in tools if isinstance(t, dict)]
                print("Tools:", ", ".join(names) if names else "(ninguna)"); continue
            if text.startswith("/call "):
                try:
                    _, rest = text.split(" ", 1)
                    tool_name, json_str = rest.strip().split(" ", 1)
                    args_json = json.loads(json_str)
                except ValueError:
                    print("Uso: /call <tool> <json_args>"); continue
                except json.JSONDecodeError as je:
                    print(f"JSON inválido: {je}"); continue
                try:
                    result = mcp.call_tool(tool_name, args_json or {})
                    print(f"[{tool_name}] →", json.dumps(result, ensure_ascii=False))
                    jsonl_log(chat_log, {"t":"manual_tool_call","ts":_ts(),"tool":tool_name,"args":args_json,"result":result})
                except Exception as e:
                    print(f"Error en tool {tool_name}: {e}")
                continue

            # Conversación con LLM
            history.append({"role":"user","content": text})
            jsonl_log(chat_log, {"t":"user","ts":_ts(),"content":text})

            try:
                # Ronda 1
                msg1 = client.messages.create(
                    model=model,
                    system=SYSTEM_PROMPT,
                    tools=tools,
                    max_tokens=1024,
                    messages=list(history),
                )
                uses = _extract_tool_uses(msg1.content)
                if not uses:
                    reply = _blocks_to_text(msg1.content)
                    print(reply or "(sin texto)")
                    history.append({"role":"assistant","content": _normalize_blocks(msg1.content)})
                    jsonl_log(chat_log, {"t":"assistant","ts":_ts(),"content":reply})
                    continue

                # Ejecutar tools y seguir
                tool_results = _call_tools_cli(mcp, uses, chat_log)
                msg2 = client.messages.create(
                    model=model,
                    system=SYSTEM_PROMPT,
                    tools=tools,
                    max_tokens=1024,
                    messages=list(history)
                        + [{"role":"assistant","content": msg1.content}]
                        + [{"role":"user","content": tool_results}],
                )
                uses2 = _extract_tool_uses(msg2.content)
                if uses2:
                    tool_results2 = _call_tools_cli(mcp, uses2, chat_log)
                    msg3 = client.messages.create(
                        model=model,
                        system=SYSTEM_PROMPT,
                        tools=tools,
                        max_tokens=1024,
                        messages=list(history)
                            + [{"role":"assistant","content": msg1.content}]
                            + [{"role":"user","content": tool_results}]
                            + [{"role":"assistant","content": msg2.content}]
                            + [{"role":"user","content": tool_results2}],
                    )
                    reply3 = _blocks_to_text(msg3.content)
                    print(reply3 or "(sin texto)")
                    history.append({"role":"assistant","content": _normalize_blocks(msg3.content)})
                    jsonl_log(chat_log, {"t":"assistant","ts":_ts(),"content":reply3})
                    continue

                reply2 = _blocks_to_text(msg2.content)
                print(reply2 or "(sin texto)")
                history.append({"role":"assistant","content": _normalize_blocks(msg2.content)})
                jsonl_log(chat_log, {"t":"assistant","ts":_ts(),"content":reply2})

            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                print("[LLM ERROR]", err)
                print(traceback.format_exc())
                jsonl_log(chat_log, {"t":"llm_error","ts":_ts(),"error":err})
                # no añadimos assistant en error, se mantiene el history hasta último user

    finally:
        mcp.shutdown()

# ------------------------------------------------------------------------------
# Argumentos y entrypoint
# ------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="host", description="Host/Chatbot MCP (console entrypoint)")
    p.add_argument("--ws","--workspace", dest="ws", default=DEFAULT_WORKSPACE, help="Workspace/session id (default: %(default)s)")

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tools", help="Listar tools (schema)").set_defaults(func=cmd_tools)
    sub.add_parser("playlists", help="Listar playlists").set_defaults(func=cmd_playlists)

    ap = sub.add_parser("add", help="Añadir canción (y manejar confirmaciones)")
    ap.add_argument("--title", required=True)
    ap.add_argument("--artists", default="")
    ap.add_argument("--confirm", type=int, help="Confirma un candidato por índice (0..n)")
    ap.set_defaults(func=cmd_add)

    cp = sub.add_parser("confirm", help="Confirmar una búsqueda previa usando índice")
    cp.add_argument("--title", required=True)
    cp.add_argument("--artists", default="")
    cp.add_argument("--index", required=True, type=int)
    cp.set_defaults(func=cmd_confirm)

    sp = sub.add_parser("show", help="Ver una playlist")
    sp.add_argument("--name", required=True)
    sp.set_defaults(func=cmd_show)

    ep = sub.add_parser("export", help="Exportar una playlist a XLSX (retorna file:// URI)")
    ep.add_argument("--name", required=True)
    ep.set_defaults(func=cmd_export)

    sub.add_parser("clear", help="Vaciar la librería (mantiene nombres de playlists)").set_defaults(func=cmd_clear)
    sub.add_parser("schema", help="Schema para registrar tools en un LLM").set_defaults(func=cmd_schema)
    sub.add_parser("repl", help="REPL de prueba (interactivo)").set_defaults(func=cmd_repl)

    # nuevo: chat
    sub.add_parser("chat", help="Chat con LLM + contexto + tool-use").set_defaults(func=cmd_chat)

    return p

def main(argv: Optional[List[str]] = None) -> int:
    apply_windows_utf8_console()
    print_startup_banner()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130

if __name__ == "__main__":
    sys.exit(main())
