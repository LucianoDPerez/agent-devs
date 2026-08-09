"""Descubrimiento y persistencia de la LÓGICA DE NEGOCIO de un repositorio.

A diferencia del `analysis` genérico (stack + resumen), esto extrae reglas
concretas que el agente debe respetar para no cometer errores: entidades de
dominio con sus campos REQUERIDOS vs opcionales, validaciones de negocio y
endpoints HTTP. El 4B ignora estas reglas cuando solo ve la estructura
general (ej: botón que valida solo `nombre` mientras el submit exige
`nombre`+`documento`).

Fuentes:
1. Determinista (sin LLM): escaneo de carpetas de dominio + regex de
   interfaces/types y mensajes de validación ("X es requerido").
2. Enriquecimiento por graph MCP (opcional): entidades, rutas HTTP y
   RAISES → ValidationError desde el knowledge graph.

Persistencia: tabla `business_rules` en ~/.agent-cache/repo_lens.db,
invalidada por snapshot_hash igual que `repos`.
"""

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from config import CACHE_DB, EXCLUDED_DIRS, EXCLUDED_FILES
from cache import normalize_path, snapshot_hash

# Carpetas donde suele vivir la lógica de dominio / entrada
_DOMAIN_DIR_HINTS = (
    "domain", "entities", "models", "schemas",
    "dto", "dtos", "validators", "validation",
    "use-cases", "usecases", "application",
)

# Versión del extractor: SIEMPRE que cambie la lógica de extracción/dedupe,
# incrementarla para invalidar caches previos (el snapshot_hash del repo NO
# captura cambios en business_logic.py mismo).
EXTRACTOR_VERSION = 2

_DOMAIN_FILE_HINTS = (
    "dto", "entity", "model", "schema", "validator",
    "validation", "input", "types", "domain", "rules",
)

# Extensiones que soporta el extractor determinista de tipos
_SUPPORTED_EXTS = {".ts", ".tsx", ".js", ".jsx", ".py", ".java", ".kt"}

# Patrones de mensaje de validación de negocio (cualquier lenguaje)
_REQUIRED_RE = re.compile(
    r"[Rr]equerido|obligatorio|[Nn]o puede estar vac[ií]o|must not be empty|"
    r"is required|mandatory"
)
_FORMAT_RE = re.compile(
    r"[Ff]ormato[^\"']{0,25}inv[aá]lido|[Ii]nvalid[^\"']{0,25}format|[Nn]o es v[aá]lido|"
    r"must match|invalid (email|phone|date|format)"
)


def _excluded(path: Path) -> bool:
    return bool(set(path.parts) & EXCLUDED_DIRS) or path.name in EXCLUDED_FILES


def discover_domain_files(repo_path: str, limit: int = 60) -> list[Path]:
    """Archivos candidatos a contener lógica de negocio.

    Prioriza rutas bajo carpetas de dominio (domain/entities/models/dto/...);
    si hay pocos, agrega archivos cuyo nombre sugiera dominio (Create*Input,
    *DTO, *Model, ...).
    """
    root = Path(repo_path)
    if not root.exists():
        return []
    by_dir, by_name = [], []
    for p in root.rglob("*"):
        if _excluded(p) or not p.is_file() or p.suffix not in _SUPPORTED_EXTS:
            continue
        rel = p.relative_to(root).as_posix()
        parts = set(rel.split("/"))
        if parts & set(_DOMAIN_DIR_HINTS):
            by_dir.append(p)
        elif any(h in p.name.lower() for h in _DOMAIN_FILE_HINTS):
            by_name.append(p)
        if len(by_dir) + len(by_name) > limit * 4:
            break
    return (by_dir[:limit] + by_name[: max(0, limit - len(by_dir))])[:limit]


_NOISE_TS_NAMES = ("Repository", "Controller", "Service", "Factory", "Adapter", "Handler", "Repository_")

def _is_method_like(ftype: str) -> bool:
    """Un 'campo' cuyo type contiene '(', '=>' o 'Promise' es un MÉTODO, no un dato."""
    return "(" in ftype or "=>" in ftype or ftype.startswith("Promise") or ftype.startswith("(")


