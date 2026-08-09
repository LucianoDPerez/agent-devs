# AgentDevs — Agente de Desarrollo (LLM Local)

Agente conversacional impulsado por tu LLM local (`llama-server` en `http://localhost:8080/v1`) para **analizar, planificar, ejecutar y revisar** repositorios de código.

El agente **clasifica automáticamente la intención del usuario** y delega al rol correspondiente, cargando solo las tools y el system prompt necesarios para cada tarea. **Recuerda la conversación** entre turnos, **persiste el historial en SQLite**, y **gestiona el contexto automáticamente** con summaries cuando se llena.

## Arquitectura (Clean Architecture + SOLID)

```
agent-lucho/
├── main.py                       # 🎯 Entry point delgado (~90 líneas)
├── config.py                     # Configuración centralizada (recursion, tokens, judge, budgets)
├── cache.py                      # Persistencia SQLite (análisis + historial de sesión)
├── llm_wrapper.py                # Wrapper de BaseChatModel (reasoning_content)
├── analyzer.py                   # Generación del análisis cacheado
├── business_logic.py             # 📋 Reglas de negocio deterministas (campos requeridos, validaciones)
│
├── core/                         # 🧠 Capa de dominio
│   ├── intents.py                #   Enum Intent (analyze/plan/execute/review/chat)
│   └── roles.py                  #   Enum Role + mapping Intent→Role + tools + prompts
│
├── orchestration/                # ⚙️ Capa de aplicación
│   ├── router.py                 #   Clasificador keyword-based de intención
│   ├── agent_builder.py          #   Factory: construye agente LangChain por rol
│   ├── session.py                #   Sesión con memoria, persistencia, summary auto, judge
│   ├── execute_bootstrap.py      #   Preload tasks, hints, repo layout, review→correction
│   └── tool_dedupe.py            #   Explore budget, ToolBudgetExceeded, dedupe, write enforcement
│
├── display/                      # 🖥️ Adaptador de presentation
│   ├── console.py                #   Streaming con rich + paneles
│   └── tui.py                    #   Input con prompt_toolkit + status bar
│
├── prompts/                      # 📝 Prompts externos (editables sin tocar código)
│   ├── classifier.md             #   Prompt del clasificador (legacy)
│   ├── analyze.md                #   System prompt del rol Análisis
│   ├── plan.md                   #   System prompt del rol Planificación
│   ├── execute.md                #   System prompt del rol Ejecución (regla #1, flujo)
│   ├── review.md                 #   System prompt del rol Revisión (checklist exhaustivo)
│   ├── judge.md                  #   System prompt del Judge LLM (valida reviews)
│   └── chat.md                   #   System prompt del rol Charla
│
├── tools/                        # 🔧 Capa de infraestructura (tools)
│   ├── __init__.py               #   Pool completo (21 tools) + subsets por rol
│   ├── filesystem.py             #   list/read/write/edit/delete files
│   ├── git.py                    #   Git + GitHub PRs
│   ├── routes.py                 #   Detección de endpoints multi-lenguaje
│   ├── search.py                 #   Búsqueda regex en código
│   ├── verify.py                 #   run_install / run_lint / run_tests / run_build
│   ├── mcp_client.py             #   Cliente MCP (codebase-memory-mcp)
│   ├── graph_trace.py            #   Tool compuesta: traza un componente en UNA llamada
│   └── _helpers.py               #   Helpers compartidos
│
├── tests/                        # 🧪 Pruebas (147 unit + 43 e2e)
│   ├── test_cache_snapshot.py    #   snapshot cache invalidation
│   ├── test_execute_bootstrap.py #   task preload, hints, review correction, minimal plan
│   ├── test_filesystem.py        #   filesystem tools
│   ├── test_git.py               #   git tools
│   ├── test_llm_tool_recovery.py #   parse text tool calls
│   ├── test_router_dedupe.py     #   intent classifier, dedupe, explore budget, exceptions
│   ├── test_routes.py            #   endpoint detection (13 frameworks)
│   ├── test_verify.py            #   stack detection, lint/test/build resolution
│   ├── test_budget.py            #   explore budget EXECUTE/REVIEW + ANALYZE/PLAN (write_pressure)
│   ├── test_business_logic.py    #   descubrimiento determinista de reglas de negocio (11)
│   ├── harness_multi_role.py     #   18 casos (5 roles) con persistencia incremental JSON
│   ├── harness_full_cycle.py     #   ciclo completo en un solo repo
│   ├── test_e2e_orchestrator.py  #   43 tests end-to-end del orquestador
│   └── TEST_REPORT_2026.md       #   Informe de pruebas multi-rol (comparación de LLMs)
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
| **🛠️ Ejecución** (executor) | 19 | All tools + lint/tests/build: codea, verifica, commitea, pushea, crea PRs. |
| **🔎 Revisión** (reviewer) | 14 | Read-only + lint/tests/build: revisa PRs, busca bugs, audita. Budget anti-loop. |
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

Levantá el servidor con el **presupuesto de razonamiento limitado** (⚠️ **requerido** para el modelo Agents-A1-4B con template `--jinja`):

```bash
llama-server \
  -hf cahlen/qwen3.5-35b-a3b-compacted-GGUF:IQ3_XXS \
  -c 35536 -ngl 99 --flash-attn on --kv-unified \
  --cache-type-k q5_0 --cache-type-v q5_0 \
  --threads 4 --threads-batch 4 \
  --batch-size 2048 --ubatch-size 1024 \
  --port 8080 --host 127.0.0.1 \
  --alias agents-a1-4b --temp 0.2 \
  --top-p 0.95 --top-k 20 --min-p 0.0 \
  --presence-penalty 1.1 --repeat-penalty 1.05 \
  --parallel 1 --jinja --cont-batching
