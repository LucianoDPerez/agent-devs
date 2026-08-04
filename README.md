# AgentDevs — Agente de Desarrollo (LLM Local)

Agente conversacional impulsado por tu LLM local (`llama-server` en `http://localhost:8080/v1`) para **analizar, planificar, ejecutar y revisar** repositorios de código.

El agente **clasifica automáticamente la intención del usuario** y delega al rol correspondiente, cargando solo las tools y el system prompt necesarios para cada tarea. **Recuerda la conversación** entre turnos, **persiste el historial en SQLite**, y **gestiona el contexto automáticamente** con summaries cuando se llena.

## Arquitectura (Clean Architecture + SOLID)

```
agent-lucho/
├── main.py                       # 🎯 Entry point delgado (~90 líneas)
├── config.py                     # Configuración centralizada
├── cache.py                      # Persistencia SQLite (análisis + historial de sesión)
├── llm_wrapper.py                # Wrapper de BaseChatModel (reasoning_content)
├── analyzer.py                   # Generación del análisis cacheado
│
├── core/                         # 🧠 Capa de dominio
│   ├── intents.py                #   Enum Intent (analyze/plan/execute/review/chat)
│   └── roles.py                  #   Enum Role + mapping Intent→Role + tools + prompts
│
├── orchestration/                # ⚙️ Capa de aplicación
│   ├── router.py                 #   Clasificador keyword-based de intención
│   ├── agent_builder.py          #   Factory: construye agente LangChain por rol
│   └── session.py                #   Sesión con memoria, persistencia, summary auto
│
├── display/                      # 🖥️ Adaptador de presentation
│   ├── console.py                #   Streaming con rich + paneles
│   └── tui.py                    #   Input con prompt_toolkit + status bar
│
├── prompts/                      # 📝 Prompts externos (editables sin tocar código)
│   ├── classifier.md             #   Prompt del clasificador (legacy)
│   ├── analyze.md                #   System prompt del rol Análisis
│   ├── plan.md                   #   System prompt del rol Planificación
│   ├── execute.md                #   System prompt del rol Ejecución
│   ├── review.md                 #   System prompt del rol Revisión
│   └── chat.md                   #   System prompt del rol Charla
│
├── tools/                        # 🔧 Capa de infraestructura (tools)
│   ├── __init__.py               #   Pool completo + subsets por rol
│   ├── filesystem.py             #   list/read/write/edit files
│   ├── git.py                    #   Git + GitHub PRs
│   ├── routes.py                 #   Detección de endpoints multi-lenguaje
│   ├── search.py                 #   Búsqueda regex en código
│   ├── mcp_client.py             #   Cliente MCP (codebase-memory-mcp)
│   └── _helpers.py               #   Helpers compartidos
│
├── tests/                        # 🧪 Pruebas
│   ├── test_filesystem.py        #   6 tests de filesystem tools
│   ├── test_git.py               #   7 tests de git tools
│   ├── test_routes.py            #   13 tests de detección de endpoints
│   ├── test_e2e_orchestrator.py  #   43 tests end-to-end del orquestador
│   └── TEST_REPORT.md            #   Informe de pruebas
│
├── pyproject.toml                # ruff + pytest config
├── requirements.txt              # Dependencias Python
└── README.md                     # Este archivo
```

### Principios aplicados

| Principio | Cómo se aplica |
|-----------|----------------|
| **SRP** | Cada archivo tiene UNA responsabilidad: `router.py` clasifica, `agent_builder.py` construye, `session.py` maneja estado + memoria |
| **OCP** | Agregar un nuevo rol = crear prompt en `prompts/` + entrada en `core/roles.py`. No toca código existente |
| **DIP** | `orchestration/` depende de abstracciones en `core/` (enums). `tools/` es infraestructura intercambiable |
| **YAGNI** | No hay clases abstractas hasta que haya 2+ implementaciones. Enums de Python sirven como contratos |
| **KISS** | Cada fase es un archivo que exporta `(prompt, tools)`. El clasificador es keyword-based (instantáneo) |
| **DRY** | La lógica de construir el agente, medir tokens, y manejar contexto está en un solo lugar |

