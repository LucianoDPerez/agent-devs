#!/usr/bin/env python3
"""Harness multi-caso: prueba los 5 roles (ANALYZE/PLAN/EXECUTE/REVIEW/CHAT)
con 3-5 casos de prueba cada uno, capturando la respuesta final de cada turno.

Persistencia incremental: cada caso terminado se vuelca a un JSON, de modo que
un corte (timeout/infra) no pierde los resultados previos.

Uso:
    python tests/harness_multi_role.py                 # todos los roles
    python tests/harness_multi_role.py --roles ANALYZE,PLAN
    python tests/harness_multi_role.py --roles EXECUTE --case-start 2
"""

import argparse
import json
import os
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.filterwarnings("ignore", category=DeprecationWarning)

SANDBOX = "/var/folders/yl/sdm2x_vd6vn1hpn98r1dy7vh0000gn/T/opencode/medicos-sandbox"

# Cada caso: (id, prompt). EXECUTE toca archivos DISTINTOS por caso para que
# los casos sean independientes y no colisionen entre sí.
CASES: dict[str, list[tuple[str, str]]] = {
    "ANALYZE": [
        ("a1", "analizá por qué no funciona el botón guardar en la vista de pacientes cuando agrego un nuevo paciente"),
        ("a2", "analizá cómo funciona la paginación y la búsqueda en el listado de pacientes. ¿Qué componente maneja cada cosa?"),
        ("a3", "analizá cómo está organizada la capa de datos: ¿quién hace las llamadas HTTP y cómo se conecta con los hooks de aplicación?"),
        ("a4", "analizá qué muestra la vista cuando no hay pacientes cargados. ¿Hay manejo de estado vacío o error?"),
        ("a5", "analizá el flujo de creación de una consulta médica: qué datos pide, qué valida y cómo se guarda"),
    ],
    "PLAN": [
        ("p1", "hacé un plan de implementación para arreglar el botón guardar de pacientes. Escribí el plan en un archivo plan_fix_boton.md"),
        ("p2", "hacé un plan para agregar búsqueda por documento en la vista de pacientes. Escribí el plan en plan_busqueda_documento.md"),
        ("p3", "hacé un plan para agregar un botón de edición de paciente en la lista. Escribí el plan en plan_editar_paciente.md"),
        ("p4", "hacé un plan para validar el formato de email en el modal de creación de paciente. Escribí el plan en plan_validar_email.md"),
    ],
    "EXECUTE": [
        ("e1", "implementá el fix del botón guardar de pacientes: el botón Guardar Paciente del modal está habilitado sin documento, y handleSubmit aborta silenciosamente. Deshabilitá el botón hasta que nombre Y documento estén completos. Editá CreatePacienteModal.tsx y comiteá con conventional commit"),
        ("e2", "implementá un cambio pequeño en PacienteSearch.tsx: agregá el atributo aria-label=\"Buscar paciente\" al input de búsqueda y un placeholder que diga Buscar por nombre o documento. Comiteá con conventional commit"),
        ("e3", "implementá un cambio pequeño en PacienteList.tsx: agregá el atributo aria-label=\"Lista de pacientes\" al contenedor <ul> del listado. Comiteá con conventional commit"),
    ],
    "REVIEW": [
        ("r1", "revisá los cambios del último commit buscando bugs y code smells, corriendo lint/tests si hay"),
        ("r2", "revisá el componente CreatePacienteModal.tsx buscando inconsistencias entre la validación del submit y la condición de habilitación del botón Guardar"),
        ("r3", "revisá si la capa de hooks (usePacientes, usePaciente, useConsultasList) maneja correctamente los errores de red y los estados de loading"),
    ],
    "CHAT": [
        ("c1", "¿qué hace esta aplicación? Resumí en pocas líneas"),
        ("c2", "¿cuál es la diferencia entre un hook y un servicio en esta arquitectura?"),
        ("c3", "¿tenés alguna sugerencia para mejorar el rendimiento del listado de pacientes?"),
    ],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=SANDBOX)
    ap.add_argument("--roles", default=",".join(CASES))
    ap.add_argument("--case-start", type=int, default=0, help="Índice de caso inicial (por rol)")
    ap.add_argument("--out", default="/tmp/harness_multi_role.json")
    args = ap.parse_args()

    from config import LLM_BASE_URL, LLM_MODEL_NAME
    from llm_wrapper import LocalLLM, reset_turn_usage
    from orchestration.session import Session

    reset_turn_usage()
    llm = LocalLLM(base_url=LLM_BASE_URL, model_name=LLM_MODEL_NAME,
                   temperature=0.2, max_tokens=3584, api_key="not-needed")

    cached = (
        "Lenguaje: TypeScript/React (Vite). Stack: React + react-router + CSS plano.\n"
        "Arquitectura: clean-layers (presentation/components/pages, application/hooks, "
        "infrastructure/http, domain). App de gestión de pacientes: página de listado "
        "con búsqueda, paginación, alta de pacientes vía modal, detalle con consultas."
    )
    s = Session(llm, args.repo, cached_analysis=cached)
    print(f"Session {s.session_id} | repo={args.repo}", flush=True)
    s.start()
    print(f"MCP: {s._mcp_count} | local: {s._local_count}", flush=True)

    roles = [r.strip().upper() for r in args.roles.split(",") if r.strip().upper() in CASES]
    out_path = Path(args.out)

    results: list[dict] = []
    if out_path.exists():
        try:
            results = json.loads(out_path.read_text())
        except (json.JSONDecodeError, OSError):
            results = []

    done_keys = {(r["role"], r["case"]) for r in results}

    for role in roles:
        for idx, (case_id, prompt) in enumerate(CASES[role]):
            if idx < args.case_start:
                continue
            if (role, case_id) in done_keys:
                print(f"[skip] {role}/{case_id} ya registrado", flush=True)
                continue
            print(f"\n{'='*70}\n→ [{role}] {case_id}: {prompt[:90]}\n{'='*70}", flush=True)
            start = time.monotonic()
            try:
                s.run_turn(prompt)
                resp = (s._last_response or "").strip()
                status = "ok"
            except Exception as e:
                resp = f"ERROR: {e}"
                status = "error"
            elapsed = round(time.monotonic() - start, 1)
            print(f"\n--- RESPONSE [{role}/{case_id}] ({elapsed}s) ---", flush=True)
            print(resp[:800], flush=True)
            print(f"--- END {role}/{case_id} ---", flush=True)

            results.append({
                "role": role, "case": case_id, "prompt": prompt,
                "response": resp, "elapsed_s": elapsed, "status": status,
                "ts": datetime.now().isoformat(timespec="seconds"),
            })
            out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
            print(f"[persistido] {out_path}", flush=True)

    print("\n\nALL ROLES DONE", flush=True)


if __name__ == "__main__":
    main()
