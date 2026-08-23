#!/usr/bin/env python3
"""Benchmark script: test multiple local LLMs on coding/analysis tasks.

Usage: python bench_models.py
"""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error

REPO_PATH = "~/demo/demo-academy"
AGENT_DIR = "~/agent-lucho"

MODELS = {
    "Agents-A1-4B": {
        "model": "~/.cache/huggingface/hub/models--InternScience--Agents-A1-4B-Q4_K_M-GGUF/snapshots/d92b02e27074b27542384f72bc0e72203c970f0f/Agents-A1-4B-Q4_K_M.gguf",
        "mmproj": "~/.cache/huggingface/hub/models--InternScience--Agents-A1-4B-Q4_K_M-GGUF/snapshots/d92b02e27074b27542384f72bc0e72203c970f0f/Agents-A1-4B-mmproj.gguf",
        "alias": "agents-a1-4b",
    },
    "Qwythos-9B": {
        "model": "~/.cache/huggingface/hub/models--empero-ai--Qwythos-9B-v2-GGUF/snapshots/97c11b03687f194b300efbdb4760d9bc4021b759/Qwythos-9B-v2-MTP-Q4_K_M.gguf",
        "mmproj": "~/.cache/huggingface/hub/models--empero-ai--Qwythos-9B-v2-GGUF/snapshots/97c11b03687f194b300efbdb4760d9bc4021b759/mmproj-Qwythos-9B-v2-BF16.gguf",
        "alias": "qwythos-9b",
    },
    "Falcon-H1R-7B": {
        "model": "~/.cache/huggingface/hub/models--tiiuae--Falcon-H1R-7B-GGUF/snapshots/2dc053e015a9e3c5b954aa81e00aaed24bef830f/Falcon-H1R-7B-Q4_K_M.gguf",
        "alias": "falcon-h1r-7b",
    },
}

LLAMA_BASE = [
    "llama-server",
    "--ctx-size", "32768",
    "--n-gpu-layers", "99",
    "--flash-attn", "on",
    "--cache-type-k", "q5_0",
    "--cache-type-v", "q5_0",
    "--batch-size", "2048",
    "--ubatch-size", "1024",
    "--parallel", "1",
    "--threads", "6",
    "--threads-batch", "6",
    "--prio", "2",
    "--jinja",
    "--port", "8080",
    "--host", "127.0.0.1",
    "--temp", "0.5",
    "--top-p", "0.95",
    "--top-k", "20",
    "--min-p", "0.0",
    "--presence-penalty", "1.1",
    "--repeat-penalty", "1.05",
]


def log(msg):
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  {msg}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)


def server_ready(url="http://127.0.0.1:8080/health", timeout=120):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            resp = urllib.request.urlopen(url, timeout=5)
            if resp.status == 200:
                return True
        except (urllib.error.URLError, ConnectionResetError, OSError):
            pass
        time.sleep(2)
    return False


def chat_completion(prompt, model="agents-a1-4b", max_tokens=512):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.5,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8080/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=120)
    data = json.loads(resp.read())
    return data


def tool_call_test(model_alias):
    """Testa si el modelo puede llamar tools correctamente."""
    body = json.dumps({
        "model": model_alias,
        "messages": [
            {"role": "system", "content": "Sos un asistente que usa herramientas cuando corresponde."},
            {"role": "user", "content": "Listame los archivos del directorio src/main/java del proyecto ~/demo/demo-academy"}
        ],
        "tools": [{
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "Lista archivos en un directorio",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Ruta del directorio"}
                    },
                    "required": ["path"]
                }
            }
        }],
        "tool_choice": "auto",
        "max_tokens": 1024,
        "temperature": 0.3,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8080/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=120)
    data = json.loads(resp.read())
    return data