def extract_entities_ts(content: str) -> list[dict]:
    """Extrae interfaces/types de TypeScript con sus campos requeridos/opcionales.

    Filtra interfaces 'de código' (Repository/Controller/Service/...), cuyos
    miembros son métodos y no datos de dominio. Prioriza DTOs y entidades.
    """
    entities = []
    for m in re.finditer(
        r"(?:export\s+)?(?:interface|type)\s+([A-Za-z_]\w*)\s*(?:extends\s+[^{]+)?\s*{([^}]*)}",
        content,
    ):
        name, body = m.group(1), m.group(2)
        if any(n in name for n in _NOISE_TS_NAMES):
            continue
        body = re.sub(r"//[^\n]*", "", body)
        fields = []
        for fm in re.finditer(r"\s*(\w+)\s*(\??):\s*([^;]+);", body):
            fname, opt, ftype = fm.group(1), fm.group(2), fm.group(3).strip()
            ftype = re.sub(r"\s+", " ", ftype)
            if _is_method_like(ftype):
                continue
            fields.append({
                "name": fname,
                "required": opt != "?",
                "type": ftype,
            })
        if fields:
            entities.append({"name": name, "kind": "interface/type", "fields": fields})
    return entities


def extract_entities_python(content: str) -> list[dict]:
    """Extrae clases dataclass/pydantic (o con tipado de campos) de Python."""
    entities = []
    for cm in re.finditer(
        r"(?:@dataclass[^\n]*\n\s*)?class\s+(\w+)[\s\S]*?(?:\nclass\s+|\Z)",
        content,
    ):
        block = cm.group(0)
        if "@dataclass" not in block and "BaseModel" not in block:
            continue
        name = cm.group(1)
        body = block.split(":", 1)[1] if ":" in block else ""
        fields = []
        for fm in re.finditer(r"[ \t]+(\w+)\s*:\s*([^#\n]+?)(?:\s*=\s*([^#\n]*))?\s*\n", body):
            fname, ftype = fm.group(1), fm.group(2).strip()
            has_default = bool(fm.group(3) and fm.group(3).strip())
            if ftype.startswith("Optional[") or ftype.endswith("| None") or ftype.startswith("str | None"):
                has_default = True
            fields.append({"name": fname, "required": not has_default, "type": ftype})
        if fields:
            entities.append({"name": name, "kind": "model", "fields": fields})
    return entities


def extract_validation_messages(content: str) -> list[str]:
    """Mensajes de validación de negocio ("X es requerido", "formato inválido")."""
    msgs = []
    for m in re.finditer(r"""["']([^"']{8,120})["']""", content):
        text = m.group(1)
        if (_REQUIRED_RE.search(text) or _FORMAT_RE.search(text)) and not text.startswith(("http", "./", "../")):
            msgs.append(text)
    return msgs[:20]


def extract_business(repo_path: str) -> dict:
    """Descubrimiento determinista (sin LLM ni MCP). Devuelve entidades y validaciones."""
    entities: list[dict] = []
    validations: list[str] = []
    for p in discover_domain_files(repo_path):
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if p.suffix in (".ts", ".tsx", ".js", ".jsx"):
            entities += extract_entities_ts(content)
        elif p.suffix == ".py":
            entities += extract_entities_python(content)
        validations += extract_validation_messages(content)
    # dedupe por nombre: si hay versiones duplicadas (ej: frontend/backend),
    # quedarse con la MÁS RESTRICTIVA (más campos requeridos) — la regla de
    # negocio es el requisito más fuerte, no el más laxo.
    by_name: dict[str, list[dict]] = {}
    for e in entities:
        by_name.setdefault(e["name"], []).append(e)
    unique = []
    for name, versions in by_name.items():
        best = max(versions, key=lambda v: sum(1 for f in v["fields"] if f["required"]))
        unique.append(best)
    entities = unique
    validations = list(dict.fromkeys(validations))
    return {"entities": entities, "validations": validations}


def _mcp_call_sync(tool, kwargs: dict, timeout: float = 30.0):
    """Invoca un tool MCP desde contexto síncrono (thread con loop propio)."""
    async def _run():
        res = await tool.ainvoke(kwargs)
        text = "".join(
            b.get("text", "") for b in res if isinstance(b, dict) and b.get("type") == "text"
        )
        return text
    loop = asyncio.new_event_loop()
    try:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(loop.run_until_complete, _run()).result(timeout=timeout)
    finally:
        loop.close()


def enrich_from_graph(mcp_tools: list, repo_path: str) -> dict:
    """Agrega entidades + rutas + validaciones desde el knowledge graph MCP."""
    if not mcp_tools:
        return {}
    by = {t.name: t for t in mcp_tools}
    lp = by.get("cm__list_projects")
    qg = by.get("cm__query_graph")
    if not (lp and qg):
        return {}
    try:
        raw = _mcp_call_sync(lp, {})
        data = json.loads(raw)
        root = normalize_path(repo_path)
        proj = next(
            (p["name"] for p in data.get("projects", []) if normalize_path(p.get("root_path", "")) == root),
            None,
        )
        if not proj:
            return {}
        out: dict = {"graph_entities": [], "routes": []}
        r = _mcp_call_sync(qg, {"query": "MATCH (n) WHERE (n:Interface OR n:Class OR n:Type) AND NOT n.is_test RETURN n.name LIMIT 25", "project": proj})
        try:
            rows = json.loads(r).get("rows", [])
            out["graph_entities"] = [row[0] for row in rows][:25]
        except (json.JSONDecodeError, KeyError):
            pass
        r2 = _mcp_call_sync(qg, {"query": "MATCH (n:Route) RETURN n.name, n.method LIMIT 30", "project": proj})
        try:
            rows = json.loads(r2).get("rows", [])
            out["routes"] = [
                f"{row[1].upper() if row[1] else 'HTTP'} {row[0]}" for row in rows
            ][:30]
        except (json.JSONDecodeError, KeyError):
            pass
        return out
    except Exception:
        return {}


