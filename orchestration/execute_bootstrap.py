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
        "Aplicá las correcciones YA con edit_file/write_file según esos hallazgos. "
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


def _build_preload_parts(
    user_input: str,
    cited: list[Path],
    task_nums: list[int],
    *,
    mode: str,
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
        + "El contenido relevante YA ESTÁ ARRIBA. Implementá YA con write_file/edit_file. "
        "Máximo 2 exploraciones (list_files recursive=false / search_code en subpath). "
        "No re-leas el archivo de tareas. No explores el repo entero. "
        "Wire módulos/providers si creás servicios. "
        "Timeout real en HTTP saliente si el AC lo pide. "
        "Si faltan vars nuevas y no hay README.md, documentá en ENV.md. "
        "Antes de terminar: checklist verde + run_lint / run_tests / run_build."
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
    return _build_preload_parts(user_input, cited, task_nums, mode="execute")


def preload_for_review(user_input: str, repo_path: str | None = None) -> str:
    """Igual que preload EXECUTE pero con instrucciones de review AC-aware."""
    cited = _collect_cited_paths(user_input, repo_path)
    if not cited:
        return user_input

    task_nums = extract_requested_task_numbers(user_input)
    return _build_preload_parts(user_input, cited, task_nums, mode="review")