## Flujo del orquestador

```
main.py (thin)
  └─ Session.start()
       ├─ init_mcp()                          ← conecta knowledge graph
       └─ build_agent(role=ANALYZE)           ← rol inicial
            ├─ load_prompt(role)              ← lee prompts/{role}.md
            ├─ tools_for_role(role)           ← subset de tools
            └─ create_agent(llm, tools, prompt) ← LangChain

  └─ Session.run_turn(user_input)            ← por cada mensaje
       ├─ classify_intent(msg)                ← keyword-based, instantáneo
       ├─ role = role_for_intent(intent)       ← mapping Intent→Role
       ├─ if role changed →
       │    build_agent(role)                  ← reconstruye agente
       ├─ messages.append(HumanMessage)        ← acumula en historial
       ├─ stream_agent_turn(agent, messages)   ← pasa historial completo
       ├─ messages.append(AIMessage)           ← acumula respuesta
       ├─ save_turn(SQLite)                    ← persiste turno completo
       ├─ check_context() →
       │    if 85% → warning
       │    if 90% → LLM summary + comprimir historial
       └─ print_turn_summary()                 ← métricas
```

## Roles y subsets de tools

| Rol | Tools | Capacidad |
|-----|-------|-----------|
| **🔍 Análisis** (analyzer) | 10 | Read-only: explorar, preguntar, entender. No codea. |
| **📋 Planificación** (planner) | 11 | Read-only + write_file: diseña solución en .md. No codea. |
| **🛠️ Ejecución** (executor) | 16 | All tools: codea, commitea, pushea, crea PRs. |
| **🔎 Revisión** (reviewer) | 10 | Read-only: revisa PRs, busca bugs, audita. No codea. |
| **💬 Charla** (chat) | 0 | Sin tools: conversación general. |

### Classifier keyword-based

El clasificador analiza el mensaje del usuario y lo mapea a una intención sin llamar al LLM (instantáneo, determinista):

- `analyze`: "analizá", "explorá", "cómo funciona", "describí", "listá"
- `plan`: "plan", "planificá", "diseñá", "desglosá", "tareas", "proponé"
- `execute`: "implementá", "escribí", "codeá", "creá un archivo", "commit", "editá"
- `review`: "revisá", "review", "buscá bugs", "auditá", "code review"
- `chat`: "hola", "gracias", "cómo estás", "chau"

> **Why keyword-based?** El LLM 4B piensa en inglés antes de responder, usando todos los tokens en razonamiento. Un clasificador keyword-based es instantáneo, determinista y 100% confiable para frases cortas en español/inglés.

## Memoria y gestión de contexto

### Memoria entre turnos

El agente **recuerda la conversación** dentro de una sesión. Cada turno acumula los mensajes (user + assistant) y los pasa completos al agente:

```
Turno 1: [user1] → [assistant1]
Turno 2: [user1, assistant1, user2] → [assistant2]
Turno 3: [user1, assistant1, user2, assistant2, user3] → [assistant3]
```

Si preguntás "¿qué me dijiste antes?", el agente lo recuerda porque tiene el historial en su contexto.

### Persistencia en SQLite

Cada turno completo se guarda en `~/.agent-cache/repo_lens.db` (tabla `session_history`):

```sql
CREATE TABLE session_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,       -- agrupa turnos de una misma sesión
    repo_path TEXT NOT NULL,        -- repo al que pertenece
    role TEXT,                      -- rol activo (analyzer/planner/executor/reviewer/chat)
    user_message TEXT,              -- lo que preguntaste
    assistant_message TEXT,         -- lo que respondió el agente
    tokens_used INTEGER,            -- tokens consumidos en el turno
    created_at TEXT                 -- timestamp
);
```

**¿Cuándo se guarda?** Después de cada turno completo (1 fila por turno). No se guardan steps intermedios (tool calls, reasoning) — solo el mensaje del usuario y la respuesta final.