def run_analysis(model_alias):
    """Corre el analyzer del agente contra el repo."""
    sys.path.insert(0, AGENT_DIR)
    from config import LLM_BASE_URL
    from llm_wrapper import LocalLLM
    from analyzer import run_analysis

    llm = LocalLLM(
        base_url=LLM_BASE_URL,
        model_name=model_alias,
        temperature=0.3,
        max_tokens=2048,
        api_key="not-needed",
    )

    tokens_received = []

    def on_tok(t):
        tokens_received.append(t)

    result = run_analysis(REPO_PATH, llm, on_token=on_tok, timeout=180)
    return result, "".join(tokens_received)


def test_basic_chat(model_alias):
    """1. Test básico: saludo + pregunta simple de codigo."""
    log(f"[BASIC CHAT] Preguntando sobre el repo...")
    resp = chat_completion(
        "Describime la arquitectura de este proyecto Java en 3 lineas maximas. "
        "El proyecto está en ~/demo/demo-academy",
        model=model_alias,
        max_tokens=512,
    )
    content = resp["choices"][0]["message"]["content"]
    usage = resp.get("usage", {})
    print(f"Respuesta: {content[:500]}")
    print(f"Tokens: prompt={usage.get('prompt_tokens','?')}, completion={usage.get('completion_tokens','?')}")
    return {
        "content": content,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
    }


def test_tool_calling(model_alias):
    """2. Test de tool calling: debe llamar list_files."""
    log(f"[TOOL CALLING] Probando tool calling...")
    resp = tool_call_test(model_alias)
    msg = resp["choices"][0]["message"]
    usage = resp.get("usage", {})

    has_tool_calls = bool(msg.get("tool_calls"))
    tool_name = ""
    if has_tool_calls:
        tool_name = msg["tool_calls"][0]["function"]["name"]
        tool_args = msg["tool_calls"][0]["function"]["arguments"]
        print(f"✅ Tool call detectada: {tool_name}({tool_args})")
    else:
        content = msg.get("content", "")
        print(f"❌ No tool call. Content: {content[:200]}")

    print(f"Tokens: prompt={usage.get('prompt_tokens','?')}, completion={usage.get('completion_tokens','?')}")
    return {
        "has_tool_calls": has_tool_calls,
        "tool_name": tool_name,
        "content": msg.get("content", ""),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
    }


def test_analysis(model_alias):
    """3. Test de análisis estructurado (JSON output)."""
    log(f"[ANALYSIS] Generando análisis del repo...")
    result, raw_output = run_analysis(model_alias)
    print(f"Lenguaje detectado: {result['language']}")
    print(f"Stack detectado: {result['tech_stack']}")
    print(f"Análisis: {result['analysis'][:300]}")
    return result


def main():
    results = {}

    for name, cfg in MODELS.items():
        log(f"INICIANDO TEST: {name}")
        # Matar servers viejos
        subprocess.run(["pkill", "-f", "llama-server"], capture_output=True)
        time.sleep(2)

        # Armar comando
        cmd = LLAMA_BASE + ["-m", cfg["model"], "--alias", cfg["alias"]]
        if cfg.get("mmproj"):
            cmd += ["--mmproj", cfg["mmproj"]]

        log(f"Lanzando: {' '.join(cmd[:6])} ...")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            if not server_ready(timeout=180):
                print(f"❌ {name}: no arrancó después de 180s")
                proc.kill()
                results[name] = {"error": "timeout"}
                continue

            print(f"✅ {name}: servidor listo")

            # Test 1: Chat básico
            r1 = test_basic_chat(cfg["alias"])

            # Test 2: Tool calling
            r2 = test_tool_calling(cfg["alias"])

            # Test 3: Análisis estructurado
            r3 = test_analysis(cfg["alias"])

            results[name] = {
                "basic_chat": r1,
                "tool_calling": r2,
                "analysis": {
                    "language": r3["language"],
                    "tech_stack": r3["tech_stack"],
                    "analysis": r3["analysis"],
                },
            }
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            time.sleep(2)

    # Resumen final
    log("RESULTADOS COMPARATIVOS")
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()