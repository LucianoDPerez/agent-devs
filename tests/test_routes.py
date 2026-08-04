"""Tests para inspect_routes: Spring WebFlux, class-level @RequestMapping."""

import tempfile
from pathlib import Path

from tools.routes import inspect_routes


def _create_repo(files: dict[str, str]) -> str:
    """Crea un repo temporal con los archivos dados."""
    tmp = tempfile.mkdtemp()
    for name, content in files.items():
        p = Path(tmp) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp


class TestSpringClassLevel:
    """@RequestMapping a nivel de clase + method-level."""

    def test_class_prefix_combined(self):
        repo = _create_repo({
            "src/UserController.java": """
@RestController
@RequestMapping("/api/users")
public class UserController {
    @GetMapping("/profile")
    public User getProfile() { }

    @PostMapping("/")
    public User create() { }

    @DeleteMapping("/{id}")
    public void delete() { }
}
""",
        })
        result = inspect_routes.invoke({"path": repo})
        assert "GET     /api/users/profile" in result
        assert "POST    /api/users/" in result
        assert "DELETE  /api/users/{id}" in result

    def test_class_prefix_with_method_request_mapping(self):
        repo = _create_repo({
            "src/SearchController.java": """
@RestController
@RequestMapping("/api/search")
public class SearchController {
    @RequestMapping(value = "/users", method = RequestMethod.GET)
    public List<User> searchUsers() { }
}
""",
        })
        result = inspect_routes.invoke({"path": repo})
        assert "GET     /api/search/users" in result

    def test_no_class_prefix(self):
        repo = _create_repo({
            "src/SimpleController.java": """
@RestController
public class SimpleController {
    @GetMapping("/health")
    public String health() { }
}
""",
        })
        result = inspect_routes.invoke({"path": repo})
        assert "GET     /health" in result


class TestWebFluxFunctional:
    """WebFlux funcional: route(GET(...), ...)."""

    def test_route_get(self):
        repo = _create_repo({
            "src/RouterConfig.java": """
public class RouterConfig {
    public RouterFunction<ServerResponse> routes() {
        return route(GET("/actuator/health"), req -> ServerResponse.ok().build());
    }
}
""",
        })
        result = inspect_routes.invoke({"path": repo})
        assert "GET     /actuator/health" in result
        assert "WebFlux functional" in result

    def test_multiple_routes(self):
        repo = _create_repo({
            "src/WebConfig.java": """
public class WebConfig {
    public RouterFunction<ServerResponse> routes() {
        return route(GET("/events"), req -> ServerResponse.ok().build())
            .andRoute(POST("/webhook"), req -> ServerResponse.ok().build())
            .andRoute(DELETE("/events/{id}"), req -> ServerResponse.ok().build());
    }
}
""",
        })
        result = inspect_routes.invoke({"path": repo})
        assert "GET     /events" in result
        assert "POST    /webhook" in result
        assert "DELETE  /events/{id}" in result

    def test_webflux_with_class_prefix(self):
        """WebFlux funcional NO hereda @RequestMapping de clase — son paradigmas distintos."""
        repo = _create_repo({
            "src/ApiRouter.java": """
@RestController
@RequestMapping("/api/v2")
public class ApiRouter {
    public RouterFunction<ServerResponse> routes() {
        return route(GET("/items"), req -> ServerResponse.ok().build());
    }
}
""",
        })
        result = inspect_routes.invoke({"path": repo})
        assert "GET     /items" in result


class TestExistingFrameworks:
    """Verificar que los frameworks existentes siguen funcionando."""

    def test_nextjs(self):
        repo = _create_repo({
            "app/api/users/route.ts": """
export async function GET(request: Request) {
    return Response.json({ users: [] });
}
export async function POST(request: Request) {
    return Response.json({ created: true });
}
""",
        })
        result = inspect_routes.invoke({"path": repo})
        assert "GET     /api/users" in result
        assert "POST    /api/users" in result

    def test_fastapi(self):
        repo = _create_repo({
            "main.py": """
from fastapi import FastAPI
app = FastAPI()

@app.get("/items")
def list_items(): pass

@app.post("/items")
def create_item(): pass
""",
        })
        result = inspect_routes.invoke({"path": repo})
        assert "GET     /items" in result
        assert "POST    /items" in result

    def test_express(self):
        repo = _create_repo({
            "server.js": """
app.get('/users', (req, res) => {});
app.post('/users', (req, res) => {});
app.delete('/users/:id', (req, res) => {});
""",
        })
        result = inspect_routes.invoke({"path": repo})
        assert "GET     /users" in result
        assert "POST    /users" in result
        assert "DELETE  /users/:id" in result

    def test_go_gin(self):
        repo = _create_repo({
            "main.go": """
func main() {
    r.GET("/ping", func(c *gin.Context) {})
    r.POST("/users", func(c *gin.Context) {})
}
""",
        })
        result = inspect_routes.invoke({"path": repo})
        assert "GET     /ping" in result
        assert "POST    /users" in result

    def test_rust_axum(self):
        repo = _create_repo({
            "src/main.rs": """
#[get("/health")]
async fn health() -> String { "ok".to_string() }

#[post("/users")]
async fn create_user() -> String { "created".to_string() }
""",
        })
        result = inspect_routes.invoke({"path": repo})
        assert "GET     /health" in result
        assert "POST    /users" in result

    def test_laravel(self):
        repo = _create_repo({
            "routes/web.php": """
Route::get('/users', [UserController::class, 'index']);
Route::post('/users', [UserController::class, 'store']);
""",
        })
        result = inspect_routes.invoke({"path": repo})
        assert "GET     /users" in result
        assert "POST    /users" in result

    def test_aspnet(self):
        repo = _create_repo({
            "Program.cs": """
app.MapGet("/users", () => "ok");
app.MapPost("/users", () => "created");
""",
        })
        result = inspect_routes.invoke({"path": repo})
        assert "GET     /users" in result
        assert "POST    /users" in result
