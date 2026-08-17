#!/usr/bin/env python3
"""Micro-benchmark de velocidad del LLM local: prefill (TTFT) + decode (tok/s).

Compara configuraciones del llama-server con el MISMO prompt realista de
EXECUTE (system + tarea ~1.5k tokens) y 300 tokens de generación. Sin el
harness: mide el modelo puro, no los retries del orquestador.

Uso:
    python benchmarks/speed_test.py [runs] [max_tokens]
"""

import json
import statistics
import sys
import time
import urllib.request

BASE = "http://localhost:8080/v1/chat/completions"

SYSTEM = (
    "Sos un agente de desarrollo experto (EXECUTE). Respondés SOLO con tool calls válidas. "
    "Tenés estas herramientas: read_file(path), write_file(path, content), edit_file(path, old_str, new_str), "
    "delete_file(path), search_code(pattern), run_lint(path), run_tests(path), run_build(path), "
    "run_install(path), run_npm_script(path, script), git_status(path), git_restore(path, files), "
    "stage_files(path, files), create_commit(path, message). "
    "Reglas: leé antes de editar, edit_file usa bloques EXACTOS del archivo, verificá con run_lint/run_tests/run_build "
    "antes de dar la tarea por terminada, nunca inventes dependencias ni versiones. "
)

USER = (
    "implementar: el repo de gestión médica tiene scaffolding de tests roto. Objetivo: que `npm run test -w backend` "
    "corra y pase. Contexto conocido: 1) el root package.json ya tiene workspaces [backend, frontend] y scripts test/lint; "
    "2) backend/jest.config.js ya existe con preset ts-jest, roots <rootDir>/src y testPathIgnorePatterns node_modules/dist; "
    "3) backend/src/tests/__tests__/server.test.ts tiene 2 tests: GET /health espera 200 y GET / espera 200 pero esa ruta "
    "no existe (devuelve 404) — hay que cambiar ese test a una ruta real o esperar 404; 4) jest se cuelga al terminar por "
    "handles abiertos de PrismaClient — hay que agregar forceExit: true a jest.config.js; 5) @types/supertest ya está "
    "instalado y el Prisma Client ya fue generado con prisma generate, no toques eso. "
    "Plan: 1) editá server.test.ts reemplazando el test de GET / por un test de 404 en una ruta inexistente; "
    "2) agregá forceExit: true en jest.config.js; 3) corré run_tests(path=\"/Users/luchop/PROYECTOS IA/Medicos\") "
    "y si falla, corregí el error concreto que reporte; 4) run_build al final. Empezá con read_file de los 2 archivos."
)


def measure(payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE, data=data, headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    first_token_at = None
    total_tokens = 0
    with urllib.request.urlopen(req, timeout=600) as resp:
        for line in resp:
            line = line.decode().strip()
            if not line.startswith("data: "):
                continue
            chunk = line[6:]
            if chunk == "[DONE]":
                break
            try:
                obj = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if first_token_at is None:
                first_token_at = time.monotonic()
            delta = obj["choices"][0]["delta"]
            # Contar TODOS los tokens emitidos (content + reasoning_content):
            # los modelos que razonan (4B) pasan minutos en reasoning antes
            # del primer content — ignorarlos falsea la velocidad real.
            if (
                delta.get("content")
                or delta.get("reasoning_content")
                or delta.get("tool_calls")
            ):
                total_tokens += 1
    end = time.monotonic()
    ttft = (first_token_at - t0) if first_token_at else None
    decode_time = (end - first_token_at) if first_token_at else 0
    return {
        "total_s": round(end - t0, 1),
        "ttft_s": round(ttft, 2) if ttft else None,
        "decode_tok_s": round(total_tokens / decode_time, 1) if decode_time else 0,
        "tokens": total_tokens,
    }


def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    max_tokens = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    payload = {
        "model": "agents-a1-4b",
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "stream": True,
    }
    prompt_tokens = len(SYSTEM.split()) + len(USER.split())
    print(f"Benchmark: prompt ~{prompt_tokens} palabras (~{int(prompt_tokens * 1.4)} tokens), "
          f"generación {max_tokens} tokens, {runs} corridas")
    results = []
    for i in range(runs):
        r = measure(payload)
        results.append(r)
        print(f"  run {i + 1}: {r}")
    med = {
        "ttft_med": round(statistics.median(r["ttft_s"] for r in results if r["ttft_s"]), 2),
        "decode_med": round(statistics.median(r["decode_tok_s"] for r in results), 1),
        "total_med": round(statistics.median(r["total_s"] for r in results), 1),
    }
    print(f"MEDIANA: {med}")


if __name__ == "__main__":
    main()
