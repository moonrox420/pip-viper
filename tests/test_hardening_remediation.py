"""Unit tests for Phase 10 hardening, remediation fixes, and PRD compliance.

Covers:
1. AIService health check strict localhost validation (PRD O5).
2. DependencyScanner check_pypi_update fail-closed behavior under Offline Mode (PRD O1/O7).
3. ProcessService routing and security argument sanitization across modules (PRD A3/S1).
4. Release packaging hygiene and zero-pollution verification (PRD P1).
"""

from __future__ import annotations

import logging
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch
import zipfile

import pytest

from desktop_packaging.build_standalone import check_release_pollution
from src.dependencies import DependencyScanner
from src.environment import EnvironmentDetector
from src.services.ai_service import AIService
from src.services.offline_service import OfflineModeError, OfflineService, assert_network_allowed
from src.services.process_service import ProcessSecurityError, ProcessService
from src.vcs import GitService


# -----------------------------------------------------------------------------
# 1. AI Service Localhost Gating Tests
# -----------------------------------------------------------------------------

def test_ai_service_health_check_blocks_external_urls(caplog: pytest.LogCaptureFixture) -> None:
    """Verify AIService.check_health() strictly rejects external (non-localhost) URLs."""
    ai = AIService.get_instance()
    original_url = ai.endpoint_url

    external_urls = [
        "https://api.openai.com/v1",
        "http://example.com:11434",
        "http://192.168.1.100:11434",
        "http://ai-server.corp.local:11434",
        "https://my-ollama.internal.net",
    ]

    try:
        for url in external_urls:
            ai.set_endpoint_url(url)
            with patch("urllib.request.urlopen") as mock_urlopen:
                with caplog.at_level(logging.ERROR):
                    result = ai.check_health()
                assert result is False
                assert not ai.is_connected
                mock_urlopen.assert_not_called()
                assert "Non-localhost AI endpoint blocked" in caplog.text
    finally:
        ai.set_endpoint_url(original_url)


def test_ai_service_health_check_permits_localhost() -> None:
    """Verify AIService.check_health() allows valid local addresses."""
    ai = AIService.get_instance()
    original_url = ai.endpoint_url

    local_urls = [
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://[::1]:11434",
        "http://0.0.0.0:11434",
    ]

    try:
        for url in local_urls:
            ai.set_endpoint_url(url)
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_resp = MagicMock()
                mock_resp.status = 200
                mock_urlopen.return_value.__enter__.return_value = mock_resp
                result = ai.check_health()
                assert result is True
                assert ai.is_connected
                mock_urlopen.assert_called_once()
    finally:
        ai.set_endpoint_url(original_url)


# -----------------------------------------------------------------------------
# 2. Dependency Update Fail-Closed Offline Tests
# -----------------------------------------------------------------------------

def test_dependencies_check_pypi_update_offline_fail_closed() -> None:
    """Verify DependencyScanner.check_pypi_update() fails closed when Offline Mode is active."""
    offline = OfflineService.get_instance()
    scanner = DependencyScanner()
    pkg_name = "_test_uncached_package_xyz_999_"
    scanner._pypi_cache.pop(pkg_name, None)

    original_state = offline.is_offline()
    try:
        offline.set_offline(True)

        with patch("urllib.request.urlopen") as mock_urlopen:
            result = scanner.check_pypi_update(pkg_name)
            assert result is None
            mock_urlopen.assert_not_called()
    finally:
        offline.set_offline(original_state)


def test_assert_network_allowed_raises_offline_mode_error() -> None:
    """Verify top-level assert_network_allowed() raises OfflineModeError when offline."""
    offline = OfflineService.get_instance()
    original_state = offline.is_offline()
    try:
        offline.set_offline(True)
        with pytest.raises(OfflineModeError) as exc_info:
            assert_network_allowed("Test Feature")
        assert "Offline Mode" in str(exc_info.value)
    finally:
        offline.set_offline(original_state)


