# host/tool_schemas.py
# Definición de tools (JSON Schema) que verá el LLM.
# Deben coincidir con los nombres expuestos por tu servidor MCP:
# add_song, list_playlists, get_playlist, export_playlist, clear_library

TOOLS = [
    {
        "name": "add_song",
        "description": "Analiza una canción (MP3/WAV/FLAC/M4A/OGG) y devuelve BPM/clave/modo/energía/brillo y las playlists sugeridas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Ruta absoluta del archivo de audio"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_playlists",
        "description": "Lista todas las playlists y el número de pistas en cada una.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_playlist",
        "description": "Devuelve todas las pistas pertenecientes a una playlist.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Nombre exacto de la playlist"}},
            "required": ["name"],
        },
    },
    {
        "name": "export_playlist",
        "description": "Exporta una playlist a CSV.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Nombre de la playlist"},
                "csv_path": {"type": "string", "description": "Ruta absoluta de salida .csv"},
            },
            "required": ["name", "csv_path"],
        },
    },
    {
        "name": "clear_library",
        "description": "Limpia el estado interno (pistas y playlists).",
        "input_schema": {"type": "object", "properties": {}},
    },
]
