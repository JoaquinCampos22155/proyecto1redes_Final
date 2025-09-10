param(
  [Parameter(Mandatory=$true)]
  [ValidateNotNullOrEmpty()]
  [string]$Name
)

# Ruta a la raíz del repo, independientemente de desde dónde ejecutes
$root = (Resolve-Path "$PSScriptRoot\..").Path
$src  = Join-Path $root ("profiles\" + $Name)
$dst  = Join-Path $root ".env"

if (!(Test-Path $src)) {
  Write-Host "No existe $src" -ForegroundColor Red
  Write-Host "Contenido de 'profiles':" -ForegroundColor Yellow
  Get-ChildItem -Name (Join-Path $root "profiles") | ForEach-Object { " - $_" }
  exit 1
}

Copy-Item -LiteralPath $src -Destination $dst -Force
Write-Host "Activado perfil: $Name → $dst" -ForegroundColor Green
