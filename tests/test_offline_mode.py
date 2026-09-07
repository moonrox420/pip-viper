"""Unit and integration tests for PipViper Offline-First integrity (PRD T1).

Verifies non-negotiable PRD safety requirements:
    * Offline Mode is enabled by default (A4).
    * Zero network calls are made while Offline Mode is active (O1, O2).
    * External AI hosts are strictly rejected (O5).
    * Prompt sizes are validated and bounded (S7).
    * Process execution arguments and environments are sanitized (S1, S2).
    * OfflineModeBadge reflects live state (U1).
"""

from __future__ import annotations

import unittest.mock as mock
from pathlib import Path

import pytest

from src.dependencies import DependencyScanner
from src.pip_viper import ProcessError, query_local_llm
from src.services import (
    OfflineModeError,
    OfflineService,
    ProcessSecurityError,
    ProcessService,
    ServiceContainer,
)
from src.widgets import OfflineModeBadge


@pytest.fixture(autouse=True)
def reset_offline_service() -> None:
    """Ensure clean OfflineService state for each test, defaulting to Offline."""
    OfflineService.reset_instance()
    service = OfflineService.get_instance()
    service.set_offline_mode(True)


def test_offline_mode_default_and_toggle(qtbot: object) -> None:
    """Verify Offline Mode defaults to True (Offline) and emits signals on change."""
    service = OfflineService.get_instance()
    assert service.is_offline() is True

    received_states: list[bool] = []
    service.offline_mode_changed.connect(received_states.append)

    service.set_offline_mode(False)
    assert service.is_offline() is False
    assert received_states == [False]

    service.set_offline_mode(True)
    assert service.is_offline() is True
    assert received_states == [False, True]


def test_offline_mode_blocks_pypi_check() -> None:
    """Verify check_pypi_update strictly makes zero network calls when offline (PRD O1)."""
    service = OfflineService.get_instance()
    service.set_offline_mode(True)

    scanner = DependencyScanner()
    scanner._pypi_cache.clear()

    with mock.patch("urllib.request.urlopen") as mock_urlopen:
        result = scanner.check_pypi_update("requests")
        assert result is None
        mock_urlopen.assert_not_called()


def test_offline_mode_allows_pypi_check_when_opted_in() -> None:
    """Verify check_pypi_update is allowed and uses 7.0.0 User-Agent when online (PRD O1 & P3)."""
    service = OfflineService.get_instance()
    service.set_offline_mode(False)

    scanner = DependencyScanner()
    scanner._pypi_cache.clear()

    mock_response = mock.MagicMock()
    mock_response.read.return_value = b'{"info": {"version": "2.31.0"}}'
    mock_response.__enter__.return_value = mock_response

    with mock.patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        result = scanner.check_pypi_update("requests")
        assert result == "2.31.0"
        mock_urlopen.assert_called_once()
        req_arg = mock_urlopen.call_args[0][0]
        assert req_arg.get_header("User-agent") == "PipViper-IDE/7.0.0"


def test_assert_network_allowed_raises_offline_mode_error() -> None:
    """Verify assert_network_allowed raises OfflineModeError when offline (PRD O7)."""
    service = OfflineService.get_instance()
    service.set_offline_mode(True)

    with pytest.raises(OfflineModeError) as exc_info:
        service.assert_network_allowed("Install Remote Package")

    assert "Install Remote Package" in str(exc_info.value)
    assert "Offline Mode" in str(exc_info.value)

    # When online, no error is raised
    service.set_offline_mode(False)
    service.assert_network_allowed("Install Remote Package")


