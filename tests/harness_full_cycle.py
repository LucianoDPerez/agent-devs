#!/usr/bin/env python3
"""Harness de pruebas reales: conduce la Session por ANALYZE → PLAN → EXECUTE → REVIEW
con el LLM 4B real sobre un repo sandbox escribible, capturando la respuesta final
de cada turno.

Uso:
    python tests/harness_full_cycle.py                     # ciclo completo
    python tests/harness_full_cycle.py --analyze-only      # solo un rol
    python tests/harness_full_cycle.py --plan-only
    python tests/harness_full_cycle.py --execute-only
    python tests/harness_full_cycle.py --review-only
"""

import argparse
import os
import sys
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

warnings.filterwarnings("ignore", category=DeprecationWarning)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="/var/folders/yl/sdm2x_vd6vn1hpn98r1dy7vh0000gn/T/opencode/medicos-sandbox")
    ap.add_argument("--analyze-only", action="store_true")
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--execute-only", action="store_true")
    ap.add_argument("--review-only", action="store_true")
    args = ap.parse_args()

    from config import LLM_BASE_URL, LLM_MODEL_NAME
    from llm_wrapper import LocalLLM, reset_turn_usage
    from orchestration.session import Session

    reset_turn_usage()

    llm = LocalLLM(
        base_url=LLM_BASE_URL,
        model_name=LLM_MODEL_NAME,
        temperature=0.2,
        max_tokens=3584,
        api_key="not-needed",
    )

    cached = (
        "Lenguaje: TypeScript/React (Vite). Stack: React + react-router + CSS plano.\n"
        "Arquitectura: clean-layers (presentation/components/pages, application/hooks, "
        "infrastructure/http, domain). App de gestión de pacientes: página de listado "
        "con búsqueda, paginación, alta de pacientes vía modal, detalle con consultas."
    )
    s = Session(llm, args.repo, cached_analysis=cached)
    print(f"\nSession {s.session_id} | repo={args.repo}", flush=True)
    s.start()
    print(f"MCP: {s._mcp_count} | local: {s._local_count}", flush=True)

    turns = []
    if args.analyze_only:
        turns.append(("ANALYZE", "analizá por qué no funciona el botón guardar en la vista de pacientes cuando agrego un nuevo paciente"))
    elif args.plan_only:
        turns.append(("PLAN", "hacé un plan de implementación para arreglar el botón guardar de pacientes. Escribí el plan en un archivo .md"))
    elif args.execute_only:
        turns.append(("EXECUTE", "implementá el fix del botón guardar de pacientes: el botón Guardar Paciente del modal está habilitado sin documento, y handleSubmit aborta silenciosamente. Deshabilitá el botón hasta que nombre Y documento estén completos. Editá CreatePacienteModal.tsx y comiteá con conventional commit"))
    elif args.review_only:
        turns.append(("REVIEW", "revisá los cambios del último commit buscando bugs y code smells, corriendo lint/tests si hay"))
    else:
        turns.append(("ANALYZE", "analizá por qué no funciona el botón guardar en la vista de pacientes cuando agrego un nuevo paciente"))
        turns.append(("PLAN", "hacé un plan de implementación para arreglar el botón guardar de pacientes. Escribí el plan en un archivo .md"))
        turns.append(("EXECUTE", "implementá el fix del botón guardar de pacientes según el plan. Editá los archivos necesarios y comiteá el cambio con conventional commit"))
        turns.append(("REVIEW", "revisá los cambios del último commit buscando bugs y code smells, corriendo lint/tests si hay"))

    for role_name, prompt in turns:
        print(f"\n{'='*70}\n→ [{role_name}] {prompt}\n{'='*70}", flush=True)
        s.run_turn(prompt)
        resp = (s._last_response or "").strip()
        print(f"\n--- RESPONSE [{role_name}] ---", flush=True)
        print(resp, flush=True)
        print(f"--- END {role_name} ---", flush=True)

    print("\n\nCYCLE DONE", flush=True)


if __name__ == "__main__":
    main()
