"""Unit tests for the decomposed PipViper Service Layer (PRD A3)."""

from __future__ import annotations

from pathlib import Path
import pytest

from src.pip_viper import LintIssue, LintSeverity, LintTool
from src.services import (
    AIService,
    CodeToolsService,
    DiagnosticsService,
    EnvironmentService,
    LinterService,
    OfflineModeError,
    OfflineService,
    PackageService,
    ProcessSecurityError,
    ProcessService,
    ServiceContainer,
)


@pytest.fixture(autouse=True)
def reset_offline() -> None:
    OfflineService.reset_instance()
    OfflineService.get_instance().set_offline_mode(True)


def test_service_container_initialization() -> None:
    """Verify ServiceContainer instantiates all required domain services (PRD A5)."""
    container = ServiceContainer.get_instance()
    assert isinstance(container.offline, OfflineService)
    assert isinstance(container.process, ProcessService)
    assert isinstance(container.environment, EnvironmentService)
    assert isinstance(container.package, PackageService)
    assert isinstance(container.linter, LinterService)
    assert isinstance(container.ai, AIService)
    assert isinstance(container.code_tools, CodeToolsService)
    assert isinstance(container.diagnostics, DiagnosticsService)


def test_environment_service_discovery() -> None:
    """Verify EnvironmentService discovers local environments without network."""
    env_service = EnvironmentService.get_instance()
    python_bin = env_service.get_runtime_python()
    assert python_bin != ""
    assert Path(python_bin).name.lower().startswith("python")

    envs = env_service.detect_environments()
    assert len(envs) > 0


def test_package_service_offline_enforcement(tmp_path: Path) -> None:
    """Verify PackageService enforces offline gating and wheelhouse paths (PRD O3, O4)."""
    offline_service = OfflineService.get_instance()
    offline_service.set_offline_mode(True)
    offline_service.set_local_wheelhouse_dir(None)

    pkg_service = PackageService.get_instance()
    python_bin = "python"

    # List command is allowed offline
    list_cmd = pkg_service.get_package_manager_cmd(python_bin, "list")
    assert "list" in list_cmd

    # Remote install is blocked offline
    with pytest.raises(OfflineModeError):
        pkg_service.get_package_manager_cmd(python_bin, "install", "requests")

    # Install with local wheelhouse is permitted offline
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    offline_service.set_local_wheelhouse_dir(wheelhouse)

    offline_cmd = pkg_service.get_package_manager_cmd(python_bin, "install", "requests")
    assert "--find-links" in offline_cmd
    assert str(wheelhouse) in offline_cmd
    assert "--no-index" in offline_cmd

    # Uninstall is allowed offline
    uninstall_cmd = pkg_service.get_package_manager_cmd(python_bin, "uninstall", "requests")
    assert "uninstall" in uninstall_cmd


def test_ai_service_configuration() -> None:
    """Verify AIService property management and localhost configuration (PRD O5)."""
    ai_service = AIService()
    assert "127.0.0.1" in ai_service.endpoint_url
    assert ai_service.model_name == "llama3"

    ai_service.set_model_name("mistral")
    assert ai_service.model_name == "mistral"

    ai_service.set_endpoint_url("http://localhost:8000/v1")
    assert ai_service.endpoint_url == "http://localhost:8000/v1"


def test_code_tools_service_operations() -> None:
    """Verify CodeToolsService local formatting and syntax checking."""
    tools = CodeToolsService.get_instance()

    syntax_res = tools.check_syntax("x = 42\n")
    assert syntax_res.is_valid is True

    bad_syntax = tools.check_syntax("x = \n")
    assert bad_syntax.is_valid is False

    format_res = tools.format_code("a=1+2\n")
    assert format_res.changed is True
    assert "a = 1 + 2" in format_res.formatted_source


def test_linter_service_output_parsing() -> None:
    """Verify LinterService output regex parsing across multiple tools."""
    linter = LinterService.get_instance()
    target_file = Path("test_file.py")

    # Test Flake8 output
    flake8_out = "test_file.py:10:4: E999 SyntaxError: invalid syntax"
    issues = linter.parse_output(LintTool.FLAKE8, flake8_out, target_file)
    assert len(issues) == 1
    assert issues[0].line == 10
    assert issues[0].column == 4
    assert issues[0].code == "E999"

    # Test Mypy output
    mypy_out = "test_file.py:25: error: Incompatible types in assignment"
    issues = linter.parse_output(LintTool.MYPY, mypy_out, target_file)
    assert len(issues) == 1
    assert issues[0].line == 25
    assert issues[0].severity == LintSeverity.ERROR
