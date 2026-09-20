"""Evita loops de tools idénticas y fuerza escritura en EXECUTE."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from langgraph.errors import GraphBubbleUp

from config import MAX_EDIT_REJECTIONS_BEFORE_OVERWRITE, MAX_FILE_READ_BYTES


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
    "write_file", "edit_file", "apply_patch", "delete_file", "stage_files", "create_commit", "push", "create_pr",
})
# Tools de lectura (no son "write" pero son acción productiva)
READISH_TOOL_NAMES = frozenset(
    {"read_file", "changed_files", "git_status", "git_log", "current_branch", "read_pr", "list_prs"}
) | MCP_READ_TOOL_NAMES

# Tools de verificación (lint/tests/build) — acción productiva
VERIFY_TOOL_NAMES = frozenset({"run_lint", "run_tests", "run_build", "run_verify", "run_install"})


def _canonical_verify_for_script(script: object) -> str | None:
    """Mapea un script de run_npm_script a su verify canónica, o None.

    El modelo evade el sistema de verify corriendo lint/tests/build por
    run_npm_script (E2E real: `lint:check` + `build` x2 en vez de run_lint/
    run_build — no reseteaban contadores, no alimentaban _verify_results y
    no tocaban el caché). Los scripts de la familia lint*/test*/build* son
    verificación y deben contar como tal en budget, caché y resultados.
    """
    if not isinstance(script, str) or not script.strip():
        return None
    base = re.split(r"[:\-_]", script.strip(), maxsplit=1)[0].lower()
    return {"lint": "run_lint", "test": "run_tests", "build": "run_build"}.get(base)


def _effective_verify_name(name: str, kwargs: dict[str, Any] | None) -> str | None:
    """Nombre canónico de verify para (tool, args), o None si no es verify.

    Cubre las verify tools directas y run_npm_script con script de la familia
    lint/test/build. El resto (db:generate, dev, install...) no es verify.
    """
    if name in VERIFY_TOOL_NAMES:
        return name
    if name == "run_npm_script":
        return _canonical_verify_for_script((kwargs or {}).get("script"))
    return None


def _in_scope(target: str, scope: frozenset) -> bool:
    """True si el path a escribir está dentro del alcance pinnado.

    Match por sufijo (la tarea cita "infra/sqs.tf", el target es absoluto)
    o por basename (la tarea cita "sqs.tf"). Los hermanos spec/test
    (foo.spec.ts de foo.ts) heredan el alcance. Fail-open ante la duda:
    un match parcial cuenta como dentro (mejor dejar pasar un auxiliar que
    frenar un fix legítimo; el runaway lo corta el tope por cantidad).
    """
    t = str(target).replace("\\", "/")
    base = t.rsplit("/", 1)[-1]
    sib = re.sub(r"\.(spec|test)(?=\.[^.]+$)", "", base)
    candidates = {base} | ({sib} if sib != base else set())
    for s in scope:
        sn = str(s).replace("\\", "/").lstrip("./")
        if not sn:
            continue
        if t == sn or t.endswith("/" + sn):
            return True
        sb = sn.rsplit("/", 1)[-1]
        if sb and sb in candidates:
            return True
    return False

# Tools que cuentan como "producto final" para evitar max_tools_before_write
# (REVIEW nunca escribe; estos son sus outputs válidos)
PRODUCTIVE_TOOL_NAMES = READISH_TOOL_NAMES | VERIFY_TOOL_NAMES

# Tools de archivo que REQUIEREN confirmación del usuario antes de ejecutarse
# (EXECUTE_CONFIRM_WRITES): el harness pausa la tool hasta que el usuario
# aprueba. stage_files/create_commit NO van acá: el commit ya tiene su propio
# prompt post-turno (EXECUTE_ASK_COMMIT).
CONFIRM_TOOL_NAMES = frozenset({"write_file", "edit_file", "apply_patch", "delete_file"})


class ToolCallDedupe:
    """Contador por (tool_name, args). Se resetea al inicio de cada turno."""

    def __init__(self, max_repeats: int = 2):
        self.max_repeats = max_repeats
        self._counts: dict[str, int] = {}
        # Alcance pinnado por tarea (T002): lo setea la sesión por turno.
        # scope_files: paths citados en las entradas pinnadas (vacío = off).
        # scope_violations: path → intentos fuera de alcance (persiste en los
        # retries del turno; reset() NO lo toca, la sesión lo gestiona).
        self.scope_files: frozenset = frozenset()
        self.scope_violations: dict[str, int] = {}
        # Rechazos de planificación protegida POR PATH y rechazos no-op POR
        # path: viven EN EL DEDUPE (objeto compartido de la sesión) porque los
        # retries RECONSTRUYEN el agente (y con él el closure del wrapper) —
        # un contador por-closure se resetea en cada retry y el fail-fast
        # pierde memoria (E2E real: PASS1 frenó 2, PASS2 re-metió 2 más).
        # La sesión los limpia por TURNO (run_turn), no reset().
        self.protected_rejects: dict[str, int] = {}
        self.noop_rejects: dict[str, int] = {}
        # Guía dinámica ante fallos POR (tool, path): vive en el dedupe por
        # la misma razón (los retries reconstruyen el closure). La sesión lo
        # limpia por turno (run_turn), no reset().
        self.fail_guides: dict[str, int] = {}

    def reset(self) -> None:
        self._counts.clear()

    def key(self, name: str, args: dict[str, Any]) -> str:
        # trace_component se dedupea por COMPONENTE, ignorando el project:
        # la tool auto-resuelve el slug del repo actual, así que llamar el
        # mismo componente con project distinto (o sin project) es la MISMA
        # consulta repetida. E2E real: sin project + slug inventado = 2
        # llamadas por lo mismo. Aplica a todos los roles (dedupe compartido).
        if name == "trace_component":
            args = {"component": args.get("component")}
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
        max_reads_per_path: int = 5,
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
        self.max_reads_per_path = max_reads_per_path
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
        self._reads_per_path: dict[str, int] = {}
        self._read_limits: dict[str, int] = {}
        self._listed_flat: set[str] = set()
        self._writes_since_verify = 0
        self._verify_streak = 0
        # Verificaciones limpias desde la última escritura: {(tool, path): resumen}.
        # Repetir lint/tests/build sin haber tocado nada es ritual, no trabajo
        # (E2E real 35B: 3 rondas = 9 verifys para 2 edits). El repetido
        # devuelve el resultado cacheado SIN ejecutar ni gastar budget.
        self._verified_clean: dict[tuple[str, str], str] = {}
        # Cache-hits de verify CONSECUTIVOS (mismo turno, sin writes en el
        # medio): el cache-hit devolvía string ignorable SIN consumir budget
        # NI incrementar _verify_streak → loop infinito de run_verify (E2E
        # real T004: 12 run_verify repetidos tras PASSED hasta ESC). Al 2º
        # cache-hit seguido se levanta excepción dura.
        self._verify_cache_hit_streak = 0
        # Cola de fallos por (tool, path) para el baseline flaco: nombres de
        # tests que fallan, sin logs (el cierre N la guarda, el turno N+1
        # distingue heredados de nuevos). Sobrevive a reset() (reintentos):
        # se limpia por TURNO vía clear_failure_tails().
        self._failure_tails: dict[tuple[str, str], list[str]] = {}
        # El wrapper de tools difiere el raise de VerifyRequired al POST-ejecución:
        # así los writes FALLIDOS se pueden refundir (no disparan compuertas
        # falsas) y el raise solo ocurre tras un write EFECTIVO. consume() con
        # este flag en False (API directa, tests) conserva el raise pre-ejecución.
        self._defer_verify_raise = False

    def reset(self) -> None:
        self._count = 0
        self._reads_after = 0
        self._total = 0
        self._wrote = False
        self._explore_exhausted = False
        self._edits_per_path.clear()
        self._reads_per_path.clear()
        self._read_limits.clear()
        self._listed_flat.clear()
        self._verified_clean.clear()
        self._writes_since_verify = 0
        self._verify_streak = 0
        self._verify_cache_hit_streak = 0

    def clear_failure_tails(self) -> None:
        """Limpia la cola de fallos (inicio de turno). Los reintentos NO la
        tocan: verificar antes del retry también es evidencia del turno."""
        self._failure_tails = {}

    @staticmethod
    def _verify_cache_key(name: str, kwargs: dict[str, Any] | None) -> tuple[str, str]:
        import os

        raw = ((kwargs or {}).get("path", "") or "").strip() or "."
        try:
            norm = os.path.normpath(os.path.abspath(raw))
        except OSError:
            norm = raw
        return (name, norm)

    def note_verify(self, name: str, kwargs: dict[str, Any] | None, result: object) -> None:
        """Registra el resultado POST-ejecución de una verify tool.

        Solo `[PASSED]` explícito cachea (con la 1ª línea como resumen).
        Cualquier otra cosa invalida esa entrada: la próxima vez se ejecuta.
        run_npm_script con script lint*/test*/build* cachea bajo su nombre
        canónico (comparte caché con run_lint/run_tests/run_build).
        """
        eff = _effective_verify_name(name, kwargs)
        if eff is None:
            return
        key = self._verify_cache_key(eff, kwargs)
        if isinstance(result, str) and result.startswith("[PASSED]"):
            first = result.splitlines()[0][:160] if result else ""
            self._verified_clean[key] = first
            self._failure_tails.pop(key, None)
        else:
            self._verified_clean.pop(key, None)
            # Cola de fallos para el baseline flaco (ver atributo). Solo si
            # hay nombres extraíbles; si no, se invalida la entrada.
            try:
                from tools.verify import extract_failing_tests

                failing = (
                    extract_failing_tests(result)
                    if isinstance(result, str)
                    else []
                )
            except Exception:
                failing = []
            if failing:
                self._failure_tails[key] = failing
            else:
                self._failure_tails.pop(key, None)

    def set_read_limit(self, path: str, limit: int) -> None:
        """Sube el tope de lecturas para UN path.

        Archivos más grandes que MAX_FILE_READ_BYTES solo se pueden leer por
        RANGOS (un read completo se trunca). max_reads_per_path castigaba esa
        lectura legítima como loop (E2E real: __init__.py de 60KB → 6 reads →
        ToolBudgetExceeded → retry sin lecturas → alucinación).
        """
        self._read_limits[path] = max(limit, self._read_limits.get(path, 0))

    def refund_write(self) -> None:
        """Un write FALLIDO (bloqueado por guard o 'old_str not found') no es
        una escritura efectiva: decrementa el contador del tope write→verify.

        Antes los intentos fallidos contaban igual: 6-7 edits rechazados
        disparaban VerifyRequired y el ping-pong de compuertas sin que el
        modelo hubiera escrito UNA línea real (E2E real, spec-kitti T7).
        """
        if self.write_pressure and self.max_writes_before_verify > 0:
            self._writes_since_verify = max(0, self._writes_since_verify - 1)

    def maybe_raise_verify_required(self) -> None:
        """Raise post-ejecución del tope write→verify (solo para writes EFECTIVOS)."""
        if (
            self.write_pressure
            and self.max_writes_before_verify > 0
            and self._writes_since_verify > self.max_writes_before_verify
        ):
            raise VerifyRequired(
                f"{self._writes_since_verify} escrituras sin correr verify "
                f"en el medio (límite: {self.max_writes_before_verify}). "
                "PARÁ de escribir a ciegas. CORRÉ AHORA "
                "run_lint(path=...), run_tests(path=...) y "
                "run_build(path=...) para verificar lo que escribiste "
                "y corregir los errores antes de seguir."
            )

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

    def _check_redundant_list(self, kwargs: dict[str, Any]) -> str | None:
        """STOP sin costo si el path ya fue listado en el turno.

        Solo se trackean listados planos: recursive=true está prohibido
        aguas abajo (nunca ejecuta, así que registrarlo envenenaría el
        tracking: un flat posterior se bloquearía por un listado que jamás
        ocurrió). E2E real T1: src plano repetido + subdirs quemaron el
        budget con 0 reads.
        """
        import os

        if (kwargs or {}).get("recursive"):
            return None
        raw = (kwargs or {}).get("path", "")
        if not raw:
            return None
        try:
            norm = os.path.normpath(os.path.abspath(raw))
            if not Path(norm).is_dir():
                return None
        except OSError:
            return None
        if norm in self._listed_flat:
            return (
                f"⛔ Ya listaste '{raw}'. No lo listes de nuevo: abrí archivos "
                f"con read_file o ubicá símbolos con trace_component."
            )
        self._listed_flat.add(norm)
        return None

    def consume(self, name: str, kwargs: dict[str, Any] | None = None) -> str | None:
        """Devuelve mensaje STOP si la llamada no debe ejecutarse."""
        kwargs = kwargs or {}
        # Listado redundante: el mismo árbol ya listado (o incluido en un
        # recursive previo) no aporta nada y quemaba el budget de exploración
        # (E2E real T1: src recursive + src plano + subdirs, 0 reads). Se
        # bloquea SIN consumir budget y se redirige a leer archivos.
        if name == "list_files":
            stop = self._check_redundant_list(kwargs)
            if stop:
                return stop
        _eff_verify = _effective_verify_name(name, kwargs)
        if _eff_verify is not None:
            key = self._verify_cache_key(_eff_verify, kwargs)
            if key in self._verified_clean:
                # Ritual de verificación: llamar la misma verify cacheada sin
                # haber escrito nada. El string devuelto era ignorable —
                # E2E real T004: 12 run_verify repetidos hasta ESC (el gate
                # retry con force_tool_calls no deja cerrar con texto y el
                # modelo re-pick-ea la tool más segura). Al 2º cache-hit
                # seguido, excepción dura que libera el turno.
                self._verify_cache_hit_streak += 1
                if self._verify_cache_hit_streak > 1:
                    raise ToolBudgetExceeded(
                        f"⛔ {_eff_verify} ya salió VERDE este turno y lo "
                        f"re-pediste {self._verify_cache_hit_streak} veces sin "
                        "editar nada. La batería está verde y cacheada: "
                        "repetirla es ritual. PARÁ AHORA: respondé el resumen "
                        "final (LISTO + evidencia archivo:línea) y terminá el "
                        "turno. Si de verdad falta un cambio, editá algo y "
                        "recién ahí corré verify de nuevo."
                    )
                return (
                    f"✅ {_eff_verify} ya verificado en este turno sin cambios desde "
                    f"entonces ({self._verified_clean[key]}). No lo re-ejecutes "
                    f"salvo que hayas editado algo: respondé el resumen final."
                )
            self._verify_cache_hit_streak = 0
        self._total += 1
        # Debug print SOLO con AGENTDEVS_DEBUG=1: iba por stderr y en modo
        # --tui pisaba la UI (stderr no pasa por el panel capturado).
        import os
        if os.environ.get("AGENTDEVS_DEBUG"):
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

        # Tope de read_file al MISMO path: el modelo entra en loop leyendo el
        # MISMO archivo con RANGOS DECRECIENTES (1450-1550, 1450-1530,
        # 1450-1510...) que esquivan el dedupe (que solo atrapa args idénticos)
        # y queman el recursion limit sin escribir nada (E2E real: __init__.py
        # leído 7+ veces en rangos decrecientes hasta los 30 pasos). Leer el
        # mismo archivo N veces es un loop: un read completo alcanza.
        if name == "read_file":
            path = (kwargs or {}).get("path", "")
            if path:
                limit = self._read_limits.get(path, self.max_reads_per_path)
                n = self._reads_per_path.get(path, 0) + 1
                self._reads_per_path[path] = n
                if n > limit:
                    raise ToolBudgetExceeded(
                        f"{n} read_file a '{path}'. Ya leíste ese archivo de más — "
                        "cada read con un rango distinto es un LOOP. "
                        "Trabajá con el contenido que ya tenés (o el ancla "
                        "inyectada) y aplicá el fix con edit_file/write_file, "
                        "o corré run_lint/run_tests/run_build."
                    )

        if name in WRITE_TOOL_NAMES:
            self._wrote = True
            self._verify_streak = 0
            self._verify_cache_hit_streak = 0
            # Cualquier escritura invalida las verificaciones cacheadas.
            self._verified_clean.clear()
            # Tope de escrituras totales sin verify en el medio: atrapa el
            # spree multi-archivo (max_edits_per_file solo capa el MISMO path).
            # Lanza VerifyRequired → session inyecta la compuerta de verify.
            if self.write_pressure and self.max_writes_before_verify > 0:
                self._writes_since_verify += 1
                if (
                    self._writes_since_verify > self.max_writes_before_verify
                    and not self._defer_verify_raise
                ):
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
        # Incluye run_npm_script lint*/test*/build* (verificación por otra vía:
        # si no reseteara, el modelo verificaría sin que el harness lo note y
        # el tope de edits lo castigaría igual). PERO: verify en loop SIN
        # escribir es un loop (15 run_lint seguidos en E2E real) — tope de
        # streak (solo EXECUTE, ver __init__).
        if _eff_verify is not None:
            self._edits_per_path.clear()
            self._writes_since_verify = 0
            self._verify_cache_hit_streak = 0
            # También resetea el contador de lecturas POR PATH y post-explore:
            # lecturas legítimas ESPACIADAS (con verify en el medio) para
            # arreglar el propio código se acumulaban hasta disparar el retry
            # write-only a mitad del fix (E2E real spec-kitti T7: 9 reads de
            # __init__.py de 77KB en rangos distintos + 6 verifies → TBE → el
            # modelo no podía terminar de arreglar funciones duplicadas).
            self._reads_per_path.clear()
            self._reads_after = 0
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

        # recursive=true prohibido (en TODOS los roles: el 4B lo usa para
        # volcar árboles enteros al contexto en vez de leer archivos).
        if name == "list_files" and kwargs.get("recursive"):
            return (
                "⛔ list_files(recursive=true) prohibido. Listá UN nivel con "
                "recursive=false y abrí archivos con read_file (o ubicá "
                "símbolos con trace_component)."
            )

        # Write pressure: si pasamos N tools sin escribir ni verify, forzar write.
        # Debe ir ANTES del explore block — los explore tools hacían early return
        # y se salteaban este check, permitiendo loops infinitos de list_files.
        # read_file ya NO es productivo en EXECUTE (solo VERIFY_TOOL_NAMES).
        # run_npm_script lint*/test*/build* SÍ es productivo (es verificación).
        if (
            self.write_pressure
            and not self._wrote
            and self._total > self.max_tools_before_write
            and name not in self._productive_names
            and _eff_verify is None
        ):
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
                        "TU ÚNICA ACCIÓN: write_file o edit_file AHORA."
                    )
                return (
                    "⛔ Exploración agotada. NO uses list_files/search_code/inspect_routes. "
                    "TU ÚNICA ACCIÓN: write_file o edit_file AHORA."
                )
            return None

        # Tras agotar explore, limitar read_file / git_status loops
        if self._explore_exhausted and name in READISH_TOOL_NAMES:
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
                        "TU ÚNICA ACCIÓN: write_file o edit_file AHORA."
                    )
                return (
                    "⛔ Demasiados read_file. NO leas más archivos. "
                    "TU ÚNICA ACCIÓN: write_file o edit_file AHORA."
                )

        return None


def _record_verify_result(
    name: str, result: object, tool_call_results: dict | None,
    kwargs: dict[str, Any] | None = None,
) -> None:
    """Registra el RESULTADO de verify tools (no solo la llamada).

    Solo `[PASSED]` explícito cuenta como pasado; `[FAILED]`, timeouts y
    mensajes de validación cuentan como no-pasado. Así el cierre
    determinístico distingue "se corrió y pasó" de "se corrió y falló".
    run_npm_script lint*/test*/build* se registra bajo su nombre canónico
    (run_lint/run_tests/run_build) para que el cierre y el commit gate lo vean.
    """
    if tool_call_results is None:
        return
    eff = _effective_verify_name(name, kwargs)
    if eff is None:
        return
    if not isinstance(result, str):
        return
    # Solo veredictos reales: [PASSED]→True, [FAILED]→False, [SKIPPED]→None
    # (sin stack: docs/infra — N/A, no fallo). Los mensajes de validación
    # (path inválido, "No 'build' script") NO se registran: no son un fallo
    # de verificación sino un mal uso de la tool (E2E T005: run_lint sobre
    # un ARCHIVO → 'is not a directory' envenenó el cierre con "FALLÓ"
    # mientras los tests estaban verdes).
    if result.startswith("[PASSED]"):
        tool_call_results[eff] = True
    elif result.startswith("[FAILED]"):
        tool_call_results[eff] = False
    elif result.startswith("[SKIPPED]"):
        tool_call_results[eff] = None


_FAIL_LINE_RE = re.compile(
    r"(FAILED|AssertionError|Error:|error:|FAIL:|✕|●.*›|panic:)",
)


def _first_failure_excerpt(result: str, max_chars: int = 800) -> str:
    """Extrae el PRIMER fallo de una salida de verify (language-agnostic).

    pytest/jest/go-test/gradle/catch imprimen líneas FAILED/Error. El modelo
    chico se ahoga en 200 líneas de output y reescribe a ciegas; con el primer
    fallo aislado puede arreglar UN punto. Cap para no comerse el contexto.
    """
    if not isinstance(result, str):
        return ""
    lines = result.splitlines()
    start = -1
    for i, ln in enumerate(lines):
        if ln.strip().startswith(("[FAILED]", "[PASSED]", "[SKIPPED]")):
            continue  # encabezado de veredicto, no el fallo en sí
        if _FAIL_LINE_RE.search(ln):
            start = i
            break
    if start < 0:
        return ""
    # Contexto: 2 líneas previas (qué test) + el fallo + siguientes, cortando
    # ante el PRÓXIMO fallo (un solo punto, no toda la lista).
    lo = max(0, start - 2)
    window = lines[lo:start + 7]
    cut = [window[0]]
    for ln in window[1:]:
        if _FAIL_LINE_RE.search(ln) and ln.strip() != lines[start].strip():
            break
        cut.append(ln)
    excerpt = "\n".join(cut).strip()
    return excerpt[:max_chars]


def _trap_failure(name: str, result: Any, kwargs: dict[str, Any] | None,
                  failure_sink: dict | None) -> None:
    """Guarda el primer fallo de verify por nombre canónico (no pisa un fallo
    previo del turno: el primero es el que hay que arreglar)."""
    if failure_sink is None or not isinstance(result, str):
        return
    if not result.startswith("[FAILED]"):
        return
    eff = _effective_verify_name(name, kwargs)
    if eff is None or eff in failure_sink:
        return
    excerpt = _first_failure_excerpt(result)
    if excerpt:
        failure_sink[eff] = excerpt


def wrap_tools_with_dedupe(
    tools: list,
    dedupe: ToolCallDedupe,
    explore_budget: ExploreBudget | None = None,
    read_cache: dict | None = None,
    repo_path: str | None = None,
    tool_call_logger: set | None = None,
    tool_call_results: dict | None = None,
    allow_overwrite_escalation: bool = True,
    confirm_callback=None,
    evidence_sink: list | None = None,
    failure_sink: dict | None = None,
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

    ``tool_call_results`` (dict): si se provee, cada verify tool ejecutada
    guarda name → bool (True solo con `[PASSED]` explícito). Permite al
    cierre determinístico distinguir "se verificó y pasó" de "se corrió y
    falló" (E2E real: `run_build` en raíz rota contaba como "build ✅").

    ``allow_overwrite_escalation``: si es False, el escalamiento de edit_file
    → write_file completo queda DESHABILITADO (write_file nunca se desbloquea
    para archivos existentes). Lo usa el retry write-only: sin read_file el
    modelo escribiría de memoria y destruiría el archivo (E2E real:
    __init__.py de 1851 líneas truncado a 78).

    ``evidence_sink`` (list): si se provee, cada EJECUCIÓN real agrega
    {"tool", "path", "ok"}. La sesión lo persiste (cache.record_evidence) y
    lo inyecta en retry/summary. Journal harness-side: cero tools nuevas para
    el modelo (un 4B no necesita más schemas).

    ``failure_sink`` (dict): si se provee, el PRIMER fallo de verify por
    nombre canónico guarda su excerpt (primer FAILED). La sesión lo inyecta
    en el gate-retry para que el modelo arregle UN punto en vez de reescribir
    a ciegas.
    """
    # Rechazos del guard quirúrgico de edit_file por path: al llegar al tope,
    # se habilita write_file completo para ese archivo (escalamiento de
    # estrategia — el modelo no converge con cirugía fina en cambios
    # estructurales). El estado vive en este closure: se recrea por agente.
    edit_rejections: dict[str, int] = {}
    if explore_budget is not None:
        # El wrapper decide el tope write→verify POST-ejecución (raise solo
        # para writes efectivos + refund de fallidos). consume() directo
        # (tests/API) conserva el raise pre-ejecución.
        explore_budget._defer_verify_raise = True
    wrapped: list[BaseTool] = []
    for t in tools:
        wrapped.append(
            _wrap_one(
                t, dedupe, explore_budget, read_cache, repo_path,
                tool_call_logger, edit_rejections, allow_overwrite_escalation,
                confirm_callback, tool_call_results, evidence_sink,
                failure_sink,
            )
        )
    return wrapped


