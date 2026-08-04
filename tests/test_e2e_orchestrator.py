"""Prueba end-to-end del orquestador multicapa de AgentDevs.

Testea clasificador (instantáneo), session flow con role switching, y
ejecución real del agente contra venture-api-academy.
"""

import json
import sys
import time
from pathlib import Path

REPO_PATH = "/Users/luciano.perez/itti/venture-api-academy"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import LLM_BASE_URL, LLM_MODEL_NAME
from llm_wrapper import LocalLLM, get_usage, reset_turn_usage
from orchestration.router import classify_intent
from core.intents import Intent
from core.roles import Role, role_for_intent, tools_for_role, load_prompt

results = []


def log_step(n: int, title: str):
    print(f"\n{'='*60}")
    print(f"  PASO {n}: {title}")
    print(f"{'='*60}")


def check(name, condition, detail=""):
    results.append({"name": name, "passed": condition, "detail": detail})
    print(f"  {'✅' if condition else '❌'} {name}" + (f" — {detail}" if detail else ""))


# ── Paso 1: Core domain ───────────────────────────────────────────────────
log_step(1, "Core domain — enums, mapeos, tools, prompts")
for intent in Intent:
    role = role_for_intent(intent)
    check(f"{intent.value} → {role.value}", role is not None)
check("analyze: 10 tools", len(tools_for_role(Role.ANALYZE)) == 10)
check("plan: 11 tools", len(tools_for_role(Role.PLAN)) == 11)
check("execute: 16 tools", len(tools_for_role(Role.EXECUTE)) == 16)
check("review: 10 tools", len(tools_for_role(Role.REVIEW)) == 10)
check("chat: 0 tools", len(tools_for_role(Role.CHAT)) == 0)
for role in Role:
    p = load_prompt(role)
    check(f"Prompt {role.value}", len(p) > 100, f"{len(p)} chars")

# ── Paso 2: Classifier (keyword-based, instant) ───────────────────────────
log_step(2, "Router — clasificador keyword-based")
test_cases = [
    ("analizame este codigo", Intent.ANALYZE),
    ("explorá la estructura del proyecto", Intent.ANALYZE),
    ("cómo funciona este módulo?", Intent.ANALYZE),
    ("hacé un plan para implementar login", Intent.PLAN),
    ("desglosá en tareas la feature X", Intent.PLAN),
    ("creá un plan para migrar a Postgres", Intent.PLAN),
    ("escribí el código del endpoint", Intent.EXECUTE),
    ("implementá el cambio y comitealo", Intent.EXECUTE),
    ("editá el archivo main.py", Intent.EXECUTE),
    ("revisá este PR por bugs", Intent.REVIEW),
    ("hacé code review del PR #5", Intent.REVIEW),
    ("buscá bugs en el último commit", Intent.REVIEW),
    ("hola como estas?", Intent.CHAT),
    ("gracias por tu ayuda", Intent.CHAT),
    (" qué hace esta función?", Intent.ANALYZE),
]
for msg, expected in test_cases:
    intent = classify_intent(None, msg)
    check(f"'{msg[:35]}'", intent == expected, f"got={intent.value}")

# ── Paso 3: Session flow con LLM real ────────────────────────────────────
log_step(3, "Session — flujo con LLM real (rol switching)")
from orchestration.session import Session

llm = LocalLLM(
    base_url=LLM_BASE_URL, model_name=LLM_MODEL_NAME,
    temperature=0.7, max_tokens=2048, api_key="not-needed",
)
session = Session(llm, REPO_PATH)
try:
    greeting = session.start()
    check("Session start", bool(greeting), f"tools={session._local_count}+{session._mcp_count}")

    # Turn 1: Chat (quick, no tools)
    reset_turn_usage()
    t0 = time.monotonic()
    session.run_turn("Hola, qué sos?")
    elapsed = time.monotonic() - t0
    out = get_usage()["turn"]["completion"]
    check("Turn 1 (chat): rol=chat", session.current_role == Role.CHAT, f"rol={session.current_role.value}")
    check("Turn 1 (chat): respuesta", out > 0, f"{out} tokens, {elapsed:.1f}s")

    # Turn 2: Analyze (uses tools, MCP)
    reset_turn_usage()
    t0 = time.monotonic()
    session.run_turn("Listá los proyectos indexados con cm__list_projects")
    elapsed = time.monotonic() - t0
    out = get_usage()["turn"]["completion"]
    check("Turn 2 (analyze): rol=analyzer", session.current_role == Role.ANALYZE, f"rol={session.current_role.value}")
    check("Turn 2 (analyze): respuesta", out > 0, f"{out} tokens, {elapsed:.1f}s")

    # Turn 3: Plan
    reset_turn_usage()
    t0 = time.monotonic()
    session.run_turn("Hacé un plan de 3 pasos para agregar un health endpoint")
    elapsed = time.monotonic() - t0
    out = get_usage()["turn"]["completion"]
    check("Turn 3 (plan): rol=planner", session.current_role == Role.PLAN, f"rol={session.current_role.value}")
    check("Turn 3 (plan): respuesta", out > 0, f"{out} tokens, {elapsed:.1f}s")

    # Turn 4: Review
    reset_turn_usage()
    t0 = time.monotonic()
    session.run_turn("Revisá el último commit buscando code smells")
    elapsed = time.monotonic() - t0
    out = get_usage()["turn"]["completion"]
    check("Turn 4 (review): rol=reviewer", session.current_role == Role.REVIEW, f"rol={session.current_role.value}")
    check("Turn 4 (review): respuesta", out > 0, f"{out} tokens, {elapsed:.1f}s")

    # Turn 5: Execute
    reset_turn_usage()
    t0 = time.monotonic()
    session.run_turn("Creá un archivo /tmp/test_e2e.md con contenido '# E2E Test OK'")
    elapsed = time.monotonic() - t0
    out = get_usage()["turn"]["completion"]
    check("Turn 5 (execute): rol=executor", session.current_role == Role.EXECUTE, f"rol={session.current_role.value}")
    check("Turn 5 (execute): respuesta", out > 0, f"{out} tokens, {elapsed:.1f}s")

    # Verify file was created
    test_file = Path("/tmp/test_e2e.md")
    check("Execute: archivo creado", test_file.exists())
    if test_file.exists():
        check("Execute: contenido correcto", "# E2E Test OK" in test_file.read_text())
        test_file.unlink()

finally:
    session.close()


# ── Resumen ───────────────────────────────────────────────────────────────
total = len(results)
passed = sum(1 for r in results if r["passed"])
print(f"\n{'='*60}")
print(f"  RESUMEN: {passed}/{total} pasaron ({passed/total*100:.0f}%)")
print(f"{'='*60}")
for r in results:
    print(f"  {'✅' if r['passed'] else '❌'} {r['name']}")

report_path = Path(__file__).parent / "test_report_e2e.json"
report_path.write_text(
    json.dumps({"summary": f"{passed}/{total} passed", "results": results}, indent=2, ensure_ascii=False),
    encoding="utf-8",
)
print(f"\n📄 {report_path}")
print(f"Repo: {REPO_PATH}")
print(f"Model: {LLM_MODEL_NAME}")