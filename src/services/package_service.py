"""Package Management Service for PipViper IDE.

Encapsulates package discovery, installation, and uninstallation.
Strictly enforces PRD requirements O1, O3, O4, S1, S2, and S3:
    * Blocks remote package installation in Offline Mode.
    * Supports offline installs from local wheelhouse / cache directories.
    * Validates all package specifier strings before process launch.
    * Spawns commands through ProcessService without shell=True.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any, Optional, Sequence

from PySide6.QtCore import QObject, Signal

from ..pip_viper import PipPackage
from .offline_service import OfflineModeError, OfflineService
from .process_service import ProcessSecurityError, ProcessService

_LOGGER = logging.getLogger("src.services.package")


class PackageService(QObject):
    """Manages Python package inventory and installation."""

    packages_refreshed = Signal(list)  # Emits list[PipPackage]

    _instance: Optional[PackageService] = None

    def __init__(
        self,
        offline_service: Optional[OfflineService] = None,
        process_service: Optional[ProcessService] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._offline_custom = offline_service
        self._process_custom = process_service

    @property
    def _offline(self) -> OfflineService:
        return self._offline_custom or OfflineService.get_instance()

    @property
    def _process(self) -> ProcessService:
        return self._process_custom or ProcessService.get_instance()

    @classmethod
    def get_instance(cls) -> PackageService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def get_package_manager_cmd(
        self,
        python_bin: str,
        action: str,
        package_name: Optional[str] = None,
    ) -> list[str]:
        """Construct sanitized command list for package operations."""
        has_uv = shutil.which("uv") is not None

        if action == "list":
            if has_uv:
                return ["uv", "pip", "list", "--format=json", "--python", python_bin]
            return [python_bin, "-m", "pip", "list", "--format=json"]

        elif action == "install":
            if not package_name:
                raise ValueError("Package name required for install action.")
            valid_name = self._process.validate_package_name(package_name)

            # Check offline mode and local wheelhouse (PRD O3 & O4)
            local_wheelhouse = self._offline.get_local_wheelhouse_dir()
            if self._offline.is_offline():
                if not local_wheelhouse:
                    self._offline.assert_network_allowed("Remote Package Installation")
                # Install from local wheelhouse
                if has_uv:
                    return [
                        "uv", "pip", "install",
                        "--no-index", "--find-links", str(local_wheelhouse),
                        valid_name, "--python", python_bin,
                    ]
                return [
                    python_bin, "-m", "pip", "install",
                    "--no-index", "--find-links", str(local_wheelhouse),
                    valid_name,
                ]

            # Online mode allowed
            if has_uv:
                return ["uv", "pip", "install", valid_name, "--python", python_bin]
            return [python_bin, "-m", "pip", "install", valid_name]

        elif action == "uninstall":
            if not package_name:
                raise ValueError("Package name required for uninstall action.")
            valid_name = self._process.validate_package_name(package_name)
            if has_uv:
                return ["uv", "pip", "uninstall", "-y", valid_name, "--python", python_bin]
            return [python_bin, "-m", "pip", "uninstall", "-y", valid_name]

        raise ValueError(f"Unknown package action: {action!r}")

    def list_packages(self, python_bin: str) -> list[PipPackage]:
        """Query installed packages using local package manager or importlib."""
        cmd = self.get_package_manager_cmd(python_bin, "list")
        res = self._process.run_command(cmd, timeout=30)
        try:
            records = json.loads(res.stdout)
            return [
                PipPackage(
                    name=item["name"],
                    version=item["version"],
                    location=item.get("location", ""),
                )
                for item in records
            ]
        except Exception as exc:
            _LOGGER.warning("Failed parsing package list output: %s", exc)
            return []

    def install_package(self, python_bin: str, package_name: str, timeout: float = 300.0) -> None:
        """Install a package with offline gating and argument sanitization."""
        cmd = self.get_package_manager_cmd(python_bin, "install", package_name)
        _LOGGER.info("Installing package with command: %s", cmd)
        self._process.run_command(cmd, timeout=timeout, check=True)

    def uninstall_package(self, python_bin: str, package_name: str, timeout: float = 120.0) -> None:
        """Uninstall a package with argument sanitization."""
        cmd = self.get_package_manager_cmd(python_bin, "uninstall", package_name)
        _LOGGER.info("Uninstalling package with command: %s", cmd)
        self._process.run_command(cmd, timeout=timeout, check=True)
