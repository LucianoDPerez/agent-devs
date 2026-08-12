"""Tests del diagnóstico determinístico de runtime (puertos)."""

import tempfile
from pathlib import Path

from orchestration.runtime_diagnostics import (
    _detect_ports,
    _parse_lsof,
    detect_runtime_issues,
    runtime_status,
)


def _repo(files: dict[str, str]) -> str:
    tmp = tempfile.mkdtemp()
    for name, content in files.items():
        p = Path(tmp) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp


def _lsof_row(command: str, pid: str = "123") -> list[dict]:
    return [{"pid": pid, "command": command}]


class TestParseLsof:
    def test_parses_rows(self):
        text = (
            "COMMAND   PID   USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME\n"
            "com.docke 34242 luchop 160u  IPv6 0x6b45ecbaf55ab46      0t0  TCP *:3000 (LISTEN)\n"
            "node    20086 luchop   16u  IPv4 0x184298b3f6cd7d8c      0t0  TCP *:5173 (LISTEN)\n"
        )
        rows = _parse_lsof(text)
        assert len(rows) == 2
        assert rows[0]["command"] == "com.docke"
        assert rows[1]["pid"] == "20086"

    def test_empty(self):
        assert _parse_lsof("") == []


class TestDetectPorts:
    def test_vite_and_server_ports(self):
        repo = _repo({
            "vite.config.ts": "server: { port: 5173 },\n",
            "backend/src/server.ts": "app.listen(3000);\n",
        })
        ports = _detect_ports(repo)
        assert 5173 in ports
        assert 3000 in ports

    def test_python_and_go(self):
        repo = _repo({
            "app.py": "uvicorn.run(app, port=8000)\n",
            "main.go": 'mux := http.NewServeMux()\n":8080"\n',
        })
        ports = _detect_ports(repo)
        assert 8000 in ports

    def test_defaults_when_no_config(self):
        repo = _repo({"README.md": "hola"})
        ports = _detect_ports(repo)
        assert 3000 in ports  # defaults razonables


class TestDetectRuntimeIssues:
    def test_docker_owns_port(self):
        repo = _repo({"backend/src/server.ts": "app.listen(3000);\n"})
        report = detect_runtime_issues(
            repo, ports=[3000],
            lsof_fn=lambda port: _lsof_row("com.docke") if port == 3000 else [],
        )
        assert "RUNTIME DIAGNÓSTICO" in report
        assert "DOCKER" in report
        assert "3000" in report
        assert "docker stop" in report

    def test_nobody_listens(self):
        repo = _repo({"backend/src/server.ts": "app.listen(3000);\n"})
        report = detect_runtime_issues(
            repo, ports=[3000], lsof_fn=lambda port: [],
        )
        assert "NADIE lo escucha" in report

    def test_local_process_ok(self):
        repo = _repo({"backend/src/server.ts": "app.listen(3000);\n"})
        report = detect_runtime_issues(
            repo, ports=[3000],
            lsof_fn=lambda port: _lsof_row("node"),
        )
        assert report == "", f"proceso local normal no debe reportar: {report}"

    def test_docker_conflict_with_local(self):
        repo = _repo({"backend/src/server.ts": "app.listen(3000);\n"})
        report = detect_runtime_issues(
            repo, ports=[3000],
            lsof_fn=lambda port: [
                {"pid": "1", "command": "com.docke"},
                {"pid": "2", "command": "node"},
            ],
        )
        assert "CONFLICTO" in report


class TestRuntimeStatus:
    def test_healthy_reports_sano(self):
        """runtime_status SIEMPRE reporta: el modelo debe saber que el
        entorno fue chequeado aunque esté sano (antes el silencio lo dejaba
        buscando bugs de código inexistentes)."""
        repo = _repo({"backend/src/server.ts": "app.listen(3000);\n"})
        report = runtime_status(
            repo, ports=[3000],
            lsof_fn=lambda port: _lsof_row("node"),
        )
        assert "entorno SANO" in report

    def test_issues_forwarded(self):
        repo = _repo({"backend/src/server.ts": "app.listen(3000);\n"})
        report = runtime_status(
            repo, ports=[3000],
            lsof_fn=lambda port: _lsof_row("com.docke"),
        )
        assert "DOCKER" in report
        assert "entorno SANO" not in report
