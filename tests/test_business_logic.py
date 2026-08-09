"""Tests para business_logic: descubrimiento determinista de reglas de negocio."""

import tempfile
from pathlib import Path

import business_logic as bl


def _create_repo(files: dict[str, str]) -> str:
    tmp = tempfile.mkdtemp()
    for name, content in files.items():
        p = Path(tmp) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp


PACIENTE_TS = """export interface Paciente {
  id: number;
  nombre: string;
}

export interface CreatePacienteInput {
  nombre: string;
  documento: string;
  telefono?: string;
}
"""

REPO_IFACE_TS = """export interface IPacienteRepository {
  create(data: CreatePacienteInput): Promise<Paciente>;
  findById(id: string): Promise<Paciente | null>;
  list(query?: string): Promise<Paciente[]>;
}
"""

PY_MODEL = """from dataclasses import dataclass, field
from typing import Optional

@dataclass
class CreatePacienteInput:
    nombre: str
    documento: str
    telefono: Optional[str] = None
"""

VALIDATION_TS = """export function validate(data) {
  if (!data.nombre) throw new Error("El nombre del paciente es requerido");
  if (!data.documento) throw new Error("El documento es requerido");
  if (!/^[^@]+@[^@]+$/.test(data.email)) throw new Error("Formato de email inválido");
}
"""


class TestExtractEntitiesTs:
    def test_required_vs_optional(self):
        entities = bl.extract_entities_ts(PACIENTE_TS)
        by_name = {e["name"]: e for e in entities}
        inp = by_name["CreatePacienteInput"]
        assert inp["fields"][0] == {"name": "nombre", "required": True, "type": "string"}
        assert inp["fields"][1]["required"] is True  # documento
        assert inp["fields"][2] == {"name": "telefono", "required": False, "type": "string"}

    def test_repository_interfaces_filtered(self):
        entities = bl.extract_entities_ts(REPO_IFACE_TS)
        assert entities == []  # solo hay métodos, no datos de dominio

    def test_comments_stripped(self):
        src = "interface Foo {\n  documento: string;  // Nueva: cédula\n}"
        entities = bl.extract_entities_ts(src)
        assert entities[0]["fields"][0]["type"] == "string"


class TestExtractEntitiesPython:
    def test_dataclass_required_optional(self):
        entities = bl.extract_entities_python(PY_MODEL)
        assert len(entities) == 1
        e = entities[0]
        req = {f["name"] for f in e["fields"] if f["required"]}
        assert req == {"nombre", "documento"}
        assert e["fields"][2]["required"] is False  # telefono con default


class TestValidationMessages:
    def test_required_and_format_detected(self):
        msgs = bl.extract_validation_messages(VALIDATION_TS)
        assert "El nombre del paciente es requerido" in msgs
        assert "Formato de email inválido" in msgs


class TestDedupeStrictest:
    def test_most_restrictive_wins(self):
        """Dos versiones del mismo DTO: gana la que exige más campos."""
        repo = _create_repo({
            "backend/src/domain/entities/Paciente.ts": (
                "export interface CreatePacienteInput {\n"
                "  nombre: string;\n"
                "  documento: string | null;\n"
                "}"
            ),
            "frontend/src/domain/Paciente.ts": PACIENTE_TS,
        })
        report = bl.extract_business(repo)
        by_name = {e["name"]: e for e in report["entities"]}
        req = {f["name"] for f in by_name["CreatePacienteInput"]["fields"] if f["required"]}
        assert req == {"nombre", "documento"}


class TestFullReport:
    def test_summary_contains_business_rules(self):
        repo = _create_repo({
            "src/domain/Paciente.ts": PACIENTE_TS + VALIDATION_TS,
        })
        report = bl.build_business_report(repo, mcp_tools=None)
        assert "REQUERIDOS: nombre, documento" in report["summary"]
        assert "El nombre del paciente es requerido" in report["summary"]

    def test_format_for_prompt(self):
        report = {"summary": "# Entidades\n- CreatePacienteInput — REQUERIDOS: nombre, documento"}
        ctx = bl.format_for_prompt(report)
        assert ctx.startswith("REGLA DE NEGOCIO")
        assert "CreatePacienteInput" in ctx

    def test_empty_report_gives_no_context(self):
        assert bl.format_for_prompt({}) == ""
        assert bl.format_for_prompt({"summary": ""}) == ""


class TestPersistence:
    def test_save_load_roundtrip(self):
        repo = _create_repo({"src/domain/Paciente.ts": PACIENTE_TS})
        report = bl.build_business_report(repo, mcp_tools=None)
        bl.save_business_rules(repo, report)
        cached = bl.load_business_rules(repo)
        assert cached is not None
        rules = cached["rules_json"]
        assert rules["entities"]
        assert rules["snapshot"] == report["snapshot"]

    def test_get_business_context_cached(self):
        repo = _create_repo({"src/domain/Paciente.ts": PACIENTE_TS})
        ctx1 = bl.get_business_context(repo, force=True)
        ctx2 = bl.get_business_context(repo, force=False)
        assert ctx1 == ctx2
        assert "CreatePacienteInput" in ctx1
