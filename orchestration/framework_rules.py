"""Framework-specific rules injector for execute/review prompts."""

from __future__ import annotations

from pathlib import Path

# Per-stack rules. Order matters: first match wins for monorepos.
_FRAMEWORK_RULES: dict[str, str] = {
    "nestjs": (
        "Stack: **NestJS/TypeScript** (detectado)\n"
        "- **DI**: NUNCA uses `constructor(private readonly baseUrl: string)` para inyectar primitivos. "
        "NestJS NO puede inyectar string sin @Inject() + factory/provider. "
        "Lee process.env directamente: `private readonly baseUrl = process.env.MI_VAR!;`\n"
        "- **Logger**: Usa `new Logger(ClaseName.name)` de `@nestjs/common`. NO uses `console.log`.\n"
        "- **HTTP Client**: Usa `axios` o `fetch`. Para axios: `axios.create({ timeout: 5000 })`. "
        "Para fetch: `AbortController` + `setTimeout(() => controller.abort(), 5000)`.\n"
        "- **Module**: `@Module({ providers: [...], exports: [...] })`. Services: `@Injectable()`.\n"
        "- **Env validation**: Al boot en main.ts. No abortes en dev, warn y seguí."
    ),
    "python_fastapi": (
        "Stack: **Python/FastAPI** (detectado)\n"
        "- **DI**: Usa `Depends()` para inyección. No pases strings como parámetros del constructor de services.\n"
        "- **Logger**: Usa `import logging; logger = logging.getLogger(__name__)`. NO uses `print()`.\n"
        "- **HTTP Client**: Usa `httpx.AsyncClient(timeout=5.0)` o `requests.Session(timeout=5.0)`.\n"
        "- **Env validation**: Usa `pydantic-settings` o `os.environ.get()` con defaults en boot."
    ),
    "python_general": (
        "Stack: **Python** (detectado)\n"
        "- **Logger**: Usa `import logging; logger = logging.getLogger(__name__)`. NO uses `print()`.\n"
        "- **HTTP Client**: Usa `httpx.AsyncClient(timeout=5.0)` o `requests.Session(timeout=5.0)`.\n"
        "- **Env validation**: Usa `os.environ.get('VAR', 'default')` o `pydantic-settings`.\n"
        "- **Type hints**: Usá type hints en todas las funciones públicas."
    ),
    "go": (
        "Stack: **Go** (detectado)\n"
        "- **DI**: Pasa dependencias vía constructor (`func NewClient(cfg Config) *Client`). "
        "NO uses globals ni singletons.\n"
        "- **Logger**: Usa `log/slog` o un logger estructurado (zap, zerolog). NO uses `fmt.Println`.\n"
        "- **HTTP Client**: `&http.Client{Timeout: 5 * time.Second}`. SIEMPRE configurá Timeout.\n"
        "- **Errors**: Retorna errors, no panic. Usá `fmt.Errorf(\"context: %w\", err)` para wrapping.\n"
        "- **Env**: Usa `os.Getenv()` con defaults. Validá al init."
    ),
    "java_spring": (
        "Stack: **Java/Spring Boot** (detectado)\n"
        "- **DI**: Usa `@ConstructorBinding` o `@Value(\"${property}\")`. NO pases strings raw al constructor sin @Value.\n"
        "- **Logger**: Usa `@Slf4j` (Lombok) o `private final Logger log = LoggerFactory.getLogger(...)`. NO uses `System.out`.\n"
        "- **HTTP Client**: Usa `RestTemplate` con `SimpleClientHttpRequestFactory{connectTimeout, readTimeout}` "
        "o `WebClient.builder().baseUrl(url).build()` con timeout configurado.\n"
        "- **@ConfigurationProperties**: Para configs de env, usa binding con validation (@Validated).\n"
        "- **@Service/@Component**: Anotá services correctamente. NO uses static methods para business logic."
    ),
    "java_general": (
        "Stack: **Java** (detectado)\n"
        "- **Logger**: Usa SLF4J (`LoggerFactory.getLogger(...)`). NO uses `System.out`.\n"
        "- **HTTP Client**: `HttpClient.newBuilder().connectTimeout(...).build()`. SIEMPRE configurá timeout.\n"
        "- **Env**: Usa `System.getenv()` con defaults. Validá al init."
    ),
    "php_laravel": (
        "Stack: **PHP/Laravel** (detectado)\n"
        "- **DI**: Usa el Service Container. Bind interfaces en el Service Provider. "
        "NO instancies clases directamente en controllers.\n"
        "- **Logger**: Usa `Log::info/error/warn()` o `report()`. NO uses `echo`/`var_dump`.\n"
        "- **HTTP Client**: Usa `Http::timeout(5)->get/post(...)`. SIEMPRE configurá timeout.\n"
        "- **Env**: Usa `config('service.name')` (binding desde .env vía config/*.php). "
        "Validá en boot del Service Provider."
    ),
    "php_general": (
        "Stack: **PHP** (detectado)\n"
        "- **Logger**: Usa PSR-3 LoggerInterface. NO uses `echo`/`var_dump`.\n"
        "- **HTTP Client**: Usa Guzzle con `['timeout' => 5]`. SIEMPRE configurá timeout.\n"
        "- **Env**: Usa `getenv()` o `$_ENV` con defaults."
    ),
    "nextjs": (
        "Stack: **Next.js/React** (detectado)\n"
        "- **Server Components**: En server-side code, lee `process.env` directamente. "
        "En client-side, usa variables con prefijo `NEXT_PUBLIC_`.\n"
        "- **Logger**: Usa `console.warn/error` (no hay Logger nativo en frontend). "
        "En API routes, usa el logger del backend si aplica.\n"
        "- **HTTP**: Usa `fetch` con `AbortController` para timeout. O usa un HTTP client con timeout configurado."
    ),
}


