"""Global pytest configuration and cleanup fixtures for PipViper test suites."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Generator

import pytest
from PySide6.QtWidgets import QApplication

# Work around Windows AppContainer / Sandbox ACL issue where mode=0o700
# strips inheritance of the sandbox SID from newly created directories/files.
if sys.platform == "win32":
    _orig_os_mkdir = os.mkdir

    def _patched_os_mkdir(path, mode=0o777, *args, **kwargs):
        if mode == 0o700:
            mode = 0o777
        return _orig_os_mkdir(path, mode, *args, **kwargs)

    os.mkdir = _patched_os_mkdir

    _orig_path_mkdir = Path.mkdir

    def _patched_path_mkdir(self, mode=0o777, parents=False, exist_ok=False):
        if mode == 0o700:
            mode = 0o777
        return _orig_path_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    Path.mkdir = _patched_path_mkdir

    _orig_os_chmod = os.chmod

    def _patched_os_chmod(path, mode, *args, **kwargs):
        if mode == 0o700:
            mode = 0o777
        try:
            return _orig_os_chmod(path, mode, *args, **kwargs)
        except OSError:
            pass

    os.chmod = _patched_os_chmod

    try:
        import _pytest.pathlib

        _orig_cleanup_symlinks = _pytest.pathlib.cleanup_dead_symlinks

        def _safe_cleanup_dead_symlinks(root):
            try:
                return _orig_cleanup_symlinks(root)
            except OSError:
                pass

        _pytest.pathlib.cleanup_dead_symlinks = _safe_cleanup_dead_symlinks

        _orig_cleanup_numbered = _pytest.pathlib.cleanup_numbered_dir

        def _safe_cleanup_numbered_dir(*args, **kwargs):
            try:
                return _orig_cleanup_numbered(*args, **kwargs)
            except OSError:
                pass

        _pytest.pathlib.cleanup_numbered_dir = _safe_cleanup_numbered_dir
    except Exception:
        pass

from src.pip_viper import shutdown_logging



@pytest.fixture(autouse=True)
def cleanup_logging_and_qt(qapp: QApplication) -> Generator[None, None, None]:
    """Ensure all file loggers are closed and flushed to avoid Windows file locks."""
    yield
    shutdown_logging()
    for handler in list(logging.getLogger().handlers):
        try:
            handler.flush()
            handler.close()
        except Exception:
            pass
        logging.getLogger().removeHandler(handler)
    if qapp is not None:
        qapp.processEvents()