def _resolve_relative_path(path: str, repo_path: str | None) -> str:
    """Convierte un path relativo a absoluto contra la raíz del repo.

    Cualquier path que no arranque con / ~ . o un prefijo de drive se considera
    relativo al repo (Gemma 4 pasa 'frontend/src/x.ts'; sin resolución,
    read_file/edit_file fallan contra el CWD del proceso). `./x` y `~/x` se
    resuelven contra el repo/home para no depender del CWD del proceso.
    """
    if not repo_path or not path:
        return path
    if path.startswith("~/"):
        try:
            return str(Path(path).expanduser())
        except OSError:
            return path
    if path.startswith("./"):
        return str(Path(repo_path) / path[2:])
    if path.startswith("../"):
        # ../ fuera del repo se deja tal cual para que la tool lo rechace;
        # ../ interno se normaliza contra el repo.
        try:
            resolved = (Path(repo_path) / path).resolve()
            repo_resolved = Path(repo_path).resolve()
            if resolved.is_relative_to(repo_resolved):
                return str(resolved)
        except OSError:
            pass
        return path
    if path.startswith("/") or (len(path) > 1 and path[1] == ":"):
        return path
    return str(Path(repo_path) / path)


def _apply_adaptive_read_limit(budget: ExploreBudget, path: str) -> None:
    """Archivos > MAX_FILE_READ_BYTES: sube el tope de lecturas por-path.

    Un read completo de un archivo de 60KB+ se trunca (MAX_FILE_READ_BYTES),
    así que la ÚNICA forma de leerlo es por rangos. Sin este ajuste,
    max_reads_per_path cortaba lecturas legítimas como si fueran un loop y
    empujaba al modelo al retry write-only (alucinación + destrucción).
    """
    if not path:
        return
    try:
        size = Path(path).stat().st_size
    except OSError:
        return
    if size <= MAX_FILE_READ_BYTES:
        return
    chunks = (size + MAX_FILE_READ_BYTES - 1) // MAX_FILE_READ_BYTES
    budget.set_read_limit(path, chunks * 2 + 4)


