"""Tests for framework-specific rules injector."""
import json
import tempfile
from pathlib import Path

from orchestration.framework_rules import (
    _detect_framework,
    _FRAMEWORK_RULES,
    inject_framework_rules,
)


class TestDetectFramework:
    def test_nestjs(self):
        tmp = tempfile.mkdtemp()
        try:
            Path(tmp, "package.json").write_text(
                json.dumps({"dependencies": {"@nestjs/common": "^10.0.0"}})
            )
            assert _detect_framework(Path(tmp)) == "nestjs"
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_go(self):
        tmp = tempfile.mkdtemp()
        try:
            Path(tmp, "go.mod").write_text("module test\ngo 1.22\n")
            assert _detect_framework(Path(tmp)) == "go"
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_python_fastapi(self):
        tmp = tempfile.mkdtemp()
        try:
            Path(tmp, "requirements.txt").write_text("fastapi\nuvicorn\n")
            assert _detect_framework(Path(tmp)) == "python_fastapi"
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_python_general(self):
        tmp = tempfile.mkdtemp()
        try:
            Path(tmp, "requirements.txt").write_text("flask\nsqlalchemy\n")
            assert _detect_framework(Path(tmp)) == "python_general"
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_java_spring(self):
        tmp = tempfile.mkdtemp()
        try:
            Path(tmp, "pom.xml").write_text("<dependencies><dependency>spring-boot</dependency></dependencies>")
            assert _detect_framework(Path(tmp)) == "java_spring"
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_php_laravel(self):
        tmp = tempfile.mkdtemp()
        try:
            Path(tmp, "composer.json").write_text(
                json.dumps({"require": {"laravel/framework": "^11.0"}})
            )
            assert _detect_framework(Path(tmp)) == "php_laravel"
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_php_general(self):
        tmp = tempfile.mkdtemp()
        try:
            Path(tmp, "composer.json").write_text(json.dumps({"require": {"monolog/monolog": "^3.0"}}))
            assert _detect_framework(Path(tmp)) == "php_general"
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_nextjs(self):
        tmp = tempfile.mkdtemp()
        try:
            Path(tmp, "package.json").write_text(
                json.dumps({"dependencies": {"next": "^14.0.0"}})
            )
            assert _detect_framework(Path(tmp)) == "nextjs"
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_framework(self):
        tmp = tempfile.mkdtemp()
        try:
            Path(tmp, "README.md").write_text("hello")
            assert _detect_framework(Path(tmp)) == ""
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_java_subdir(self):
        tmp = tempfile.mkdtemp()
        try:
            backend = Path(tmp) / "backend"
            backend.mkdir()
            (backend / "pom.xml").write_text("<project></project>")
            assert _detect_framework(Path(tmp)) == "java_general"
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class TestInjectFrameworkRules:
    def test_nestjs_rules(self):
        tmp = tempfile.mkdtemp()
        try:
            Path(tmp, "package.json").write_text(
                json.dumps({"dependencies": {"@nestjs/common": "^10.0.0"}})
            )
            rules = inject_framework_rules(tmp)
            assert "DI" in rules
            assert "process.env" in rules
            assert "Logger" in rules
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_go_rules(self):
        tmp = tempfile.mkdtemp()
        try:
            Path(tmp, "go.mod").write_text("module test\ngo 1.22\n")
            rules = inject_framework_rules(tmp)
            assert "http.Client" in rules
            assert "slog" in rules or "Logger" in rules
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_unknown_returns_empty(self):
        assert inject_framework_rules("/nonexistent/path") == ""

    def test_none_returns_empty(self):
        assert inject_framework_rules(None) == ""

    def test_all_frameworks_have_rules(self):
        for fw in _FRAMEWORK_RULES:
            assert len(_FRAMEWORK_RULES[fw]) > 50, f"Rules too short for {fw}"
