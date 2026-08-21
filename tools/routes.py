"""Detección de endpoints HTTP por framework.

Cubre Next.js App Router, Express/Fastify/NestJS, FastAPI/Flask/Django, Go
(net/http, gin, echo, chi, fiber), Rust (axum, actix, rocket), Java/Kotlin
(Spring Boot + WebFlux, JAX-RS), PHP (Laravel, Symfony, Slim), C# (ASP.NET Core)
y Ruby (Rails, Sinatra).
"""

import re
from pathlib import Path

from langchain_core.tools import tool

from config import MAX_SEARCH_RESULT_CHARS

from ._helpers import _is_excluded, _read_text

_ROUTE_HANDLER_RE = re.compile(
    r"export\s+(?:async\s+function|const)\s+(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)",
    re.IGNORECASE,
)
_DEFAULT_HANDLER_RE = re.compile(
    r"export\s+default\s+(?:async\s+)?(?:function|\()", re.IGNORECASE
)
_EXPRESS_RE = re.compile(
    r"(?:app|router|api|fastify|server)\.(get|post|put|patch|delete|all)\s*\(\s*[\"'\`]([^\"'\`]+)[\"'\`]",
    re.IGNORECASE,
)
_NEST_RE = re.compile(
    r"@(Get|Post|Put|Delete|Patch|All)\s*\(\s*[\"'\`]([^\"'\`]+)[\"'\`]",
    re.IGNORECASE,
)
_FASTAPI_RE = re.compile(
    r"@(?:app|router)\.(get|post|put|patch|delete)\s*\(\s*[\"'\`]([^\"'\`]+)[\"'\`]",
    re.IGNORECASE,
)
_FLASK_RE = re.compile(
    r"@\w+\.route\s*\(\s*[\"'\`]([^\"'\`]+)[\"'\`]",
    re.IGNORECASE,
)
_DJANGO_RE = re.compile(
    r"\b(?:path|re_path)\s*\(\s*r?[\"'\`]([^\"'\`]+)[\"'\`]",
    re.IGNORECASE,
)
_GO_RE = re.compile(
    r"\.(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|ANY|HandleFunc|Handle)\s*\(\s*\"([^\"]+)\"",
    re.IGNORECASE,
)
_RUST_ATTR_RE = re.compile(
    r"#\[(?:[a-zA-Z_]+::)?(get|post|put|patch|delete)\s*\(\s*\"([^\"]+)\"",
    re.IGNORECASE,
)
_RUST_ROUTE_RE = re.compile(
    r"#\[route\s*\(\s*\"([^\"]+)\"\s*,\s*method\s*=\s*\"(GET|POST|PUT|PATCH|DELETE)\"",
    re.IGNORECASE,
)
_SPRING_MAPPING_RE = re.compile(
    r"@(Get|Post|Put|Delete|Patch)Mapping\s*\(\s*\"([^\"]+)\"",
    re.IGNORECASE,
)
_SPRING_REQUEST_RE = re.compile(r"@RequestMapping\s*\(([^()]*)\)")
_SPRING_REQUEST_METHOD_RE = re.compile(
    r"method\s*=\s*(?:RequestMethod\.)?(GET|POST|PUT|DELETE|PATCH)", re.IGNORECASE
)
_SPRING_REQUEST_PATH_RE = re.compile(r"(?:value|path)\s*=\s*\"([^\"]+)\"", re.IGNORECASE)
# Class-level @RequestMapping (path only, no method)
_SPRING_CLASS_REQUEST_RE = re.compile(
    r"@RequestMapping\s*\(\s*(?:value\s*=\s*)?\"([^\"]+)\"", re.IGNORECASE
)
# WebFlux functional: route(GET("/path"), ...) or route(POST("/path"), ...)
_WEBFLUX_ROUTE_RE = re.compile(
    r"route\s*\(\s*(GET|POST|PUT|DELETE|PATCH)\s*\(\s*\"([^\"]+)\"",
    re.IGNORECASE,
)
_JAXRS_METHOD_RE = re.compile(r"@(GET|POST|PUT|DELETE|PATCH)\b", re.IGNORECASE)
_JAXRS_PATH_RE = re.compile(r"@Path\s*\(\s*\"([^\"]+)\"", re.IGNORECASE)
_LARAVEL_RE = re.compile(
    r"Route::(get|post|put|patch|delete|any)\s*\(\s*[\"'\`]([^\"'\`]+)[\"'\`]",
    re.IGNORECASE,
)
_SYMFONY_RE = re.compile(
    r"#\[Route\s*\(([^)]*)\)",
    re.IGNORECASE,
)
_SLIM_RE = re.compile(
    r"->(get|post|put|patch|delete)\s*\(\s*[\"'\`]([^\"'\`]+)[\"'\`]",
    re.IGNORECASE,
)
_ASPNET_MAP_RE = re.compile(
    r"\.Map(Get|Post|Put|Patch|Delete)\s*\(\s*\"([^\"]+)\"",
    re.IGNORECASE,
)
_ASPNET_HTTP_PATH_RE = re.compile(
    r"\[Http(Get|Post|Put|Patch|Delete)\s*\(\s*\"([^\"]+)\"",
    re.IGNORECASE,
)
_ASPNET_ROUTE_ATTR_RE = re.compile(r"\[Route\(\s*\"([^\"]+)\"", re.IGNORECASE)
_RAILS_RE = re.compile(
    r"^\s*(get|post|put|patch|delete|match)\s+[\"'\`]([^\"'\`]+)[\"'\`]",
    re.MULTILINE,
)
_SYMFONY_METHODS_RE = re.compile(r"methods\s*:\s*\[([^\]]*)\]", re.IGNORECASE)