def _write_succeeded(result: Any) -> bool:
    """Las tools de escritura devuelven '✅ ...' solo en éxito.

    '⛔ BLOQUEADO', 'old_str not found', 'File does not exist' y rechazos
    quirúrgicos son intentos FALLIDOS: no deben contar como escritura.
    """
    return isinstance(result, str) and result.strip().startswith("✅")


def _guide_on_failure(name: str, kwargs: dict[str, Any], result: Any,
                      dedupe: ToolCallDedupe | None) -> Any:
    """Intervención dinámica: ante un resultado FALLIDO, agrega una línea de
    guía accionable (qué hacer en vez de reintentar lo mismo). Nativo a la
    arquitectura (sin skills ni otra LLM): el wrapper ya ve nombre, args y
    resultado de cada llamada.

    Al 2º fallo sobre el mismo (tool, path) escala: pide parar y reportar en
    texto. Contador en el dedupe compartido (sobrevive rebuilds, como
    protected_rejects/noop_rejects). Solo toca strings no-exitosos; los ✅ y
    los mensajes que YA guían (⛔ con instrucción) pasan intactos.
    """
    if not isinstance(result, str) or result.strip().startswith("✅"):
        return result
    path = str((kwargs or {}).get("path", ""))
    hint = ""
    if name == "read_file" and "is a directory" in result:
        hint = "💡 Eso es un directorio: exploralo con list_files (read_file es para archivos)."
    elif "File does not exist" in result or "does not exist" in result[:80]:
        hint = (
            "💡 El path no existe: ubicá el real con search_code (por símbolo) "
            "o list_files (por directorio). No adivines variantes del nombre."
        )
    elif name in ("edit_file", "apply_patch") and "old_str not found" in result:
        hint = (
            "💡 El bloque no matchea: releé con read_file y copiá el old_str "
            "LITERAL del archivo (2-5 líneas de contexto)."
        )
    if not hint:
        return result
    key = f"{name}::{path}"
    guides = getattr(dedupe, "fail_guides", None)
    if guides is not None:
        n = guides.get(key, 0) + 1
        guides[key] = n
        if n >= 2:
            hint += (
                f" Ya fallaste {n} veces sobre este path: PARÁ de reintentar "
                "la misma vía, explicá en texto qué necesitás y esperá."
            )
    return result + "\n" + hint


