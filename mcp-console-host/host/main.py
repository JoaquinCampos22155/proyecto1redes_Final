# host/main.py
from __future__ import annotations
import argparse, json, sys, shlex
from typing import Any, Dict, Optional, List

from host.settings import (
    DEFAULT_WORKSPACE,
    apply_windows_utf8_console,
    print_startup_banner,
)
from host.mcp_adapter import MCPAdapter, MCPNeedsConfirmation, MCPServerError, MCPAdapterError

def jprint(obj: Any) -> None:
    try:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    except Exception:
        print(str(obj))

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
            # confirmar usando índice directo
            ok = mcp.add_song(args.title, args.artists or "", candidate_index=int(args.confirm))
            jprint({"status":"ok", "chosen": ok.chosen})
            return 0

        try:
            ok = mcp.add_song(args.title, args.artists or "")
            jprint({"status":"ok", "chosen": ok.chosen})
            return 0
        except MCPNeedsConfirmation as cf:
            # Mostrar candidatos y sugerir confirmación
            payload = {
                "status":"needs_confirmation",
                "message": cf.message,
                "candidates": cf.candidates,  # list[dict]
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
    # atajo explícito: confirmación separada
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
        jprint(out)  # { uri: file://..., rows: N }
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
    # útil si tu LLM necesita registrar tools: imprime el JSON Schema “en vivo”
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
                # parse muy simple
                # admite: add "titulo" -a "artistas"
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
                name = raw[7:].strip().strip('"')
                jprint(mcp.export_playlist(name)); continue
            if raw == "clear":
                jprint(mcp.clear_library()); continue
            print("Comando no reconocido. Usa: tools | playlists | add | confirm | show | export | clear | exit")
    finally:
        mcp.shutdown()

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
