# AgentDevs installer — Windows (PowerShell)
# Crea el venv, instala el paquete editable (+ deps) y deja el comando global
# `agent-devs` disponible desde cualquier carpeta. Al final corre el doctor.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "🚀 AgentDevs installer (Windows)"
Write-Host "================================"

# ── Python 3.10+ ────────────────────────────────────────────────────────────
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Se requiere Python 3.10+ (winget install Python.Python.3.12)"
    exit 1
}
Write-Host "✅ Python OK"

# ── venv + paquete editable ────────────────────────────────────────────────
if (-not (Test-Path ".venv")) {
    Write-Host "🔧 Creando venv (.venv)…"
    python -m venv .venv
}
.\.venv\Scripts\Activate.ps1
Write-Host "🔧 Instalando AgentDevs editable + dependencias…"
pip install --upgrade pip -q
pip install -e .
deactivate

# ── comando global: shim cmd en %USERPROFILE%\bin + PATH de usuario ───────
$binDir = Join-Path $env:USERPROFILE "bin"
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
$repoDir = (Get-Location).Path
$shim = Join-Path $binDir "agent-devs.cmd"
@"
@echo off
"$repoDir\.venv\Scripts\agent-devs.exe" %*
"@ | Set-Content -Encoding ASCII $shim
Write-Host "✅ Comando global: $shim -> $repoDir\.venv\Scripts\agent-devs.exe"

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (($userPath -split ";") -notcontains $binDir) {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$binDir", "User")
    Write-Host "ℹ️  Agregué $binDir al PATH de usuario — abrí otra terminal para que aplique"
}

# ── doctor: verifica todo e instala faltantes ──────────────────────────────
Write-Host ""
& $shim --doctor
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host ""
Write-Host "Listo. Desde cualquier repositorio:"
Write-Host "    cd C:\tu\proyecto && agent-devs ."
