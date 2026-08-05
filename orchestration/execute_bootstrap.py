"""Helpers para enriquecer el mensaje de EXECUTE/REVIEW y acotar loops."""

from __future__ import annotations

import re
from pathlib import Path

from config import EXECUTE_PRELOAD_MAX_CHARS, EXECUTE_PRELOAD_MAX_FILES

# Paths absolutos a archivos de texto/tareas citados en el prompt del usuario
_ABS_FILE_RE = re.compile(
    r"(/(?:[\w.-]+/)+[\w.-]+\.(?:md|txt|json|yml|yaml))",
)
# Paths relativos tipo `lucho-plans/tasks.md` o lucho-plans/tasks.md
_REL_FILE_RE = re.compile(
    r"(?:^|[\s`\"'(])((?:[\w.-]+/)+[\w.-]+\.(?:md|txt|json|yml|yaml))",
)

# "Tarea 1", "tarea 2"
_TASK_SINGULAR_RE = re.compile(r"\btarea\s+(\d+)\b", re.IGNORECASE)
# "tareas 1, 2 y 3" / "tareas 1 y 2"
_TASKS_LIST_RE = re.compile(
    r"\btareas\s+((?:\d+(?:\s*(?:,|y|e)\s*)?)+)",
    re.IGNORECASE,
)

