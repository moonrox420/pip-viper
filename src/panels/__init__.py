"""Interactive utility panels positioned at the bottom of the workspace.

Decomposed into submodules per PRD Architecture Requirement A2.
"""

from .base import (
    _LOGGER,
    _SEVERITY_COLOR_KEYS,
    _severity_color,
    _section_label,
    _qcolor,
)
from .output_panel import OutputPanel
from .repl_panel import ReplPanel, ReplVariable, TerminalPanel
from .code_tools_panel import CodeToolsPanel
from .ai_panel import AiPanel
from .lint_widget import LintWidget
from .git_panel import GitPanel, GitWidget
from .package_manager_widget import PackageManagerWidget
from .log_panel import LogPanel, LogWidget
from .debug_panel import DebugPanel
from .internals_panel import InternalsPanel
from .test_runner_panel import TestRunnerPanel
from .dependency_studio_panel import DependencyStudioPanel
from ..vcs import (
    GitBranch,
    GitCommit,
    GitFileStatus,
    GitStatusEntry,
)

__all__ = [
    "OutputPanel",
    "ReplPanel",
    "ReplVariable",
    "TerminalPanel",
    "CodeToolsPanel",
    "AiPanel",
    "LintWidget",
    "GitPanel",
    "GitWidget",
    "GitBranch",
    "GitCommit",
    "GitFileStatus",
    "GitStatusEntry",
    "PackageManagerWidget",
    "LogWidget",
    "LogPanel",
    "DebugPanel",
    "InternalsPanel",
    "TestRunnerPanel",
    "DependencyStudioPanel",
]