_SKIPPED_MARKER_RE = re.compile(r"^\.\.\. \(lines \d+ to \d+ skipped\) \.\.\.$")


def _result_ok(name: str, result: Any) -> bool:
    """True si la tool se ejecutó con éxito (para el journal de evidencia)."""
    if not isinstance(result, str):
        return True
    head = result[:120]
    for marker in ("⛔", "File does not exist", "does not exist", "old_str not found",
                   "is a directory", "not found in", "RECHAZADO", "BLOQUEADO",
                   "NO-OP", "no se pudo", "Failed to"):
        if marker in head or marker in result[:200]:
            return False
    return True


def _note_evidence(evidence_sink: list | None, name: str,
                   kwargs: dict[str, Any], result: Any) -> None:
    """Journal harness-side (sin tools nuevas para el modelo): 1 entrada por
    ejecución real. La sesión lo persiste en SQLite y lo inyecta en retry y
    summary para que sobreviva al compact."""
    if evidence_sink is None:
        return
    with contextlib.suppress(Exception):
        evidence_sink.append({
            "tool": name,
            "path": str((kwargs or {}).get("path", ""))[:300],
            "ok": _result_ok(name, result),
        })


def _strip_read_artifacts(content: str) -> str:
    """Quita del resultado de read_file los artefactos del harness (header
    '📄 path' y marcadores '(lines X to Y skipped)') ANTES de cachearlo.

    El ancla del retry inyecta este contenido al modelo; los marcadores lo
    confundían ("... (lines 1 to 84 skipped) ..." lo hacía creer que NO tenía
    el contenido) y lo empujaban a leer/reescribir de memoria (E2E real).
    """
    lines = content.splitlines()
    if lines and lines[0].startswith("📄"):
        lines = lines[1:]
    if lines and lines[0].strip() and set(lines[0]) <= {"─"}:
        lines = lines[1:]
    lines = [ln for ln in lines if not _SKIPPED_MARKER_RE.match(ln)]
    return "\n".join(lines)


