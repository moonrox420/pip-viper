"""Package Controller for PipViper IDE.

Orchestrates package inventory inspection, installation, and requirements sync (PRD A1).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from ..pip_viper import PipPackage, run_in_thread
from ..services.offline_service import OfflineModeError, OfflineService
from ..services.package_service import PackageService

_LOGGER = logging.getLogger("src.controllers.package")


class PackageController(QObject):
    """Coordinates package actions, background execution, and feedback."""

    packages_updated = Signal(list)  # Emits list[PipPackage]
    status_message = Signal(str)
    operation_finished = Signal()

    def __init__(
        self,
        package_service: Optional[PackageService] = None,
        offline_service: Optional[OfflineService] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._service = package_service or PackageService.get_instance()
        self._offline = offline_service or OfflineService.get_instance()

    @property
    def service(self) -> PackageService:
        return self._service

    def refresh_packages(self, python_bin: str) -> None:
        """Asynchronously query installed packages."""
        self.status_message.emit("Scanning installed environment packages...")

        def worker() -> list[PipPackage]:
            return self._service.list_packages(python_bin)

        def on_done(packages: list[PipPackage]) -> None:
            self.packages_updated.emit(packages)
            self.status_message.emit(f"Discovered {len(packages)} installed packages.")
            self.operation_finished.emit()

        def on_err(err_msg: str) -> None:
            _LOGGER.warning("Package list failed: %s", err_msg)
            self.status_message.emit(f"Package scan error: {err_msg}")
            self.operation_finished.emit()

        bg_worker = run_in_thread(worker)
        bg_worker.signals.result.connect(on_done)
        bg_worker.signals.error.connect(on_err)

    def install_package(
        self,
        python_bin: str,
        package_name: str,
        parent_widget: Optional[QWidget] = None,
    ) -> None:
        """Install a package with offline gating and non-blocking worker."""
        if self._offline.is_offline() and not self._offline.get_local_wheelhouse_dir():
            msg = (
                f"Cannot install '{package_name}': PipViper is running in Offline Mode.\n\n"
                f"To install packages:\n"
                f"• Switch to Online Mode (Opt-in) via the status bar.\n"
                f"• Or configure a local wheelhouse directory for offline installs."
            )
            self.status_message.emit(f"Install blocked: Offline Mode active.")
            if parent_widget:
                QMessageBox.warning(parent_widget, "Offline Mode Active", msg)
            return

        self.status_message.emit(f"Installing '{package_name}'...")

        def worker() -> None:
            self._service.install_package(python_bin, package_name)

        def on_done() -> None:
            self.status_message.emit(f"Successfully installed '{package_name}'.")
            self.refresh_packages(python_bin)

        def on_err(err: str) -> None:
            _LOGGER.error("Package installation failed: %s", err)
            self.status_message.emit(f"Failed installing '{package_name}'.")
            if parent_widget:
                QMessageBox.critical(parent_widget, "Package Install Error", f"Installation failed:\n{err}")

        bg_worker = run_in_thread(worker)
        bg_worker.signals.finished.connect(on_done)
        bg_worker.signals.error.connect(on_err)

    def uninstall_package(
        self,
        python_bin: str,
        package_name: str,
        parent_widget: Optional[QWidget] = None,
    ) -> None:
        """Uninstall a package with non-blocking execution."""
        self.status_message.emit(f"Uninstalling '{package_name}'...")

        def worker() -> None:
            self._service.uninstall_package(python_bin, package_name)

        def on_done() -> None:
            self.status_message.emit(f"Successfully uninstalled '{package_name}'.")
            self.refresh_packages(python_bin)

        def on_err(err: str) -> None:
            _LOGGER.error("Package uninstall failed: %s", err)
            self.status_message.emit(f"Failed uninstalling '{package_name}'.")
            if parent_widget:
                QMessageBox.critical(parent_widget, "Package Uninstall Error", f"Uninstall failed:\n{err}")

        bg_worker = run_in_thread(worker)
        bg_worker.signals.finished.connect(on_done)
        bg_worker.signals.error.connect(on_err)