### Gestión automática de contexto

El contexto tiene un límite (`-c 32000` en llama-server). El agente maneja esto automáticamente:

| % de contexto | Acción |
|---------------|--------|
| < 85% | Normal |
| 85% | ⚠️ Warning: "Contexto al 85% — usá /new para empezar sesión nueva" |
| 90% | 📦 LLM genera summary del historial viejo, comprime a ~200 palabras, preserva últimos 2 mensajes |

El summary incluye: tareas realizadas, archivos tocados, decisiones técnicas, y tickets de Jira mencionados.

### Comandos de sesión

| Comando | Acción |
|---------|--------|
| `/new` | Resetea el historial, genera nuevo `session_id`, mantiene el análisis cacheado del repo |
| `/history` | Muestra los últimos 10 turnos guardados en SQLite para ese repo |
| `exit` | Cierra la sesión |

## TUI — Interfaz interactiva

El agente usa `prompt_toolkit` + `rich` para una terminal experience profesional:

### Input

| Tecla | Acción |
|-------|--------|
| `Enter` | Enviar mensaje |
| `⌥+Enter` (Option+Enter) | Salto de línea (multilinea) |
| `←` `→` `↑` `↓` | Mover cursor / historial |
| `Home` / `End` | Inicio / fin de línea |
| `Ctrl+A` / `Ctrl+E` | Inicio / fin (Emacs) |
| `Backspace`/`Delete` | Editar texto |
| `Ctrl+C` | Cancelar turno del agente |
| Mouse wheel | Scroll de la conversación (scroll nativo de la terminal) |

### Status bar (bottom toolbar)

Mientras escribís, una barra inferior muestra en tiempo real:

```
🌿 main  ⚡ 12,450 tokens  🔍 Análisis  📁 venture-ueno-ads  Enter=envía · ⌥+Enter=salto · Ctrl+C=cancela · exit=sale · scroll=mouse wheel
```

- **🌿 branch**: rama git actual del repo
- **⚡ tokens**: tokens totales consumidos en la sesión
- **🔍 role**: rol activo del orquestador
- **📁 repo**: nombre corto del repositorio

### Output

- **Welcome panel** con info del LLM, repo, modelo y tools
- **💭 Razonando…** — el razonamiento del modelo en dim cyan (separado del response)
- **─** — separador entre razonamiento y respuesta
- **🔧 tool_name{args}** — tool calls con badge azul
- **Métricas panel** al final de cada turno (tiempo, tokens, cacheados)
- **Role switch indicator** al cambiar de rol
- **Warning de contexto** al 85% y summary automático al 90%

## Requisitos

| Componente | Requisito | Verificar |
|---|---|---|
| Python | 3.10+ (tested on 3.14) | `python3 --version` |
| git | Cualquier versión moderna | `git --version` |
| gh | GitHub CLI autenticado | `gh auth status` |
| llama-server | Compilado con soporte multimodal | `llama-server --help` |
| codebase-memory-mcp | 0.8+ | `codebase-memory-mcp --version` |

## Instalación completa recomendada

### 1. llama-server (LLM local)

```bash
# Opción A: Homebrew (macOS)
brew install llama.cpp
```

Levantá el servidor:

```bash
llama-server \
  -hf InternScience/Agents-A1-4B-Q4_K_M-GGUF:Q4_K_M \
  -c 32000 -ngl 99 --flash-attn on --kv-unified \
  --cache-type-k q5_0 --cache-type-v q5_0 \
  --threads 4 --threads-batch 4 \
  --batch-size 2048 --ubatch-size 1024 \
  --port 8080 --host 127.0.0.1 \
  --alias agents-a1-4b --temp 0.85 \
  --top-p 0.95 --top-k 20 --min-p 0.0 \
  --presence-penalty 1.1 --repeat-penalty 1.0 \
  --parallel 1 --jinja --cont-batching
```

### 2. git + GitHub CLI

```bash
brew install git gh
gh auth login
```

### 3. codebase-memory-mcp (knowledge graph)

```bash
curl -fsSL https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/install.sh | bash
```