def _nextjs_route(rel: str) -> str:
    segs = rel.split("/")
    if segs and segs[0] == "app":
        segs = segs[1:]
    if segs and segs[-1] == "route.ts":
        segs = segs[:-1]
    segs = [s for s in segs if s and not (s.startswith("(") and s.endswith(")"))]
    route = "/" + "/".join(segs)
    route = re.sub(r"\[([^\]]+)\]", r":\1", route)
    return route


def _purpose_before(text: str, method: str) -> str:
    m = re.search(
        r"export\s+(?:async\s+function|const)\s+" + method,
        text,
        re.IGNORECASE,
    )
    if not m:
        return ""
    head = text[: m.start()][-1200:]
    blocks = re.findall(r"/\*\*(.*?)\*/", head, re.DOTALL)
    if blocks:
        lines = [ln.strip().lstrip("*").strip() for ln in blocks[-1].splitlines()]
        lines = [ln for ln in lines if ln]
        if lines:
            return lines[0][:140]
    comments = re.findall(r"//\s*([^\n]+)", head)
    if comments:
        c = comments[-1].strip()[:140]
        if not re.search(r"://|[;\"'\`{}()=]|\b(get|post|put|patch|delete)\b", c, re.IGNORECASE):
            return c
    return ""


def _scan_nextjs(p: Path, rel: str) -> list[str]:
    text = _read_text(p)
    if not text:
        return []
    methods = _ROUTE_HANDLER_RE.findall(text)
    if not methods and _DEFAULT_HANDLER_RE.search(text):
        methods = ["HANDLER"]
    route = _nextjs_route(rel)
    out = []
    for mth in methods:
        purpose = _purpose_before(text, mth)
        suffix = f" — {purpose}" if purpose else ""
        out.append(f"{mth:7} {route}{suffix}")
    return out


def _scan_js(p: Path) -> list[str]:
    text = _read_text(p)
    if not text:
        return []
    out = []
    for mth, r in _EXPRESS_RE.findall(text):
        out.append(f"{mth.upper():7} {r}")
    for mth, r in _NEST_RE.findall(text):
        out.append(f"{mth.upper():7} {r}")
    return out


