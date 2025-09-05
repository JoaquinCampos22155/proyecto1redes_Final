# host/tool_schemas.py
# Definición de tools (JSON Schema) que verá el LLM.
# Deben coincidir con el servidor MCP:
# add_song, list_playlists, get_playlist, export_playlist, clear_library

TOOLS = [
    {
        "name": "add_song",
        "description": (
            "Analiza una canción (MP3/WAV/FLAC/M4A/OGG) y devuelve BPM, clave, modo, "
            "energía y brillo; además, sugiere una o más playlists según reglas por BPM/energía."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Ruta absoluta del archivo de audio (ej: C:\\\\music\\\\tema.mp3)"
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_playlists",
        "description": "Lista todas las playlists calculadas y el número de pistas en cada una.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_playlist",
        "description": "Devuelve todas las pistas pertenecientes a una playlist.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Nombre exacto de la playlist (ej: 'Pop 100–130')."
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "export_playlist",
        "description": "Exporta una playlist a CSV en la ruta indicada.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Nombre de la playlist (ej: 'House 115–130')."
                },
                "csv_path": {
                    "type": "string",
                    "description": "Ruta absoluta del archivo .csv de salida."
                },
            },
            "required": ["name", "csv_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "clear_library",
        "description": "Limpia el estado interno (todas las pistas y playlists calculadas).",
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]
