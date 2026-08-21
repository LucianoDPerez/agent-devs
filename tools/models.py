"""Detección de modelos/entidades de datos por framework, en una llamada.

El hermano de inspect_routes para la capa de datos: cubre Prisma, SQLAlchemy,
Django ORM, TypeORM, Mongoose y Rails (db/schema.rb). Responde "¿qué
tablas/modelos hay?" sin leer archivos uno por uno — misma filosofía de alta
densidad para modelos chicos.

Output: MODEL — tabla: X — N campos → RelacionA, RelacionB (archivo).
Los detalles de columnas/tipos se leen con read_file sobre el archivo dado.
"""

import re
from pathlib import Path

from langchain_core.tools import tool

from config import MAX_SEARCH_RESULT_CHARS

from ._helpers import _is_excluded, _read_text

_PRISMA_MODEL_RE = re.compile(r"^model\s+(\w+)\s*\{", re.MULTILINE)
_PRISMA_MAP_RE = re.compile(r'@@map\("([^"]+)"\)')
_SA_CLASS_RE = re.compile(r"class\s+(\w+)\s*\(\s*(?:db\.|sqlalchemy\.)?Base\s*\):")
_SA_TABLE_RE = re.compile(r"__tablename__\s*=\s*[\"'](\w+)[\"']")
_SA_COLUMN_RE = re.compile(r"^\s+(\w+)\s*=\s*(?:db\.)?Column\(", re.MULTILINE)
_SA_REL_RE = re.compile(r"relationship\(\s*[\"'](\w+)[\"']")
_DJANGO_CLASS_RE = re.compile(r"class\s+(\w+)\s*\(\s*models\.Model\s*\):")
_DJANGO_FIELD_RE = re.compile(r"^\s+(\w+)\s*=\s*models\.", re.MULTILINE)
_DJANGO_REL_RE = re.compile(r"(?:ForeignKey|OneToOneField|ManyToManyField)\(\s*[\"']?(\w+)")
_TYPEORM_ENTITY_RE = re.compile(r"@Entity\([^)]*\)\s*(?:export\s+)?class\s+(\w+)")
_TYPEORM_COLUMN_RE = re.compile(r"@Column(?:RelationId)?\(")
_TYPEORM_REL_RE = re.compile(r"@(?:OneToOne|OneToMany|ManyToOne|ManyToMany)\(\s*\(\)\s*=>\s*(\w+)")
_MONGOOSE_MODEL_RE = re.compile(r"mongoose\.model[^(]*\(\s*[\"'](\w+)[\"']")
_RAILS_TABLE_RE = re.compile(r'create_table\s+"(\w+)"')


def _prisma_block(text: str, start: int) -> str:
    """Contenido del bloque model que abre en `start` (hasta la llave de cierre)."""
    open_brace = text.find("{", start)
    if open_brace == -1:
        return ""
    depth = 0
    for i in range(open_brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace + 1 : i]
    return ""


def _scan_prisma(text: str) -> list[str]:
    out = []
    models = _PRISMA_MODEL_RE.findall(text)
    model_names = set(models)
    for m in _PRISMA_MODEL_RE.finditer(text):
        name = m.group(1)
        body = _prisma_block(text, m.start())
        if not body:
            continue
        fields = []
        relations = []
        for ln in body.splitlines():
            f = ln.strip()
            if not f or f.startswith("//") or f.startswith("@@"):
                continue
            parts = f.split()
            if len(parts) >= 2 and re.match(r"^\w+$", parts[0]):
                fields.append(parts[0])
                if parts[1] in model_names and parts[1] != name:
                    relations.append(parts[1])
        table_m = _PRISMA_MAP_RE.search(body)
        table = f" — tabla: {table_m.group(1)}" if table_m else ""
        rel = f" → {', '.join(sorted(set(relations)))}" if relations else ""
        out.append(f"{name}{table} — {len(fields)} campos{rel}")
    return out