# Split markdown by "## ... Tarea N:" headings
_TASK_SECTION_RE = re.compile(
    r"(^##[^\n]*\bTarea\s+(\d+)\b[^\n]*\n.*?)(?=^##[^\n]*\bTarea\s+\d+\b|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)

# Criterios de aceptación: - [ ] texto / * [ ] texto
_CHECKBOX_RE = re.compile(r"^[-*]\s+\[\s*\]\s+(.+)$", re.MULTILINE)

# Líneas de hallazgos en pastes de review
_FINDING_LINE_RE = re.compile(
    r"(?i)(?:critical|cr[ií]tic|warning|falta|problema|no existe|no hay|"
    r"no se ha|debe|missing|incomplete).{0,160}"
)


def extract_requested_task_numbers(user_input: str) -> list[int]:
    """Números de tarea que el usuario pidió explícitamente (orden de aparición)."""
    seen: set[int] = set()
    nums: list[int] = []

    def _add(n: int) -> None:
        if n not in seen:
            seen.add(n)
            nums.append(n)

    for match in _TASK_SINGULAR_RE.finditer(user_input):
        _add(int(match.group(1)))

    for match in _TASKS_LIST_RE.finditer(user_input):
        for n in re.findall(r"\d+", match.group(1)):
            _add(int(n))

    return nums


def filter_task_sections(content: str, task_numbers: list[int]) -> str:
    """Deja solo las secciones ## Tarea N pedidas. Si no hay números, devuelve todo."""
    if not task_numbers:
        return content

    wanted = set(task_numbers)
    sections: list[str] = []
    for match in _TASK_SECTION_RE.finditer(content):
        num = int(match.group(2))
        if num in wanted:
            sections.append(match.group(1).strip())

    if not sections:
        # El archivo no usa el formato "Tarea N" → devolver completo
        return content

    header = (
        f"# Alcance: SOLO Tarea(s) {', '.join(str(n) for n in task_numbers)}\n"
        "Ignorá cualquier otra tarea del archivo. No implementes nada fuera de este alcance.\n\n"
    )
    return header + "\n\n---\n\n".join(sections)


def extract_checklist_items(content: str) -> list[str]:
    """Extrae ítems `- [ ] ...` de un markdown de tareas."""
    items: list[str] = []
    seen: set[str] = set()
    for match in _CHECKBOX_RE.finditer(content):
        text = match.group(1).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
    return items


def format_done_checklist(items: list[str], *, mode: str = "execute") -> str:
    """Bloque ANTES DE TERMINAR / validar AC según rol."""
    if not items:
        return ""
    lines = "\n".join(f"- [ ] {item}" for item in items)
    if mode == "review":
        return (
            "\n\nCRITERIOS DE ACEPTACIÓN (fuente de verdad del review):\n"
            f"{lines}\n"
            "Clasificá CRITICAL solo si un ítem del checklist NO está cumplido en el diff. "
            "No inventes requisitos de seguridad/perf que no estén en estos criterios.\n"
        )
    return (
        "\n\nANTES DE TERMINAR — cada ítem debe estar cumplido con evidencia en el código:\n"
        f"{lines}\n"
        "No des por finalizado si queda alguno sin implementar. "
        "Tras escribir: verificá el checklist, luego run_lint / run_tests / run_build.\n"
    )



def suggest_minimal_files(checklist: list[str], stacks: list[str] | None = None) -> str:
    """Plan de archivos mínimos según checkboxes (anti-sobreingeniería del 4B)."""
    if not checklist:
        return ""
    joined = " ".join(checklist).lower()
    stacks = stacks or []
    files: list[str] = []

    # broader: env-related checkboxes
    if any(k in joined for k in ("variable", "entorno", "env.md", "readme/env", "configuración", "configuracion")):
        files.append(".env.example — agregar SOLO las vars del AC (URL, API_KEY, timeout, etc.)")
        files.append("ENV.md en la RAÍZ del repo — documentar SOLO esas vars (no bajo src/, no volcar todo el .env)")
        if "node" in stacks:
            files.append("apps/api/src/main.ts (o bootstrap/config existente) — validación al iniciar")
        elif "python" in stacks:
            files.append("settings/config/main existente — validación al iniciar")
        elif "go" in stacks:
            files.append("cmd/*/main.go o config — validación al iniciar")
        elif "java" in stacks:
            files.append("application.yml + validación al boot")
        else:
            files.append("entry/config existente — validación al iniciar")

    if any(k in joined for k in ("adaptador", "adapter", "http", "cliente", "client", "integraci")):
        if "node" in stacks:
            files.append(
                "1 adapter/client de integración (ej. *.adapter.ts) con métodos HTTP "
                "(get/post/put/delete/request) + registrar en app.module — "
                "SIN CRUD de dominio (createBanner/updateBanner/etc.), SIN controllers"
            )
        elif "python" in stacks:
            files.append(
                "1 client/adapter de integración (métodos HTTP genéricos) — "
                "SIN routers/endpoints de negocio inventados"
            )
        elif "go" in stacks:
            files.append(
                "1 package client/adapter (métodos HTTP) — SIN handlers HTTP de negocio inventados"
            )
        elif "java" in stacks:
            files.append(
                "1 clase client/adapter (métodos HTTP) — SIN controllers inventados"
            )
        else:
            files.append("1 archivo adapter/client HTTP de integración — métodos HTTP genéricos")

    if not files:
        return ""

    lines = "\n".join(f"- {f}" for f in files)
    return (
        "\n\nPLAN DE ARCHIVOS MÍNIMOS (no creés más que esto salvo que el AC lo exija):\n"
        f"{lines}\n"
        "PROHIBIDO: inventar CRUD de dominio, controllers, secrets extra, o features fuera del checklist.\n"
        "ENV.md siempre en la RAÍZ del repositorio.\n"
    )


def extract_review_findings(user_input: str, limit: int = 12) -> list[str]:
    """Extrae líneas de hallazgos CRITICAL/WARNING/falta del paste de review."""
    findings: list[str] = []
    seen: set[str] = set()
    for raw in user_input.splitlines():
        line = raw.strip().lstrip("#*- ").strip()
        if not line or len(line) > 220:
            continue
        if not _FINDING_LINE_RE.search(line):
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        findings.append(line)
        if len(findings) >= limit:
            break
    return findings


def build_paste_correction_suffix(user_input: str) -> str:
    """Instrucción para aplicar correcciones pegadas (sin hardcodes Nest)."""
    findings = extract_review_findings(user_input)
    block = ""
    if findings:
        listed = "\n".join(f"- {f}" for f in findings)
        block = f"Hallazgos a resolver (en orden):\n{listed}\n\n"
    return (
        "\n\nINSTRUCCIÓN: "
        + block
        + "Los hallazgos YA ESTÁN en el mensaje. NO re-leas los mismos archivos en loop. "
        "Aplicá las correcciones YA con edit_file, write_file o delete_file según esos hallazgos. "
        "Si un hallazgo pide eliminar un archivo, usá delete_file(path=...). "
        "Máximo 1 read_file por archivo tocado. "
        "Cumplí cada CRITICAL/WARNING antes de terminar; "
        "no agregues refactors fuera de lo pedido."
    )


def _collect_cited_paths(user_input: str, repo_path: str | None) -> list[Path]:
    """Paths absolutos o relativos (respecto a repo_path) citados en el mensaje."""
    cited: list[Path] = []
    seen: set[str] = set()

    def _try_add(p: Path) -> None:
        try:
            key = str(p.resolve())
        except OSError:
            key = str(p)
        if key in seen:
            return
        if not p.is_file():
            return
        seen.add(key)
        cited.append(p)

    for match in _ABS_FILE_RE.finditer(user_input):
        _try_add(Path(match.group(1)))
        if len(cited) >= EXECUTE_PRELOAD_MAX_FILES:
            return cited

    if repo_path:
        root = Path(repo_path)
        for match in _REL_FILE_RE.finditer(user_input):
            rel = match.group(1)
            # Evitar re-agregar absolutos ya capturados
            if rel.startswith("/"):
                continue
            _try_add(root / rel)
            if len(cited) >= EXECUTE_PRELOAD_MAX_FILES:
                break

    return cited



# Perfiles de hints por stack. Solo se inyecta lo que exista en el repo.
# Orden de detección: marcadores de raíz (pueden coexistir en monorepos).
_HINT_ENV_FILES = (".env.example", ".env.sample", ".env.template", "env.example")

_HINT_PROFILES: dict[str, dict[str, tuple[str, ...]]] = {
    "node": {
        "list_dirs": (
            "apps/api/src",
            "apps/web/src",
            "apps/api",
            "apps/web",
            "packages",
            "src",
            "app",
            "lib",
            "server",
        ),
        "entry_files": (
            "apps/api/src/main.ts",
            "apps/api/src/app.module.ts",
            "apps/api/src/index.ts",
            "src/main.ts",
            "src/index.ts",
            "src/app.ts",
            "src/server.ts",
            "app/main.ts",
            "server.js",
            "index.js",
            "package.json",
        ),
    },
    "python": {
        "list_dirs": (
            "src",
            "app",
            "apps",
            "backend",
            "api",
            "services",
            "pkg",
        ),
        "entry_files": (
            "main.py",
            "app/main.py",
            "src/main.py",
            "api/main.py",
            "src/app.py",
            "app/__init__.py",
            "manage.py",
            "pyproject.toml",
            "requirements.txt",
        ),
    },
    "go": {
        "list_dirs": (
            "cmd",
            "internal",
            "pkg",
            "api",
            "src",
        ),
        "entry_files": (
            "main.go",
            "cmd/server/main.go",
            "cmd/api/main.go",
            "cmd/main.go",
            "go.mod",
        ),
    },
    "java": {
        "list_dirs": (
            "src/main/java",
            "src/main/resources",
            "src/main",
            "app/src/main/java",
            "src",
        ),
        "entry_files": (
            "src/main/resources/application.yml",
            "src/main/resources/application.yaml",
            "src/main/resources/application.properties",
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
            "settings.gradle",
            "settings.gradle.kts",
        ),
    },
    "generic": {
        "list_dirs": (
            "src",
            "app",
            "apps",
            "lib",
            "cmd",
            "internal",
            "pkg",
            "backend",
            "api",
        ),
        "entry_files": (),
    },
}


def detect_repo_stacks(root: Path) -> list[str]:
    """Detecta stacks presentes por marcadores de raíz (orden estable)."""
    found: list[str] = []

    def _add(name: str) -> None:
        if name not in found:
            found.append(name)

    if (root / "package.json").is_file():
        _add("node")
    if (root / "go.mod").is_file():
        _add("go")
    if any(
        (root / name).is_file()
        for name in ("pom.xml", "build.gradle", "build.gradle.kts")
    ):
        _add("java")
    # Java en submódulo típico
    if "java" not in found:
        for sub in ("app", "backend", "api", "service", "services"):
            d = root / sub
            if any(
                (d / name).is_file()
                for name in ("pom.xml", "build.gradle", "build.gradle.kts")
            ):
                _add("java")
                break
    if any(
        (root / name).is_file()
        for name in ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile")
    ):
        _add("python")

    return found or ["generic"]


def _collect_go_cmd_mains(root: Path, limit: int = 3) -> list[Path]:
    """Encuentra cmd/*/main.go sin explorar todo el árbol."""
    cmd = root / "cmd"
    if not cmd.is_dir():
        return []
    found: list[Path] = []
    try:
        for child in sorted(cmd.iterdir()):
            if not child.is_dir():
                continue
            main = child / "main.go"
            if main.is_file():
                found.append(main)
                if len(found) >= limit:
                    break
    except OSError:
        return []
    return found


def _collect_java_application_files(root: Path, limit: int = 2) -> list[Path]:
    """Busca *Application.java bajo src/main/java (acotado)."""
    bases = [root / "src" / "main" / "java", root / "app" / "src" / "main" / "java"]
    found: list[Path] = []
    for base in bases:
        if not base.is_dir():
            continue
        try:
            for hit in base.rglob("*Application.java"):
                if hit.is_file():
                    found.append(hit)
                    if len(found) >= limit:
                        return found
        except OSError:
            continue
    return found


def inject_repo_hints(repo_path: str | None, *, max_chars: int = 8_000) -> str:
    """Precarga layout + env + entrypoints según stacks detectados.

    Stack-aware (node/python/go/java/generic). Solo inyecta paths que existan.
    """
    if not repo_path:
        return ""
    root = Path(repo_path)
    if not root.is_dir():
        return ""

    stacks = detect_repo_stacks(root)
    parts: list[str] = []
    budget = max_chars

    def _take(label: str, text: str) -> None:
        nonlocal budget
        if budget <= 0 or not text:
            return
        chunk = text if len(text) <= budget else text[:budget] + "\n… (truncated)"
        budget -= len(chunk)
        parts.append(f"--- {label} (YA CARGADO — no lo vuelvas a listar/leer) ---")
        parts.append(chunk)
        parts.append(f"--- FIN {label} ---")
        parts.append("")

    _take("stacks detectados", ", ".join(stacks))

    list_dirs: list[str] = []
    entry_files: list[str] = []
    seen_dirs: set[str] = set()
    seen_files: set[str] = set()
    for stack in stacks:
        profile = _HINT_PROFILES.get(stack, _HINT_PROFILES["generic"])
        for d in profile["list_dirs"]:
            if d not in seen_dirs:
                seen_dirs.add(d)
                list_dirs.append(d)
        for f in profile["entry_files"]:
            if f not in seen_files:
                seen_files.add(f)
                entry_files.append(f)

    listed = 0
    for rel in list_dirs:
        if listed >= 2 or budget <= 0:
            break
        d = root / rel
        if not d.is_dir():
            continue
        try:
            names = sorted(p.name for p in d.iterdir())[:50]
        except OSError:
            continue
        _take(f"listing {rel}", "\n".join(names))
        listed += 1

    for name in _HINT_ENV_FILES:
        if budget <= 0:
            break
        env_ex = root / name
        if env_ex.is_file():
            try:
                _take(name, env_ex.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
            break

    loaded_entries = 0
    for rel in entry_files:
        if loaded_entries >= 3 or budget <= 0:
            break
        f = root / rel
        if not f.is_file():
            continue
        try:
            _take(rel, f.read_text(encoding="utf-8", errors="replace"))
            loaded_entries += 1
        except OSError:
            continue

    if "go" in stacks and loaded_entries < 3:
        for main in _collect_go_cmd_mains(root):
            if loaded_entries >= 3 or budget <= 0:
                break
            try:
                rel = str(main.relative_to(root))
                _take(rel, main.read_text(encoding="utf-8", errors="replace"))
                loaded_entries += 1
            except OSError:
                continue

    if "java" in stacks and loaded_entries < 3:
        for app_file in _collect_java_application_files(root):
            if loaded_entries >= 3 or budget <= 0:
                break
            try:
                rel = str(app_file.relative_to(root))
                _take(rel, app_file.read_text(encoding="utf-8", errors="replace"))
                loaded_entries += 1
            except OSError:
                continue

    if not parts:
        return ""
    return (
        "CONTEXTO DE REPO PRECARGADO (stack-aware; no gastes exploraciones en esto):\n\n"
        + "\n".join(parts)
    )




def inject_git_context(repo_path: str | None, *, max_chars: int = 6_000) -> str:
    """Captura el estado de git: archivos modificados, staged, y diff resumido.

    Así el modelo sabe qué código ya existe y qué falta implementar.
    Cuando la rama actual no es main, SIEMPRE muestra el diff contra main
    (archivos commiteados en la rama) para que el review pueda verlos.
    """
    if not repo_path:
        return ""
    root = Path(repo_path)
    if not root.is_dir():
        return ""

    import subprocess as _sub

    def _git(args: list[str]) -> str:
        try:
            r = _sub.run(
                ["git"] + args, cwd=root, capture_output=True, text=True, timeout=10,
            )
            return (r.stdout or "").strip()
        except Exception:
            return ""

    branch = _git(["branch", "--show-current"]) or "HEAD"
    status = _git(["status", "--short"])

    parts: list[str] = []
    budget = max_chars

    def _take(label: str, text: str) -> None:
        nonlocal budget
        if budget <= 0 or not text:
            return
        chunk = text if len(text) <= budget else text[:budget] + "\n… (truncated)"
        budget -= len(chunk)
        parts.append(f"--- {label} (YA CARGADO — no gastes exploraciones en esto) ---")
        parts.append(chunk)
        parts.append(f"--- FIN {label} ---")
        parts.append("")

    # Working tree changes (unstaged + staged)
    if status:
        _take(f"git status (branch: {branch})", status)
        staged_diff = _git(["diff", "--cached", "--stat"])
        if staged_diff:
            _take("git diff --cached --stat (staged)", staged_diff)
        unstaged_diff = _git(["diff", "--stat"])
        if unstaged_diff:
            _take("git diff --stat (unstaged)", unstaged_diff)

    # SIEMPRE mostrar diff contra main cuando la rama actual no es main
    # (incluye archivos commiteados en la rama + pending changes)
    if branch and branch != "main":
        diff_stat = _git(["diff", f"main...{branch}", "--stat"])
        if diff_stat:
            _take(f"git diff main...{branch} --stat (all changes on branch)", diff_stat)
        log = _git(["log", "--oneline", "-10", f"main...{branch}"])
        if log:
            _take(f"git log main...{branch} (last 10 commits)", log)
        changed = _git(["diff", f"main...{branch}", "--name-only"])
        if changed:
            _take(f"git diff main...{branch} --name-only (files to review)", changed)

    if not parts:
        return ""

    return (
        "ESTADO DE GIT (qué código ya existe — usalo para decidir qué falta):\n\n"
        + "\n".join(parts)
    )


def _build_preload_parts(
    user_input: str,
    cited: list[Path],
    task_nums: list[int],
    *,
    mode: str,
    repo_path: str | None = None,
) -> str:
    parts = [user_input, ""]
    budget = EXECUTE_PRELOAD_MAX_CHARS
    all_checklist: list[str] = []

    for p in cited:
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if p.suffix.lower() == ".md" and task_nums:
            raw = filter_task_sections(raw, task_nums)

        if p.suffix.lower() == ".md":
            all_checklist.extend(extract_checklist_items(raw))

        chunk = raw if len(raw) <= budget else raw[:budget] + "\n… (truncated)"
        budget -= len(chunk)
        scope = (
            f" (solo Tarea(s) {', '.join(map(str, task_nums))})"
            if task_nums and p.suffix.lower() == ".md"
            else ""
        )
        parts.append(
            f"--- CONTENIDO YA CARGADO DE {p}{scope} (NO lo vuelvas a leer con read_file) ---"
        )
        parts.append(chunk)
        parts.append(f"--- FIN {p.name} ---")
        parts.append("")
        if budget <= 0:
            break

    # Deduplicar checklist preservando orden
    seen_items: set[str] = set()
    checklist: list[str] = []
    for item in all_checklist:
        if item not in seen_items:
            seen_items.add(item)
            checklist.append(item)

    parts.append(format_done_checklist(checklist, mode=mode))
    if mode == "execute" and checklist:
        stacks = detect_repo_stacks(Path(repo_path)) if repo_path else []
        parts.append(suggest_minimal_files(checklist, stacks))

    if mode == "review":
        scope_rule = ""
        if task_nums:
            listed = ", ".join(f"Tarea {n}" for n in task_nums)
            scope_rule = (
                f"Validá ÚNICAMENTE {listed} contra el checklist. "
            )
        parts.append(
            "INSTRUCCIÓN OBLIGATORIA (REVIEW AC-AWARE): "
            + scope_rule
            + "El contenido de tareas YA ESTÁ ARRIBA. "
            "Revisá el diff/branch contra esos criterios. "
            "CRITICAL = checkbox no cumplido. "
            "Emítí el informe UNA vez y terminá."
        )
        return "\n".join(p for p in parts if p is not None)

    scope_rule = ""
    if task_nums:
        listed = ", ".join(f"Tarea {n}" for n in task_nums)
        scope_rule = (
            f"ALCANCE ESTRICTO: implementá ÚNICAMENTE {listed}. "
            "Cuando esas estén hechas y verificadas (checklist), PARÁ. "
            "NO implementes otras tareas aunque existan en el archivo. "
        )

    parts.append(
        "INSTRUCCIÓN OBLIGATORIA: "
        + scope_rule
        + "El contenido relevante YA ESTÁ ARRIBA (tasks + layout + .env.example + main). "
        "TU PRIMERA ACCIÓN DEBE SER write_file, edit_file o delete_file — no explores. "
        "DIFF MÍNIMO: seguí el PLAN DE ARCHIVOS MÍNIMOS; no inventes CRUD/controllers/secrets. "
        "ENV.md en la RAÍZ del repo (solo vars del AC). "
        "Adapter = integración HTTP genérica (get/post/put/delete/request), no service de dominio. "
        "Máximo 1 list_files(recursive=false) SOLO si falta un path concreto. "
        "recursive=true está PROHIBIDO. Timeout HTTP REAL si el AC lo pide. "
        "Antes de terminar: checklist verde + run_install (si falta deps) + run_lint / run_tests / run_build."
    )
    return "\n".join(p for p in parts if p is not None)


def preload_cited_files(user_input: str, repo_path: str | None = None) -> str:
    """Si el usuario cita paths a archivos existentes, inyecta contenido + checklist.

    Si pide Tarea 1 / Tarea 2, solo inyecta esas secciones del markdown
    (evita que el 4B implemente las 9 tareas del archivo).
    """
    cited = _collect_cited_paths(user_input, repo_path)
    if not cited:
        return user_input

    task_nums = extract_requested_task_numbers(user_input)
    out = _build_preload_parts(user_input, cited, task_nums, mode="execute", repo_path=repo_path)
    hints = inject_repo_hints(repo_path)
    git_ctx = inject_git_context(repo_path)
    if hints or git_ctx:
        marker = "INSTRUCCIÓN OBLIGATORIA:"
        if git_ctx:
            out = out + "\n\n" + git_ctx
        if hints:
            if marker in out:
                out = out.replace(marker, hints + "\n" + marker, 1)
            else:
                out = out + "\n\n" + hints
        # Banner al INICIO: el 4B ignora instrucciones al final y se pone a list_files
        banner = (
            "⛔ PROHIBIDO usar list_files / search_code / inspect_routes en este turno. "
            "El layout del repo, .env.example y entrypoints YA ESTÁN ABAJO "
            "(bloque CONTEXTO DE REPO PRECARGADO). "
            "Tu PRIMERA tool call DEBE ser write_file o edit_file.\n\n"
        )
        out = banner + out
    return out



def preload_for_review(user_input: str, repo_path: str | None = None) -> str:
    """Igual que preload EXECUTE pero con instrucciones de review AC-aware.

    Si el usuario no cita archivos, inyecta el git status automáticamente
    para que el review se centre en los archivos modificados.

    Cuando el working tree está clean (código ya commiteado), muestra el diff
    contra main y obliga al reviewer a LEER los archivos listados.
    """
    cited = _collect_cited_paths(user_input, repo_path)
    task_nums = extract_requested_task_numbers(user_input)
    git_ctx = inject_git_context(repo_path)

    if cited:
        out = _build_preload_parts(user_input, cited, task_nums, mode="review", repo_path=repo_path)
        if git_ctx:
            out = out + "\n\n" + git_ctx
        return out

    # Sin archivos citados: inyectar git context para review de cambios recientes
    if git_ctx:
        return (
            user_input
            + "\n\n" + git_ctx
            + "\n\nINSTRUCCIÓN OBLIGATORIA (REVIEW AC-AWARE): "
            "El git status YA ESTÁ ARRIBA. "
            "LEÉ CADA archivo listado en `git diff main...BRANCH --name-only` con read_file. "
            "Si ese bloque no existe, leé los archivos del git status. "
            "Clasificá CRITICAL / WARNING / SUGGESTION. "
            "Cita archivo:línea para cada hallazgo. "
            "Emítí el informe UNA vez y terminá."
        )

    return user_input
