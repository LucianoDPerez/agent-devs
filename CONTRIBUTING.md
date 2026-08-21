# Contribuir a AgentDevs

¡Gracias por el interés en contribuir! Este documento te deja andando en minutos.

## Requisitos

- Python 3.10+
- git
- Opcional (para probar el agente de verdad): [llama.cpp](https://github.com/ggml-org/llama.cpp) con un modelo sirviendo en `http://localhost:8080`

## Setup de desarrollo

```bash
git clone https://github.com/LucianoDPerez/agent-devs.git && cd agent-devs
./install.sh          # macOS/Linux   |   Windows: .\install.ps1
```

O manual si preferís:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

La instalación es **editable**: tu código queda conectado al comando `agent-devs`, sin reinstalar entre cambios.

## Correr los tests

```bash
.venv/bin/python -m pytest tests/
```

- La suite completa **no necesita LLM ni conexión**: son unitarias/integración livianas.
- `tests/test_e2e_orchestrator.py` es un **script manual** (necesita llama-server corriendo): pytest lo ignora por diseño; corrélo directo (`python tests/test_e2e_orchestrator.py`) solo cuando quieras el E2E real.

## Lint

```bash
.venv/bin/ruff check .
```

Config en `pyproject.toml` (línea 120, select E/F/W/I/UP/B/SIM).

## Estilo de commits

[Conventional Commits](https://www.conventionalcommits.org/es-es/), mensajes en español (coherente con el historial):

```text
feat(execute): soporte de tareas bulk con batches persistentes
fix(tui): ESC cancela el turno sin traceback
docs(readme): ...
test(router): ...
chore(deps): ...
```

## Flujo de trabajo

1. **Issue primero**: buscá si ya existe; si no, crealo con los templates (bug / feature).
2. **Branch desde `main`**: `feat/mi-feature` o `fix/mi-bug`.
3. **PR** vinculando el issue, completando el template.
4. Antes de pushear: `pytest` verde + `ruff check` sin errores.

Si el cambio toca comportamiento visible (flags, formato de salida, requisitos), actualizá `README.md` o `docs/ARCHITECTURE.md` en el mismo PR.

## Estructura del proyecto

Mapa completo de capas, roles, tools y decisiones de diseño: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

En corto: `main.py` (CLI/TUI) → `orchestration/` (router, sesión, presupuestos) → `tools/` (filesystem, git, verify) → `core/` (roles, intents) + `display/` (consola y TUI Textual).

## Reportar vulnerabilidades

**No abras un issue público.** Seguí [SECURITY.md](SECURITY.md).