```

> **Modelo recomendado:** `cahlen/qwen3.5-35b-a3b-compacted-GGUF` (35B A3B activo). En las
> pruebas comparativas resuelve los 18 casos multi-rol (0 fallos) mientras que el 4B dejó
> 8/18 vacíos — ver `tests/TEST_REPORT_2026.md`.
>
> **¿Por qué `--reasoning-budget 256` (solo 4B)?** El modelo Agents-A1-4B usa un template
> `--jinja` con bloque de reasoning. Sin límite (`--reasoning-budget` no especificado,
> default `-1` = unlimited), el modelo puede gastar **todos** sus tokens de salida en
> `reasoning_content` y nunca producir `content` ni `tool_calls`, especialmente con prompts
> complejos. `--reasoning-budget 256` corta el pensamiento a 256 tokens; el
> `--reasoning-budget-message` fuerza el modelo a emitir `content`/`tool_calls` inmediatamente
> después. Con el 35B no hace falta (razona y responde sin quemar el budget).
>
> **Verificá que el flag esté activo:** `ps aux | grep llama-server | grep reasoning-budget`

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

## Lógica de negocio (inyección de reglas concretas)

`business_logic.py` extrae reglas de negocio **sin usar el LLM** (determinista) y las inyecta
en el system prompt de **todos** los roles via `cached_analysis`:

1. **Descubrimiento determinista:** escanea carpetas de dominio (`domain/entities/models/dto/...`)
   y extrae interfaces/types con campos **REQUERIDOS vs opcionales** + mensajes de validación
   ("X es requerido", "formato inválido"). Deduplica eligiendo la versión **más restrictiva**
   (ej: el frontend exige `documento`, el backend lo permite null → gana el requerido).
2. **Graph MCP (enriquecimiento):** entidades y endpoints HTTP desde el knowledge graph.
3. **Persistencia:** tabla `business_rules` en `~/.agent-cache/repo_lens.db`, invalidada por
   `snapshot_hash`.

**Motivación:** el LLM ignora reglas concretas (campos requeridos, validaciones) cuando solo
ve la estructura general del repo. Con la regla inyectada (ej: "`CreatePacienteInput` exige
`nombre` y `documento` requeridos"), el agente detecta el bug del botón habilitado sin
`documento`. Tests: `tests/test_business_logic.py` (11).

## Retry ANALYZE/PLAN sin tools de búsqueda

Los roles ANALYZE y PLAN usan un **budget de exploración propio** (`write_pressure=False` en
`tool_dedupe.py`): capa la búsqueda MCP (`cm__search_graph`, `cm__trace_path`, etc.) y la
lectura repetida, pero **NUNCA presiona a escribir** (el usuario los usa para analizar/
planificar, no para tocar el repo).

Cuando el presupuesto se agota, `session.py` dispara `_retry_analyze_no_explore`:

1. **`_system_trace_for(user_msg)`** — el SISTEMA (no el LLM) resuelve el término del usuario
   con la tool compuesta `trace_component` (resolve + source + usos en UNA llamada),
   garantizando un ancla con la cadena correcta.
2. **`build_agent(no_explore=True)`** — reconstruye el agente con **CERO tools**. El modelo
   responde su análisis/plan en texto plano usando el contexto que ya leyó (verificado:
   responde el diagnóstico correcto en ~4-8 min, sin loops).
3. **`max_reasoning_seconds=None`** en el retry — el modelo razona 3-6 min y responde; cortar
   por timeout lo mataba justo antes de la respuesta final.

## Tests

### Unit tests (147 tests)

```bash
python -m pytest tests/ -v --ignore=tests/test_e2e_orchestrator.py
```

Cubren: filesystem (11), git (7), route detection (13), dedupe + explore budget (13),
cache snapshot (3), execute bootstrap (28), llm recovery (4), routes (13), verify (10),
business logic (11), budget ANALYZE/PLAN (6) — frameworks: Next.js/Express/FastAPI/Go/Rust/Spring/Laravel/ASP.NET.

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

- El modelo local **piensa en voz alta** antes de cada acción — esa salida se muestra en vivo (streaming) como `💭 Razonando…`
- **Con `--reasoning-budget` en el server (4B)**: el thinking se corta, el resto va a content/tool_calls
- **Sin `--reasoning-budget`**: el modelo puede gastar todos los tokens en reasoning → el agente detecta esto, corta a los `MAX_REASONING_SECONDS` (300s) y reintenta con contexto recortado
- El análisis inicial puede tardar **1-2 minutos** (el modelo razona mucho)
- El clasificador keyword-based es **instantáneo** (no gasta tokens del LLM)
- La memoria entre turnos **acumula tokens** — el contexto crece con cada mensaje
- Al 90% de contexto, el agente **genera un summary automáticamente** (~2-3s extra)
- `/new` resetea el historial instantáneamente sin perder el análisis cacheado del repo
- `max_tokens` por rol: 4096 (ANALYZE/REVIEW/PLAN) / 2560 (EXECUTE) / 3584 (default). El
  4B quemaba el budget en razonamiento; 4096 garantiza espacio para la respuesta final
- Cada turno muestra tiempo y tokens (in/out/cacheados) y el acumulado de sesión

## Protección anti-loop

El modelo 4B tiende a ignorar instrucciones de texto y repetir tool calls hasta agotar el `recursion_limit`. El sistema usa múltiples capas de defensa:

### `ToolBudgetExceeded` (exception-based enforcement)

Tres capas de defensa progresiva:

1. **Explore/Dedupe → STRING STOP**: Cuando explore tools o dedupe se agotan, devuelven un string `⛔` que le dice al modelo que escriba. El modelo puede reintentar con otra herramienta (ej: después de `list_files` bloqueado, intenta `edit_file`).
2. **Read limit → STRING STOP**: Misma lógica tras agotar `max_reads_after_explore`.
3. **`max_tools_before_write` → EXCEPTION**: Si el modelo acumula N tool calls sin escribir nada, se lanza `ToolBudgetExceeded` que **interrumpe el turno forzosamente**. Esto rompe loops infinitos que el 4B ignora.

### Explore budget (`ExploreBudget`)

| Rol | `max_calls` | `max_reads_after_explore` | `max_tools_before_write` | Descripción |
|-----|-------------|--------------------------|--------------------------|-------------|
| EXECUTE | 2 | 2 | 4 | Explora, luego escribe |
| EXECUTE (hints) | 0 | 2 | 4 | No explora, directo a escribir |
| REVIEW | 1 | 5 | 10 | Explora mínimo, lee diffs |
| REVIEW (hints) | 0 | 2 | 5 | No explora, lee diffs del git context |
| ANALYZE | 4 | 8 | 0 (`write_pressure=False`) | Capa búsquedas MCP, NUNCA fuerza a escribir |
| PLAN | 4 | 8 | 0 (`write_pressure=False`) | Ídem |

`max_calls=0` activa `_explore_exhausted=True` inmediatamente y usa exceptions (no strings)
para detener loops del modelo 4B.

**`write_pressure=False` (ANALYZE/PLAN):** las búsquedas MCP (`cm__search_graph`,
`cm__trace_path`, `cm__query_graph`, etc.) gastan presupuesto de exploración. Al agotarse
lanza `ToolBudgetExceeded` → `session.py` dispara el retry **sin tools de búsqueda**
(`_retry_analyze_no_explore`), no el retry write-only. Ver "Retry ANALYZE/PLAN" arriba.

### Dedupe (`ToolCallDedupe`)

Rastreía llamadas idénticas `(tool_name, args)`. Tras `max_repeats=2` (o 1 con hints), la tercera llamada idéntica devuelve `⛔ STOP` (string), permitiendo al modelo reintentar con otra herramienta.

### Preload hints

`execute_bootstrap.py` detecta el tipo de repo (Node/NestJS, Python, Go, Java/Spring) e inyecta hints de layout en el prompt, junto con el checklist de aceptación de tareas. Esto reduce la necesidad de explorar el repo.

### Flujo de interrupción

```
Dedupe repetida → ToolBudgetExceeded (GraphBubbleUp) → PROPAGA through LangGraph ToolNode
  ↓ (NO es atrapada como tool error — el 4B ignora strings de error)