def _scan_sqlalchemy(text: str) -> list[str]:
    out = []
    for m in _SA_CLASS_RE.finditer(text):
        name = m.group(1)
        # cuerpo hasta el próximo class top-level o fin
        nxt = _SA_CLASS_RE.search(text, m.end())
        body = text[m.start() : nxt.start() if nxt else len(text)]
        t = _SA_TABLE_RE.search(body)
        cols = _SA_COLUMN_RE.findall(body)
        rels = sorted(set(_SA_REL_RE.findall(body)))
        table = f" — tabla: {t.group(1)}" if t else ""
        rel = f" → {', '.join(rels)}" if rels else ""
        out.append(f"{name}{table} — {len(cols)} campos{rel}")
    return out


def _scan_django(text: str) -> list[str]:
    out = []
    for m in _DJANGO_CLASS_RE.finditer(text):
        name = m.group(1)
        nxt = _DJANGO_CLASS_RE.search(text, m.end())
        body = text[m.start() : nxt.start() if nxt else len(text)]
        fields = _DJANGO_FIELD_RE.findall(body)
        rels = sorted(set(_DJANGO_REL_RE.findall(body)))
        rel = f" → {', '.join(rels)}" if rels else ""
        out.append(f"{name} — {len(fields)} campos{rel}")
    return out


def _scan_typeorm(text: str) -> list[str]:
    out = []
    for m in _TYPEORM_ENTITY_RE.finditer(text):
        name = m.group(1)
        nxt = _TYPEORM_ENTITY_RE.search(text, m.end())
        body = text[m.start() : nxt.start() if nxt else len(text)]
        cols = len(_TYPEORM_COLUMN_RE.findall(body))
        rels = sorted(set(_TYPEORM_REL_RE.findall(body)))
        rel = f" → {', '.join(rels)}" if rels else ""
        out.append(f"{name} — {cols} campos{rel}")
    return out


def _scan_mongoose(text: str) -> list[str]:
    return [f"{n} (mongoose)" for n in sorted(set(_MONGOOSE_MODEL_RE.findall(text)))]


def _scan_rails(text: str) -> list[str]:
    return [f"{t} (tabla)" for t in sorted(set(_RAILS_TABLE_RE.findall(text)))]


@tool
def inspect_models(path: str) -> str:
    """
    List all data models / database entities in the repository in one call.
    Detects by stack: Prisma (schema.prisma), SQLAlchemy, Django ORM,
    TypeORM, Mongoose, Rails (db/schema.rb).
    Returns one line per model: Model — tabla: X — N campos → relaciones (file).
    Use read_file on the returned file to see full columns/types.
    Usage: inspect_models(path="/Users/me/repo")
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

        def _with_file(entries: list[str], _rel: str = rel) -> list[str]:
            return [f"{e} ({_rel})" for e in entries]

        text = None
        if p.name == "schema.prisma":
            collect("PRISMA", _with_file(_scan_prisma(_read_text(p))))
        elif p.name == "schema.rb":
            collect("RAILS (schema.rb)", _with_file(_scan_rails(_read_text(p))))
        elif p.suffix == ".py":
            text = _read_text(p)
            sa = _scan_sqlalchemy(text)
            dj = _scan_django(text)
            if sa:
                collect("SQLALCHEMY", _with_file(sa))
            if dj:
                collect("DJANGO ORM", _with_file(dj))
        elif p.suffix in (".ts", ".tsx", ".js", ".jsx"):
            text = _read_text(p)
            te = _scan_typeorm(text)
            mg = _scan_mongoose(text)
            if te:
                collect("TYPEORM", _with_file(te))
            if mg:
                collect("MONGOOSE", _with_file(mg))

    parts = []
    for label, entries in groups.items():
        parts.append(f"{label}:\n" + "\n".join(entries))
    if truncated:
        parts.append(f"... (truncado a {MAX_SEARCH_RESULT_CHARS:,} caracteres)")
    if not parts:
        return f"No se detectaron modelos de datos en {path}"
    return "\n\n".join(parts)
