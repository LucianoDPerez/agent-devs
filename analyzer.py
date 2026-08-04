"""Generación de análisis de repositorios para el caché.

`run_analysis()` recolecta contexto (estructura + manifests + README),
detecta el lenguaje de forma determinista y pide al LLM un resumen breve.
Devuelve un dict listo para `cache.save_analysis()`.
"""

import asyncio
import json
import re
from collections import Counter
from pathlib import Path

from langchain_core.messages import HumanMessage

from cache import snapshot_hash
from config import (
    EXCLUDED_DIRS,
    EXCLUDED_FILES,
    MAX_CONTEXT_CHARS,
)
from llm_wrapper import LocalLLM

# Manifiestos que identifican el lenguaje del proyecto
_MANIFEST_LANG = {
    "pom.xml": "java",
    "build.gradle": "java",
    "build.gradle.kts": "java",
    "settings.gradle": "java",
    "gradlew": "java",
    "requirements.txt": "python",
    "pyproject.toml": "python",
    "setup.py": "python",
    "Pipfile": "python",
    "poetry.lock": "python",
    "package.json": "javascript",
    "pnpm-lock.yaml": "javascript",
    "yarn.lock": "javascript",
    "go.mod": "go",
    "Cargo.toml": "rust",
    "composer.json": "php",
    "Gemfile": "ruby",
    "*.csproj": "csharp",
    "mix.exs": "elixir",
}

_EXT_LANG = {
    ".java": "java", ".kt": "kotlin",
    ".py": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".php": "php",
    ".rb": "ruby",
    ".cs": "csharp",
    ".ex": "elixir",
    ".swift": "swift",
}


def _excluded(path: Path) -> bool:
    return bool(set(path.parts) & EXCLUDED_DIRS) or path.name in EXCLUDED_FILES


def _file_tree(root: Path, limit: int = 200) -> str:
    lines = []
    count = 0
    for p in sorted(root.rglob("*")):
        if _excluded(p):
            continue
        rel = p.relative_to(root).as_posix()
        if p.is_dir():
            lines.append(f"  {rel}/")
        else:
            lines.append(f"  {rel} ({p.stat().st_size:,}b)")
        count += 1
        if count >= limit:
            lines.append("  ... (truncado)")
            break
    return "\n".join(lines)


def _read_manifest(root: Path) -> str:
    """Lee los archivos de manifiesto clave (package.json, pom.xml, etc.)."""
    parts = []
    for name in ("package.json", "pom.xml", "requirements.txt", "pyproject.toml",
                 "go.mod", "Cargo.toml", "build.gradle", "composer.json"):
        p = root / name
        if p.exists() and p.is_file():
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
                parts.append(f"### {name}\n{content[:4000]}")
            except OSError:
                continue
    return "\n\n".join(parts)


def _read_readme(root: Path) -> str:
    for name in ("README.md", "README.rst", "readme.md", "README"):
        p = root / name
        if p.exists() and p.is_file():
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
                return f"### {name}\n{content[:4000]}"
            except OSError:
                continue
    return ""


def build_context(repo_path: str) -> str:
    root = Path(repo_path)
    sections = [f"# Estructura del repositorio: {repo_path}\n" + _file_tree(root)]
    manifest = _read_manifest(root)
    if manifest:
        sections.append(manifest)
    readme = _read_readme(root)
    if readme:
        sections.append(readme)
    context = "\n\n".join(sections)
    return context[:MAX_CONTEXT_CHARS]


def detect_language(repo_path: str) -> str:
    root = Path(repo_path)
    manifest_lang = None
    ext_counts: Counter = Counter()
    for p in root.rglob("*"):
        if _excluded(p) or not p.is_file():
            continue
        if p.name in _MANIFEST_LANG and manifest_lang is None:
            manifest_lang = _MANIFEST_LANG[p.name]
        lang = _EXT_LANG.get(p.suffix)
        if lang:
            ext_counts[lang] += 1
    if manifest_lang:
        return manifest_lang
    if ext_counts:
        return ext_counts.most_common(1)[0][0]
    return "desconocido"


def detect_stack(repo_path: str) -> str:
    """Detecta el stack de forma determinista leyendo los manifests."""
    root = Path(repo_path)
    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            if deps:
                names = list(deps.keys())[:8]
                return " + ".join(names)
        except (json.JSONDecodeError, OSError):
            pass
    pom = root / "pom.xml"
    if pom.exists():
        try:
            content = pom.read_text(encoding="utf-8", errors="replace")
            props = re.findall(r"<artifactId>([\w.-]+)</artifactId>", content)
            if props:
                return " + ".join(props[:8])
        except OSError:
            pass
    return ""


def _readme_summary(repo_path: str) -> str:
    """Fallback determinista: primeras líneas de texto del README."""
    root = Path(repo_path)
    for name in ("README.md", "README.rst", "readme.md", "README"):
        p = root / name
        if p.exists() and p.is_file():
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                text = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
                return " ".join(text[:40])[:600]
            except OSError:
                continue
    return ""


def _extract_json(text: str):
    """Busca el primer bloque JSON balanceado `{...}` en el texto."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None
    return None


def stream_llm(llm: LocalLLM, messages, on_token, timeout: float = 150.0) -> str:
    """Ejecuta el LLM con streaming, notificando tokens y acumulando el texto.
    Con timeout: si el modelo no termina, devuelve lo acumulado hasta ahora."""
    accumulated = []

    def _push(tok: str):
        accumulated.append(tok)
        on_token(tok)

    async def _run():
        async for chunk in llm.astream(messages):
            if chunk.content:
                _push(str(chunk.content))

    try:
        asyncio.run(asyncio.wait_for(_run(), timeout))
    except asyncio.TimeoutError:
        on_token("\n\n⏱️  Tiempo límite alcanzado; se usa lo generado hasta ahora.")
    return "".join(accumulated)


def run_analysis(repo_path: str, llm: LocalLLM, on_token=None, timeout: float = 150.0) -> dict:
    """Ejecuta el análisis con streaming y devuelve el dict listo para cachear."""
    on_token = on_token or (lambda _tok: None)
    root = Path(repo_path)
    if not root.exists():
        raise FileNotFoundError(f"No existe el repositorio: {repo_path}")

    language = detect_language(repo_path)
    fallback_stack = detect_stack(repo_path) or language
    fallback_summary = _readme_summary(repo_path) or (
        f"Repositorio en {repo_path}. Lenguaje detectado: {language}."
    )

    context = build_context(repo_path)
    prompt = f"""Analizá el repositorio cuya estructura y archivos clave te doy a continuación.

{context}

Respondé ÚNICAMENTE con un JSON válido en este formato:
{{
  "stack": "tecnologías principales en 1 línea (ej: Spring Boot 3 + MySQL, o FastAPI + SQLite, o React + Vite + Express)",
  "summary": "resumen en español de 80-120 palabras: qué hace el proyecto, arquitectura general y estructura de carpetas"
}}
No agregues texto fuera del JSON."""

    on_token("\n📄 Generando análisis...\n")
    text = stream_llm(llm, [HumanMessage(prompt)], on_token, timeout=timeout)

    data = _extract_json(text)
    if data and data.get("summary"):
        stack = str(data.get("stack") or fallback_stack)
        summary = str(data["summary"])
    else:
        stack = fallback_stack
        summary = fallback_summary

    return {
        "path": repo_path,
        "snapshot": snapshot_hash(repo_path),
        "language": language,
        "tech_stack": stack,
        "analysis": summary,
    }