session.py la atrapa → recorta contexto a 3 msgs + instrucción forzada → RETRY
  ↓ (si retry también falla)
max_tool_calls=12 → ToolCallLimitExceeded → corta stream → retry
  ↓
Max 1 retry → turno termina con mensaje claro al usuario
```

### Protección contra reasoning-only (4B con template `--jinja`)

El modelo `agents-a1-4b` (template `--jinja`) **siempre razoné antes de producir contenido**. Con prompts complejos (system + cache + task + hints), el razonamiento puede crecer hasta agotar todo el `max_tokens` sin producir `content` ni `tool_calls`. El sistema usa **dos capas** para manejar esto:

**Capa 1 — `--reasoning-budget` (server-side, primaria)**

Configurado en el `llama-server` startup (ver Instalación). Limita el reasoning a 256 tokens (EXECUTE) / 512 (REVIEW). El server inyecta `--reasoning-budget-message` cuando el presupuesto se agota, forzando `content`/`tool_calls`.

**Verificá que el flag esté activo:** `ps aux | grep llama-server | grep reasoning-budget`

**Capa 2 — Detección + retry (client-side, secundaria)**

Si el server NO tiene `--reasoning-budget` configurado, el código cliente lo detecta y reintenta:

1. `stream_agent_turn` (`display/console.py`): rastrea si se produjo `content`/`tool_calls`. Si el stream termina con **solo reasoning**, levanta `ReasoningOnlyResponse`.
2. `MAX_REASONING_SECONDS = 30` corta el stream si el modelo lleva 30s razonando sin producir output.
3. `MAX_TOOL_CALLS_PER_TURN = 12` corta el stream después de 12 tool calls (detiene loops de read_file). Levanta `ToolCallLimitExceeded`.
4. `session.py` atrapa la excepción → recorta el contexto a 3 mensajes + agrega instrucción forzada ("YA RACIONALIZASTE. Tu PRIMERA acción DEBE ser write_file") → reintenta una vez.
5. `agent_builder.py` pasa `max_reasoning_tokens` via `extra_body` (best-effort; funcionan si el server lo soporta, se ignoran si no).

### Protección contra loops de read_file (4B ignora strings de error)

El 4B modelo tiende a leer el mismo archivo 7+ veces porque **ignora los strings de error** que LangGraph devuelve como `ToolMessage`, y **no puede decidir cuándo parar de explorar** si tiene `read_file`/`list_files` disponibles. El sistema usa 4 mecanismos:

| Mecanismo | Cómo funciona | Efectividad |
|-----------|--------------|-------------|
| **`ToolBudgetExceeded` (GraphBubbleUp)** | Hereda `GraphBubbleUp` → LangGraph ToolNode la RE-RAIZA (no la convierte a ToolMessage) → propaga a `session.py` → retry | ✅ Elimina loops de dedupe |
| **`MAX_TOOL_CALLS_PER_TURN = 12`** | Contador en `stream_agent_turn`: corta el stream después de 12 tool calls → `ToolCallLimitExceeded` → retry | ✅ Límite duro, 4B no puede ignorar |
| **Retry write-only (`force_write`)** | En el retry, `build_agent(force_write=True)` reconstruye el agente con **SOLO** `write_file`/`edit_file`/`delete_file` + git-write + verify. **SIN** `read_file`/`list_files`/`search_code`. El 4B sin lectura disponible escribe directo | ✅✅ **DEFINITIVO** (verificado: escribe controller NestJS real en el retry) |
| **`EXECUTE_RECURSION_LIMIT = 10`** | Límite LangGraph de agent steps. Si todo lo demás falla, corta a los 10 pasos | ✅ Último recurso |
| Stop strings (consume) | `consume()` devuelve `"⛔ ..."` como tool result | ❌ 4B las ignora |

**Hallazgo clave (verificado en pruebas contra el server)**: el root cause definitivo del loop NO es el reasoning ni las excepciones — es que **el 4B no puede autolimitarse la exploración**. Con `read_file` + `list_files` disponibles, las usa infinitamente (13+ reads, 0 writes en 309s). Sin esas tools (write-only), escribe código real en ~30-70s (`write_file` con controller NestJS de 1,859 chars → logger, fire-and-forget, manejo 404, etc.).

**Importante**: el prompt `execute.md` también se simplificó. Antes decía "TU PRIMERA tool call DEBE SER write_file. NO empieces con read_file" — el 4B entraba en parálisis razonando sobre la contradicción "no debo leer pero necesito entender qué hay". Ahora dice "leé máximo 2 archivos si necesitás un patrón; después escribí" y el retry fuerza write-only.

**Configuración** (`config.py`):

| Parámetro | Default | Rol |
|-----------|---------|-----|
| `MAX_TOOL_CALLS_PER_TURN` | 20 | Límite duro de tool calls por turno |
| `EXECUTE_RECURSION_LIMIT` | 30 | Límite LangGraph de agent steps (EXECUTE) |
| `EXECUTE_MAX_REASONING_TOKENS` | 256 | EXECUTE (via extra_body, best-effort) |
| `REVIEW_MAX_REASONING_TOKENS` | 512 | REVIEW (via extra_body, best-effort) |
| `ANALYZE_MAX_REASONING_TOKENS` | 64 | ANALYZE (via extra_body, best-effort) |
| `PLAN_MAX_REASONING_TOKENS` | 128 | PLAN (via extra_body, best-effort) |
| `MAX_REASONING_SECONDS` | 300 | Time budget para cortar stream si solo razoné (None en retry no_explore) |
| `TURN_IDLE_TIMEOUT` | 360 | Idle máximo por turno (6 min) |
| `REASONING_RETRY_ENABLED` | True | Activa/desactiva retry automático |

### Protección de archivos de planificación

El 4B tiende a **reescribir `tasks.md`** (precargado en el prompt) como su primer `write_file`, corrompiendo el plan. Ahora está **protegido a nivel de tool** (`tools/filesystem.py`):

| Tool | Comportamiento |
|------|---------------|
| `write_file` | Rechaza con `⛔ PLANIFICACIÓN PROHIBIDO` si el path es tarea/plan/PRD |
| `edit_file` | Igual — no edita archivos de planificación |
| `delete_file` | Igual — no borra archivos de planificación |

**Patterns protegidos** (`config.py`):
- **Nombres**: `tasks.md`, `task.md`, `plan.md`, `prd.md`, `roadmap.md`, `backlog.md`, `agenda.md`, etc.
- **Directorios**: `.agent-devs`, `.agent`, `plans`, `planning`, `_plans`, `.atl`, `tasks`

Esto se aplica SIEMPRE (incluyendo el retry write-only y la corrección post-review), así el modelo no puede corromper la fuente de verdad de la tarea.

### Anti-duplicación de trabajo y verify idempotente

El 4B a veces, tras un corte, **rehace trabajo ya commiteado** (reescribe + duplica commits) o queda bloqueado por re-correr `run_lint`/`run_tests`. Fixes en `tool_dedupe.py` + `session.py`:

| Comportamiento | Fix |
|----------------|-----|
| `run_lint`/`run_tests`/`run_build` re-corridos | **Nunca** se bloquean por dedupe (son idempotentes — re-verificar tras cada edición es correcto) |
| `read_file` del mismo archivo (relectura inofensiva) | Devuelve STRING informativo, NO lanza excepción → no dispara retry completo |
| Rehacer un commit reciente tras corte | El retry detecta el commit en los últimos 5 min y ordena "NO reescribas, solo verificá y terminá" |
