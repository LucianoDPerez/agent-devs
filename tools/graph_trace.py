"""Tool compuesta: trazado determinístico de un componente sobre el knowledge graph.

El 4B no puede orquestar exploraciones multi-paso del grafo (search_graph →
get_code_snippet → buscar usos → leer página). Esta tool hace el recorrido EN
CÓDIGO: dado un componente, devuelve su source + dónde se referencia en el
repo + el source de la página que lo renderiza, en UNA sola llamada. El modelo
solo lee el resultado y responde el análisis.

Nota: los edges CALLS del grafo suelen estar incompletos en proyectos React,
así que los "usos" se resuelven con grep real del filesystem (search_code), no
con trace_path.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool, tool

from tools._helpers import _read_text, _is_excluded
from tools.search import search_code

# Output limits: el 4B no puede razonar sobre 16K chars de source. Reducido
# para que el modelo procese el diagnóstico y pase a escribir.
_SNIPPET_MAX_CHARS = 3000
_PAGE_MAX_CHARS = 3000
_USAGES_MAX = 20
_PAGES_MAX = 2


async def _resolve_project_key(
    mcp_by_name: dict[str, Any], repo_path: str, requested: str = ""
) -> str:
    """Resuelve la key del proyecto indexado del repo (o valida `requested`).

    El modelo 4B NO conoce la key exacta (ej. 'Users-luchop-PROYECTOS-IA-Medicos')
    y la inventa → trace_component falla y el modelo repite la llamada hasta que
    el dedupe lo corta. El SISTEMA resuelve: match por root_path → basename.
    """
    lp = mcp_by_name.get("cm__list_projects")
    if lp is None:
        return requested or ""
    raw = await lp.ainvoke({})
    text = ""
    if isinstance(raw, list):
        text = "".join(
            b.get("text", "") for b in raw
            if isinstance(b, dict) and b.get("type") == "text"
        )
    elif isinstance(raw, str):
        text = raw
    if not text:
        return requested or ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return requested or ""
    projects = data.get("projects", []) or []
    names = [p.get("name", "") for p in projects]

    if requested and requested in names:
        return requested

    repo_norm = repo_path.rstrip("/")
    for p in projects:
        rp = (p.get("root_path") or "").rstrip("/")
        if rp == repo_norm:
            return p.get("name", "")

    import os
    basename = os.path.basename(repo_norm).lower()
    for p in projects:
        if basename and basename in (p.get("root_path") or "").lower():
            return p.get("name", "")
    return ""


def _extract_text(blocks: Any) -> str:
    """Convierte el resultado de una tool MCP (content blocks) a texto plano."""
    if isinstance(blocks, list):
        parts: list[str] = []
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return "".join(parts)
    if isinstance(blocks, str):
        return blocks
    return json.dumps(blocks, default=str)


def _parse_json(text: str) -> dict:
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _pick_best_match(name: str, results: list[dict]) -> dict | None:
    """Elige el nodo que mejor matchea `name`.

    Acepta (en orden):
    1. Match exacto de nombre (cualquier label).
    2. Function/Component cuyo nombre contenga el término o sus palabras.
    3. Module/File cuyo nombre de archivo (sin extensión) sea EXACTAMENTE el
       término o termine en '/<término>' — targets tipo 'dashboardRoutes' que
       apuntan al ROUTER/controller de backend, no a una componente de UI.
    Devuelve None si no hay match confiable (queries en lenguaje natural
    devuelven Methods irrelevantes) → el caller cae al fallback filesystem.
    """
    lower = name.lower()
    for r in results:
        if r.get("name", "").lower() == lower:
            return r  # match exacto (aunque sea un Method)
    for r in results:
        if r.get("label") in ("Function", "Component"):
            rname = r.get("name", "").lower()
            # el nombre de la componente debe relacionarse con el término
            if lower in rname or any(w in rname for w in re.split(r"\s+|_|/", lower) if len(w) >= 3):
                return r
    # Module/File: el término nombra un ARCHIVO (ej. 'dashboardRoutes').
    # Match fuerte = basename sin extensión idéntico al término.
    terms = [w for w in re.split(r"\s+|_|/", lower) if len(w) >= 3]
    for r in results:
        if r.get("label") not in ("Module", "File"):
            continue
        fname = (r.get("file_path") or r.get("name") or "").split("/")[-1]
        stem = fname.lower()
        for ext in (".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".java",
                    ".kt", ".php", ".cs", ".rb", ".rs", ".vue", ".svelte"):
            if stem.endswith(ext):
                stem = stem[: -len(ext)]
                break
        if stem == lower or stem == lower.replace(".ts", ""):
            return r
        if terms and all(w in stem for w in terms):
            return r
    return None


def _extract_exported_component(term: str, repo_path: str) -> str | None:
    """Busca en el filesystem componentes exportados relacionados con el término.

    El término puede ser el nombre real ('CreatePacienteModal'), una parte
    ('PacienteForm') o una descripción/label ('botón guardar de pacientes').
    Estrategia: grep de palabras clave del término, juntar definiciones
    `export function <X>`, y rankear por densidad de coincidencias + preferencia
    por Form/Modal (contienen botones de submit) sobre List/Table.
    """
    words = [w for w in re.split(r"\s+|_|/", term.lower()) if len(w) >= 3]
    if not words:
        return None
    # (file_keyword_hits, component) → counts
    file_hits: dict[str, int] = {}
    file_defs: dict[str, list[str]] = {}
    for w in words:
        hits = search_code.invoke({"path": repo_path, "pattern": re.escape(w)})
        if hits.startswith("No matches") or hits.startswith("Path does not exist"):
            continue
        for ln in hits.splitlines():
            m = re.match(r"^([^:]+):\d+:", ln)
            if not m:
                continue
            path = m.group(1)
            file_hits[path] = file_hits.get(path, 0) + 1
            dm = re.match(r"^[^:]+:\d+:\s*export function\s+(\w+)", ln)
            if dm:
                file_defs.setdefault(path, []).append(dm.group(1))

    if not file_defs:
        return None
    # Score: densidad de palabras clave en el archivo + preferencia de tipo
    def _score(fname: str) -> int:
        s = 0
        if "form" in fname.lower() or "modal" in fname.lower():
            s += 3  # formularios/modales tienen botones de submit
        elif "page" in fname.lower():
            s += 2
        elif "list" in fname.lower() or "table" in fname.lower():
            s -= 2  # listas/tablas no tienen botón guardar
        return s

    best: tuple[float, int, str] | None = None
    for path, defs in file_defs.items():
        hits = file_hits.get(path, 0)
        for fname in dict.fromkeys(defs):  # dedupe preservando orden
            if not re.match(r"^[A-Z]", fname):
                continue
            score = (hits + 0.0, _score(fname), -fname.count("Props"))
            if best is None or score > best:
                best = (*score, fname)
    return best[3] if best else None


async def _ainvoke_named(mcp_by_name: dict[str, Any], tool_name: str, **kwargs: Any) -> Any:
    tool = mcp_by_name.get(tool_name)
    if tool is None:
        return f"⚠️ MCP tool {tool_name} no disponible."
    return await tool.ainvoke(kwargs)


def _cap(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[:limit] + "\n… [truncado]"
    return text


async def _find_page_for_term(
    term: str, repo_path: str, mcp_by_name: dict[str, Any], project: str
) -> str:
    """Resuelve el término como componente del filesystem y devuelve la página
    que lo renderiza (para términos que no matchean exacto en el grafo)."""
    exported = _extract_exported_component(term, repo_path)
    if not exported:
        return ""
    sres = mcp_by_name.get("cm__search_graph")
    if sres is None:
        return ""
    raw = await sres.ainvoke({"query": exported, "project": project, "limit": 5})
    data = _parse_json(_extract_text(raw))
    best = _pick_best_match(exported, data.get("results", []) or [])
    if best is None:
        return ""
    qn = best.get("qualified_name", "")
    snip = mcp_by_name.get("cm__get_code_snippet")
    if snip is None:
        return ""
    raw2 = await snip.ainvoke({"qualified_name": qn, "project": project})
    src = _parse_json(_extract_text(raw2)).get("source", "")
    usage = search_code.invoke({"path": repo_path, "pattern": re.escape(exported)})
    usages_lines = []
    for ln in usage.splitlines()[1:]:
        m = re.match(r"^([^:]+):(\d+):", ln)
        if m and "/pages/" in m.group(1):
            usages_lines.append(ln)
    parts = [f"→ componente relacionada: {exported} ({best.get('file_path')})"]
    if src:
        parts.append(f"SOURCE DE '{exported}':\n{_cap(src, _SNIPPET_MAX_CHARS)}")
    if usages_lines:
        parts.append(f"USOS EN PÁGINAS:\n" + "\n".join(usages_lines))
    return "\n".join(parts)


def _find_file_named(name: str, repo_path: str) -> Path | None:
    """Archivo cuyo basename (sin extensión conocida) == `name`.

    Para targets tipo 'dashboardRoutes' — routers/controllers de backend — que
    el grafo NO puede resolver: la búsqueda por texto de cm__search_graph
    filtra los labels File/Module (y en MCP <0.9 name_pattern no está
    soportado). El filesystem es la fuente confiable para este caso.
    """
    root = Path(repo_path)
    if not root.exists() or not name:
        return None
    best: Path | None = None
    try:
        for p in root.rglob(f"{name}.*"):
            if _is_excluded(p) or not p.is_file():
                continue
            if best is None or len(p.parts) < len(best.parts):
                best = p
    except OSError:
        return None
    return best


def build_trace_component(mcp_tools: list[StructuredTool], repo_path: str) -> StructuredTool:
    """Crea la tool `trace_component` usando las tools MCP cargadas.

    ``mcp_tools``: tools MCP (prefijo ``cm__``) ya conectadas.
    ``repo_path``: path del repo en disco (para el grep real de usos).
    """
    mcp_by_name = {t.name: t for t in mcp_tools}

    @tool
    async def trace_component(component: str, project: str = "") -> str:
        """Traza una componente de un proyecto en una sola llamada: resuelve su
        qualified_name, devuelve su source completo, dónde se usa en el repo y
        el source de la PÁGINA que la renderiza. Es la cadena completa para
        diagnosticar un bug de UI. Usala PRIMERO; después solo respondés el
        análisis (no hace falta buscar más).
        component: nombre de la función/componente (ej. 'CreatePacienteModal').
        project: key del proyecto indexado (opcional — si lo omitís o es
        inválido, el sistema la resuelve automáticamente del repo actual).
        """
        # El sistema resuelve la key correcta (el modelo la inventa a veces)
        project = await _resolve_project_key(mcp_by_name, repo_path, project)
        if not project:
            lp = mcp_by_name.get("cm__list_projects")
            extra = ""
            if lp is not None:
                try:
                    raw = await lp.ainvoke({})
                    t = _extract_text(raw)
                    data = _parse_json(t)
                    extra = " Proyectos disponibles: " + ", ".join(
                        p.get("name", "") for p in (data.get("projects", []) or [])
                    ) or ""
                except Exception:
                    pass
            return (
                "No se pudo resolver el project key del knowledge graph para "
                f"'{repo_path}'.{extra} Usá cm__list_projects si está disponible."
            )

        # 1) Resolver la componente en el grafo (exacto por nombre)
        sres = await _ainvoke_named(
            mcp_by_name, "cm__search_graph", query=component, project=project, limit=10
        )
        sdata = _parse_json(_extract_text(sres))
        best = _pick_best_match(component, sdata.get("results", []) or [])
        resolved_name = component
        if best is None:
            # Fallback: resolver por TEXTO que aparece en el código (ej. el label
            # de un botón 'Guardar Paciente' o una descripción). El 4B no conoce
            # los nombres reales de las componentes.
            exported = _extract_exported_component(component, repo_path)
            if exported:
                sres2 = await _ainvoke_named(
                    mcp_by_name, "cm__search_graph", query=exported, project=project, limit=10
                )
                sdata = _parse_json(_extract_text(sres2))
                best = _pick_best_match(exported, sdata.get("results", []) or [])
                resolved_name = exported
        if best is None:
            # Fallback FINAL: el término nombra un ARCHIVO del backend
            # (router/controller/service, ej. 'dashboardRoutes'). El grafo no
            # lo devuelve (search textual filtra File/Module) pero el
            # filesystem lo resuelve en una pasada.
            ffile = _find_file_named(component, repo_path)
            if ffile is not None:
                rel = ffile.relative_to(Path(repo_path)).as_posix()
                src = _read_text(ffile)
                parts = [f"🔎 RESOLUCIÓN (archivo): {ffile.name}\n   archivo: {rel}"]
                if src:
                    parts.append(f"\n📄 SOURCE DE '{ffile.name}':\n{_cap(src, _SNIPPET_MAX_CHARS)}")
                usages = search_code.invoke({"path": repo_path, "pattern": re.escape(ffile.stem)})
                if not str(usages).startswith(("No matches", "Path does not exist")):
                    ulines = str(usages).splitlines()
                    if len(ulines) > _USAGES_MAX + 1:
                        ulines = ulines[: _USAGES_MAX + 1] + ["… (más coincidencias truncadas)"]
                    parts.append(f"\n🔗 USOS DE '{ffile.stem}' EN EL REPO (archivo:línea: texto):")
                    parts.append("\n".join(ulines[1:]))
                parts.append(
                    "\n→ Es un router/módulo del backend: sus endpoints delegan en "
                    "handlers (controllers/services) visibles en el source de arriba. "
                    "Cadena completa; no hace falta buscar más."
                )
                return "\n".join(parts)

        if best is None:
            return (
                f"No se encontró '{component}' ni ninguna componente relacionada en "
                f"'{project}'.\n"
                "Verificá el project con cm__list_projects (debe ser la key exacta) "
                "o probá un nombre/descripción distinto."
            )

        qn = best.get("qualified_name", "")
        out: list[str] = []
        out.append(
            f"🔎 RESOLUCIÓN: {best.get('name')} ({best.get('label')})\n"
            f"   archivo: {best.get('file_path')}:{best.get('start_line')}-{best.get('end_line')}\n"
            f"   qualified_name: {qn}"
        )

        # 2) Source completo de la componente
        snip = await _ainvoke_named(
            mcp_by_name, "cm__get_code_snippet", qualified_name=qn, project=project
        )
        snip_data = _parse_json(_extract_text(snip))
        source = snip_data.get("source") or snip_data.get("content") or ""
        if source:
            out.append(f"\n📄 SOURCE DE '{best.get('name')}':\n{_cap(source, _SNIPPET_MAX_CHARS)}")
        else:
            out.append(f"\n📄 Source no disponible para '{qn}'")

        # 3) Dónde se usa (grep real del filesystem — más confiable que los edges del grafo)
        pattern = re.escape(resolved_name)
        usages = search_code.invoke({"path": repo_path, "pattern": pattern})
        usage_files: list[str] = []
        if not usages.startswith("No matches") and not usages.startswith("Path does not exist"):
            lines = usages.splitlines()
            if len(lines) > _USAGES_MAX + 1:
                lines = lines[: _USAGES_MAX + 1] + ["… (más coincidencias truncadas)"]
            out.append(f"\n🔗 USOS DE '{resolved_name}' EN EL REPO (archivo:línea: texto):")
            out.append("\n".join(lines[1:]))  # saltear el header "Found N match(es)"
            # Archivos de PÁGINAS que renderizan la componente (clave para bugs de UI)
            for ln in lines[1:]:
                m = re.match(r"^([^:]+):(\d+):", ln)
                if m and "/pages/" in m.group(1):
                    usage_files.append(m.group(1))
        else:
            out.append(f"\n🔗 Sin referencias textuales a '{resolved_name}' en {repo_path}")

        # 4) Source de las páginas que la renderizan (cadena UI completa)
        for p in usage_files[:_PAGES_MAX]:
            full = Path(repo_path) / p
            page_src = _read_text(full)
            if page_src:
                out.append(
                    f"\n📄 PÁGINA QUE RENDERIZA '{resolved_name}' ({p}):\n"
                    f"{_cap(page_src, _PAGE_MAX_CHARS)}"
                )

        # 4b) Si la componente NO la renderiza ninguna página (es huérfana o el
        # término era una descripción), buscar por el término original en el
        # filesystem y apuntar a la página que SÍ usa componentes relacionados.
        if not usage_files and resolved_name != component:
            page_hint = await _find_page_for_term(component, repo_path, mcp_by_name, project)
            if page_hint:
                out.append(f"\n📄 PÁGINA RELACIONADA (por término '{component}'):\n{_cap(page_hint, _PAGE_MAX_CHARS)}")

        out.append(
            "\n→ Para diagnosticar un bug de UI: seguí el handler de submit/guardar "
            "en la PÁGINA y en la componente. Con esto ya tenés la cadena completa; "
            "no hace falta buscar más."
        )
        return "\n".join(out)

    return trace_component
