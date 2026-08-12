"""Tests del detector determinístico de path mismatches frontend↔backend."""

import tempfile
from pathlib import Path

from orchestration.path_mismatch import (
    _norm,
    apply_mismatch_fixes,
    detect_path_mismatches,
)


def _repo(files: dict[str, str]) -> str:
    tmp = tempfile.mkdtemp()
    for name, content in files.items():
        p = Path(tmp) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp


def _medicos_like_repo(frontend_path: str = "'/pacientes'") -> str:
    return _repo({
        "frontend/src/application/services/api.ts": (
            "import { apiClient } from '../../infrastructure/http/apiClient';\n"
            "export const pacientesApi = {\n"
            f"  list: () => apiClient.get<PaginatedResult<Paciente>>({frontend_path}),\n"
            "};\n"
        ),
        "frontend/src/infrastructure/http/apiClient.ts": (
            "export const apiClient = {\n"
            "  get: (path) => fetch(path),\n"
            "};\n"
        ),
        "backend/src/server.ts": (
            "import { apiRoutes } from './interfaces/http/routes';\n"
            "app.use('/api', apiRoutes);\n"
        ),
        "backend/src/interfaces/http/routes/index.ts": (
            "import { pacientesRoutes } from './pacientesRoutes';\n"
            "router.use('/pacientes', pacientesRoutes);\n"
        ),
        "backend/src/interfaces/http/routes/pacientesRoutes.ts": (
            "const router = express.Router();\n"
            "router.get('/', controller.list);\n"
            "router.get('/:id', controller.getById);\n"
        ),
    })


class TestNorm:
    def test_query_and_trailing_slash(self):
        assert _norm("/pacientes?page=1&limit=20") == "/pacientes"
        assert _norm("/pacientes/") == "/pacientes"

    def test_params_normalized(self):
        assert _norm("/pacientes/:id") == "/pacientes/{P}"
        assert _norm("/pacientes/[id]") == "/pacientes/{P}"
        assert _norm("/pacientes/${id}") == "/pacientes/{P}"


class TestDetect:
    def test_detects_missing_api_prefix(self):
        repo = _medicos_like_repo()
        out = detect_path_mismatches(repo)
        assert "PATH MISMATCH DETECTED" in out
        assert "'/pacientes'" in out
        assert "'/api/pacientes'" in out
        assert "api.ts" in out

    def test_no_findings_when_paths_aligned(self):
        repo = _medicos_like_repo(frontend_path="'/api/pacientes'")
        out = detect_path_mismatches(repo)
        assert out == "", f"esperaba sin hallazgos, got: {out[:200]}"

    def test_no_findings_without_frontend(self):
        repo = _repo({
            "backend/src/server.ts": "app.use('/api', apiRoutes);\n",
        })
        assert detect_path_mismatches(repo) == ""

    def test_ignores_node_modules(self):
        repo = _medicos_like_repo()
        p = Path(repo) / "frontend/node_modules/pkg/index.ts"
        p.parent.mkdir(parents=True)
        p.write_text("apiClient.get('/otro')\n", encoding="utf-8")
        out = detect_path_mismatches(repo)
        assert "otro" not in out