### 4. Proyecto + dependencias

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest  # para correr tests
```

### Dependencias clave

| Paquete | Versión | Uso |
|---|---|---|
| `langchain` | >=1.0.0 | Framework principal (create_agent) |
| `langgraph` | >=1.0.0 | Grafo de agente reactivo |
| `langchain-openai` | >=1.0.0 | Conexión al LLM vía OpenAI-compatible |
| `langchain-mcp-adapters` | >=0.3.1 | Integración con MCP servers |
| `mcp` | >=1.24.0,<2.0 | SDK de Model Context Protocol |
| `prompt_toolkit` | >=3.0.0 | TUI: input con edición, status bar |
| `rich` | >=13.0.0 | Output formateado: paneles, colores, markdown |
| `gnureadline` | >=8.1.2 | GNU readline para macOS (legacy) |

## Script de arranque `agentdevs`

`agentdevs` es un launcher de bash que automatiza la instalación y ejecución del agente sobre cualquier repositorio. Vive en la raíz del proyecto (`agentdevs`) y es el binario que se instala en tu `PATH`.

**Qué hace al ejecutarlo:**

1. Resuelve el directorio del proyecto a ruta absoluta (antes de cualquier `cd`)
2. Crea el venv `.venv` si no existe
3. Instala las dependencias la primera vez (marcador `.venv/.deps_installed`)
4. Ejecuta `main.py <ruta_absoluta_del_repo>`

### Instalación

```bash
# Clonar el proyecto (si no lo tenés)
git clone <tu-repo> ~/agent-lucho

# Crear el symlink en tu PATH (macOS/Homebrew)
ln -s "$HOME/agent-lucho/agentdevs" /opt/homebrew/bin/agentdevs

# Asegurar permisos de ejecución
chmod +x ~/agent-lucho/agentdevs
```

### Uso

```bash
agentdevs .                  # analiza el directorio actual
agentdevs /ruta/al/repo      # analiza un repo específico
agentdevs                    # sin argumentos → directorio actual
```

Cada ejecución verifica el venv y las dependencias automáticamente — no hace falta setup manual.

## Uso

```bash
# Modo interactivo (clasifica y delega automáticamente)
python main.py "/ruta/al/repo"

# Generar y guardar el análisis del repo en caché
python main.py --analyze "/ruta/al/repo"