def _status_flip_candidate(name: str, kwargs: dict[str, Any]) -> bool:
    """True si el write/edit a un protegido parece un flip pending→DONE.

    Heurística de PASILLO (la autoridad es filesystem._status_only_flip sobre
    el JSON completo): si parece flip, no se intercepta en el wrapper y la
    tool decide. Flujo del usuario: el agente marca tareas Done en tasks.json.
    apply_patch incluido (T013: el modelo prefiere el patch atómico para el
    flip y sin pasillo el bloqueo le rompía el cierre).
    """
    if name == "edit_file":
        o = (kwargs or {}).get("old_str") or ""
        n = (kwargs or {}).get("new_str") or ""
        return bool(
            "status" in o and "status" in n
            and "done" in n.lower() and "done" not in o.lower()
        )
    if name == "write_file":
        c = (kwargs or {}).get("content") or ""
        return bool("status" in c and '"done"' in c.lower().replace(" ", ""))
    if name == "apply_patch":
        import json as _json

        raw = (kwargs or {}).get("edits") or ""
        try:
            parsed = _json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return False
        if not isinstance(parsed, list) or not parsed:
            return False
        for e in parsed:
            if not isinstance(e, dict):
                return False
            o = str(e.get("old_string") or e.get("old_str") or "")
            n = str(e.get("new_string") or e.get("new_str") or "")
            if not (
                "status" in o.lower() and "pending" in o.lower()
                and "status" in n.lower() and "done" in n.lower()
            ):
                return False
        return True
    return False


