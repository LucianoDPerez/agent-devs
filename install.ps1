# AgentDevs installer — Windows (PowerShell)
#
# Dos modos:
#   1) Dentro del repo (clonado):   .\install.ps1
#   2) One-liner remoto:
#      irm https://raw.githubusercontent.com/LucianoDPerez/agent-devs/main/install.ps1 | iex
#      → clona el repo en %USERPROFILE%\.agent-devs y sigue igual.
#
# En ambos casos: venv + paquete editable + comando global `agent-devs`
# + doctor final.
$ErrorActionPreference = "Stop"

# Emojis/UTF-8 en consolas Windows (PS 5.1 default es otra codificación)
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$repoUrl = if ($env:AGENTDEVS_REPO_URL) { $env:AGENTDEVS_REPO_URL } else { "https://github.com/LucianoDPerez/agent-devs.git" }

if ((Test-Path "./pyproject.toml") -and (Select-String -Path ./pyproject.toml -Pattern 'name = "agent-devs"' -Quiet)) {
    Write-Host "📁 Instalando desde checkout existente: $(Get-Location)"
} else {
    $installDir = if ($env:AGENTDEVS_HOME) { $env:AGENTDEVS_HOME } else { Join-Path $env:USERPROFILE ".agent-devs" }
    if (Test-Path (Join-Path $installDir ".git")) {
        Write-Host "📁 Actualizando checkout existente en $installDir…"
        git -C $installDir pull --ff-only -q
    } else {
        Write-Host "📁 Clonando AgentDevs en $installDir…"
        git clone --depth 1 $repoUrl $installDir
    }
    Set-Location $installDir
}

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
# Activar el venv tolerando Execution Policy restrictiva (default en PS5.1)
try {
    .\.venv\Scripts\Activate.ps1
} catch {
    & .\.venv\Scripts\Activate.ps1 -ExecutionPolicy Bypass
}
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
call "$repoDir\.venv\Scripts\agent-devs.exe" %*
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