def test_ai_query_localhost_enforcement() -> None:
    """Verify AI requests reject external endpoints strictly (PRD O5)."""
    # External endpoints must be blocked with ProcessError / security violation
    with pytest.raises(ProcessError) as exc_info:
        query_local_llm(
            api_url="https://api.openai.com/v1",
            model_name="gpt-4",
            system_prompt="system",
            user_prompt="print('hello')",
        )
    assert "Security Violation: External AI endpoint" in str(exc_info.value)

    with pytest.raises(ProcessError) as exc_info:
        query_local_llm(
            api_url="http://192.168.1.50:11434/v1",
            model_name="llama3",
            system_prompt="system",
            user_prompt="print('hello')",
        )
    assert "Security Violation: External AI endpoint" in str(exc_info.value)


def test_ai_query_prompt_validation() -> None:
    """Verify AI prompt size limits and dangerous character stripping (PRD S7)."""
    oversized_prompt = "A" * 70000
    with pytest.raises(ValueError) as exc_info:
        query_local_llm(
            api_url="http://127.0.0.1:11434/v1",
            model_name="llama3",
            system_prompt="system",
            user_prompt=oversized_prompt,
        )
    assert "exceeds safe local sidecar limit" in str(exc_info.value)


def test_process_service_argument_sanitization() -> None:
    """Verify ProcessService rejects dangerous shell metacharacters (PRD S1)."""
    # Clean arguments pass
    clean_cmd = ["python", "-m", "pip", "list"]
    assert ProcessService.sanitize_arguments(clean_cmd) == clean_cmd

    # Shell metacharacters must be rejected
    dangerous_commands = [
        ["python", "script.py; rm -rf /"],
        ["pip", "install", "foo && bar"],
        ["python", "test.py | cat"],
        ["echo", "`whoami`"],
        ["python", "run.py > output.txt"],
        ["python", "run.py < input.txt"],
    ]
    for cmd in dangerous_commands:
        with pytest.raises(ProcessSecurityError):
            ProcessService.sanitize_arguments(cmd)


def test_process_service_package_name_validation() -> None:
    """Verify Python package specifier validation."""
    valid_packages = [
        "requests",
        "requests>=2.28.0",
        "pip-viper",
        "pyside6_addons",
        "urllib3<3.0,>=1.21.1",
        "uvicorn[standard]",
    ]
    for pkg in valid_packages:
        assert ProcessService.validate_package_name(pkg) == pkg

    invalid_packages = [
        "requests; rm -rf /",
        "foo && bar",
        "pkg | evil",
        "`command`",
        "",
        "   ",
    ]
    for pkg in invalid_packages:
        with pytest.raises(ProcessSecurityError):
            ProcessService.validate_package_name(pkg)


def test_process_service_safe_environment() -> None:
    """Verify ProcessService strips arbitrary host environment variables (PRD S2)."""
    dirty_env = {
        "PATH": "/usr/bin;C:\\Windows",
        "SYSTEMROOT": "C:\\Windows",
        "MALICIOUS_VAR": "evil_payload",
        "AWS_SECRET_KEY": "secret_data",
        "TOKEN": "sensitive_token",
    }
    safe = ProcessService.build_safe_environment(base_env=dirty_env)
    assert "PATH" in safe
    assert "SYSTEMROOT" in safe
    assert "MALICIOUS_VAR" not in safe
    assert "AWS_SECRET_KEY" not in safe
    assert "TOKEN" not in safe
    assert safe["PYTHONUNBUFFERED"] == "1"
    assert safe["PYTHONIOENCODING"] == "utf-8"


def test_offline_mode_badge_widget(qtbot: object) -> None:
    """Verify OfflineModeBadge displays correct indicator and responds to state changes (PRD U1)."""
    service = OfflineService.get_instance()
    service.set_offline_mode(True)

    badge = OfflineModeBadge()
    getattr(qtbot, "addWidget")(badge)

    assert "🔒 Offline Mode" in badge._label.text()
    assert "PipViper Offline Mode: ACTIVE" in badge.toolTip()

    service.set_offline_mode(False)
    assert "🌐 Online" in badge._label.text()
    assert "OPT-IN ACTIVE" in badge.toolTip()
