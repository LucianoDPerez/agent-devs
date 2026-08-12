"""Detección determinística de mismatches de paths frontend ↔ backend.

El 4B/Gemma-E4B no puede hacer el diagnóstico causal cross-file de forma
confiable (hook → servicio → rutas backend). En vez de llenar el prompt con
hints por cada clase de bug, el SISTEMA compara en código los paths que usa
el frontend contra las rutas que expone el backend y reporta el hallazgo
concreto ("PATH MISMATCH DETECTED: frontend llama a '/pacientes' pero el
backend sirve '/api/pacientes'. Fix: agregá el prefijo en archivo:línea").

Escala: funciona para CUALQUIER mismatch de paths (pacientes, consultas,
migración, cualquier endpoint, cualquier framework Express/Nest/FastAPI/...).
"""

from __future__ import annotations

import re
from pathlib import Path

from tools._helpers import _is_excluded, _read_text
from tools.routes import (
    _EXPRESS_RE,
    _NEST_RE,
    _FASTAPI_RE,
    _FLASK_RE,
    _DJANGO_RE,
    _GO_RE,
    _RUST_ATTR_RE,
    _RUST_ROUTE_RE,
    _SPRING_MAPPING_RE,
    _LARAVEL_RE,
    _SYMFONY_RE,
    _SLIM_RE,
    _ASPNET_MAP_RE,
    _RAILS_RE,
)

# Todos los patrones de rutas por framework (framework-agnóstico: el detector
# debe funcionar en CUALQUIER repo, no solo Express/FastAPI).
ROUTE_PATTERNS: list = [
    _EXPRESS_RE, _NEST_RE, _FASTAPI_RE, _FLASK_RE, _DJANGO_RE,
    _GO_RE, _RUST_ATTR_RE, _RUST_ROUTE_RE, _SPRING_MAPPING_RE,
    _LARAVEL_RE, _SYMFONY_RE, _SLIM_RE, _ASPNET_MAP_RE, _RAILS_RE,
]

# Llamadas HTTP en el frontend: apiClient.get("/x"), fetch("/x"), axios.post, etc.
# El path puede ser string o template literal (backtick) — capturamos la parte
# literal (hasta el primer ${). [^()]* en el generic: soporta generics ANIDADOS
# como get<PaginatedResult<Paciente>> (con [^>]* se rompía en el primer '>').
_FETCH_CALL_RE = re.compile(
    r"(?:apiClient|client|api|axios|fetch|request|http)\s*\.\s*"
    r"(?:get|post|put|patch|delete|request)\s*"
    r"(?:<[^()]*>)?\s*\(\s*[\"'\`]([^\"'\`]*?)[\"'\`]",
    re.IGNORECASE,
)

# Montajes de routers: app.use('/api', apiRoutes) / router.use('/pacientes', ...)
_MOUNT_RE = re.compile(
    r"(?:app|router)\.use\s*\(\s*[\"'\`]([^\"'\`]+)[\"'\`]",
    re.IGNORECASE,
)

# Dir donde vive el frontend (para extraer paths de fetch) y el backend
# (para extraer rutas). El frontend se detecta por dirs frontend/web/client;
# el backend es TODO lo demás (los scanners de rutas corren sobre cualquier
# archivo que no sea del frontend — cubre backend/, server/, src/ sueltos).
_FRONTEND_MARKERS = ("frontend", "web", "client")
_SKIP_DIRS = {"node_modules", "dist", "build", ".next", ".nuxt", "coverage", "public", "tests", "test", "__tests__"}


def _is_frontend_file(rel: str) -> bool:
    parts = rel.split("/")
    return any(m in parts for m in _FRONTEND_MARKERS)


def _is_backend_file(rel: str) -> bool:
    return not _is_frontend_file(rel)


def _norm(path: str) -> str:
    """Normaliza un path de ruta: sin query, sin trailing slash, params como {P}."""
    p = path.split("?")[0].split("#")[0].strip().rstrip("/")
    if not p:
        return "/"
    p = re.sub(r"\$\{[^}]*\}", "{P}", p)          # template literal ${id}
    p = re.sub(r":[A-Za-z_][\w-]*", "{P}", p)     # :id (Express/Go/Rails)
    p = re.sub(r"\[[^\]]*\]", "{P}", p)           # [id] (Next.js)
    p = re.sub(r"\{[^}]*\}", "{P}", p)            # {id} (FastAPI/Spring)
    return p


def _resource_words(path: str) -> list[str]:
    """Palabras clave del path (segmentos), para matcheo por recurso."""
    words: list[str] = []
    for seg in _norm(path).split("/"):
        seg = re.sub(r"\W", "", seg)
        if seg and seg != "P" and seg not in ("api",):
            words.append(seg)
    return words


def _iter_code_files(root: Path) -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in (
            ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go", ".rb",
        ):
            rel = p.relative_to(root).as_posix()
            if _is_excluded(p) or any(f"/{s}/" in f"/{rel}/" or rel.startswith(f"{s}/") for s in _SKIP_DIRS):
                continue
            out.append((p, rel))
    return out


