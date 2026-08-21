#!/usr/bin/env bash
# AgentDevs installer — macOS / Linux
# Crea el venv, instala el paquete editable (+ deps) y deja el comando global
# `agent-devs` disponible desde cualquier carpeta. Al final corre el doctor.
set -euo pipefail
cd "$(dirname "$0")"

echo "🚀 AgentDevs installer (macOS/Linux)"
echo "===================================="

# ── Python 3.10+ ────────────────────────────────────────────────────────────
PY=${PYTHON:-python3}
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    echo "❌ Se requiere Python 3.10+ y '$PY' no lo es."
    echo "   Instalá una versión reciente (brew install python / apt install python3) o:"
    echo "   PYTHON=/ruta/a/python3 ./install.sh"
    exit 1
fi
echo "✅ Python OK ($($PY --version))"

# ── venv + paquete editable ────────────────────────────────────────────────
if [ ! -d .venv ]; then
    echo "🔧 Creando venv (.venv)…"
    "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
echo "🔧 Instalando AgentDevs editable + dependencias…"
pip install --upgrade pip -q
pip install -e .

deactivate

# ── comando global: shim en ~/.local/bin ──────────────────────────────────
BIN_DIR="$HOME/.local/bin"
REPO_DIR="$(pwd)"
mkdir -p "$BIN_DIR"
SHIM="$BIN_DIR/agent-devs"
printf '#!/bin/sh\nexec "%s/.venv/bin/agent-devs" "$@"\n' "$REPO_DIR" > "$SHIM"
chmod +x "$SHIM"
echo "✅ Comando global: $SHIM -> $REPO_DIR/.venv/bin/agent-devs"

case ":${PATH}:" in
    *":$BIN_DIR:"*) ;;
    *)
        RC="$HOME/.bashrc"
        [ "${SHELL##*/}" = "zsh" ] && RC="$HOME/.zshrc"
        {
            echo ""
            echo "# AgentDevs CLI (agregado por install.sh)"
            echo 'export PATH="$HOME/.local/bin:$PATH"'
        } >> "$RC"
        echo "ℹ️  Agregué $BIN_DIR al PATH en $RC — abrí otra terminal (o: source $RC)"
        ;;
esac

# ── doctor: verifica todo e instala faltantes ──────────────────────────────
echo ""
"$SHIM" --doctor || true

echo ""
echo "Listo. Desde cualquier repositorio:"
echo "    cd /tu/proyecto && agent-devs ."
