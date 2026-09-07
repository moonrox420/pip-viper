"""Composition Root and Service Container for PipViper IDE.

Implements PRD Requirement A5: Single composition root so all services can be
injected, tested with test doubles, and centralized across controllers.
"""

from __future__ import annotations

from typing import Optional

from ..pip_viper import JediService
from ..vcs import GitService
from .ai_service import AIService
from .code_tools_service import CodeToolsService
from .diagnostics_service import DiagnosticsService
from .environment_service import EnvironmentService
from .linter_service import LinterService
from .offline_service import OfflineService
from .package_service import PackageService
from .process_service import ProcessService


class ServiceContainer:
    """Dependency injection container holding all application service singletons."""

    _instance: Optional[ServiceContainer] = None

    def __init__(
        self,
        offline_service: Optional[OfflineService] = None,
        process_service: Optional[ProcessService] = None,
        environment_service: Optional[EnvironmentService] = None,
        package_service: Optional[PackageService] = None,
        linter_service: Optional[LinterService] = None,
        ai_service: Optional[AIService] = None,
        code_tools_service: Optional[CodeToolsService] = None,
        diagnostics_service: Optional[DiagnosticsService] = None,
        git_service: Optional[GitService] = None,
        jedi_service: Optional[JediService] = None,
    ) -> None:
        self.offline = offline_service or OfflineService.get_instance()
        self.process = process_service or ProcessService.get_instance()
        self.environment = environment_service or EnvironmentService.get_instance()
        self.package = package_service or PackageService.get_instance()
        self.linter = linter_service or LinterService.get_instance()
        self.ai = ai_service or AIService.get_instance()
        self.code_tools = code_tools_service or CodeToolsService.get_instance()
        self.diagnostics = diagnostics_service or DiagnosticsService.get_instance()
        self.git = git_service or GitService()
        self.jedi = jedi_service or JediService()

    @classmethod
    def get_instance(cls) -> ServiceContainer:
        """Access global composition root."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def set_instance(cls, container: Optional[ServiceContainer]) -> None:
        """Override global container (useful for automated testing and mocking)."""
        cls._instance = container