def detect_path_mismatches(repo_path: str, *, max_findings: int = 5) -> str:
    """Compara paths del frontend con rutas del backend. Devuelve el bloque
    de hallazgos listo para inyectar en el mensaje EXECUTE (o '' si no hay)."""
    root = Path(repo_path)
    if not root.is_dir():
        return ""
    frontend_paths, backend_routes, backend_mounts, backend_resources = (
        _collect_repo_info(root)
    )
    if not backend_resources or not frontend_paths:
        return ""
    findings = _compute_findings(
        frontend_paths, backend_routes, backend_mounts, backend_resources,
        max_findings=max_findings,
    )
    if not findings:
        return ""
    return _format_findings(findings)


def _collect_repo_info(root: Path) -> tuple[list, set, list, set]:
    """(frontend_paths, backend_routes, backend_mounts, backend_resources)."""
    frontend_paths: list[tuple[str, str, int]] = []  # (path_literal, file, line)
    backend_routes: set[str] = set()
    backend_mounts: list[str] = []
    backend_resources: set[str] = set()

    for p, rel in _iter_code_files(root):
        text = _read_text(p)
        if not text:
            continue
        if _is_frontend_file(rel):
            for m in _FETCH_CALL_RE.finditer(text):
                literal = m.group(1).strip()
                if literal.startswith("/") and "://" not in literal:
                    line = text[: m.start()].count("\n") + 1
                    frontend_paths.append((literal, rel, line))
        if _is_backend_file(rel):
            for m in _MOUNT_RE.finditer(text):
                mount = m.group(1).strip().rstrip("/")
                if mount.startswith("/"):
                    backend_mounts.append(mount)
            for pat in ROUTE_PATTERNS:
                for m in pat.finditer(text):
                    groups = m.groups()
                    if pat in (_EXPRESS_RE, _NEST_RE):
                        path_str = groups[1] if len(groups) > 1 else groups[0]
                    elif len(groups) == 2:
                        # (método, path) o (path, método) según el patrón
                        g0, g1 = str(groups[0]), str(groups[1])
                        g1_is_path = g1.startswith("/")
                        g0_is_path = g0.startswith("/")
                        if g0_is_path and not g1_is_path:
                            path_str = g0
                        elif g1_is_path:
                            path_str = g1
                        else:
                            path_str = g0
                    else:
                        path_str = groups[0]
                    if path_str and str(path_str).startswith("/"):
                        backend_routes.add(_norm(str(path_str)))
                        backend_resources.update(_resource_words(str(path_str)))

    for m in backend_mounts:
        backend_resources.update(_resource_words(m))
    return frontend_paths, backend_routes, backend_mounts, backend_resources


def _compute_findings(
    frontend_paths: list[tuple[str, str, int]],
    backend_routes: set[str],
    backend_mounts: list[str],
    backend_resources: set[str],
    max_findings: int = 5,
) -> list[dict]:
    """Findings estructurados: {literal, rel, line, target}.

    ``target``: el path corregido (prefijo + literal) si el fix es grounded
    (el recurso del target existe en el backend). None si no hay fix seguro.
    """
    # Prefijo API canónico del backend: los mounts tipo app.use('/api', ...)
    # (Express/Nest) o, si no hay, el prefijo común derivado de las rutas
    # mismas (Go/Spring/Laravel declaran '/api/users' directamente).
    mount_prefixes = sorted(
        {m for m in backend_mounts if m.startswith("/api") or m == "/api"},
        key=len, reverse=True,
    )
    api_prefixes = mount_prefixes
    prefix_from_routes = False
    if not api_prefixes and any(r.startswith("/api") for r in backend_routes):
        api_prefixes = ["/api"]
        prefix_from_routes = True

    # Rutas RESUELTAS: con mounts (Express), el path correcto del frontend es
    # prefijo + literal del router; con rutas absolutas (Go/Spring), las rutas
    # YA llevan el prefijo. Un match contra el literal crudo del router es
    # FALSE NEGATIVE: '/consultas/:id' del router NO es '/api/consultas/:id'.
    resolved_routes: set[str] = set()
    if mount_prefixes:
        resolved_routes = {
            _norm(p + r) for p in mount_prefixes for r in backend_routes
        }
    elif prefix_from_routes:
        resolved_routes = set(backend_routes)
    findings: list[dict] = []
    seen: set[tuple] = set()
    for literal, rel, line in frontend_paths:
        fn = _norm(literal)
        # Dedupe por (literal, rel, line): el MISMO literal en líneas distintas
        # necesita fix por separado (el codemod reemplaza por línea — bug real:
        # getById y update con '/api/api/pacientes/${id}' en líneas distintas,
        # solo se arreglaba la primera). No dedupe por normalizado: "/pacientes"
        # y "/pacientes?page=..." son literales distintos.
        key = (literal, rel, line)
        if key in seen:
            continue
        seen.add(key)
        if not fn.startswith("/") or fn == "/":
            continue
        # ¿Ya es correcto? Con prefijos montados → ruta resuelta; sin
        # prefijos → match directo contra los literales del router.
        if api_prefixes:
            # PREFIJO DUPLICADO: '/api/api/pacientes' empieza con '/api' y el
            # check startswith() lo daba por correcto (bug real: 9 líneas
            # rotas en Medicos invisibles para el detector). Corregir a
            # '/api/pacientes' si el resto queda grounded.
            dup_corrected = None
            for p in api_prefixes:
                if fn.startswith(p) and fn[len(p):].startswith(p):
                    corrected = literal
                    while corrected.startswith(p) and corrected[len(p):].startswith(p):
                        corrected = corrected[len(p):]
                    cnorm = _norm(corrected)
                    if cnorm in resolved_routes or any(
                        w in backend_resources for w in _resource_words(cnorm)
                    ):
                        dup_corrected = corrected
                        break
            if dup_corrected is not None:
                findings.append({
                    "literal": literal,
                    "rel": rel,
                    "line": line,
                    "target": dup_corrected,
                })
                if len(findings) >= max_findings:
                    break
                continue
            if fn in resolved_routes:
                continue
            if any(fn.startswith(p) for p in api_prefixes):
                continue
        else:
            if fn in backend_routes:
                continue
        # ¿El recurso existe en el backend pero el path NO matchea?
        words = _resource_words(fn)
        if not words:
            continue
        if not any(w in backend_resources for w in words):
            continue
        # Hallazgo: el recurso existe, pero el path no coincide con ninguna
        # ruta → probablemente falta el prefijo del backend.
        target = None
        if api_prefixes:
            cand = _norm(api_prefixes[0] + literal)
            if cand in backend_routes or any(
                w in backend_resources for w in _resource_words(cand)
            ):
                target = f"{api_prefixes[0]}{literal}"
        findings.append({
            "literal": literal,
            "rel": rel,
            "line": line,
            "target": target,
        })
        if len(findings) >= max_findings:
            break
    return findings