def _scan_python(text: str) -> list[str]:
    out = []
    for mth, r in _FASTAPI_RE.findall(text):
        out.append(f"{mth.upper():7} {r}")
    for r in _FLASK_RE.findall(text):
        out.append(f"{'GET+':7} {r}  (Flask route, ver methods en el decorador)")
    for r in _DJANGO_RE.findall(text):
        out.append(f"{'HANDLER':7} {r}")
    return out


def _scan_go(text: str) -> list[str]:
    out = []
    for mth, r in _GO_RE.findall(text):
        method = mth.upper()
        if method in ("HANDLEFUNC", "HANDLE"):
            method = "HANDLER"
        out.append(f"{method:7} {r}")
    return out


def _scan_rust(text: str) -> list[str]:
    out = []
    for r, mth in _RUST_ROUTE_RE.findall(text):
        out.append(f"{mth.upper():7} {r}  (axum #[route])")
    for mth, r in _RUST_ATTR_RE.findall(text):
        out.append(f"{mth.upper():7} {r}  (#[{mth.lower()}])")
    return out


def _scan_java(text: str) -> list[str]:
    """Detecta endpoints en Java/Kotlin: Spring (annotation-based + class-level)
    y WebFlux funcional (RouterFunction)."""
    out = []

    # 1. Detectar @RequestMapping a nivel de clase
    class_prefix = ""
    class_match = _SPRING_CLASS_REQUEST_RE.search(text)
    if class_match:
        class_prefix = class_match.group(1).rstrip("/")

    # 2. @XxxMapping a nivel de método (con prefijo de clase)
    for mth, r in _SPRING_MAPPING_RE.findall(text):
        full_path = f"{class_prefix}{r}" if class_prefix else r
        out.append(f"{mth.upper():7} {full_path}")

    # 3. @RequestMapping a nivel de método (con prefijo de clase)
    for m in _SPRING_REQUEST_RE.finditer(text):
        body = m.group(1)
        rpath = _SPRING_REQUEST_PATH_RE.search(body)
        method_match = _SPRING_REQUEST_METHOD_RE.search(body)
        method = method_match.group(1).upper() if method_match else "HANDLER"
        route = rpath.group(1) if rpath else ""
        if route:
            full_path = f"{class_prefix}{route}" if class_prefix else route
            out.append(f"{method:7} {full_path}  (@RequestMapping)")

    # 4. WebFlux funcional: route(GET("/path"), ...)
    for mth, r in _WEBFLUX_ROUTE_RE.findall(text):
        out.append(f"{mth.upper():7} {r}  (WebFlux functional)")

    # 5. JAX-RS
    for m in _JAXRS_METHOD_RE.finditer(text):
        method = m.group(1).upper()
        after = text[m.end():m.end() + 400]
        pm = _JAXRS_PATH_RE.search(after)
        route = pm.group(1) if pm else ""
        if route:
            out.append(f"{method:7} {route}  (JAX-RS)")

    return out


def _scan_php(text: str) -> list[str]:
    out = []
    for mth, r in _LARAVEL_RE.findall(text):
        out.append(f"{mth.upper():7} {r}  (Laravel)")
    for m in _SYMFONY_RE.finditer(text):
        body = m.group(1)
        rm = re.search(r"[\"'\`]([^\"'\`]+)[\"'\`]", body)
        if not rm:
            continue
        route = rm.group(1)
        methods = _SYMFONY_METHODS_RE.search(body)
        if methods:
            names = [x.strip().strip("'\"") for x in methods.group(1).split(",") if x.strip()]
            label = " ".join(n.upper() for n in names)
        else:
            label = "HANDLER"
        out.append(f"{label:7} {route}  (Symfony)")
    for mth, r in _SLIM_RE.findall(text):
        out.append(f"{mth.upper():7} {r}  (Slim)")
    return out


