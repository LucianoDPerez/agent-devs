"""Tests para el repo map (símbolos) y el mensaje de commit preguntado."""

import tempfile
from pathlib import Path

from orchestration.execute_bootstrap import _extract_symbols, build_symbol_map
from orchestration.session import _build_commit_message


def _repo(files: dict[str, str]) -> str:
    tmp = tempfile.mkdtemp()
    for name, content in files.items():
        p = Path(tmp) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp


class TestSymbolMap:
    def test_extract_ts_symbols(self):
        p = Path(tempfile.mkdtemp()) / "usePacientes.ts"
        p.write_text(
            "export function usePacientes() {\n"
            "  const load = async () => {}\n"
            "  return { load }\n"
            "}\n"
            "export const fetchPacientes = async () => {}\n"
            "export class PacienteService {}\n",
            encoding="utf-8",
        )
        syms = _extract_symbols(p)
        names = " | ".join(syms)
        assert "usePacientes" in names
        assert "fetchPacientes" in names
        assert "PacienteService" in names

    def test_extract_python_go_java(self):
        p = Path(tempfile.mkdtemp()) / "service.py"
        p.write_text(
            "import os\n\n"
            "def list_pacientes(repo):\n"
            "    pass\n\n"
            "class PacienteService:\n"
            "    pass\n",
            encoding="utf-8",
        )
        syms = " | ".join(_extract_symbols(p))
        assert "list_pacientes" in syms
        assert "PacienteService" in syms

    def test_build_symbol_map(self):
        repo = _repo({
            "src/hooks/usePacientes.ts": (
                "export function usePacientes() {\n  return {}\n}\n"
            ),
            "src/pages/PacientesPage.tsx": (
                "export function PacientesPage() {\n  return null\n}\n"
            ),
            "src/utils/helper.ts": "export const helper = () => 1\n",
        })
        out = build_symbol_map(repo, ["src/hooks", "src/pages", "src/utils"])
        assert "usePacientes" in out
        assert "PacientesPage" in out
        assert "hooks/usePacientes.ts" in out
        assert "utils/helper.ts" not in out or "helper" in out

    def test_build_symbol_map_ignores_excluded(self):
        repo = _repo({
            "src/app.ts": "export function main() {}\n",
            "src/node_modules/x.ts": "export function evil() {}\n",
        })
        out = build_symbol_map(repo, ["src"])
        assert "main" in out
        assert "evil" not in out


class TestCommitMessage:
    def test_fix_kind_for_bug_requests(self):
        assert _build_commit_message("solucionar el error al cargar pacientes").startswith("fix: ")

    def test_feat_kind_for_implement_requests(self):
        assert _build_commit_message("implementá el login con JWT").startswith("feat: ")

    def test_has_summary_and_no_crash(self):
        msg = _build_commit_message("hacé lo que sea")
        assert len(msg) > 6
        assert "hacé lo que sea" in msg