def build_business_report(repo_path: str, mcp_tools: list = None) -> dict:
    """Combina descubrimiento determinista + graph MCP en un reporte estructurado."""
    det = extract_business(repo_path)
    graph = enrich_from_graph(mcp_tools or [], repo_path)
    report = {
        "snapshot": snapshot_hash(repo_path),
        "extractor_version": EXTRACTOR_VERSION,
        "entities": det["entities"],
        "validations": det["validations"],
        "graph_entities": graph.get("graph_entities", []),
        "routes": graph.get("routes", []),
        "summary": "",
    }
    lines = []
    entities = det["entities"]
    if entities:
        lines.append("# Entidades de dominio")
        for e in entities:
            req = [f["name"] for f in e["fields"] if f["required"]]
            opt = [f["name"] for f in e["fields"] if not f["required"]]
            parts = []
            if req:
                parts.append("REQUERIDOS: " + ", ".join(req))
            if opt:
                parts.append("opcionales: " + ", ".join(opt))
            lines.append(f"- {e['name']} ({e['kind']}) — {'; '.join(parts)}")
    if det["validations"]:
        lines.append("\n# Validaciones de negocio detectadas")
        for v in det["validations"]:
            lines.append(f"- \"{v}\"")
    if report["routes"]:
        lines.append("\n# Endpoints HTTP")
        lines += [f"- {r}" for r in report["routes"]]
    if graph.get("graph_entities"):
        lines.append("\n# Entidades indexadas en el graph")
        lines.append(", ".join(report["graph_entities"]))
    report["summary"] = "\n".join(lines)
    return report


def format_for_prompt(report: dict) -> str:
    """Convierte el reporte en el bloque de contexto que se inyecta al agente."""
    if not report or not report.get("summary"):
        return ""
    return (
        "REGLA DE NEGOCIO (entidades, campos requeridos y validaciones "
        "detectadas en el repo — RESPETALAS, no las asumas):\n"
        f"{report['summary']}"
    )


# ── Persistencia (tabla business_rules en repo_lens.db) ────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_business_table() -> None:
    import sqlite3
    conn = sqlite3.connect(CACHE_DB)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS business_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            snapshot_hash TEXT,
            rules_json TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def save_business_rules(repo_path: str, report: dict) -> None:
    _ensure_business_table()
    import sqlite3
    conn = sqlite3.connect(CACHE_DB)
    now = _now()
    conn.execute(
        """
        INSERT INTO business_rules (path, snapshot_hash, rules_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            snapshot_hash = excluded.snapshot_hash,
            rules_json = excluded.rules_json,
            updated_at = excluded.updated_at
        """,
        (normalize_path(repo_path), report["snapshot"], json.dumps(report, ensure_ascii=False), now, now),
    )
    conn.commit()
    conn.close()


def load_business_rules(repo_path: str) -> dict | None:
    _ensure_business_table()
    import sqlite3
    conn = sqlite3.connect(CACHE_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM business_rules WHERE path = ?",
        (normalize_path(repo_path),),
    ).fetchone()
    conn.close()
    if not row:
        return None
    data = dict(row)
    try:
        data["rules_json"] = json.loads(data["rules_json"] or "{}")
    except json.JSONDecodeError:
        data["rules_json"] = {}
    return data


def get_business_context(repo_path: str, mcp_tools: list = None, force: bool = False) -> str:
    """Bloque de contexto de lógica de negocio listo para inyectar.

    Usa el caché si está fresco; si no, regenera y persiste.
    """
    repo_path = normalize_path(repo_path)
    cached = load_business_rules(repo_path) if not force else None
    cache_fresh = (
        cached
        and cached.get("snapshot_hash") == snapshot_hash(repo_path)
        and cached.get("rules_json", {}).get("extractor_version") == EXTRACTOR_VERSION
    )
    if cache_fresh:
        report = cached.get("rules_json") or {}
    else:
        report = build_business_report(repo_path, mcp_tools)
        try:
            save_business_rules(repo_path, report)
        except Exception:
            pass
    return format_for_prompt(report)