def _wrap_one(
    tool: BaseTool,
    dedupe: ToolCallDedupe,
    explore_budget: ExploreBudget | None,
    read_cache: dict | None = None,
    repo_path: str | None = None,
    tool_call_logger: set | None = None,
    edit_rejections: dict | None = None,
    allow_overwrite_escalation: bool = True,
    confirm_callback=None,
    tool_call_results: dict | None = None,
    evidence_sink: list | None = None,
    failure_sink: dict | None = None,
) -> BaseTool:
    name = tool.name

    # Guard quirúrgico de edit_file rechazado N veces sobre el mismo archivo →
    # habilitar write_file completo (escalamiento de estrategia). El estado
    # viene del closure de wrap_tools_with_dedupe (compartido entre tools).
    def _escalate_edit_rejections(path: str, result: Any, allow: bool) -> Any:
        if not isinstance(result, str):
            return result
        if "QUIRÚRGICAS" not in result:
            return result
        if not allow:
            # Retry SIN read_file (write-only): el modelo no puede ver el
            # contenido real del archivo. El overwrite completo escribiría de
            # memoria y DESTRUIRÍA el archivo (E2E real: __init__.py de 1851
            # líneas truncado a 78). Nunca desbloquear write_file acá: forzar
            # bloques más chicos o fallar el turno (mejor fallar que destruir).
            return (
                result
                + "\n⚠️ En este retry write_file está BLOQUEADO para archivos "
                "existentes (no tenés read_file para ver su contenido real). "
                "Aplicá el cambio en bloques MÁS CHICOS (≤20 líneas) con "
                "edit_file, o no lo apliques."
            )
        if edit_rejections is None:
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
        # Fail-fast para archivos de PLANIFICACIÓN protegidos (tasks/plan/PRD):
        # el guard de filesystem devuelve string y el modelo reintenta variando
        # los bloques (objeto entero → línea suelta → micro-bloque), así que el
        # dedupe por args idénticos NUNCA lo atrapa (E2E real: 4 edits a
        # tasks.json con old_str distintos). Se cuenta POR PATH: intento 1 =
        # STOP terminal (no reintentar), intento 2+ al MISMO path = excepción
        # directa. No pasa por el mensaje genérico "ya se ejecutó" (mentira:
        # nunca se escribió, está bloqueado).
        if name in ("write_file", "edit_file", "apply_patch", "delete_file"):
            _ppath = (kwargs or {}).get("path", "")
            if _ppath:
                try:
                    from tools.filesystem import _is_protected_task_path as _is_prot

                    if (
                        _is_prot(_ppath)
                        and not _status_flip_candidate(name, kwargs or {})
                    ):
                        # Pasillo del flujo del usuario: marcar tasks DONE es
                        # legítimo (status pending→DONE). Si parece flip no se
                        # intercepta: filesystem valida el JSON completo.
                        _pkey = str(_ppath)
                        # Contador EN el dedupe compartido: sobrevive a los
                        # rebuilds del agente en los retries (un dict por
                        # closure se reseteaba en cada retry y el fail-fast
                        # perdía memoria — E2E real: PASS1 frenó 2, PASS2
                        # re-metió 2 más).
                        _prejects = getattr(dedupe, "protected_rejects", None)
                        if _prejects is None:
                            n_prot = dedupe.register(name, {"path": _pkey})
                        else:
                            n_prot = _prejects.get(_pkey, 0) + 1
                            _prejects[_pkey] = n_prot
                        if n_prot >= 2:
                            raise ToolBudgetExceeded(
                                f"⛔ '{_ppath}' es planificación PROTEGIDA "
                                f"({n_prot} intentos). ÚNICA excepción "
                                "permitida: marcar status pending→DONE en "
                                "tasks.json con edit_file. Cualquier otro "
                                "cambio está PROHIBIDO: cerrá con texto y "
                                "avisale al usuario."
                            )
                        return (
                            "return",
                            (
                                f"⛔ '{_ppath}' es un archivo de PLANIFICACIÓN "
                                "PROTEGIDO. ÚNICA edición permitida: marcar "
                                "status pending→DONE. Implementá el código en "
                                "los archivos del repo y cerrá con un resumen."
                            ),
                        )
                except ToolBudgetExceeded:
                    raise
                except Exception:
                    pass
        # Breaker de alcance pinnado (T002): contar escrituras a archivos que
        # la tarea pinnada NO cita. Los primeros auxiliares (tests, spec,
        # schema) pasan en silencio; al superar el tope se levanta excepción
        # (E2E real: T002 de infra/sqs.tf → 5 writes de T003). El cierre
        # determinístico reporta la lista.
        if name in ("write_file", "edit_file", "apply_patch", "delete_file"):
            _tp = (kwargs or {}).get("path", "")
            _scope = getattr(dedupe, "scope_files", None)
            if _tp and _scope and not _in_scope(_tp, _scope):
                from config import SCOPE_MAX_OUT_OF_SCOPE_WRITES

                _viol = dedupe.scope_violations
                _viol[str(_tp)] = _viol.get(str(_tp), 0) + 1
                _total_out = sum(_viol.values())
                if _total_out > SCOPE_MAX_OUT_OF_SCOPE_WRITES:
                    _shown = ", ".join(sorted(_viol)[:6])
                    raise ToolBudgetExceeded(
                        f"⛔ SCOPE CREEP: {_total_out} escrituras fuera del "
                        f"alcance pinnado ({_shown}). La tarea cita: "
                        f"{', '.join(sorted(_scope)[:8])}. "
                        "Si la tarea ya está hecha, PARÁ y reportá con "
                        "evidencia en texto. Si necesitás otro archivo, "
                        "pedilo explícito en tu respuesta y esperá."
                    )
        if explore_budget is not None and name == "read_file":
            _apply_adaptive_read_limit(explore_budget, kwargs.get("path", ""))
        if explore_budget is not None:
            stop = explore_budget.consume(name, kwargs)
            if stop:
                return ("return", stop)
        n = dedupe.register(name, kwargs)
        # VERIFY tools (lint/tests/build) son idempotentes: re-correrlas tras
        # cada edición es correcto y NO es un loop. Nunca bloquearlas por dedupe.
        # Incluye run_npm_script lint*/test*/build* (verificación por otra vía).
        if _effective_verify_name(name, kwargs) is not None:
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
        # Cache contenido leído: el retry lo inyecta como anclaje
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
                read_cache[path] = _strip_read_artifacts(result)
        elif name == "trace_component":
            # El resultado de trace_component (source + página + usos) vive en el
            # state del graph y se PIERDE al cortar por budget. Cachearlo permite
            # que el retry no_explore de ANALYZE/PLAN lo inyecte como anclaje.
            comp = kwargs.get("component")
            if isinstance(result, str) and comp:
                read_cache[f"[trace:{comp}]"] = result
        elif name == "cm__get_code_snippet":
            # Mismo caso que trace_component (E2E real T1: el PASS1 obtuvo
            # source vía snippet crudo, el retry recortó el ToolMessage y el
            # modelo declaró "ningún archivo leído" ignorando su propia
            # lectura). Sin caché, el ancla queda vacío y el retry es ciego.
            qn = kwargs.get("qualified_name") or kwargs.get("query") or "?"
            if isinstance(result, str) and result.strip():
                read_cache[f"[snippet:{qn}]"] = result

    def _rejected_message(kwargs: dict[str, Any]) -> str:
        """Mensaje que ve el modelo cuando el usuario rechaza un write/edit/delete."""
        path = kwargs.get("path", "?")
        return (
            f"⛔ EL USUARIO RECHAZÓ la operación {name} sobre '{path}'. "
            "NO la ejecutes. No insistas con este cambio: pedí permiso de nuevo "
            "solo si vas a hacer algo distinto, o explicá en texto por qué lo "
            "necesitabas y esperá instrucciones."
        )

    async def _confirm_async(kwargs: dict[str, Any]) -> bool:
        """Pide confirmación al usuario sin bloquear el event loop del grafo."""
        return await asyncio.to_thread(confirm_callback, name, kwargs)

    def _invoke(**kwargs):
        kwargs = _resolve_kwargs(kwargs)
        action, value = _policy(kwargs)
        if action == "return":
            return value
        # Guarda de idempotencia ANTES de pedir aprobación: si old_str ==
        # new_str o el new_str ya está en el archivo, es no-op. No tiene
        # sentido pedir 6 aprobaciones idénticas ni contar el archivo como
        # modificado. Se devuelve el mismo mensaje que filesystem.py para
        # cortar el loop sin molestar al usuario.
        # Nota: este check va ANTES del confirm para no molestar, pero
        # DESPUÉS de _policy — _policy ya incrementó _writes_since_verify
        # para WRITE_TOOL_NAMES, así que hay que hacer refund si es no-op.
        def _refund_idempotent():
            if explore_budget is not None and name in WRITE_TOOL_NAMES:
                explore_budget.refund_write()

        def _noop_strike(path_key: str, why: str) -> str:
            """No-op registrado POR PATH en el dedupe compartido. Al 2º no-op
            al MISMO path levanta excepción: el string ignorable devolvía el
            modelo al mismo edit 5 veces (E2E real: 5 NO-OP a infra/iam.tf
            con old_str==new_str hasta quemar el budget)."""
            _nrej = getattr(dedupe, "noop_rejects", None)
            if _nrej is None:
                return why
            _n = _nrej.get(path_key, 0) + 1
            _nrej[path_key] = _n
            if _n >= 2:
                raise ToolBudgetExceeded(
                    f"⛔ {path_key}: {_n} edits SIN CAMBIOS (no-op). PARÁ: el "
                    "archivo YA cumple lo que buscás. NO lo edites de nuevo. "
                    "Corré run_lint/run_tests/run_build si corresponde y "
                    "cerrá el turno con el resumen y la evidencia."
                )
            return why

        if name == "edit_file":
            _old = kwargs.get("old_str", "")
            _new = kwargs.get("new_str", "")
            if _old.strip() == _new.strip() and _old.strip():
                _refund_idempotent()
                return _noop_strike(
                    kwargs.get("path", "desconocido"),
                    (
                        "⛔ NO-OP edit: old_str == new_str (no cambiarías NADA).\n"
                        "Si los archivos YA cumplen la tarea, NO llames edit_file: "
                        "corré run_lint/run_tests para verificarlo y terminá con un resumen."
                    ),
                )
            _path = kwargs.get("path", "")
            if _path and _new.strip():
                try:
                    _content = Path(_path).read_text(encoding="utf-8")
                    if _new.strip() in _content and _old.strip() not in _content:
                        _refund_idempotent()
                        return _noop_strike(
                            _path,
                            (
                                f"old_str not found in {_path} — PERO tu new_str YA ESTÁ en el "
                                "archivo: el cambio ya está aplicado.\n"
                                "NO repitas este edit. Si venías diciendo 'corro los tests': "
                                "llamá AHORA run_lint/run_tests/run_build y terminá."
                            ),
                        )
                except OSError:
                    pass
        if name == "apply_patch":
            _path = kwargs.get("path", "")
            _edits_raw = kwargs.get("edits", "")
            if _path and _edits_raw:
                try:
                    import json as _json

                    _parsed = _json.loads(_edits_raw) if isinstance(_edits_raw, str) else _edits_raw
                    if isinstance(_parsed, list) and _parsed:
                        _content = Path(_path).read_text(encoding="utf-8")
                        if all(
                            (e.get("new_string") or e.get("new_str") or "").strip() in _content
                            for e in _parsed
                        ):
                            _refund_idempotent()
                            return _noop_strike(
                                _path,
                                (
                                    f"All {len(_parsed)} edits already applied in {_path} — no changes needed. "
                                    "Run verification and finish."
                                ),
                            )
                except OSError:
                    pass
                except Exception:
                    pass
        if (
            confirm_callback is not None
            and name in CONFIRM_TOOL_NAMES
            and not confirm_callback(name, kwargs)
        ):
            return _rejected_message(kwargs)
        if tool_call_logger is not None:
            tool_call_logger.add(name)
            # run_npm_script lint*/test*/build* también loguea su nombre
            # canónico: la sesión detecta verify por _called_tools y si solo
            # ve "run_npm_script" la verificación no cuenta (bypass E2E real).
            _eff_log = _effective_verify_name(name, kwargs)
            if _eff_log is not None and _eff_log != name:
                tool_call_logger.add(_eff_log)
        result = tool.invoke(kwargs)
        # Mal uso de verify (mensaje de validación, sin veredicto): no cuenta
        # como verificación del turno (T005: run_lint sobre un archivo
        # envenenó el cierre). Se descarta del logger y de los resultados.
        # [SKIPPED] (docs/infra sin stack) SÍ queda logueado: es un veredicto N/A.
        _bad_verify_call = (
            _effective_verify_name(name, kwargs) is not None
            and isinstance(result, str)
            and not result.startswith(("[PASSED]", "[FAILED]", "[SKIPPED]"))
        )
        if _bad_verify_call and tool_call_logger is not None:
            tool_call_logger.discard(name)
            _eff_bad = _effective_verify_name(name, kwargs)
            if _eff_bad and _eff_bad != name:
                tool_call_logger.discard(_eff_bad)
        _record_verify_result(name, result, tool_call_results, kwargs)
        _trap_failure(name, result, kwargs, failure_sink)
        if explore_budget is not None:
            explore_budget.note_verify(name, kwargs, result)
        if explore_budget is not None and name in WRITE_TOOL_NAMES:
            if _write_succeeded(result):
                explore_budget.maybe_raise_verify_required()
            else:
                explore_budget.refund_write()
        # Write bloqueado/fallido = NO escribió: descartarlo del logger
        # (independiente del budget). Sin esto, un write rechazado por el
        # guard quedaba en _called_tools y el cierre honesto de "ya está
        # implementado" no podía dispararse (E2E T014: turnos fallidos por
        # un write fantasma que tildaba "wrote").
        if name in WRITE_TOOL_NAMES and not _write_succeeded(result) and tool_call_logger is not None:
            tool_call_logger.discard(name)
        if name == "edit_file":
            result = _escalate_edit_rejections(kwargs.get("path", ""), result, allow_overwrite_escalation)
        result = _guide_on_failure(name, kwargs, result, dedupe)
        _note_evidence(evidence_sink, name, kwargs, result)
        _cache_read(kwargs, result)
        return result

    async def _ainvoke(**kwargs):
        kwargs = _resolve_kwargs(kwargs)
        action, value = _policy(kwargs)
        if action == "return":
            return value
        def _refund_idempotent_async():
            if explore_budget is not None and name in WRITE_TOOL_NAMES:
                explore_budget.refund_write()

        def _noop_strike_async(path_key: str, why: str) -> str:
            _nrej = getattr(dedupe, "noop_rejects", None)
            if _nrej is None:
                return why
            _n = _nrej.get(path_key, 0) + 1
            _nrej[path_key] = _n
            if _n >= 2:
                raise ToolBudgetExceeded(
                    f"⛔ {path_key}: {_n} edits SIN CAMBIOS (no-op). PARÁ: el "
                    "archivo YA cumple lo que buscás. NO lo edites de nuevo. "
                    "Corré run_lint/run_tests/run_build si corresponde y "
                    "cerrá el turno con el resumen y la evidencia."
                )
            return why

        if name == "edit_file":
            _old = kwargs.get("old_str", "")
            _new = kwargs.get("new_str", "")
            if _old.strip() == _new.strip() and _old.strip():
                _refund_idempotent_async()
                return _noop_strike_async(
                    kwargs.get("path", "desconocido"),
                    (
                        "⛔ NO-OP edit: old_str == new_str (no cambiarías NADA).\n"
                        "Si los archivos YA cumplen la tarea, NO llames edit_file: "
                        "corré run_lint/run_tests para verificarlo y terminá con un resumen."
                    ),
                )
            _path = kwargs.get("path", "")
            if _path and _new.strip():
                try:
                    _content = Path(_path).read_text(encoding="utf-8")
                    if _new.strip() in _content and _old.strip() not in _content:
                        _refund_idempotent_async()
                        return _noop_strike_async(
                            _path,
                            (
                                f"old_str not found in {_path} — PERO tu new_str YA ESTÁ en el "
                                "archivo: el cambio ya está aplicado.\n"
                                "NO repitas este edit. Si venías diciendo 'corro los tests': "
                                "llamá AHORA run_lint/run_tests/run_build y terminá."
                            ),
                        )
                except OSError:
                    pass
        if name == "apply_patch":
            _path = kwargs.get("path", "")
            _edits_raw = kwargs.get("edits", "")
            if _path and _edits_raw:
                try:
                    import json as _json2

                    _parsed2 = _json2.loads(_edits_raw) if isinstance(_edits_raw, str) else _edits_raw
                    if isinstance(_parsed2, list) and _parsed2:
                        _content2 = Path(_path).read_text(encoding="utf-8")
                        if all(
                            (e.get("new_string") or e.get("new_str") or "").strip() in _content2
                            for e in _parsed2
                        ):
                            _refund_idempotent_async()
                            return _noop_strike_async(
                                _path,
                                (
                                    f"All {len(_parsed2)} edits already applied in {_path} — no changes needed. "
                                    "Run verification and finish."
                                ),
                            )
                except OSError:
                    pass
                except Exception:
                    pass
        if (
            confirm_callback is not None
            and name in CONFIRM_TOOL_NAMES
            and not await _confirm_async(kwargs)
        ):
            return _rejected_message(kwargs)
        if tool_call_logger is not None:
            tool_call_logger.add(name)
            # run_npm_script lint*/test*/build* también loguea su nombre
            # canónico (ver _invoke).
            _eff_log_a = _effective_verify_name(name, kwargs)
            if _eff_log_a is not None and _eff_log_a != name:
                tool_call_logger.add(_eff_log_a)
        result = await tool.ainvoke(kwargs)
        # Mal uso de verify (mensaje de validación): no cuenta como
        # verificación del turno (ver _invoke).
        _bad_verify_call_a = (
            _effective_verify_name(name, kwargs) is not None
            and isinstance(result, str)
            and not result.startswith(("[PASSED]", "[FAILED]", "[SKIPPED]"))
        )
        if _bad_verify_call_a and tool_call_logger is not None:
            tool_call_logger.discard(name)
            _eff_bad_a = _effective_verify_name(name, kwargs)
            if _eff_bad_a and _eff_bad_a != name:
                tool_call_logger.discard(_eff_bad_a)
        _record_verify_result(name, result, tool_call_results, kwargs)
        _trap_failure(name, result, kwargs, failure_sink)
        if explore_budget is not None:
            explore_budget.note_verify(name, kwargs, result)
        if explore_budget is not None and name in WRITE_TOOL_NAMES:
            if _write_succeeded(result):
                explore_budget.maybe_raise_verify_required()
            else:
                explore_budget.refund_write()
        # Write bloqueado/fallido = NO escribió (ver _invoke).
        if name in WRITE_TOOL_NAMES and not _write_succeeded(result) and tool_call_logger is not None:
            tool_call_logger.discard(name)
        if name == "edit_file":
            result = _escalate_edit_rejections(kwargs.get("path", ""), result, allow_overwrite_escalation)
        result = _guide_on_failure(name, kwargs, result, dedupe)
        _note_evidence(evidence_sink, name, kwargs, result)
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