# Listar análisis guardados
python main.py --list
```

### Ejemplos por rol

El orquestador **detecta automáticamente** la intención y carga el rol adecuado:

```bash
>>> analiza este proyecto y decime qué arquitectura tiene      # → 🔍 Análisis
>>> hacé un plan de 3 pasos para agregar un health endpoint    # → 📋 Planificación
>>> implementá el endpoint /api/v2/health y comitealo          # → 🛠️ Ejecución
>>> revisá el PR #5 buscando bugs y code smells                # → 🔎 Revisión
>>> hola, cómo estás?                                           # → 💬 Charla
```

> El usuario no necesita saber qué rol existe. El orquestador lo deduce del mensaje.

### Comandos de sesión

```bash
>>> /new        # nueva sesión (resetea historial, mantiene cache de repo)
>>> /history    # muestra los últimos 10 turnos guardados en SQLite
>>> exit        # cierra la sesión
```

## Herramientas disponibles (30 tools)

### Filesystem (4)

| Tool | Descripción |
|---|---|
| `list_files(path, recursive)` | Lista archivos en un directorio |
| `read_file(path, start_line, end_line)` | Lee contenido de archivo (trunca > 50KB) |
| `write_file(path, content)` | Crea o sobrescribe un archivo |
| `edit_file(path, old_str, new_str)` | Reemplaza texto exacto en un archivo |

### Search (1)

| Tool | Descripción |
|---|---|
| `search_code(path, pattern)` | Búsqueda regex en archivos del repo |

### Routes (1)

| Tool | Descripción |
|---|---|
| `inspect_routes(path)` | Detecta endpoints multi-lenguaje (Next.js/Express/Fastify/NestJS, FastAPI/Flask/Django, Go, Rust, Java/Kotlin, PHP, C#, Ruby) |

### Git (10)

| Tool | Descripción |
|---|---|
| `current_branch(path)` | Rama actual del repo |
| `changed_files(path)` | Archivos modificados (staged/unstaged/untracked) |
| `git_status(path)` | Resumen: rama, ahead/behind, cambios |
| `git_log(path, limit)` | Historial de commits (oneline) |
| `stage_files(path, files)` | Stagea archivos antes de commitear |
| `create_commit(path, message)` | Commitea staged (conventional commits) |
| `push(path, remote, branch)` | Pusha la rama (setea upstream) |
| `create_pr(path, title, body, base)` | Pusha + abre PR con `gh` |
| `read_pr(path, number?)` | Lee PR (metadata + diff) para review |
| `list_prs(path, state)` | Lista PRs para review |

### Knowledge Graph (14, codebase-memory-mcp)

| Tool | Descripción |
|---|---|
| `cm__search_graph` | Buscar símbolos/clases/funciones por nombre o semántica |
| `cm__trace_path` | "¿Qué rompe si cambio X?" — callers/callees/imports |
| `cm__get_code_snippet` | Leer la implementación exacta de una función/clase |
| `cm__get_architecture` | Resumen estructural del repo |
| `cm__list_projects` | Listar repos indexados |
| `cm__index_repository` | Indexar/reindexar un repo en el graph |
| `cm__search_code` | Búsqueda augmentada por grafo |
| `cm__query_graph` | Queries Cypher avanzadas |
| ... | (6 más) |

## Tests

### Unit tests (26 tests)

```bash
python -m pytest tests/ -v --ignore=tests/test_e2e_orchestrator.py
```

Cubren: filesystem tools (6), git tools (7), route detection (13) — Next.js/Express/FastAPI/Go/Rust/Spring/Laravel/ASP.NET.

### E2E tests (43 tests)

```bash
python tests/test_e2e_orchestrator.py
```

Cubren: core domain (15), classifier (15), session flow con LLM real (13):

| Turno | Rol | Acción | Resultado |
|-------|-----|--------|-----------|
| 1 | 💬 chat | Saludo | Agente responde 226 tokens |
| 2 | 🔍 analyze | `cm__list_projects` | Lista 7 proyectos indexados |
| 3 | 📋 plan | Explora + plan 3 pasos | 1,158 tokens, identifica Spring Boot + WebFlux |
| 4 | 🔎 review | `read_pr` PR #343 | Encuentra 4 code smells (Dockerfile, nested vars, regex, CODEOWNERS) |
| 5 | 🛠️ execute | `write_file` | Crea `/tmp/test_e2e.md` con contenido correcto |

> Última ejecución: **43/43 (100%)** — ver `tests/TEST_REPORT.md`

## Compatibilidad

- Python 3.10+ (tested on 3.14)
- `langchain-mcp-adapters 0.3.1` requiere `mcp>=1.24.0,<2.0`
- `create_agent` de `langchain.agents` (v1.x)
- `bind_tools` usa `model_copy(deep=False)` para evitar pickle error con AsyncOpenAI locks
- `prompt_toolkit` con `mouse_support=False` para no interferir con scroll nativo de la terminal

## Notas de rendimiento

- El modelo local (Agents-A1-4B) **piensa en voz alta** antes de cada acción — esa salida se muestra en vivo (streaming)
- El análisis inicial puede tardar **1-2 minutos** (el modelo razona mucho)
- El clasificador keyword-based es **instantáneo** (no gasta tokens del LLM)
- La memoria entre turnos **acumula tokens** — el contexto crece con cada mensaje
- Al 90% de contexto, el agente **genera un summary automáticamente** (~2-3s extra)
- `/new` resetea el historial instantáneamente sin perder el análisis cacheado del repo
- `max_tokens=2048` (chat) / `1024` (análisis) son los límites estables
- Cada turno muestra tiempo y tokens (in/out/cacheados) y el acumulado de sesión