def _scan_csharp(text: str) -> list[str]:
    out = []
    for mth, r in _ASPNET_MAP_RE.findall(text):
        out.append(f"{mth.upper():7} {r}  (minimal API)")
    for mth, r in _ASPNET_HTTP_PATH_RE.findall(text):
        out.append(f"{mth.upper():7} {r}")
    for r in _ASPNET_ROUTE_ATTR_RE.findall(text):
        out.append(f"{'HANDLER':7} {r}")
    return out


def _scan_ruby(text: str) -> list[str]:
    out = []
    for mth, r in _RAILS_RE.findall(text):
        out.append(f"{mth.upper():7} {r}")
    return out


@tool
def inspect_routes(path: str) -> str:
    """
    List all HTTP endpoints exposed in the repository in one call.
    Detects by framework: Next.js/Express/Fastify/NestJS, FastAPI/Flask/Django,
    Go (net/http, gin, echo, chi, fiber), Rust (axum, actix, rocket),
    Java/Kotlin (Spring Boot + WebFlux annotation-based & functional, JAX-RS),
    PHP (Laravel, Symfony, Slim), C# (ASP.NET Core) and Ruby (Rails, Sinatra).
    Returns one line per endpoint: METHOD /route — purpose (when available)
    (source file relative to the repo root). The file attribution answers
    "which controller/router contains X?" directly from this single call.
    Usage: inspect_routes(path="/Users/me/repo")
    """
    root = Path(path)
    if not root.exists():
        return (
            f"Path does not exist: {path}. "
            "Do not retry this path. Use list_files on a parent that exists."
        )

    groups: dict[str, list[str]] = {}
    used = 0
    truncated = False

    def collect(label: str, entries: list[str]):
        nonlocal used, truncated
        if truncated or not entries:
            return
        bucket = groups.setdefault(label, [])
        for text in entries:
            cost = len(text) + 1
            if used + cost > MAX_SEARCH_RESULT_CHARS:
                truncated = True
                return
            bucket.append(text)
            used += cost

    for p in sorted(root.rglob("*")):
        if _is_excluded(p) or not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()

        # Atribución de archivo por endpoint: la pregunta "¿qué controller
        # tiene X?" debe responderse con ESTA llamada, no con búsquedas
        # adicionales (los modelos chicos quemaban el presupuesto explorando
        # después de que la info ya estaba en pantalla).
        def _with_file(entries: list[str], _rel: str = rel) -> list[str]:
            return [f"{e} ({_rel})" for e in entries]

        if p.name == "route.ts":
            collect("NEXT.JS (App Router)", _with_file(_scan_nextjs(p, rel)))
        elif p.suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            collect("EXPRESS / FASTIFY / NESTJS", _with_file(_scan_js(p)))
        elif p.suffix == ".py":
            collect("PYTHON (FastAPI/Flask/Django)", _with_file(_scan_python(_read_text(p))))
        elif p.suffix == ".go":
            collect("GO (net/http / gin / echo / chi / fiber)", _with_file(_scan_go(_read_text(p))))
        elif p.suffix == ".rs":
            collect("RUST (axum / actix / rocket)", _with_file(_scan_rust(_read_text(p))))
        elif p.suffix in (".java", ".kt"):
            collect("JAVA / KOTLIN (Spring / JAX-RS)", _with_file(_scan_java(_read_text(p))))
        elif p.suffix == ".php":
            collect("PHP (Laravel / Symfony / Slim)", _with_file(_scan_php(_read_text(p))))
        elif p.suffix == ".cs":
            collect("C# (ASP.NET Core)", _with_file(_scan_csharp(_read_text(p))))
        elif p.suffix == ".rb":
            collect("RUBY (Rails / Sinatra)", _with_file(_scan_ruby(_read_text(p))))

    parts = []
    for label, entries in groups.items():
        parts.append(f"{label}:\n" + "\n".join(entries))
    if truncated:
        parts.append(f"... (truncado a {MAX_SEARCH_RESULT_CHARS:,} caracteres)")
    if not parts:
        return f"No se detectaron endpoints HTTP en {path}"
    return "\n\n".join(parts)