class TestApplyFixes:
    def test_applies_prefix_codemod(self):
        repo = _medicos_like_repo()
        report = apply_mismatch_fixes(repo)
        assert "PATH FIX APLICADO" in report
        assert "'/pacientes' → '/api/pacientes'" in report
        content = (
            Path(repo) / "frontend/src/application/services/api.ts"
        ).read_text()
        assert "apiClient.get<PaginatedResult<Paciente>>('/api/pacientes')" in content

    def test_already_fixed_is_noop(self):
        repo = _medicos_like_repo(frontend_path="'/api/pacientes'")
        assert apply_mismatch_fixes(repo) == ""
        content = (
            Path(repo) / "frontend/src/application/services/api.ts"
        ).read_text()
        assert "/api/pacientes" in content

    def test_template_literal_gets_prefix(self):
        repo = _medicos_like_repo(
            frontend_path="`/pacientes?page=${page}&limit=${limit}`"
        )
        report = apply_mismatch_fixes(repo)
        assert "PATH FIX APLICADO" in report
        content = (
            Path(repo) / "frontend/src/application/services/api.ts"
        ).read_text()
        assert "`/api/pacientes?page=${page}&limit=${limit}`" in content

    def test_route_matching_literal_but_missing_mount_prefix(self):
        """Un path que matchea el literal del router pero NO la ruta resuelta
        (prefijo + literal) sigue siendo un mismatch — '/consultas/:id' del
        router no es '/api/consultas/:id' del backend montado."""
        repo = _repo({
            "frontend/src/application/services/api.ts": (
                "export const consultasApi = {\n"
                "  remove: (id) => apiClient.delete(`/consultas/${id}`),\n"
                "};\n"
            ),
            "backend/src/server.ts": "app.use('/api', apiRoutes);\n",
            "backend/src/interfaces/http/routes/consultasRoutes.ts": (
                "router.delete('/consultas/:id', controller.delete);\n"
            ),
        })
        report = apply_mismatch_fixes(repo)
        assert "PATH FIX APLICADO" in report
        content = (
            Path(repo) / "frontend/src/application/services/api.ts"
        ).read_text()
        assert "apiClient.delete(`/api/consultas/${id}`)" in content

    def test_go_absolute_routes_detected(self):
        """Go declara rutas absolutas ('/api/usuarios') sin mounts Express —
        el prefijo se deriva de las rutas mismas."""
        repo = _repo({
            "frontend/src/services/api.ts": (
                "export const usuariosApi = {\n"
                "  list: () => apiClient.get<Usuario[]>('/usuarios'),\n"
                "};\n"
            ),
            "backend/main.go": (
                "package main\n"
                "mux.HandleFunc(\"/api/usuarios\", listUsuarios)\n"
                "mux.HandleFunc(\"/api/usuarios/{id}\", getUsuario)\n"
            ),
        })
        report = apply_mismatch_fixes(repo)
        assert "PATH FIX APLICADO" in report
        content = (Path(repo) / "frontend/src/services/api.ts").read_text()
        assert "apiClient.get<Usuario[]>('/api/usuarios')" in content

    def test_no_double_prefix_when_literal_is_substring(self):
        """Regresión: '/pacientes' es substring de '/api/pacientes' ya
        arreglado. El codemod debe reemplazar SOLO la línea detectada, no
        global (antes duplicaba el prefijo: /api/api/pacientes)."""
        repo = _repo({
            "frontend/src/application/services/api.ts": (
                "export const pacientesApi = {\n"
                "  list: () => apiClient.get(`/pacientes?page=${p}`),\n"
                "  getById: (id) => apiClient.get(`/pacientes/${id}`),\n"
                "  create: (data) => apiClient.post(\"/pacientes\", data),\n"
                "};\n"
            ),
            "backend/src/server.ts": "app.use('/api', apiRoutes);\n",
            "backend/src/interfaces/http/routes/index.ts": (
                "router.use('/pacientes', pacientesRoutes);\n"
            ),
            "backend/src/interfaces/http/routes/pacientesRoutes.ts": (
                "router.get('/', c.list);\nrouter.post('/', c.create);\n"
            ),
        })
        report = apply_mismatch_fixes(repo)
        assert "PATH FIX APLICADO" in report
        content = (
            Path(repo) / "frontend/src/application/services/api.ts"
        ).read_text()
        assert "/api/api" not in content, f"Doble prefijo: {content}"
        assert content.count("/api/pacientes") == 3, content

    def test_detects_existing_double_prefix(self):
        """Regresión (bug real en Medicos): '/api/api/pacientes' YA presente
        en el repo era INVISIBLE para el detector (startswith('/api') lo daba
        por correcto). Ahora debe detectarse y corregirse a '/api/pacientes'."""
        repo = _repo({
            "frontend/src/application/services/api.ts": (
                "export const pacientesApi = {\n"
                "  list: () => apiClient.get(`/api/api/pacientes?page=${p}`),\n"
                "  getById: (id) => apiClient.get(`/api/api/pacientes/${id}`),\n"
                "};\n"
            ),
            "backend/src/server.ts": "app.use('/api', apiRoutes);\n",
            "backend/src/interfaces/http/routes/index.ts": (
                "router.use('/pacientes', pacientesRoutes);\n"
            ),
            "backend/src/interfaces/http/routes/pacientesRoutes.ts": (
                "router.get('/', c.list);\n"
            ),
        })
        report = apply_mismatch_fixes(repo)
        assert "PATH FIX APLICADO" in report
        content = (
            Path(repo) / "frontend/src/application/services/api.ts"
        ).read_text()
        assert "/api/api" not in content, f"Quedó doble: {content}"
        assert content.count("/api/pacientes") == 2, content
