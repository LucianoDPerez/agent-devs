"""Evita loops de tools idénticas y fuerza escritura en EXECUTE."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from langgraph.errors import GraphBubbleUp

from config import MAX_EDIT_REJECTIONS_BEFORE_OVERWRITE


class ToolBudgetExceeded(GraphBubbleUp):
    """Raised when a tool call is blocked by dedupe or explore budget.

    Inherits GraphBubbleUp (NOT Exception) so that LangGraph's ToolNode
    RE-RAISES it instead of catching it as a regular tool error and
    converting it to a ToolMessage string. The 4B model ignores strings.

    GraphBubbleUp propagation:
      tool.invoke() → BaseTool.run() → re-raises
      ToolNode._execute_tool_sync → `except GraphBubbleUp: raise` → propagates
      agent.astream() → raises
      stream_agent_turn → propagates (finally cleans up stream)
      session.py → caught by `except ToolBudgetExceeded`
    """
    pass


class VerifyRequired(ToolBudgetExceeded):
    """El modelo escribió N veces sin correr verify (lint/tests/build) en el medio.

    Subclase de ToolBudgetExceeded: session.py la maneja ANTES que el genérico
    e inyecta la compuerta de verificación (GATE_RETRY_TOOLS, que SÍ tiene
    run_lint/run_tests/run_build) en lugar del retry write-only — ese retry no
    tiene verify tools y chocaría con el mismo tope al instante.
    """
    pass


# Tools MCP (cm__*) de BÚSQUEDA en el knowledge graph — gastan presupuesto de
# exploración. El 4B en ANALYZE/PLAN se mareaba re-buscando lo mismo con
# queries distintas (el dedupe solo frena args idénticos).
MCP_EXPLORE_TOOL_NAMES = frozenset({
    "cm__search_graph", "cm__trace_path", "cm__query_graph",
    "cm__get_architecture", "cm__search_code", "cm__detect_changes",
})
# Tools MCP de LECTURA puntual (equivalen a read_file) — se limitan post-explore.
MCP_READ_TOOL_NAMES = frozenset({"cm__get_code_snippet"})

# Tools que gastan presupuesto de exploración (EXECUTE/REVIEW + ANALYZE/PLAN vía MCP)
EXPLORE_TOOL_NAMES = (
    frozenset({"list_files", "search_code", "inspect_routes", "trace_component"})
    | MCP_EXPLORE_TOOL_NAMES
)
WRITE_TOOL_NAMES = frozenset({
    "write_file", "edit_file", "delete_file", "stage_files", "create_commit", "push", "create_pr",
})
# Tools de lectura (no son "write" pero son acción productiva)
READISH_TOOL_NAMES = frozenset(
    {"read_file", "changed_files", "git_status", "git_log", "current_branch", "read_pr", "list_prs"}
) | MCP_READ_TOOL_NAMES

# Tools de verificación (lint/tests/build) — acción productiva
VERIFY_TOOL_NAMES = frozenset({"run_lint", "run_tests", "run_build", "run_install"})

# Tools que cuentan como "producto final" para evitar max_tools_before_write
# (REVIEW nunca escribe; estos son sus outputs válidos)
PRODUCTIVE_TOOL_NAMES = READISH_TOOL_NAMES | VERIFY_TOOL_NAMES


class ToolCallDedupe:
    """Contador por (tool_name, args). Se resetea al inicio de cada turno."""

    def __init__(self, max_repeats: int = 2):
        self.max_repeats = max_repeats
        self._counts: dict[str, int] = {}

    def reset(self) -> None:
        self._counts.clear()

    def key(self, name: str, args: dict[str, Any]) -> str:
        try:
            payload = json.dumps(args, sort_keys=True, default=str)
        except TypeError:
            payload = str(args)
        return f"{name}::{payload}"

    def register(self, name: str, args: dict[str, Any]) -> int:
        k = self.key(name, args)
        self._counts[k] = self._counts.get(k, 0) + 1
        return self._counts[k]


class ExploreBudget:
    """Límite duro de exploraciones + presión a escribir (EXECUTE).

    El 4B ignora mensajes STOP y sigue con read_file hasta recursion_limit.
    Por eso también limitamos reads post-explore y tool calls totales sin write.
    """

    def __init__(
        self,
        max_calls: int = 2,
        max_reads_after_explore: int = 2,
        max_tools_before_write: int = 5,
        *,
        write_pressure: bool = True,
        productive_names: frozenset | None = None,
        max_edits_per_file: int = 4,
        max_writes_before_verify: int = 0,
        max_verify_before_write: int = 5,
    ):
        """``write_pressure=False`` → modo ANALYZE/PLAN: capa la exploración
        pero NUNCA presiona a escribir. Al agotar el presupuesto lanza
        ``ToolBudgetExceeded`` de inmediato (el 4B ignora strings), lo que en
        session.py dispara un retry SIN tools de búsqueda (no write-only).

        ``productive_names``: override de PRODUCTIVE_TOOL_NAMES. EXECUTE
        solo considera VERIFY tools como productivas (read_file NO cuenta —
        el modelo se escondía en lecturas infinitas sin escribir).

        ``max_edits_per_file``: tope de edit_file al MISMO path sin correr
        verify en el medio. El dedupe solo atrapa args idénticos; el modelo
        en loop variaba los bloques (8 edit_file a un path corrompiendo el
        JSX por partes). Correr lint/tests/build resetea el contador.

        ``max_writes_before_verify``: tope de escrituras TOTALES (cualquier
        archivo) sin correr verify en el medio (EXECUTE). max_edits_per_file
        no atrapa el spree multi-archivo (15 writes ciegos en la iteración de
        Medicos). Al superarlo lanza ``VerifyRequired`` → session inyecta la
        compuerta de verificación en vez del retry write-only.

        ``max_verify_before_write``: tope de verify calls SEGUIDAS sin escribir
        (solo aplica cuando max_writes_before_verify > 0, i.e. EXECUTE). El
        modelo entraba en loop de run_lint/run_tests sin escribir NADA (15
        run_lint seguidos, E2E real): el dedupe nunca bloquea verify tools por
        diseño y la write pressure las considera productivas → loop infinito
        hasta recursion limit. Una verificación honesta va acompañada de
        escritura o cierre; el 6to verify sin write es un loop.
        """
        self.max_calls = max_calls
        self.max_reads_after_explore = max_reads_after_explore
        self.max_tools_before_write = max_tools_before_write
        self.max_edits_per_file = max_edits_per_file
        self.max_writes_before_verify = max_writes_before_verify
        self.max_verify_before_write = max_verify_before_write
        self.write_pressure = write_pressure
        self._productive_names = (
            productive_names if productive_names is not None
            else PRODUCTIVE_TOOL_NAMES
        )
        self._count = 0
        self._reads_after = 0
        self._total = 0
        self._wrote = False
        self._explore_exhausted = False
        self._edits_per_path: dict[str, int] = {}
        self._writes_since_verify = 0
        self._verify_streak = 0

    def reset(self) -> None:
        self._count = 0
        self._reads_after = 0
        self._total = 0
        self._wrote = False
        self._explore_exhausted = self.max_calls <= 0
        self._edits_per_path.clear()
        self._writes_since_verify = 0
        self._verify_streak = 0

    def limit_reads_now(self) -> None:
        """Activa el tope de lecturas (max_reads_after_explore) DE INMEDIATO,
        sin esperar a que el modelo agote la exploración. Lo usa el retry
        write-only: "lectura acotada" real, no una promesa incumplida (los
        reads no se capaban hasta agotar explore, y en el retry casi nunca
        exploraban → reads ilimitados)."""
        self._explore_exhausted = True

    @property
    def used(self) -> int:
        return self._count

    def consume(self, name: str, kwargs: dict[str, Any] | None = None) -> str | None:
        """Devuelve mensaje STOP si la llamada no debe ejecutarse."""
        kwargs = kwargs or {}
        self._total += 1
        import sys
        print(f"[BUDGET] total={self._total} count={self._count} max={self.max_calls} "
              f"write_pressure={self.write_pressure} name={name}", file=sys.stderr, flush=True)

        # max_calls puede cambiar post-reset (hints_on → 0 en session.py)
        if self.max_calls <= 0:
            self._explore_exhausted = True

        # Tope de edit_file al MISMO archivo sin verify en el medio: el modelo
        # en loop varía los bloques (dedupe ciego) y corrompe el archivo por
        # partes (E2E: 8 edits a PacienteDetailPage.tsx). Verify resetea.
        # DEBE ir ANTES del early-return de WRITE_TOOL_NAMES (edit_file está
        # en ese set y se saltearía el chequeo).
        if name == "edit_file":
            path = (kwargs or {}).get("path", "")
            if path:
                n = self._edits_per_path.get(path, 0) + 1
                self._edits_per_path[path] = n
                if n > self.max_edits_per_file:
                    raise ToolBudgetExceeded(
                        f"{n} edit_file a '{path}' sin correr verify en el medio. "
                        "PARÁ de editar a ciegas. Releé el archivo con read_file "
                        "y aplicá UN edit con el bloque EXACTO del archivo real, "
                        "o corré run_lint/run_tests/run_build para verificar "
                        "el estado actual."
                    )

        if name in WRITE_TOOL_NAMES:
            self._wrote = True
            self._verify_streak = 0
            # Tope de escrituras totales sin verify en el medio: atrapa el
            # spree multi-archivo (max_edits_per_file solo capa el MISMO path).
            # Lanza VerifyRequired → session inyecta la compuerta de verify.
            if self.write_pressure and self.max_writes_before_verify > 0:
                self._writes_since_verify += 1
                if self._writes_since_verify > self.max_writes_before_verify:
                    raise VerifyRequired(
                        f"{self._writes_since_verify} escrituras sin correr verify "
                        f"en el medio (límite: {self.max_writes_before_verify}). "
                        "PARÁ de escribir a ciegas. CORRÉ AHORA "
                        "run_lint(path=...), run_tests(path=...) y "
                        "run_build(path=...) para verificar lo que escribiste "
                        "y corregir los errores antes de seguir."
                    )
            return None

        # Verify tools resetean los contadores de edits y escrituras: después
        # de verificar, el estado es conocido y editar de nuevo es legítimo.
        # PERO: verify en loop SIN escribir es un loop (15 run_lint seguidos
        # en E2E real) — tope de streak (solo EXECUTE, ver __init__).
        if name in VERIFY_TOOL_NAMES:
            self._edits_per_path.clear()
            self._writes_since_verify = 0
            if self.write_pressure and self.max_writes_before_verify > 0:
                self._verify_streak += 1
                if self._verify_streak > self.max_verify_before_write:
                    raise ToolBudgetExceeded(
                        f"{self._verify_streak} verifies seguidos sin escribir nada. "
                        "Correr run_lint/run_tests/run_build en loop no arregla "
                        "nada. TU ÚNICA ACCIÓN: aplicá el cambio con "
                        "edit_file/write_file, o si ya está hecho, continuá con "
                        "stage_files + create_commit."
                    )

        # VERIFY / git RO siempre permitidos una vez que ya escribió
        # (y también antes, con tope de tools sin write)

        # recursive=true prohibido en EXECUTE
        if name == "list_files" and kwargs.get("recursive"):
            return (
                "⛔ list_files(recursive=true) prohibido en EXECUTE. "
                "Usá recursive=false o ESCRIBÍ YA con write_file/edit_file."
            )

        # Write pressure: si pasamos N tools sin escribir ni verify, forzar write.
        # Debe ir ANTES del explore block — los explore tools hacían early return
        # y se salteaban este check, permitiendo loops infinitos de list_files.
        # read_file ya NO es productivo en EXECUTE (solo VERIFY_TOOL_NAMES).
        if (
            self.write_pressure
            and not self._wrote
            and self._total > self.max_tools_before_write
        ):
            if name not in self._productive_names:
                raise ToolBudgetExceeded(
                    f"{self._total} tool calls sin escribir código ni verificar. "
                    "NO explores ni leas más. TU ÚNICA ACCIÓN: write_file o edit_file AHORA."
                )

        if name in EXPLORE_TOOL_NAMES:
            self._count += 1
            if self._count >= self.max_calls:
                self._explore_exhausted = True
            if self._count > self.max_calls:
                # Modo ANALYZE/PLAN: excepción directa (el 4B ignora strings) →
                # session.py reintenta SIN tools de búsqueda, no write-only.
                if not self.write_pressure:
                    raise ToolBudgetExceeded(
                        "Exploración agotada. NO uses más tools de búsqueda "
                        "(cm__search_graph/cm__trace_path). "
                        "Respondé TU ANÁLISIS/PLAN AHORA con lo que ya leíste."
                    )
                # Cuando el modelo ignora strings STOP (4B), usar exception
                if self.max_calls <= 0:
                    raise ToolBudgetExceeded(
                        "Exploración prohibida (max_calls=0). "
                        "TU ÚNICA ACCIÓN: write_file, edit_file o delete_file AHORA."
                    )
                return (
                    "⛔ Exploración agotada. NO uses list_files/search_code/inspect_routes. "
                    "TU ÚNICA ACCIÓN: write_file, edit_file o delete_file AHORA."
                )
            return None

        # Tras agotar explore, limitar read_file / git_status loops
        if self._explore_exhausted:
            if name in READISH_TOOL_NAMES:
                self._reads_after += 1
                if self._reads_after > self.max_reads_after_explore:
                    if not self.write_pressure:
                        raise ToolBudgetExceeded(
                            "Demasiadas lecturas. NO leas más. "
                            "Respondé TU ANÁLISIS/PLAN AHORA con lo que ya leíste."
                        )
                    if self.max_calls <= 0:
                        raise ToolBudgetExceeded(
                            "Demasiados read_file. NO leas más. "
                            "TU ÚNICA ACCIÓN: write_file, edit_file o delete_file AHORA."
                        )
                    return (
                        "⛔ Demasiados read_file. NO leas más archivos. "
                        "TU ÚNICA ACCIÓN: write_file, edit_file o delete_file AHORA."
                    )

        return None


def wrap_tools_with_dedupe(
    tools: list,
    dedupe: ToolCallDedupe,
    explore_budget: ExploreBudget | None = None,
    read_cache: dict | None = None,
    repo_path: str | None = None,
    tool_call_logger: set | None = None,
) -> list:
    """Envuelve tools: dedupe idéntico + (opcional) explore/write guard.

    ``read_cache`` (dict path→content): si se provee, cada read_file exitoso
    almacena su contenido. El retry write-only inyecta ese contenido como
    anclaje para que el modelo pueda reescribir archivos sin necesidad de leer.

    ``repo_path``: si se provee, los paths RELATIVOS que pasen las tools se
    resuelven contra la raíz del repo (Gemma 4 tiende a pasar paths relativos;
    sin resolución, read_file falla contra el CWD del proceso).

    ``tool_call_logger`` (set): si se provee, cada tool invocada agrega su
    nombre al set. La sesión lo usa para saber si el modelo corrió verify
    tools (la compuerta de verificación NO puede ver los tool calls en el
    estado del grafo — escanear self._messages daba falsos positivos).
    """
    # Rechazos del guard quirúrgico de edit_file por path: al llegar al tope,
    # se habilita write_file completo para ese archivo (escalamiento de
    # estrategia — el modelo no converge con cirugía fina en cambios
    # estructurales). El estado vive en este closure: se recrea por agente.
    edit_rejections: dict[str, int] = {}
    wrapped: list[BaseTool] = []
    for t in tools:
        wrapped.append(
            _wrap_one(
                t, dedupe, explore_budget, read_cache, repo_path,
                tool_call_logger, edit_rejections,
            )
        )
    return wrapped


def _resolve_relative_path(path: str, repo_path: str | None) -> str:
    """Convierte un path relativo a absoluto contra la raíz del repo.

    Cualquier path que no arranque con / ~ . o un prefijo de drive se considera
    relativo al repo (Gemma 4 pasa 'frontend/src/x.ts'; sin resolución,
    read_file/edit_file fallan contra el CWD del proceso)."""
    if not repo_path or not path:
        return path
    if path.startswith(("/", "~", "./", "../")) or (len(path) > 1 and path[1] == ":"):
        return path
    return str(Path(repo_path) / path)


def _wrap_one(
    tool: BaseTool,
    dedupe: ToolCallDedupe,
    explore_budget: ExploreBudget | None,
    read_cache: dict | None = None,
    repo_path: str | None = None,
    tool_call_logger: set | None = None,
    edit_rejections: dict | None = None,
) -> BaseTool:
    name = tool.name

    # Guard quirúrgico de edit_file rechazado N veces sobre el mismo archivo →
    # habilitar write_file completo (escalamiento de estrategia). El estado
    # viene del closure de wrap_tools_with_dedupe (compartido entre tools).
    def _escalate_edit_rejections(path: str, result: Any) -> Any:
        if edit_rejections is None or not isinstance(result, str):
            return result
        if "QUIRÚRGICAS" not in result:
            return result
        n = edit_rejections.get(path, 0) + 1
        edit_rejections[path] = n
        if n < MAX_EDIT_REJECTIONS_BEFORE_OVERWRITE:
            return result
        try:
            from tools.filesystem import WRITE_OVERRIDE_PATHS
            WRITE_OVERRIDE_PATHS.add(path)
        except Exception:
            pass
        return (
            f"⛔ edit_file quirúrgico está BLOQUEADO para '{path}' y ya "
            f"fallaste {n} veces intentando editarlo por partes. "
            f"CAMBIÁ DE ESTRATEGIA: reemplazá el archivo COMPLETO con write_file.\n"
            f"  1) read_file(path='{path}') para ver el contenido EXACTO actual.\n"
            f"  2) write_file(path='{path}', content='<archivo COMPLETO con tu cambio>').\n"
            f"     ⚠️  El overwrite de ESTE archivo está habilitado por el sistema.\n"
            f"  3) PRESERVÁ todo lo que existe (imports, componentes, estado, "
            f"handlers, SVG) — solo aplicá TU cambio encima.\n"
            f"  4) Después corré run_lint/run_tests/run_build."
        )

    def _resolve_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
        # Paths relativos → absolutos contra la raíz del repo (Gemma 4 usa
        # paths relativos y read_file/edit_file fallaban contra el CWD).
        if repo_path and "path" in kwargs and isinstance(kwargs["path"], str):
            kwargs = {**kwargs, "path": _resolve_relative_path(kwargs["path"], repo_path)}
        return kwargs

    def _policy(kwargs: dict[str, Any]) -> tuple[str, Any]:
        """Presupuesto de exploración + dedupe.

        Retorna ("return", value) para devolver un string de STOP sin invocar
        la tool, o ("proceed", None) para ejecutarla. `ToolBudgetExceeded`
        (GraphBubbleUp) se re-lanza siempre: es la única forma de frenar el 4B.
        """
        kwargs = _resolve_kwargs(kwargs)
        if explore_budget is not None:
            stop = explore_budget.consume(name, kwargs)
            if stop:
                return ("return", stop)
        n = dedupe.register(name, kwargs)
        # VERIFY tools (lint/tests/build) son idempotentes: re-correrlas tras
        # cada edición es correcto y NO es un loop. Nunca bloquearlas por dedupe.
        if name in VERIFY_TOOL_NAMES:
            return ("proceed", None)
        if name == "read_file" and n > dedupe.max_repeats:
            # Releer el MISMO archivo no es un loop crítico como la exploración:
            # devolver STRING (no exception) para no disparar un retry completo.
            # El modelo ignora a veces, pero read_file no quema recursion como explore.
            return (
                "return",
                (
                    f"⛔ Ya leíste {kwargs.get('path','')} ({n} veces). "
                    "No lo vuelvas a leer con los mismos args. Trabajá con lo que "
                    "ya tenés o escribí/edita el código."
                ),
            )
        if n > dedupe.max_repeats:
            # Para write tools: devolver STRING (no exception) — el archivo ya
            # está escrito, dejar que el modelo continúe (commit/verify).
            # Solo RAISE si repite demasiado (evita loop infinito).
            if name in WRITE_TOOL_NAMES and n <= dedupe.max_repeats + 2:
                return (
                    "return",
                    (
                        f"✅ {name} ya se ejecutó con estos args ({n} veces). "
                        "El código ya está escrito. NO lo escribas de nuevo. "
                        "Continuá con stage_files + create_commit, o con run_lint/run_tests."
                    ),
                )
            # RAISE instead of return string — the 4B model ignores text responses
            # and keeps calling the same tool, burning recursion limit.
            raise ToolBudgetExceeded(
                f"⛔ Called {name} with same args {n} times. "
                "STOP: Do NOT call it again. If this is write_file/edit_file, "
                "the path may be wrong or a directory exists at that path. "
                "Choose a different file path."
            )
        return ("proceed", None)

    def _cache_read(kwargs: dict[str, Any], result: Any) -> None:
        # Cache contenido leído: el retry write-only lo inyecta como anclaje
        if read_cache is None:
            return
        if name == "read_file":
            path = kwargs.get("path")
            # NO cachear errores (path inexistente, directorio): contaminan el
            # ancla del retry con mensajes de error en vez de contenido útil.
            if (
                isinstance(result, str)
                and path
                and not result.startswith("File does not exist")
                and "is a directory" not in result
                and "does not exist" not in result[:80]
            ):
                read_cache[path] = result
        elif name == "trace_component":
            # El resultado de trace_component (source + página + usos) vive en el
            # state del graph y se PIERDE al cortar por budget. Cachearlo permite
            # que el retry no_explore de ANALYZE/PLAN lo inyecte como anclaje.
            comp = kwargs.get("component")
            if isinstance(result, str) and comp:
                read_cache[f"[trace:{comp}]"] = result

    def _invoke(**kwargs):
        kwargs = _resolve_kwargs(kwargs)
        action, value = _policy(kwargs)
        if action == "return":
            return value
        if tool_call_logger is not None:
            tool_call_logger.add(name)
        result = tool.invoke(kwargs)
        if name == "edit_file":
            result = _escalate_edit_rejections(kwargs.get("path", ""), result)
        _cache_read(kwargs, result)
        return result

    async def _ainvoke(**kwargs):
        kwargs = _resolve_kwargs(kwargs)
        action, value = _policy(kwargs)
        if action == "return":
            return value
        if tool_call_logger is not None:
            tool_call_logger.add(name)
        result = await tool.ainvoke(kwargs)
        if name == "edit_file":
            result = _escalate_edit_rejections(kwargs.get("path", ""), result)
        _cache_read(kwargs, result)
        return result

    # MCP tools (langchain-mcp-adapters) son StructuredTool ASYNC-ONLY
    # (solo `coroutine`, sin `func`): llamarlas con tool.invoke() lanza
    # "StructuredTool does not support sync invocation." Por eso el wrapper
    # expone AMBOS paths — ToolNode elige ainvoke() cuando hay coroutine.
    return StructuredTool.from_function(
        func=_invoke,
        coroutine=_ainvoke,
        name=name,
        description=tool.description,
        args_schema=getattr(tool, "args_schema", None),
    )
