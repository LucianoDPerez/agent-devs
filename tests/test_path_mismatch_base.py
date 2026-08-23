"""PATH FIX: entender la BASE_URL del frontend para no romper api.ts.

E2E real: Medicos — apiClient con BASE_URL='.../api' y api.ts con literales
'/api/pacientes' → el detector reescribía mal (o no detectaba) el doble
prefijo. La semántica correcta con base /api: el literal es 'la ruta del
router sin prefijo' porque la base lo aporta.
"""
import tempfile
from pathlib import Path

from orchestration.path_mismatch import detect_path_mismatches


def _mk(base_has_api: bool, literal: str) -> str:
    tmp = Path(tempfile.mkdtemp())
    (front := tmp / "frontend" / "src" / "application" / "services").mkdir(parents=True)
    (infra := tmp / "frontend" / "src" / "infrastructure" / "http").mkdir(parents=True)
    (back := tmp / "backend" / "src").mkdir(parents=True)
    base = 'const BASE_URL = "http://localhost:3000/api";\n' if base_has_api else 'const BASE_URL = "http://localhost:3000";\n'
    (infra / "apiClient.ts").write_text(base + 'export const apiClient = { get: (p) => fetch(`${BASE_URL}${p}`) };\n')
    imp = 'import { apiClient } from "../../infrastructure/http/apiClient";\n' if base_has_api else ''
    (front / "api.ts").write_text(imp + f"export const x = {{ list: () => apiClient.get(`{literal}`) }};\n")
    (back / "app.module.ts").write_text('app.use("/api", apiRoutes);\n')
    (back / "routes.ts").write_text('router.get("/pacientes", list);\n')
    return detect_path_mismatches(str(tmp))


def test_base_api_literal_duplicado_sugiere_quitar_prefijo():
    out = _mk(True, "/api/pacientes?page=1")
    assert "debería ser '/pacientes?page=1'" in out


def test_base_api_literal_correcto_sin_findings():
    assert _mk(True, "/pacientes?page=1") == ""


def test_sin_base_literal_sin_prefijo_sugiere_agregar():
    out = _mk(False, "/pacientes?page=1")
    assert "debería ser '/api/pacientes?page=1'" in out


def test_sin_base_literal_con_prefijo_correcto_sin_findings():
    assert _mk(False, "/api/pacientes?page=1") == ""