# -----------------------------------------------------------------------------
# 3. ProcessService Routing and Security Tests
# -----------------------------------------------------------------------------

def test_process_service_blocks_dangerous_metacharacters() -> None:
    """Verify ProcessService.sanitize_arguments rejects command injection attempts."""
    dangerous_commands = [
        ["git", "status; rm -rf /"],
        ["python", "-c", "print(1) & calc.exe"],
        ["pip", "install", "foo | dir"],
        ["python", "`whoami`"],
        ["echo", "$SECRET"],
        ["cat", "file > output.txt"],
    ]

    for cmd in dangerous_commands:
        with pytest.raises(ProcessSecurityError):
            ProcessService.sanitize_arguments(cmd)


def test_process_service_permits_git_format_delimiters() -> None:
    """Verify ProcessService permits pipe delimiters inside git log format flags."""
    git_cmd = [
        "git",
        "log",
        "-n",
        "50",
        "--pretty=format:%H|%h|%an|%ae|%ad|%s",
        "--date=iso-strict",
    ]
    sanitized = ProcessService.sanitize_arguments(git_cmd)
    assert sanitized == git_cmd


def test_vcs_routes_through_process_service(tmp_path: Path) -> None:
    """Verify GitService._run routes through ProcessService.get_instance().run_command()."""
    repo = GitService()
    ps = ProcessService.get_instance()

    with patch.object(ps, "run_command", wraps=ps.run_command) as mock_run:
        try:
            repo.init_repo(tmp_path)
        except Exception:
            pass
        assert mock_run.called


def test_environment_detector_routes_through_process_service(tmp_path: Path) -> None:
    """Verify EnvironmentDetector uses ProcessService for probing Python versions."""
    detector = EnvironmentDetector()
    ps = ProcessService.get_instance()
    fake_py = tmp_path / "standalone_python.exe"
    fake_py.write_text("binary", encoding="utf-8")

    with patch.object(ps, "run_command") as mock_run:
        mock_proc = MagicMock()
        mock_proc.stdout = "Python 3.12.0\n"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        ver = detector.probe_python_version(fake_py)
        assert mock_run.called
        assert ver == "3.12.0"


# -----------------------------------------------------------------------------
# 4. Release Hygiene and Packaging Tests
# -----------------------------------------------------------------------------

def test_clean_workspace_pollution_purges_dirty_items(tmp_path: Path) -> None:
    """Verify clean_workspace_pollution effectively purges all bytecode and temp folders (PRD P1)."""
    from desktop_packaging.build_standalone import clean_workspace_pollution

    # Create artificial pollution
    pycache = tmp_path / "src" / "__pycache__"
    pycache.mkdir(parents=True)
    (pycache / "app.cpython-312.pyc").write_text("bytecode")
    (tmp_path / "temp_pytest").mkdir()
    (tmp_path / ".pytest_tmp_run").mkdir()
    (tmp_path / "src" / "valid.py").write_text("# valid code")

    # Verify pollution is detected
    bad = check_release_pollution(tmp_path)
    assert len(bad) > 0

    # Purge
    clean_workspace_pollution(tmp_path)

    # Verify workspace is now 100% clean
    bad_after = check_release_pollution(tmp_path)
    assert len(bad_after) == 0
    assert (tmp_path / "src" / "valid.py").exists()


def test_release_zip_has_zero_pollution() -> None:
    """Verify pipviper.zip (if built) contains zero .pyc or __pycache__ entries."""
    zip_path = Path("pipviper.zip")
    if not zip_path.exists():
        pytest.skip("pipviper.zip not built yet")

    with zipfile.ZipFile(zip_path, "r") as zf:
        bad_entries = [
            n for n in zf.namelist()
            if "__pycache__" in n or n.endswith((".pyc", ".pyo")) or ".pytest_tmp" in n
        ]
        assert len(bad_entries) == 0, f"Found {len(bad_entries)} bad entries in pipviper.zip: {bad_entries[:5]}"
