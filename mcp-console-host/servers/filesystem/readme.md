# Ejecución de filesystemMCP

## Pasos para ejecutar

```bash
# Activar el entorno desde PowerShell con el perfil filesystem.env
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\use_env.ps1 filesystem.env

# activacion del entorno
.\.venv\Scripts\Activate.ps1

# correr el chat 
python -m host.main chat

# comandos para tools y funciones clave: 

/tools

/call list_allowed_directories {}

/call write_file {"path":"C:/Users/jjcam/Desktop/Semestre_8/Redes/proyecto1redes_Final/mcp-console-host/servers/filesystem/root/README.md","content":"Hola MCP estamos en la prueba de clase"}

/call read_text_file {"path":"C:/Users/jjcam/Desktop/Semestre_8/Redes/proyecto1redes_Final/mcp-console-host/servers/filesystem/root/README.md"}

/call list_directory {"path":"C:/Users/jjcam/Desktop/Semestre_8/Redes/proyecto1redes_Final/mcp-console-host/servers/filesystem/root"}

/call edit_file {"path":"C:/Users/jjcam/Desktop/Semestre_8/Redes/proyecto1redes_Final/mcp-console-host/servers/filesystem/root/README.md","edits":[{"oldText":"Hola MCP estamos en la prueba de clase","newText":"Hola MCP EDITADO"}]}

/call move_file {"source":"C:/Users/jjcam/Desktop/Semestre_8/Redes/proyecto1redes_Final/mcp-console-host/servers/filesystem/root/README.md","destination":"C:/Users/jjcam/Desktop/Semestre_8/Redes/proyecto1redes_Final/mcp-console-host/servers/filesystem/root/docs/README.txt"}

/call get_file_info {"path":"C:/Users/jjcam/Desktop/Semestre_8/Redes/proyecto1redes_Final/mcp-console-host/servers/filesystem/root/docs/README.txt"}
```



## Tools para el gui ayuda

```bash
tú: Lista los archivos de la raíz permitida

tú: Crea README.md con 'Hola mundo' en la carpeta docs

tú: muéstrame su contenido de la carpeta docs

tú: que mas puedes hacer?

tú: mueve el archivo README.md que has creado de la carpeta docs a la carpeta root

tú: Crear directorios dentro de docs que se llame docspt2

```