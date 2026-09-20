# Correr las 25 en otra máquina (ej. qwen3.6-35b-a3b Q3_XXS)

Todo copy-paste. Al final hay UN solo comando que corre todo.

## 1. Repo y Python

```bash
git clone https://github.com/LucianoDPerez/agent-devs.git && cd agent-devs
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider  # tiene que dar verde
```

## 2. Toolchains (solo lo que falte)

```bash
which node go java cmake pytest || echo "FALTA algo de esto"
brew install catch2  # solo C++ (macOS). Linux: sudo apt install catch2
```

## 3. Dataset (30 MB)

```bash
git clone --depth 1 https://github.com/Aider-AI/polyglot-benchmark.git /tmp/polyglot-benchmark
```

## 4. Modelo (terminal 1)

```bash
llama-server -hf <tu-gguf-qwen3.6-35b-a3b-Q3_XXS> --port 8080
# Tiene que responder: curl -s http://localhost:8080/v1/models
```

## 5. Corrida (terminal 2, ~4-8 hs)

```bash
cd agent-devs
nohup .venv/bin/python benchmarks/polyglot-225/run_225.py --all \
  --model qwen3.6-35b-a3b > benchmarks/polyglot-225/results/run_all.log 2>&1 &
tail -f benchmarks/polyglot-225/results/run_all.log
```

## 6. Resultado

```bash
cat benchmarks/polyglot-225/results/summary.jsonl
```

Cada línea: `{"id","lang","passed","secs","tools"}`. Comparar contra
`benchmarks/polyglot-225/REPORTE.md` (baseline Spark 4B: 2/25).
