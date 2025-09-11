# Ejecución de GithubMCP

## Pasos para ejecutar

```bash
# Activar el entorno desde PowerShell con el perfil github.env
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\use_env.ps1 github.env

# activacion del entorno
.\.venv\Scripts\Activate.ps1

# correr el chat 
python -m host.main chat

python -m host.gui_app
# comandos para tools y funciones clave: 

/call search_repositories {"query":"user:JoaquinCampos22155 proyecto1redes_Final"}

/call list_commits {"owner":"JoaquinCampos22155","repo":"proyecto1redes_Final","sha":"main","per_page":5}

/call create_branch {"owner":"JoaquinCampos22155","repo":"proyecto1redes_Final","branch":"mcp/cli-02","from_branch":"main"}

/call create_or_update_file {"owner":"JoaquinCampos22155","repo":"proyecto1redes_Final","path":"docs/mcp_prueba.md","content":"# Prueba \nEste fue creado por el MCP en la rama mcp/cli-01.\n","message":"chore(mcp): add docs/mcp_prueba.md","branch":"mcp/cli-01"}

```



## Tools para el gui ayuda

```bash
tú: busca el repositorio JoaquinCampos22155/proyecto1redes_Final 
tú: lista los últimos 2 commits de main en JoaquinCampos22155/proyecto1redes_Final
tú: crea una rama llamada mcp/gui-05 desde main en JoaquinCampos22155/proyecto1redes_Final
tú: en esa rama, crea docs/mcp_gui.md con: # Prueba GUI MCP Archivo creado desde el GUI usando el servidor MCP de GitHub. mensaje de commit: chore(mcp): add docs/mcp_gui.md


tú: en la rama mcp/gui-05, sube en un solo commit estos archivos: docs/notes/mcp-nota.txt con el texto: "nota de prueba"; y en docs/notes/github_mcp_smoke.txt con el texto: "ok" 

tú: lista el último commit de la rama mcp/gui-05 y dime qué archivos incluyó
```