def _detect_framework(root: Path) -> str:
    """Detect framework by marker files."""
    # NestJS
    pkg = root / "package.json"
    if pkg.is_file():
        import json
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            deps = set((data.get("dependencies") or {}).keys()) | set((data.get("devDependencies") or {}).keys())
            if "@nestjs/common" in deps or "@nestjs/core" in deps:
                return "nestjs"
            if "next" in deps:
                return "nextjs"
        except (json.JSONDecodeError, OSError):
            pass

    # PHP/Laravel
    composer = root / "composer.json"
    if composer.is_file():
        import json
        try:
            data = json.loads(composer.read_text(encoding="utf-8"))
            deps = set((data.get("require") or {}).keys())
            if "laravel/framework" in deps:
                return "php_laravel"
            return "php_general"
        except (json.JSONDecodeError, OSError):
            pass

    # Go
    if (root / "go.mod").is_file():
        return "go"

    # Java
    for marker in ("pom.xml", "build.gradle", "build.gradle.kts"):
        if (root / marker).is_file():
            # Check for Spring
            try:
                text = (root / marker).read_text(errors="replace")
                if "spring" in text.lower() or "webflux" in text.lower():
                    return "java_spring"
            except OSError:
                pass
            return "java_general"

    # Also check in subdirectories for monorepos
    for sub in ("backend", "api", "service", "server", "apps/api", "apps/server"):
        d = root / sub
        for marker in ("pom.xml", "build.gradle", "build.gradle.kts"):
            if (d / marker).is_file():
                try:
                    text = (d / marker).read_text(errors="replace")
                    if "spring" in text.lower():
                        return "java_spring"
                except OSError:
                    pass
                return "java_general"

    # Python
    for marker in ("pyproject.toml", "requirements.txt", "setup.py"):
        if (root / marker).is_file():
            try:
                text = (root / marker).read_text(errors="replace")
                if "fastapi" in text.lower():
                    return "python_fastapi"
            except OSError:
                pass
            return "python_general"

    return ""


def inject_framework_rules(repo_path: str | None) -> str:
    """Returns framework-specific coding rules for the detected stack.

    Empty string if no framework detected.
    """
    if not repo_path:
        return ""
    framework = _detect_framework(Path(repo_path))
    if not framework:
        return ""
    rules = _FRAMEWORK_RULES.get(framework, "")
    if not rules:
        return ""
    return f"\n{rules}\n"