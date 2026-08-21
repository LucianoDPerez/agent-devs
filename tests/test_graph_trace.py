"""Tests para trace_component: fallback filesystem para targets file-level.

Los routers/controllers de backend (ej. 'dashboardRoutes') no aparecen en la
búsqueda textual del grafo (cm__search_graph filtra labels File/Module), así
que trace_component debe resolverlos por filesystem y devolver source + usos
en una sola llamada.
"""

import asyncio
import json
import tempfile
from pathlib import Path

from tools.graph_trace import build_trace_component, _find_file_named

class FakeMCP:
    """Tool MCP falsa: devuelve siempre el mismo payload de texto."""

    def __init__(self, name: str, payload: str):
        self.name = name
        self.payload = payload

    async def ainvoke(self, args=None):
        return [{"type": "text", "text": self.payload}]


def _fake_tools(repo: str):
    projects = json.dumps({"projects": [{"name": "p1", "root_path": repo}]})
    empty = json.dumps({"total": 0, "results": [], "has_more": False})
    return [
        FakeMCP("cm__list_projects", projects),
        FakeMCP("cm__search_graph", empty),
    ]


def _repo_with_router() -> str:
    tmp = tempfile.mkdtemp()
    rel = Path(tmp) / "backend" / "src" / "interfaces" / "http" / "routes"
    rel.mkdir(parents=True)
    (rel / "dashboardRoutes.ts").write_text(
        'import { Router } from "express";\n'
        'const router = Router();\n'
        'router.get("/dashboard", stats);\n'
        "export default router;\n",
        encoding="utf-8",
    )
    # importador real: los usos grep-ean CONTENIDO, no filenames
    app = Path(tmp) / "backend" / "src" / "app.ts"
    app.write_text(
        'import dashboardRoutes from "./interfaces/http/routes/dashboardRoutes";\n'
        "app.use(dashboardRoutes);\n",
        encoding="utf-8",
    )
    # señuelo excluido: mismo nombre dentro de node_modules
    nm = Path(tmp) / "frontend" / "node_modules" / "fake-pkg"
    nm.mkdir(parents=True)
    (nm / "dashboardRoutes.js").write_text("export {};", encoding="utf-8")
    return tmp


class TestFindFileNamed:
    def test_encuentra_archivo_por_stem(self):
        repo = _repo_with_router()
        found = _find_file_named("dashboardRoutes", repo)
        assert found is not None
        assert found.name == "dashboardRoutes.ts"
        assert "node_modules" not in str(found)

    def test_sin_match_devuelve_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            assert _find_file_named("noExisteNada", tmp) is None


class TestTraceComponentFallback:
    def test_router_backend_resuelve_en_una_llamada(self):
        repo = _repo_with_router()
        tc = build_trace_component(_fake_tools(repo), repo)
        out = asyncio.run(tc.ainvoke({"component": "dashboardRoutes", "project": "backend"}))
        assert "RESOLUCIÓN (archivo): dashboardRoutes.ts" in out
        assert "SOURCE DE 'dashboardRoutes.ts'" in out
        assert "USOS DE 'dashboardRoutes'" in out
        assert "No se encontró" not in out

    def test_project_inventado_se_autocorrige(self):
        # 'backend' no es un slug válido: la tool lo resuelve sola del repo.
        repo = _repo_with_router()
        tc = build_trace_component(_fake_tools(repo), repo)
        out = asyncio.run(tc.ainvoke({"component": "dashboardRoutes", "project": "inventado"}))
        assert "RESOLUCIÓN (archivo)" in out