def detect_path_mismatches(repo_path: str, *, max_findings: int = 5) -> str:
    """Compara paths del frontend con rutas del backend. Devuelve el bloque
    de hallazgos listo para inyectar en el mensaje EXECUTE (o '' si no hay)."""
    root = Path(repo_path)
    if not root.is_dir():
        return ""
    frontend_paths, backend_routes, backend_mounts, backend_resources = (
        _collect_repo_info(root)
    )
    if not backend_resources or not frontend_paths:
        return ""
    findings = _compute_findings(
        frontend_paths, backend_routes, backend_mounts, backend_resources,
        max_findings=max_findings,
    )
    if not findings:
        return ""
    return _format_findings(findings)


def _format_findings(findings: list[dict]) -> str:
    lines = []
    for f in findings:
        if f["target"]:
            lines.append(
                f"  - '{f['literal']}' ({f['rel']}:{f['line']}) → debería ser "
                f"'{f['target']}'"
            )
        else:
            lines.append(
                f"  - '{f['literal']}' ({f['rel']}:{f['line']}) no matchea "
                f"ninguna ruta del backend"
            )
    return (
        "⚠️ PATH MISMATCH DETECTED (comparación automática frontend↔backend):\n"
        + "\n".join(lines)
    )


def apply_mismatch_fixes(repo_path: str, *, max_findings: int = 25) -> str:
    """Aplica DETERMINÍSTICAMENTE los fixes de path (codemod).

    El modelo chico aplica fixes mecánicos a medias (en E2E real arregló 6
    paths y se saltó otros 6 reescribiendo el archivo entero). El SISTEMA
    reemplaza los literales exactos (file:line) con su target grounded.

    Devuelve un reporte de lo aplicado ('' si no hubo nada que aplicar).
    """
    root = Path(repo_path)
    if not root.is_dir():
        return ""
    frontend_paths, backend_routes, backend_mounts, backend_resources = (
        _collect_repo_info(root)
    )
    if not backend_resources or not frontend_paths:
        return ""
    findings = _compute_findings(
        frontend_paths, backend_routes, backend_mounts, backend_resources,
        max_findings=max_findings,
    )
    applied: list[str] = []
    for f in findings:
        target = f["target"]
        if not target or target == f["literal"]:
            continue
        p = root / f["rel"]
        try:
            lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            continue
        idx = f["line"] - 1
        if not (0 <= idx < len(lines)):
            continue
        # Reemplazo SOLO en la línea detectada. content.replace() GLOBAL era un
        # bug: el literal '/pacientes' es substring de '/api/pacientes' ya
        # arreglado → duplicaba el prefijo (/api/api/pacientes, E2E real).
        if f["literal"] in lines[idx]:
            lines[idx] = lines[idx].replace(f["literal"], target)
            p.write_text("".join(lines), encoding="utf-8")
            applied.append(
                f"'{f['literal']}' → '{target}' ({f['rel']}:{f['line']})"
            )
    if not applied:
        return ""
    return (
        "✅ PATH FIX APLICADO POR EL SISTEMA (codemod determinístico — NO lo "
        "rehagas ni lo reviertas):\n" + "\n".join(f"  - {a}" for a in applied)
    )
