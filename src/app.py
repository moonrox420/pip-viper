"""Main window, signal orchestration, action routing, and application entry point.

This module acts as the orchestrator of the PipViper IDE. It:
    * Initializes and houses the top-level QMainWindow layout.
    * Decouples heavy execution processes (linters, pip, git) from the UI thread.
    * Interconnects the editor workspace actions with local Jedi auto-completion processes.
    * Manages active workspace directories, file life-cycle, and unified QSS themes.
    * Tracks real-time dependency health via debounced background AST analysis.
    * Wires the interactive local AI Assistant to perform offline structural refactoring.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

# Fix for running directly (python app.py) on Windows
if __name__ == "__main__" and not __package__:
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "src"

from PySide6.QtCore import QProcess, QSettings, Qt, QTimer, Slot
from PySide6.QtGui import QAction, QCloseEvent, QFont, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QInputDialog,
    QLabel, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QSplitter, QStatusBar, QTabWidget, QToolBar,
    QVBoxLayout, QWidget,
)

from .code_tools import (
    CodeFormatter,
    CodeGenerator,
    CodeToolsPipeline,
    DocstringGenerationResult,
    FormatResult,
    ImportOrganizeResult,
    PipelineResult,
    RenameResult,
    SymbolRenamer,
    SyntaxAutoFixer,
    SyntaxCheckResult,
    SyntaxFixResult,
    TestGenerationResult,
    UnusedCleanupResult,
)
from .editor import CodeEditor, EditorContainer, EditorTabs
from .diff_viewer import DiffViewerDialog, DiffViewerWidget
from .vcs import GitDiffHunk, GitService
from .panels import (
    AiPanel, CodeToolsPanel, DebugPanel, GitPanel, GitWidget, InternalsPanel, LintWidget, LogWidget,
    OutputPanel, PackageManagerWidget, ReplPanel, TerminalPanel, TestRunnerPanel,
    DependencyStudioPanel,
)

from .environment import (
    EnvironmentDetector,
    EnvironmentPickerDialog,
    EnvironmentSelectorWidget,
    EnvironmentType,
    PythonEnvironment,
)
from .dependencies import (
    DependencyGraph,
    DependencyScanner,
    WorkspaceRequirementsReport,
)
from .diagnostics import DiagnosticIssue, MypyService, RuffService
from .testing import PytestEngine, TestItem, TestStatus, TestSuiteSummary
from .internals import (
    AstInspectionResult,
    AstInspector,
    BytecodeDisassembler,
    DisassemblyResult,
    ExecutionProfiler,
    ProfileResult,
    SymtableInspector,
    SymtableResult,
)
from . import (
    AppConfig, ColorPalette, EditorTheme, JediResult, JediService,
    LintIssue, LintSeverity, LintTool, LinterError, LinterParseError,
    PipPackage, ProcessTimeoutError, configure_logging, get_missing_imports,
    get_palette, map_module_to_pypi, query_local_llm,
    run_in_thread,
)
from .navigation import (
    CommandPaletteDialog,
    FuzzyMatcher,
    QuickOpenDialog,
    SearchInFilesWidget,
)
from .styles import get_stylesheet
from .widgets import DocumentOutlineWidget, FileExplorer, MemoryMeterWidget, OfflineModeBadge
from .controllers import (
    AIController,
    EditorController,
    EnvironmentController,
    LayoutController,
    PackageController,
    RunDebugController,
    StatusBarController,
)
from .services import (
    AIService,
    EnvironmentService,
    OfflineModeError,
    OfflineService,
    PackageService,
    ProcessSecurityError,
    ProcessService,
    ServiceContainer,
)

_LOGGER: logging.Logger = logging.getLogger("src.app")

# Bump this whenever a QSplitter's pane composition changes (widgets added,
# removed, or reordered). _restore_layout_state() uses it to refuse to apply
# saved pane sizes from an incompatible older layout shape.
_LAYOUT_SCHEMA_VERSION = 3

_LINTER_OUTPUT_PATTERNS: dict[LintTool, re.Pattern[str]] = {
    LintTool.FLAKE8: re.compile(
        r"^(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<code>[A-Z]\d+)\s+(?P<msg>.+)$"
    ),
    LintTool.PYLINT: re.compile(
        r"^(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<code>[CWEF]\d+):\s*(?P<msg>.+)$"
    ),
    LintTool.MYPY: re.compile(
        r"^(?P<file>[^:]+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<msg>.+)$"
    ),
}

_LINTER_SEVERITY_MAPPERS: dict[LintTool, Callable[[str], LintSeverity]] = {
    LintTool.FLAKE8: lambda code: LintSeverity.WARNING,
    LintTool.PYLINT: lambda code: (
        LintSeverity.ERROR if code.startswith("E") else LintSeverity.WARNING
    ),
    LintTool.MYPY: lambda _code: LintSeverity.ERROR,
}


def extract_code_from_markdown(text: str) -> str:
    """Robustly extracts python source code from markdown backtick fences if present.

    Prevents raw Markdown explanations from polluting refactored code blocks.
    """
    # Look for explicitly fenced python blocks: ```python <source> ```
    pattern = r"```[ \t]*python\s*\n(.*?)\n\s*```"
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()

    # Fall back to general generic code blocks: ``` <source> ```
    generic_pattern = r"```\s*\n(.*?)\n\s*```"
    match = re.search(generic_pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Fallback to stripping leading or trailing standalone fences
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
    if cleaned.endswith("```"):
        cleaned = re.sub(r"\n?```$", "", cleaned)
    return cleaned.strip()


# -----------------------------------------------------------------------------
# Side-by-Side Code Review Diff Layout Dialog
# -----------------------------------------------------------------------------


class DiffDialog(QDialog):
    """An interactive window showing side-by-side original and AI-modified code."""

    def __init__(
        self,
        original_code: str,
        modified_code: str,
        palette: ColorPalette,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review AI Refactoring Diffs")
        self.resize(1100, 750)

        self.setStyleSheet(
            f"QDialog {{ background-color: {palette.background}; color: {palette.text}; }}"
            f"QLabel {{ font-weight: bold; color: {palette.text}; }}"
            f"QPushButton {{ background-color: {palette.panel}; color: {palette.text}; border: 1px solid {palette.border}; border-radius: 3px; padding: 6px 12px; }}"
            f"QPushButton:hover {{ background-color: {palette.selection}; }}"
        )

        label_left = QLabel("Current Source Code:")
        label_right = QLabel("Refactored Code Adjustments:")

        self.original_view = QPlainTextEdit(self)
        self.original_view.setReadOnly(True)
        self.original_view.setFont(QFont("Consolas", 10))
        self.original_view.setPlainText(original_code)

        self.modified_view = QPlainTextEdit(self)
        self.modified_view.setFont(QFont("Consolas", 10))
        self.modified_view.setPlainText(modified_code)

        col_layout = QHBoxLayout()
        left_box = QVBoxLayout()
        left_box.addWidget(label_left)
        left_box.addWidget(self.original_view)

        right_box = QVBoxLayout()
        right_box.addWidget(label_right)
        right_box.addWidget(self.modified_view)

        col_layout.addLayout(left_box, 1)
        col_layout.addLayout(right_box, 1)

        self.accept_button = QPushButton("Accept and Update Code", self)
        self.accept_button.clicked.connect(self.accept)

        self.reject_button = QPushButton("Discard Suggestion", self)
        self.reject_button.clicked.connect(self.reject)

        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        button_layout.addWidget(self.reject_button)
        button_layout.addWidget(self.accept_button)

        layout = QVBoxLayout(self)
        layout.addLayout(col_layout)
        layout.addLayout(button_layout)

    def get_modified_code(self) -> str:
        """Return the user-reviewed code block."""
        return self.modified_view.toPlainText()


# -----------------------------------------------------------------------------
# Main Orchestrator Window
# -----------------------------------------------------------------------------


class MainWindow(QMainWindow):
    """The central graphical user interface manager for PipViper."""

    def __init__(self, application_config: AppConfig) -> None:
        super().__init__()
        self._config: AppConfig = application_config
        self._palette: ColorPalette = get_palette(application_config.theme)
        self._project_root: Path | None = None
        self._run_process: QProcess | None = None
        self._debug_process: QProcess | None = None
        self._debug_stdout_buffer: str = ""
        self._configured_editors: set[int] = set()

        configure_logging(log_directory=application_config.log_directory)
        _LOGGER.info(
            "PipViper application starting (Theme: %s)",
            application_config.theme.value,
        )

        self.setWindowTitle("PipViper IDE")
        self.resize(1500, 950)

        self._init_services()
        self._init_timers()
        self._init_panels()
        self._init_layout()
        self._init_controllers()
        self._wire_signals()

        self._build_menu()
        self._build_toolbar()
        self._build_status_bar()
        self._apply_theme()
        self._open_welcome_buffer()
        self._restore_layout_state()
        self._check_first_run_offline_prompt()

    def _init_services(self) -> None:
        """Initialize central service composition root and subsystem services (PRD A3, A5)."""
        self._container = ServiceContainer.get_instance()
        self._process_service = ProcessService.get_instance()
        self._offline_service = OfflineService.get_instance()
        self._package_service = PackageService.get_instance()
        self._ai_service = AIService.get_instance()
        self._env_service = EnvironmentService.get_instance()

        self._jedi: JediService = JediService(self)
        self._git_service: GitService = GitService()
        self._ruff_service: RuffService = RuffService(self._project_root)
        self._mypy_service: MypyService = MypyService(self._project_root)
        self._pytest_engine: PytestEngine = PytestEngine(self)
        self._env_detector: EnvironmentDetector = EnvironmentDetector(self._project_root)
        self._active_env: PythonEnvironment = self._resolve_initial_environment()
        self._dep_scanner: DependencyScanner = DependencyScanner(self._project_root)

    def _init_timers(self) -> None:
        """Initialize debounced update timers."""
        self._scan_debounce_timer = QTimer(self)
        self._scan_debounce_timer.setSingleShot(True)
        self._scan_debounce_timer.setInterval(2000)
        self._scan_debounce_timer.timeout.connect(self._on_scan_debounce_timeout)

        self._diff_debounce_timer = QTimer(self)
        self._diff_debounce_timer.setSingleShot(True)
        self._diff_debounce_timer.setInterval(350)
        self._diff_debounce_timer.timeout.connect(self._refresh_gutter_diffs)

        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(self._on_autosave_timeout)
        if self._config.auto_save_seconds > 0:
            self._autosave_timer.start(self._config.auto_save_seconds * 1000)

        self._diag_debounce_timer = QTimer(self)
        self._diag_debounce_timer.setSingleShot(True)
        self._diag_debounce_timer.setInterval(250)
        self._diag_debounce_timer.timeout.connect(self._run_live_diagnostics)

    def _init_panels(self) -> None:
        """Construct modular UI panel instances (PRD A2)."""
        self._file_explorer: FileExplorer = FileExplorer(self)
        self._outline: DocumentOutlineWidget = DocumentOutlineWidget(self)
        self._editor_tabs: EditorTabs = EditorTabs(
            self._palette, self._jedi, self._config, self
        )
        self._ai_panel: AiPanel = AiPanel(self._palette, self)
        self._ai_panel.setMinimumWidth(300)
        self._ai_panel_last_width: int = 340

        self._output_panel: OutputPanel = OutputPanel(self._palette, self)
        self._repl_panel: ReplPanel = ReplPanel(
            self._palette, self._config.python_executable, self
        )
        self._terminal_panel: TerminalPanel = TerminalPanel(self._palette, self)
        self._debug_panel: DebugPanel = DebugPanel(self._palette, self)
        self._lint_widget: LintWidget = LintWidget(self._palette, self)
        self._code_tools_panel: CodeToolsPanel = CodeToolsPanel(self._palette, self)
        self._git_widget: GitPanel = GitPanel(self._palette, self)
        self._pkg_widget: PackageManagerWidget = PackageManagerWidget(
            self._palette, self._config.python_executable, self
        )
        self._log_widget: LogWidget = LogWidget(self)
        self._log_widget.load_file(self._config.log_directory / "src.log")
        self._internals_panel: InternalsPanel = InternalsPanel(self._palette, self)
        self._test_panel: TestRunnerPanel = TestRunnerPanel(self._palette, self)

        self._search_panel: SearchInFilesWidget = SearchInFilesWidget(self)
        self._quick_open_dialog: QuickOpenDialog = QuickOpenDialog(self)
        self._command_palette: CommandPaletteDialog = CommandPaletteDialog(self)
        self._dep_panel: DependencyStudioPanel = DependencyStudioPanel(self._palette, self)

        if self._project_root:
            self._quick_open_dialog.set_workspace_root(self._project_root)
            self._search_panel.set_workspace_root(self._project_root)

    def _init_layout(self) -> None:
        """Assemble splitters and panels into the main dock window layout."""
        # Sidebar: workspace tree stacked above the document outline
        self._sidebar_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self._sidebar_splitter.setChildrenCollapsible(False)
        self._sidebar_splitter.addWidget(self._file_explorer)
        self._sidebar_splitter.addWidget(self._outline)
        self._sidebar_splitter.setStretchFactor(0, 2)
        self._sidebar_splitter.setStretchFactor(1, 1)
        self._sidebar_splitter.setSizes([460, 260])

        # Content row: sidebar | editor | AI assistant
        self._content_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._content_splitter.setChildrenCollapsible(False)
        self._content_splitter.addWidget(self._sidebar_splitter)
        self._content_splitter.addWidget(self._editor_tabs)
        self._content_splitter.addWidget(self._ai_panel)
        self._content_splitter.setStretchFactor(0, 0)
        self._content_splitter.setStretchFactor(1, 1)
        self._content_splitter.setStretchFactor(2, 0)
        self._content_splitter.setSizes([300, 900, 340])
        self._content_splitter.setCollapsible(2, True)

        self._bottom_tabs = QTabWidget(self)
        for label_text, widget in [
            ("▶ Output", self._output_panel),
            ("🔍 Search", self._search_panel),
            ("🧪 Tests", self._test_panel),
            ("🐚 REPL", self._repl_panel),
            ("💻 Terminal", self._terminal_panel),
            ("🔬 Internals", self._internals_panel),
            ("🐞 Debug", self._debug_panel),
            ("🔍 Lint", self._lint_widget),
            ("🛠 Code Tools", self._code_tools_panel),
            ("🔀 Git", self._git_widget),
            ("📦 Packages", self._pkg_widget),
            ("🕸 Dependencies", self._dep_panel),
            ("📜 Log", self._log_widget),
        ]:
            self._bottom_tabs.addTab(widget, label_text)
        self._bottom_tabs.setMinimumHeight(150)
        self._bottom_tabs.currentChanged.connect(self._on_bottom_tab_changed)

        self._main_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.addWidget(self._content_splitter)
        self._main_splitter.addWidget(self._bottom_tabs)
        self._main_splitter.setStretchFactor(0, 3)
        self._main_splitter.setStretchFactor(1, 1)
        self._main_splitter.setSizes([650, 300])
        self.setCentralWidget(self._main_splitter)

    def _init_controllers(self) -> None:
        """Instantiate focused controllers (PRD A1)."""
        self._layout_controller = LayoutController(
            self,
            self._main_splitter,
            self._content_splitter,
            self._sidebar_splitter,
            self._bottom_tabs,
            self._ai_panel,
            self,
        )
        self._editor_controller = EditorController(self._editor_tabs, self)
        self._run_debug_controller = RunDebugController(self._process_service, self)
        self._package_controller = PackageController(
            self._package_service, self._offline_service, self
        )
        self._ai_controller = AIController(self._ai_service, self)
        self._env_controller = EnvironmentController(self._env_service, self)

    def _wire_signals(self) -> None:
        """Route and connect signals between components and controllers."""
        self._jedi.results.connect(self._on_jedi_result)
        self._file_explorer.file_activated.connect(self._open_file)
        self._outline.item_selected.connect(self._jump_in_current_editor)

        self._editor_tabs.editor_changed.connect(self._on_editor_changed)
        self._editor_tabs.unsaved_close_requested.connect(self._on_unsaved_close_requested)
        self._editor_tabs.send_to_repl_requested.connect(self._on_send_to_repl)
        self._editor_tabs.breakpoint_toggled.connect(self._on_breakpoint_toggled)
        self._editor_tabs.test_run_requested.connect(self._on_gutter_test_run_requested)
        self._editor_tabs.open_file_requested.connect(
            lambda file_path, line, col: self._open_file_at(file_path, line, col)
        )
        self._editor_tabs.install_requested.connect(
            lambda modules: self._install_missing_dependencies(
                modules, self._editor_tabs.current_editor()
            ) if self._editor_tabs.current_editor() else None
        )

        self._ai_panel.chat_submitted.connect(self._on_ai_chat_submitted)
        self._ai_panel.refactor_requested.connect(self._on_ai_refactor_requested)

        self._debug_panel.start_requested.connect(self._start_visual_debugging)
        self._debug_panel.continue_requested.connect(self._on_debug_continue)
        self._debug_panel.step_over_requested.connect(self._on_debug_step_over)
        self._debug_panel.step_into_requested.connect(self._on_debug_step_into)
        self._debug_panel.step_out_requested.connect(self._on_debug_step_out)
        self._debug_panel.stop_requested.connect(self._stop_debugger)
        self._debug_panel.restart_requested.connect(self._restart_debugging)
        self._debug_panel.frame_jump_requested.connect(self._on_debug_frame_jump)
        self._debug_panel.frame_selected.connect(self._on_debug_frame_selected)
        self._debug_panel.breakpoint_removed.connect(self._on_debug_breakpoint_removed)
        self._debug_panel.launch_requested.connect(self._run_debugger)

        self._lint_widget.run_requested.connect(self._run_linters)
        self._lint_widget.issue_activated.connect(self._jump_to_lint_issue)

        self._code_tools_panel.check_syntax_requested.connect(self._on_check_syntax_requested)
        self._code_tools_panel.autofix_requested.connect(self._on_autofix_requested)
        self._code_tools_panel.format_requested.connect(self._on_format_requested)
        self._code_tools_panel.organize_imports_requested.connect(self._on_organize_imports_requested)
        self._code_tools_panel.remove_unused_requested.connect(self._on_remove_unused_requested)
        self._code_tools_panel.full_pipeline_requested.connect(self._on_full_pipeline_requested)
        self._code_tools_panel.generate_docstrings_requested.connect(self._on_generate_docstrings_requested)
        self._code_tools_panel.generate_tests_requested.connect(self._on_generate_tests_requested)
        self._code_tools_panel.rename_requested.connect(self._on_rename_requested)
        self._code_tools_panel.issue_activated.connect(self._jump_to_lint_issue)

        self._git_widget.file_activated.connect(self._open_file)
        self._git_widget.diff_requested.connect(self._show_diff_viewer)
        self._git_widget.status_refreshed.connect(self._on_git_status_refreshed)
        self._git_widget.branch_changed.connect(lambda _b: self._on_git_status_refreshed())

        self._pkg_widget.refresh_requested.connect(self._refresh_packages)
        self._pkg_widget.install_requested.connect(self._install_package)
        self._pkg_widget.uninstall_requested.connect(self._uninstall_package)

        self._internals_panel.line_activated.connect(self._jump_to_internals_line)
        self._internals_panel.node_activated.connect(self._jump_to_internals_node)
        self._internals_panel.file_and_line_activated.connect(self._on_internals_file_and_line_activated)
        self._internals_panel.profile_requested.connect(self._on_profile_code_requested)

        self._test_panel.run_all_requested.connect(self._run_all_tests)
        self._test_panel.run_failed_requested.connect(self._run_failed_tests)
        self._test_panel.run_file_requested.connect(self._run_file_tests)
        self._test_panel.run_test_requested.connect(self._run_single_test)
        self._test_panel.refresh_requested.connect(self._refresh_test_discovery)
        self._test_panel.stop_requested.connect(self._pytest_engine.stop_run)
        self._test_panel.jump_to_source_requested.connect(self._jump_to_source_location)

        self._pytest_engine.test_started.connect(self._on_test_started)
        self._pytest_engine.test_finished.connect(self._on_test_finished)
        self._pytest_engine.run_finished.connect(self._on_test_run_finished)
        self._pytest_engine.discovery_finished.connect(self._test_panel.set_tests)

        self._search_panel.match_selected.connect(
            lambda file_path, line, col: self._open_file_at(file_path, line, col)
        )
        self._quick_open_dialog.file_selected.connect(
            lambda file_path, line: self._open_file_at(file_path, line, 0)
        )

        self._dep_panel.refresh_requested.connect(self._refresh_dependencies)
        self._dep_panel.upgrade_requested.connect(self._upgrade_package)
        self._dep_panel.uninstall_requested.connect(self._uninstall_package)
        self._dep_panel.sync_requirements_requested.connect(self._show_requirements_sync_dialog)

    def _check_first_run_offline_prompt(self) -> None:
        """Prompt user on first run to choose Offline Mode vs Limited-Online Mode (PRD P6)."""
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return

        settings = QSettings("PipViper", "PipViperIDE")
        if settings.value("first_run_prompt_completed", False, type=bool):
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Welcome to PipViper IDE — Offline Choice")
        dialog.resize(560, 360)
        layout = QVBoxLayout(dialog)

        title = QLabel("🔒 Choose Your Network Operating Mode", dialog)
        title.setStyleSheet("font-size: 16px; font-weight: bold; margin-bottom: 8px;")
        layout.addWidget(title)

        desc = QLabel(
            "PipViper is an offline-first Python IDE.\n\n"
            "• Offline Mode (Default & Recommended):\n"
            "  Zero network sockets are opened. Core editing, running, debugging,\n"
            "  linting, formatting, testing, local Git, and local AI (Ollama) work 100% offline.\n"
            "  PyPI package installs and external network requests are strictly blocked.\n\n"
            "• Limited Online Mode:\n"
            "  Allows explicit package installations and updates from PyPI.\n"
            "  Telemetry and silent background network calls remain completely disabled.\n\n"
            "You can change this setting at any time via the badge in the status bar.",
            dialog,
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        btn_layout = QHBoxLayout()
        btn_offline = QPushButton("🔒 Keep Offline Mode (Recommended)", dialog)
        btn_offline.setStyleSheet("padding: 8px 16px; font-weight: bold;")
        btn_online = QPushButton("🌐 Enable Limited Online Mode", dialog)
        btn_online.setStyleSheet("padding: 8px 16px;")

        def choose_offline() -> None:
            self._offline_service.set_offline(True)
            settings.setValue("first_run_prompt_completed", True)
            dialog.accept()

        def choose_online() -> None:
            self._offline_service.set_offline(False)
            settings.setValue("first_run_prompt_completed", True)
            dialog.accept()

        btn_offline.clicked.connect(choose_offline)
        btn_online.clicked.connect(choose_online)

        btn_layout.addWidget(btn_offline)
        btn_layout.addWidget(btn_online)
        layout.addLayout(btn_layout)

        dialog.exec()

    def get_runtime_python(self) -> str:
        """Resolve the python executable for executing user code.

        If a local .venv folder is found within the active project root,
        uses its python interpreter. Otherwise, falls back to the configured
        interpreter.
        """
        if hasattr(self, "_active_env") and self._active_env and self._active_env.executable.is_file():
            return str(self._active_env.executable)

        if self._project_root:
            venv_python_win = self._project_root / ".venv" / "Scripts" / "python.exe"
            venv_python_unix = self._project_root / ".venv" / "bin" / "python"

            if venv_python_win.is_file():
                return str(venv_python_win)
            if venv_python_unix.is_file():
                return str(venv_python_unix)

        return self._config.python_executable

    def _restore_layout_state(self) -> None:
        """Restore window geometry and splitter sizes saved from a prior session.

        Splitter state is only restored when it was saved under a matching
        ``_LAYOUT_SCHEMA_VERSION``. Restoring saved pane positions into a
        splitter that now has a different number of panes (e.g. after adding
        the AI assistant column) makes Qt silently collapse whichever pane it
        doesn't recognize to zero width -- which is exactly what made the AI
        panel disappear after this update. A version mismatch falls back to
        the fresh sizes already set in __init__ instead of trusting the stale
        state.
        """
        settings = QSettings("PipViper", "PipViperIDE")

        saved_geometry = settings.value("window/geometry")
        if saved_geometry is not None:
            self.restoreGeometry(saved_geometry)

        saved_schema_version = settings.value("layout/schemaVersion", 0, type=int)
        if saved_schema_version != _LAYOUT_SCHEMA_VERSION:
            return

        for settings_key, splitter in (
            ("layout/sidebarSplitter", self._sidebar_splitter),
            ("layout/contentSplitter", self._content_splitter),
            ("layout/mainSplitter", self._main_splitter),
        ):
            saved_state = settings.value(settings_key)
            if saved_state is not None:
                splitter.restoreState(saved_state)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Confirm discarding unsaved changes, then persist window/layout state."""
        if self._editor_tabs.has_unsaved_changes():
            unsaved_names = ", ".join(
                editor.file_path().name for editor in self._editor_tabs.modified_editors()
            )
            response = QMessageBox.question(
                self,
                "Unsaved Changes",
                f"The following file(s) have unsaved changes:\n\n{unsaved_names}\n\n"
                "Quit without saving?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if response != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

        try:
            self._diag_debounce_timer.stop()
        except Exception:
            pass
        try:
            self._scan_debounce_timer.stop()
        except Exception:
            pass
        try:
            self._autosave_timer.stop()
        except Exception:
            pass
        try:
            self._repl_panel.cleanup()
        except Exception:
            pass
        try:
            self._terminal_panel.cleanup()
        except Exception:
            pass
        try:
            self._stop_debugger()
        except Exception:
            pass
        try:
            self._pytest_engine.stop_run()
        except Exception:
            pass
        try:
            self._jedi.stop()
        except Exception:
            pass

        settings = QSettings("PipViper", "PipViperIDE")
        settings.setValue("window/geometry", self.saveGeometry())
        settings.setValue("layout/schemaVersion", _LAYOUT_SCHEMA_VERSION)
        settings.setValue("layout/sidebarSplitter", self._sidebar_splitter.saveState())
        settings.setValue("layout/contentSplitter", self._content_splitter.saveState())
        settings.setValue("layout/mainSplitter", self._main_splitter.saveState())
        super().closeEvent(event)

    def _get_virtualenv_path(self) -> str:
        """Dynamically resolve the virtual environment path based on python_executable."""
        if hasattr(self, "_active_env") and self._active_env:
            return str(self._active_env.prefix)

        python_path = Path(self._config.python_executable)

        # 1. Inspect standard layout directories: .venv/bin/python or .venv/Scripts/python.exe
        if python_path.parent.name in ("Scripts", "bin"):
            candidate = python_path.parent.parent
            if (candidate / "pyvenv.cfg").is_file():
                return str(candidate)

        # 2. Check if a local .venv folder exists in project workspace root
        if self._project_root:
            local_venv = self._project_root / ".venv"
            if local_venv.is_dir() and (local_venv / "pyvenv.cfg").is_file():
                return str(local_venv)

        # Fallback to general dynamic layout
        return str(python_path.parent.parent)

    def _apply_theme(self) -> None:
        self._palette = get_palette(self._config.theme)
        self.setStyleSheet(get_stylesheet(self._palette))
        self._editor_tabs.set_palette(self._palette)

        # Cascade palette refreshes dynamically to every single nested tab pane
        self._output_panel.set_palette(self._palette)
        self._test_panel.set_palette(self._palette)
        self._repl_panel.set_palette(self._palette)
        self._terminal_panel.set_palette(self._palette)
        self._debug_panel.set_palette(self._palette)
        self._lint_widget.set_palette(self._palette)
        self._code_tools_panel.set_palette(self._palette)
        self._git_widget.set_palette(self._palette)
        self._pkg_widget.set_palette(self._palette)
        self._internals_panel.set_palette(self._palette)
        self._dep_panel.set_palette(self._palette)
        if hasattr(self, "_env_selector"):
            self._env_selector.set_palette(self._palette)
        if hasattr(self, "_memory_meter"):
            self._memory_meter.set_palette(self._palette)
        if hasattr(self, "_offline_badge"):
            self._offline_badge.set_palette(self._palette)

        self._theme_label.setText(self._config.theme.value.capitalize())
        self._status_label.setText(
            f"Active theme updated to: {self._config.theme.value}"
        )

    def set_theme(self, theme: EditorTheme) -> None:
        """Apply a new application editor theme."""
        self._config = self._config.model_copy(update={"theme": theme})
        self._apply_theme()

    def _toggle_ai_panel(self, checked: bool) -> None:
        """Show or hide the AI assistant column, remembering its last width."""
        sizes = self._content_splitter.sizes()
        if checked:
            restored_width = self._ai_panel_last_width or 340
            sizes[1] = max(200, sizes[1] - restored_width)
            sizes[2] = restored_width
        else:
            if sizes[2] > 0:
                self._ai_panel_last_width = sizes[2]
            sizes[1] += sizes[2]
            sizes[2] = 0
        self._content_splitter.setSizes(sizes)

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("&File")
        self._add_action(
            file_menu, "&New File", QKeySequence.StandardKey.New, self._new_file
        )
        self._add_action(
            file_menu,
            "&Open File...",
            QKeySequence.StandardKey.Open,
            self._open_file_dialog,
        )
        self._add_action(file_menu, "Open &Folder...", None, self._open_folder_dialog)
        file_menu.addSeparator()
        self._add_action(
            file_menu, "&Quick Open...", QKeySequence("Ctrl+P"), self._open_quick_open
        )
        self._add_action(
            file_menu, "&Save", QKeySequence.StandardKey.Save, self._save_current
        )
        self._add_action(
            file_menu,
            "Save &As...",
            QKeySequence.StandardKey.SaveAs,
            self._save_current_as,
        )
        file_menu.addSeparator()
        file_menu.addAction("E&xit", self.close)

        edit_menu = menu_bar.addMenu("&Edit")
        self._add_action(
            edit_menu,
            "&Command Palette...",
            QKeySequence("Ctrl+Shift+P"),
            self._open_command_palette,
        )
        self._add_action(
            edit_menu, "&Find...", QKeySequence.StandardKey.Find, self._find
        )
        self._add_action(
            edit_menu,
            "Find in &Files...",
            QKeySequence("Ctrl+Shift+H"),
            self._focus_search_in_files,
        )
        self._add_action(
            edit_menu, "&Go to Line...", QKeySequence("Ctrl+G"), self._goto_line
        )
        self._add_action(
            edit_menu, "&Go to Definition", QKeySequence("F12"), self._on_definition_requested
        )
        edit_menu.addSeparator()
        self._add_action(
            edit_menu,
            "&Select Next Occurrence",
            QKeySequence("Ctrl+D"),
            self._on_select_next_occurrence,
        )
        self._add_action(
            edit_menu,
            "&Fold Block",
            QKeySequence("Ctrl+Shift+["),
            self._on_fold_block,
        )
        self._add_action(
            edit_menu,
            "&Unfold Block",
            QKeySequence("Ctrl+Shift+]"),
            self._on_unfold_block,
        )
        self._add_action(edit_menu, "Fold &All", None, self._on_fold_all)
        self._add_action(edit_menu, "Unfold All", None, self._on_unfold_all)

        view_menu = menu_bar.addMenu("&View")
        theme_menu = view_menu.addMenu("&Theme")
        for theme in EditorTheme:
            action = QAction(theme.value.capitalize(), self)
            action.triggered.connect(
                lambda _checked_state=False, t=theme: self.set_theme(t)
            )
            theme_menu.addAction(action)
        view_menu.addSeparator()
        self._toggle_ai_action = QAction("Show AI &Assistant Column", self)
        self._toggle_ai_action.setCheckable(True)
        self._toggle_ai_action.setChecked(True)
        self._toggle_ai_action.setShortcut(QKeySequence("Ctrl+Shift+A"))
        self._toggle_ai_action.triggered.connect(self._toggle_ai_panel)
        view_menu.addAction(self._toggle_ai_action)
        view_menu.addSeparator()
        self._add_action(
            view_menu,
            "Split Editor &Right",
            QKeySequence("Ctrl+\\"),
            self._on_split_right,
        )
        self._add_action(
            view_menu,
            "Split Editor &Down",
            QKeySequence("Ctrl+Alt+\\"),
            self._on_split_down,
        )
        self._add_action(
            view_menu,
            "&Close Split Pane",
            None,
            self._on_close_split,
        )
        self._add_action(
            view_menu,
            "Open &Git Source Control",
            QKeySequence("Ctrl+Shift+G"),
            self._show_git_panel,
        )

        run_menu = menu_bar.addMenu("&Run")

        self._add_action(
            run_menu,
            "&Start / Continue Debugging",
            QKeySequence("F5"),
            self._on_debug_key_f5,
        )
        self._add_action(
            run_menu,
            "&Run (Without Debugging)",
            QKeySequence("Ctrl+F5"),
            self._run_current,
        )
        self._add_action(
            run_menu,
            "Step &Over",
            QKeySequence("F10"),
            self._on_debug_step_over,
        )
        self._add_action(
            run_menu,
            "Step &Into",
            QKeySequence("F11"),
            self._on_debug_step_into,
        )
        self._add_action(
            run_menu,
            "Step O&ut",
            QKeySequence("Shift+F11"),
            self._on_debug_step_out,
        )
        self._add_action(
            run_menu,
            "Toggle &Breakpoint",
            QKeySequence("F9"),
            self._toggle_current_line_breakpoint,
        )
        self._add_action(
            run_menu,
            "&Stop Debugging",
            QKeySequence("Shift+F5"),
            self._stop_debugger,
        )
        run_menu.addSeparator()
        self._add_action(
            run_menu,
            "Inspect &Internals (Bytecode, AST)",
            QKeySequence("F8"),
            self._show_internals_panel,
        )
        run_menu.addSeparator()
        self._add_action(
            run_menu,
            "Send Selection to REPL",
            QKeySequence("Ctrl+Return"),
            self._on_send_selection_menu,
        )
        self._add_action(
            run_menu,
            "Open &REPL",
            QKeySequence("Ctrl+Shift+R"),
            self._show_repl_panel,
        )
        self._add_action(
            run_menu,
            "Open &Terminal",
            QKeySequence("Ctrl+Shift+T"),
            self._show_terminal_panel,
        )
        run_menu.addSeparator()
        self._add_action(
            run_menu,
            "Run &All Tests",
            QKeySequence("Ctrl+Shift+U"),
            self._run_all_tests,
        )
        self._add_action(
            run_menu,
            "Run &Active File Tests",
            QKeySequence("Ctrl+Shift+F"),
            self._run_current_file_tests,
        )
        self._add_action(
            run_menu,
            "Run &Mypy Type Check",
            QKeySequence("F7"),
            self._run_mypy_check,
        )
        run_menu.addSeparator()
        self._add_action(
            run_menu,
            "Select &Python Interpreter...",
            QKeySequence("Ctrl+Shift+I"),
            self._show_environment_picker,
        )
        self._add_action(
            run_menu,
            "Open &Dependency Studio",
            None,
            self._show_dependencies_panel,
        )
        self._add_action(
            run_menu,
            "&Sync Requirements...",
            None,
            self._show_requirements_sync_dialog,
        )

        help_menu = menu_bar.addMenu("&Help")
        help_menu.addAction("&About", self._show_about)
        help_menu.addAction("&Keyboard Shortcuts", self._show_shortcuts)

        # Index all menus into Universal Command Palette
        for menu in [file_menu, edit_menu, view_menu, run_menu, help_menu]:
            self._command_palette.register_qactions_from_menu(menu.title(), menu.actions())

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main", self)
        toolbar.setMovable(False)
        for label_text, slot in [
            ("📄 New", self._new_file),
            ("📂 Open", self._open_file_dialog),
            ("💾 Save", self._save_current),
            ("▶ Run", self._run_current),
            ("🧪 Tests", self._show_tests_panel),
            ("🐞 Debug", self._start_visual_debugging),
            ("■ Stop", self._stop_run),
            ("🔬 Internals", self._show_internals_panel),
            ("🔍 Lint", self._run_linters),
            ("🛠 Code Tools", self._show_code_tools_panel),
            ("📦 Packages", self._refresh_packages),
            ("🕸 Dependencies", self._show_dependencies_panel),
            ("🌿 Git", self._show_git_panel),
        ]:
            action = QAction(label_text, self)
            action.triggered.connect(slot)
            toolbar.addAction(action)
        self.addToolBar(toolbar)

    def _build_status_bar(self) -> None:
        bar = QStatusBar(self)
        self._status_label = QLabel("Ready", self)
        self._cursor_label = QLabel("Ln 1, Col 1", self)
        self._theme_label = QLabel(self._config.theme.value.capitalize(), self)
        self._env_selector = EnvironmentSelectorWidget(self._palette, self)
        self._env_selector.set_environment(self._active_env)
        self._env_selector.clicked.connect(self._show_environment_picker)

        self._git_branch_label = QLabel("🌿 (no repo)", self)
        self._git_branch_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._git_branch_label.setToolTip("Click to open Git Source Control")
        self._git_branch_label.mousePressEvent = lambda _e: self._show_git_panel()

        self._memory_meter = MemoryMeterWidget(self._palette, parent=self)
        self._memory_meter.open_memory_profiler_requested.connect(self._show_memory_profiler_panel)

        self._offline_badge = OfflineModeBadge(palette=self._palette, parent=self)

        bar.addWidget(self._status_label, 1)
        bar.addPermanentWidget(self._cursor_label)
        bar.addPermanentWidget(self._git_branch_label)
        bar.addPermanentWidget(self._offline_badge)
        bar.addPermanentWidget(self._memory_meter)
        bar.addPermanentWidget(self._env_selector)
        bar.addPermanentWidget(self._theme_label)
        self.setStatusBar(bar)


    def _add_action(
        self,
        menu: QMenu,
        text: str,
        shortcut: QKeySequence | QKeySequence.StandardKey | str | None,
        slot: Callable[[], None],
    ) -> None:
        action = QAction(text, self)
        if shortcut is not None:
            action.setShortcut(shortcut)
        action.triggered.connect(slot)
        menu.addAction(action)

    def _new_file(self) -> None:
        path = Path(f"untitled_{self._editor_tabs.editor_count() + 1}.py")
        self._editor_tabs.add_editor(path, "")

    def _open_file_dialog(self) -> None:
        start = str(self._project_root) if self._project_root else str(Path.home())
        file_path_str, _ = QFileDialog.getOpenFileName(
            self, "Open File", start, "Python Files (*.py);;All Files (*)"
        )
        if file_path_str:
            self._open_file(Path(file_path_str))

    def _open_folder_dialog(self) -> None:
        start = str(self._project_root) if self._project_root else str(Path.home())
        folder_str = QFileDialog.getExistingDirectory(self, "Open Folder", start)
        if folder_str:
            folder = Path(folder_str)
            self._project_root = folder
            self._file_explorer.set_root(folder)
            self._git_widget.set_project_root(folder)
            
            # --- Dynamic Swapping of Sub-Environments ---
            self._jedi.set_project_path(folder)
            self._repl_panel.set_project_root(folder)
            self._terminal_panel.set_project_root(folder)
            self._repl_panel.set_python_executable(self.get_runtime_python())
            self._repl_panel.reset_process()
            self._quick_open_dialog.set_workspace_root(folder)
            self._search_panel.set_workspace_root(folder)
            
            self._status_label.setText(f"Opened project: {folder}")

    def _open_file(self, path: Path) -> None:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exception:
            _LOGGER.exception("Failed to read %s", path)
            QMessageBox.warning(
                self, "Open File", f"Could not read {path}:\n{exception}"
            )
            return
        editor = self._editor_tabs.add_editor(path, content)
        editor.set_file_path(path)
        self._status_label.setText(f"Opened {path.name}")
        self._scan_dependencies(editor)

    def _open_file_at(self, file_path: str | Path, line: int = 1, col: int = 0) -> None:
        """Open a file at a specific line and column coordinate, centering the cursor."""
        path = Path(file_path).resolve()
        if not path.exists():
            return
        self._open_file(path)
        editor = self._editor_tabs.current_editor()
        if editor is not None:
            editor.jump_to_line(line, col)

    @Slot()
    def _open_quick_open(self) -> None:
        """Open the floating Quick Open file finder (Ctrl+P)."""
        if self._project_root:
            self._quick_open_dialog.set_workspace_root(self._project_root)
        self._quick_open_dialog.open_dialog()

    @Slot()
    def _open_command_palette(self) -> None:
        """Open the universal Command Palette launcher (Ctrl+Shift+P)."""
        self._command_palette.open_dialog()

    @Slot()
    def _focus_search_in_files(self) -> None:
        """Focus the Search in Files tab in the bottom dock (Ctrl+Shift+H)."""
        for i in range(self._bottom_tabs.count()):
            if self._bottom_tabs.widget(i) is self._search_panel:
                self._bottom_tabs.setCurrentIndex(i)
                break
        self._search_panel.focus_search()

    def _save_current(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        path = editor.file_path()
        if path.name.startswith("untitled_") or not path.exists():
            self._save_current_as()
            return
        if self._write_file(path, editor.toPlainText()):
            self._editor_tabs.notify_saved(editor)
            self._scan_dependencies(editor)
            self._refresh_gutter_diffs(path)
            self._git_widget.refresh_status()

    def _save_current_as(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        start = str(self._project_root) if self._project_root else str(Path.home())
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Save File As", start, "Python Files (*.py)"
        )
        if not path_str:
            return
        path = Path(path_str)
        if self._write_file(path, editor.toPlainText()):
            editor.set_file_path(path)
            self._editor_tabs.notify_saved(editor)
            self._scan_dependencies(editor)
            self._refresh_gutter_diffs(path)
            self._git_widget.refresh_status()

    def _write_file(self, path: Path, content: str) -> bool:

        """Write ``content`` to ``path``, returning whether the write succeeded."""
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as exception:
            _LOGGER.exception("Failed to save content write to %s", path)
            QMessageBox.warning(
                self, "Save File", f"Could not save workspace path:\n{exception}"
            )
            return False
        self._status_label.setText(f"Saved file {path.name}")
        return True

    def _on_unsaved_close_requested(self, tab_index: int) -> None:
        """Prompt to save/discard when a tab with unsaved changes is closed."""
        container = self._editor_tabs.container_at(tab_index)
        if container is None:
            return
        editor = container.editor
        response = QMessageBox.question(
            self,
            "Unsaved Changes",
            f"'{editor.file_path().name}' has unsaved changes.\n\nSave before closing?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if response == QMessageBox.StandardButton.Cancel:
            return
        if response == QMessageBox.StandardButton.Save:
            path = editor.file_path()
            if path.name.startswith("untitled_") or not path.exists():
                # Route through the normal Save As flow so the user can
                # choose a real path; if they cancel that dialog, the tab
                # stays open with its unsaved changes rather than vanishing.
                previous_current = self._editor_tabs.current_editor()
                self._editor_tabs.set_current_editor(editor)
                self._save_current_as()
                if editor.is_modified():
                    if previous_current is not None:
                        self._editor_tabs.set_current_editor(previous_current)
                    return
            elif not self._write_file(path, editor.toPlainText()):
                return
            else:
                self._editor_tabs.notify_saved(editor)
        current_index = self._editor_tabs.tab_index_for(editor)
        if current_index is not None:
            self._editor_tabs.close_tab(current_index)

    def _on_autosave_timeout(self) -> None:
        """Persist every open editor with unsaved changes to its existing path.

        Buffers that have never been saved (``untitled_*.py`` or a path that
        doesn't exist on disk yet) are skipped -- autosave should never pop
        a "Save As" dialog in the background while the user is typing.
        """
        saved_count = 0
        for editor in self._editor_tabs.modified_editors():
            path = editor.file_path()
            if path.name.startswith("untitled_") or not path.exists():
                continue
            if self._write_file(path, editor.toPlainText()):
                self._editor_tabs.notify_saved(editor)
                saved_count += 1
        if saved_count:
            self._status_label.setText(f"Auto-saved {saved_count} file(s).")

    def _on_editor_changed(self, editor: CodeEditor) -> None:
        if editor is None:
            return

        editor_identifier = id(editor)
        if editor_identifier not in self._configured_editors:
            editor.cursor_moved.connect(self._on_cursor_moved)
            editor.completions_requested.connect(self._on_completion_requested)
            editor.signatures_requested.connect(self._on_signature_requested)
            editor.hover_requested.connect(self._on_hover_requested)
            editor.definition_requested.connect(self._on_definition_requested)
            editor.references_requested.connect(self._on_references_requested)

            editor.textChanged.connect(self._trigger_dependency_scan)
            editor.textChanged.connect(self._trigger_live_diagnostics)
            editor.textChanged.connect(self._trigger_diff_refresh)
            editor.textChanged.connect(
                lambda: self._outline.update_from_source(editor.toPlainText())
            )

            container = self._get_editor_container(editor)
            if container is not None:
                container.banner.install_requested.connect(
                    lambda modules, ed=editor: self._install_missing_dependencies(
                        modules, ed
                    )
                )

            self._configured_editors.add(editor_identifier)

        self._outline.update_from_source(editor.toPlainText())
        self._scan_dependencies(editor)
        self._test_panel.set_active_file(editor.file_path())
        self._trigger_live_diagnostics()
        self._refresh_gutter_diffs()

    def _get_editor_container(self, editor: CodeEditor) -> EditorContainer | None:
        """Find the EditorContainer layout wrapping the specified editor instance."""
        return self._editor_tabs.container_for(editor)

    def _trigger_diff_refresh(self) -> None:
        self._diff_debounce_timer.start()

    def _show_git_panel(self) -> None:
        """Focus the Git panel in the bottom dock."""
        for i in range(self._bottom_tabs.count()):
            if self._bottom_tabs.widget(i) is self._git_widget:
                self._bottom_tabs.setCurrentIndex(i)
                break
        self._git_widget.refresh_status()

    def _on_git_status_refreshed(self) -> None:
        branch = self._git_widget._current_branch or "(no repo)"
        self._git_branch_label.setText(f"🌿 {branch}")
        self._refresh_gutter_diffs()

    def _show_diff_viewer(self, file_path: Path, is_staged: bool) -> None:
        """Open the rich visual diff dialog for the specified file."""
        if self._project_root is None:
            return

        old_text = self._git_service.get_file_at_head(self._project_root, file_path) or ""

        # Check if open in an editor tab first (to show live edits)
        ed = self._editor_tabs.editor_for_path(file_path)
        if ed is not None:
            new_text = ed.toPlainText()
        else:
            full_path = (
                self._project_root / file_path
                if not file_path.is_absolute()
                else file_path
            )
            if full_path.exists() and full_path.is_file():
                try:
                    new_text = full_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    new_text = ""
            else:
                new_text = ""

        dialog = DiffViewerDialog(
            palette=self._palette,
            file_path=file_path,
            old_text=old_text,
            new_text=new_text,
            old_title="HEAD",
            new_title="Working Tree" if not is_staged else "Staged Index",
            is_staged=is_staged,
            parent=self,
        )
        dialog.stage_requested.connect(self._git_widget._stage_file)
        dialog.unstage_requested.connect(self._git_widget._unstage_file)
        dialog.discard_requested.connect(self._git_widget._discard_file)
        dialog.exec()

    def _refresh_gutter_diffs(self, file_path: Path | None = None) -> None:
        """Recalculate Git diff hunks for the active editor and update gutter markers."""
        if self._project_root is None or not self._git_service.is_git_repo(self._project_root):
            return

        editor = self._editor_tabs.current_editor()
        if editor is None:
            return

        ed_path = editor.file_path()
        if file_path is not None and ed_path.resolve() != file_path.resolve():
            return

        root = self._project_root
        current_text = editor.toPlainText()

        def compute_hunks() -> list[GitDiffHunk]:
            base_text = self._git_service.get_file_at_head(root, ed_path)
            if base_text is None:
                return []
            return self._git_service.compute_file_diff_hunks(base_text, current_text)

        worker = run_in_thread(compute_hunks)
        worker.signals.result.connect(
            lambda hunks, p=ed_path: self._editor_tabs.set_git_diff_for_path(p, hunks)
        )

    def _trigger_dependency_scan(self) -> None:
        self._scan_debounce_timer.start()

    def _trigger_live_diagnostics(self) -> None:
        """Trigger debounced in-memory Ruff code diagnostics."""
        self._diag_debounce_timer.start()


    def _run_live_diagnostics(self) -> None:
        """Execute sub-10ms Ruff linting and update editor squiggles."""
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        source = editor.toPlainText()
        file_path = editor.file_path()

        def do_check() -> list[DiagnosticIssue]:
            return self._ruff_service.check_source(source, file_path)

        def on_check_done(issues: list[DiagnosticIssue]) -> None:
            curr = self._editor_tabs.current_editor()
            if curr is editor:
                editor.set_diagnostics(issues)

        worker = run_in_thread(do_check)
        worker.signals.result.connect(on_check_done)

    # -------------------------------------------------------------------------
    # Visual Pytest Runner Handlers
    # -------------------------------------------------------------------------

    def _show_tests_panel(self) -> None:
        """Focus the visual test runner dock and discover tests if empty."""
        self._bottom_tabs.setCurrentWidget(self._test_panel)
        if not self._test_panel._all_items:
            self._refresh_test_discovery()

    def _refresh_test_discovery(self) -> None:
        """Asynchronously discover tests across the active project root."""
        project_root = self._project_root or Path.cwd()
        py_exe = self.get_runtime_python()
        self._status_label.setText("Discovering tests...")

        def do_discover() -> list[TestItem]:
            return self._pytest_engine.discover_tests(project_root, py_exe)

        def on_discover_done(items: list[TestItem]) -> None:
            self._test_panel.set_tests(items)
            self._status_label.setText(f"Discovered {len(items)} test(s).")

        worker = run_in_thread(do_discover)
        worker.signals.result.connect(on_discover_done)

    def _run_all_tests(self) -> None:
        """Execute the entire test suite in the project workspace."""
        self._bottom_tabs.setCurrentWidget(self._test_panel)
        self._test_panel.set_running(True)
        project_root = self._project_root or Path.cwd()
        py_exe = self.get_runtime_python()
        self._status_label.setText("Running full test suite...")
        self._pytest_engine.start_run(project_root, py_exe)

    def _run_failed_tests(self) -> None:
        """Re-run only previously failed tests."""
        self._bottom_tabs.setCurrentWidget(self._test_panel)
        self._test_panel.set_running(True)
        project_root = self._project_root or Path.cwd()
        py_exe = self.get_runtime_python()
        self._status_label.setText("Running failed tests (--lf)...")
        self._pytest_engine.start_run(project_root, py_exe, failed_only=True)

    def _run_file_tests(self, file_path: Path) -> None:
        """Run tests located inside a specific file."""
        self._bottom_tabs.setCurrentWidget(self._test_panel)
        self._test_panel.set_running(True)
        project_root = self._project_root or Path.cwd()
        py_exe = self.get_runtime_python()
        self._status_label.setText(f"Running tests in {file_path.name}...")
        try:
            rel_path = str(file_path.relative_to(project_root)).replace("\\", "/")
        except ValueError:
            rel_path = str(file_path).replace("\\", "/")
        self._pytest_engine.start_run(project_root, py_exe, node_ids=[rel_path])

    def _run_current_file_tests(self) -> None:
        """Run tests in the currently open editor tab."""
        editor = self._editor_tabs.current_editor()
        if editor is not None:
            self._run_file_tests(editor.file_path())

    def _run_single_test(self, node_id: str) -> None:
        """Execute a targeted test item by its Pytest node ID."""
        self._bottom_tabs.setCurrentWidget(self._test_panel)
        self._test_panel.set_running(True)
        project_root = self._project_root or Path.cwd()
        py_exe = self.get_runtime_python()
        self._status_label.setText(f"Running test: {node_id}...")
        self._pytest_engine.start_run(project_root, py_exe, node_ids=[node_id])

    def _on_gutter_test_run_requested(
        self, file_path: Path, test_name: str, _line: int
    ) -> None:
        """Handle user clicking a gutter play button in the code editor."""
        project_root = self._project_root or Path.cwd()
        try:
            rel_path = str(file_path.relative_to(project_root)).replace("\\", "/")
        except ValueError:
            rel_path = str(file_path).replace("\\", "/")

        target_node = None
        for nid in self._test_panel._items_by_node_id:
            if nid.startswith(rel_path) and test_name in nid:
                target_node = nid
                break

        if target_node:
            self._run_single_test(target_node)
        else:
            self._bottom_tabs.setCurrentWidget(self._test_panel)
            self._test_panel.set_running(True)
            self._pytest_engine.start_run(
                project_root,
                self.get_runtime_python(),
                node_ids=[rel_path],
                extra_args=["-k", test_name],
            )

    @Slot(str)
    def _on_test_started(self, node_id: str) -> None:
        self._test_panel.update_test_status(node_id, TestStatus.RUNNING)

    @Slot(str, str, float, str)
    def _on_test_finished(
        self, node_id: str, status: str, duration_ms: float, traceback: str
    ) -> None:
        self._test_panel.update_test_status(node_id, status, duration_ms, traceback)

    @Slot(object)
    def _on_test_run_finished(self, summary: TestSuiteSummary) -> None:
        self._test_panel.set_running(False)
        self._test_panel.set_summary(summary)
        self._status_label.setText(
            f"Test run completed: {summary.passed} passed, {summary.failed} failed ({summary.duration_sec}s)"
        )

    def _jump_to_source_location(self, file_path: Path, line: int) -> None:
        """Jump to the source file and line from a test item double-click."""
        project_root = self._project_root or Path.cwd()
        full_path = project_root / file_path if not file_path.is_absolute() else file_path
        if full_path.is_file():
            self._open_file(full_path)
            editor = self._editor_tabs.current_editor()
            if editor:
                editor.jump_to_line(line, 0)

    def _run_mypy_check(self) -> None:
        """Run Mypy static type checking on the currently focused editor."""
        editor = self._editor_tabs.current_editor()
        if not editor:
            return
        file_path = editor.file_path()
        self._status_label.setText(f"Running Mypy type-check on {file_path.name}...")

        def do_mypy() -> list[DiagnosticIssue]:
            return self._mypy_service.check_file(file_path)

        def on_mypy_done(issues: list[DiagnosticIssue]) -> None:
            curr = self._editor_tabs.current_editor()
            if curr is editor:
                editor.set_diagnostics(issues)
            self._status_label.setText(
                f"Mypy check complete: {len(issues)} issue(s) reported."
            )
            lint_issues = [
                LintIssue(
                    tool=LintTool.MYPY,
                    file=iss.file_path,
                    line=iss.line,
                    column=iss.column,
                    code=iss.code,
                    message=iss.message,
                    severity=LintSeverity.ERROR if iss.severity == "error" else LintSeverity.WARNING,
                )
                for iss in issues
            ]
            self._lint_widget.populate_issues(lint_issues)
            if issues:
                self._bottom_tabs.setCurrentWidget(self._lint_widget)

        worker = run_in_thread(do_mypy)
        worker.signals.result.connect(on_mypy_done)

    def _on_scan_debounce_timeout(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        self._scan_dependencies(editor)

    def _scan_dependencies(self, editor: CodeEditor) -> None:
        """Analyze file source asynchronously for missing package dependencies."""
        source_code = editor.toPlainText()
        file_directory = editor.file_path().parent

        def perform_scan() -> list[str]:
            return get_missing_imports(source_code, file_directory)

        def handle_scan_result(missing_modules: list[str]) -> None:
            container = self._get_editor_container(editor)
            if container is not None:
                container.banner.show_warnings(missing_modules)

        worker = run_in_thread(perform_scan)
        worker.signals.result.connect(handle_scan_result)

    def get_package_manager_cmd(self, action: str, package_name: str | None = None) -> list[str]:
        """Resolves the correct terminal command structure for package operations.

        Dynamically auto-detects if 'uv' is present. If found, leverages its speed.
        If missing, falls back transparently to native pip running under the
        resolved runtime python interpreter.
        """
        import shutil
        python_bin = self.get_runtime_python()
        has_uv = shutil.which("uv") is not None

        if action == "list":
            if has_uv:
                virtualenv_path = self._get_virtualenv_path()
                return ["uv", "pip", "list", "--format=json", "--python", virtualenv_path]
            else:
                return [python_bin, "-m", "pip", "list", "--format=json"]

        elif action == "install":
            assert package_name is not None
            if has_uv:
                virtualenv_path = self._get_virtualenv_path()
                return ["uv", "pip", "install", package_name, "--python", virtualenv_path]
            else:
                return [python_bin, "-m", "pip", "install", package_name]

        elif action == "uninstall":
            assert package_name is not None
            if has_uv:
                virtualenv_path = self._get_virtualenv_path()
                return ["uv", "pip", "uninstall", "-y", package_name, "--python", virtualenv_path]
            else:
                return [python_bin, "-m", "pip", "uninstall", "-y", package_name]

        return []

    def _install_missing_dependencies(
        self, missing_modules: list[str], editor: CodeEditor
    ) -> None:
        """Asynchronously install missing dependency packages via uv or pip."""
        if self._offline_service.is_offline() and not self._offline_service.get_local_wheelhouse_dir():
            msg = (
                f"Cannot install dependencies {missing_modules}: PipViper is running in Offline Mode.\n\n"
                f"To install packages:\n"
                f"• Switch to Online Mode (Opt-in) via the status bar.\n"
                f"• Or configure a local wheelhouse directory for offline installs."
            )
            self._status_label.setText("Install blocked: Offline Mode active.")
            QMessageBox.warning(self, "Offline Mode Active", msg)
            return

        pypi_packages = [
            map_module_to_pypi(module_name) for module_name in missing_modules
        ]
        packages_string = ", ".join(pypi_packages)
        self._status_label.setText(f"Installing missing packages: {packages_string}...")

        def run_install() -> None:
            safe_env = self._process_service.build_safe_environment()
            for package in pypi_packages:
                cmd = self.get_package_manager_cmd("install", package)
                sanitized_cmd = self._process_service.sanitize_arguments(cmd)
                subprocess.run(sanitized_cmd, env=safe_env, timeout=300, check=True)

        def on_install_finished() -> None:
            self._status_label.setText(f"Successfully installed: {packages_string}")
            self._scan_dependencies(editor)
            self._refresh_packages()

        worker = run_in_thread(run_install)
        worker.signals.finished.connect(on_install_finished)
        worker.signals.error.connect(self._on_package_operation_failed)

    @Slot(int, int)
    def _on_cursor_moved(self, line: int, column: int) -> None:
        self._cursor_label.setText(f"Ln {line}, Col {column}")
        if hasattr(self, "_internals_panel") and self._bottom_tabs.currentWidget() == self._internals_panel:
            self._internals_panel.highlight_bytecode_for_line(line)

    @Slot(int, int)
    def _jump_in_current_editor(self, line: int, column: int) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        editor.jump_to_line(line, column)

    # -------------------------------------------------------------------------
    # Thread-Safe Jedi Signal Handlers
    # -------------------------------------------------------------------------

    @Slot(int, str, int, int)
    def _on_completion_requested(
        self, token: int, source: str, line: int, column: int
    ) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        self._jedi.request_completion(source, line, column, editor.file_path())

    @Slot(int, str, int, int)
    def _on_signature_requested(
        self, token: int, source: str, line: int, column: int
    ) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        self._jedi.request_signature(source, line, column, editor.file_path())

    @Slot(int, str, int, int)
    def _on_hover_requested(
        self, token: int, source: str, line: int, column: int
    ) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        self._jedi.request_hover(source, line, column, editor.file_path())

    @Slot()
    def _on_definition_requested(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        cursor = editor.textCursor()
        self._jedi.request_definition(
            editor.toPlainText(),
            cursor.blockNumber() + 1,
            cursor.columnNumber(),
            editor.file_path(),
        )

    @Slot()
    def _on_references_requested(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        cursor = editor.textCursor()
        self._jedi.request_references(
            editor.toPlainText(),
            cursor.blockNumber() + 1,
            cursor.columnNumber(),
            editor.file_path(),
        )

    @Slot()
    def _on_select_next_occurrence(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is not None:
            editor.select_next_occurrence()

    @Slot()
    def _on_fold_block(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is not None:
            editor.fold_current()

    @Slot()
    def _on_unfold_block(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is not None:
            editor.unfold_current()

    @Slot()
    def _on_fold_all(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is not None:
            editor.fold_all()

    @Slot()
    def _on_unfold_all(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is not None:
            editor.unfold_all()

    @Slot()
    def _on_split_right(self) -> None:
        self._editor_tabs.split_right()

    @Slot()
    def _on_split_down(self) -> None:
        self._editor_tabs.split_down()

    @Slot()
    def _on_close_split(self) -> None:
        self._editor_tabs.close_split()

    @Slot(int, object)
    def _on_jedi_result(self, token: int, result: JediResult) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        editor.apply_jedi_result(token, result)

    # -------------------------------------------------------------------------
    # Thread-Safe Program Execution Handles
    # -------------------------------------------------------------------------

    def _run_current(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        source_code = editor.toPlainText()
        if not source_code.strip():
            return
        file_path = editor.file_path()
        if file_path.name.startswith("untitled_"):
            QMessageBox.information(
                self, "Execution Error", "Save the file before running."
            )
            return
        if not file_path.exists():
            QMessageBox.warning(self, "Execution Error", f"File not found: {file_path}")
            return

        self._output_panel.clear()
        self._output_panel.append_stdout(f"--- execution started: {file_path} ---\n")

        self._run_process = QProcess(self)
        self._run_process.setProgram(self.get_runtime_python())
        self._run_process.setArguments(["-I", "-B", "-u", str(file_path)])
        self._run_process.setProcessChannelMode(
            QProcess.ProcessChannelMode.SeparateChannels
        )

        self._run_process.readyReadStandardOutput.connect(self._on_process_stdout_ready)
        self._run_process.readyReadStandardError.connect(self._on_process_stderr_ready)
        self._run_process.finished.connect(self._on_run_finished)
        self._run_process.start()

        self._bottom_tabs.setCurrentWidget(self._output_panel)

    @Slot()
    def _on_process_stdout_ready(self) -> None:
        if self._run_process is None:
            return
        output_bytes = bytes(self._run_process.readAllStandardOutput().data())
        output_text = output_bytes.decode("utf-8", errors="replace")
        self._output_panel.append_stdout(output_text)

    @Slot()
    def _on_process_stderr_ready(self) -> None:
        if self._run_process is None:
            return
        error_bytes = bytes(self._run_process.readAllStandardError().data())
        error_text = error_bytes.decode("utf-8", errors="replace")
        self._output_panel.append_stderr(error_text)

    def _stop_run(self) -> None:
        if (
            self._run_process is not None
            and self._run_process.state() != QProcess.ProcessState.NotRunning
        ):
            self._run_process.kill()

    @Slot(int, object)
    def _on_run_finished(self, exit_code: int, _exit_status: object) -> None:
        self._output_panel.append_stdout(
            f"\n--- process exited with code {exit_code} ---"
        )
        self._status_label.setText(f"Run completed (exit status: {exit_code})")

    # -------------------------------------------------------------------------
    # Visual Graphical Debugger & debugpy Execution Engine
    # -------------------------------------------------------------------------

    def _start_visual_debugging(self) -> None:
        """Launch the active file under PipViper's visual graphical debugger harness."""
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        file_path = editor.file_path()
        if file_path.name.startswith("untitled_") or not file_path.exists():
            QMessageBox.information(
                self, "Debug Error", "Save the file before starting debugging."
            )
            return

        self._stop_debugger()
        self._debug_panel.clear_console()
        self._debug_panel.log_message(
            f"--- Starting Visual Debugger for {file_path.name} ---"
        )

        all_bps = self._editor_tabs.get_all_breakpoints()
        bps_list = [
            {"file": str(fp), "line": ln}
            for fp, lines in all_bps.items()
            for ln in lines
        ]

        harness_path = Path(__file__).resolve().parent / "debug_harness.py"

        self._debug_process = QProcess(self)
        self._debug_process.setProgram(self.get_runtime_python())

        arguments = [
            str(harness_path),
            str(file_path),
            "--breakpoints",
            json.dumps(bps_list),
        ]
        if not bps_list:
            arguments.append("--stop-on-entry")

        self._debug_process.setArguments(arguments)
        self._debug_process.setProcessChannelMode(
            QProcess.ProcessChannelMode.SeparateChannels
        )
        if self._project_root:
            self._debug_process.setWorkingDirectory(str(self._project_root))
        else:
            self._debug_process.setWorkingDirectory(str(file_path.parent))

        self._debug_stdout_buffer = ""
        self._debug_process.readyReadStandardOutput.connect(self._on_visual_debug_stdout)
        self._debug_process.readyReadStandardError.connect(self._on_visual_debug_stderr)
        self._debug_process.finished.connect(self._on_visual_debug_finished)

        self._debug_process.start()
        self._debug_panel.set_session_state("running", str(file_path))
        self._debug_panel.update_breakpoints(all_bps)
        self._bottom_tabs.setCurrentWidget(self._debug_panel)

    @Slot()
    def _on_visual_debug_stdout(self) -> None:
        if self._debug_process is None:
            return
        raw = bytes(self._debug_process.readAllStandardOutput().data()).decode(
            "utf-8", errors="replace"
        )
        self._debug_stdout_buffer += raw

        while "\n" in self._debug_stdout_buffer:
            line, self._debug_stdout_buffer = self._debug_stdout_buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                continue

            if line.startswith("__PIPVIPER_DBG__"):
                payload_json = line[len("__PIPVIPER_DBG__") :]
                try:
                    payload = json.loads(payload_json)
                    self._handle_debugger_event(payload)
                except Exception as exc:
                    _LOGGER.warning("Failed parsing debugger event: %s", exc)
            else:
                self._debug_panel.log_message(line)

    @Slot()
    def _on_visual_debug_stderr(self) -> None:
        if self._debug_process is None:
            return
        raw = bytes(self._debug_process.readAllStandardError().data()).decode(
            "utf-8", errors="replace"
        )
        self._debug_panel.log_message(raw)

    @Slot(int, object)
    def _on_visual_debug_finished(self, exit_code: int, _exit_status: object) -> None:
        self._debug_panel.set_session_state("inactive")
        self._editor_tabs.clear_all_execution_lines()
        self._debug_panel.log_message(
            f"\n--- Debug process exited with code {exit_code} ---"
        )

    def _handle_debugger_event(self, payload: dict[str, Any]) -> None:
        event = payload.get("event")
        if event == "paused":
            file_str = payload.get("file", "")
            line = payload.get("line", 0)
            stack = payload.get("call_stack", [])
            locals_data = payload.get("locals", [])
            globals_data = payload.get("globals", [])
            exc = payload.get("exception")

            self._debug_panel.set_session_state("paused", file_str, line, exc)
            self._debug_panel.update_call_stack(stack)
            self._debug_panel.update_variables(locals_data, globals_data)

            if file_str and line > 0:
                p = Path(file_str)
                if not self._editor_tabs.editor_for_path(p) and p.exists():
                    self._open_file(p)
                self._editor_tabs.set_execution_line(p, line)

            self._bottom_tabs.setCurrentWidget(self._debug_panel)

        elif event == "resumed":
            self._debug_panel.set_session_state("running")
            self._editor_tabs.clear_all_execution_lines()

        elif event == "terminated":
            exit_code = payload.get("exit_code", 0)
            self._debug_panel.set_session_state("inactive")
            self._editor_tabs.clear_all_execution_lines()
            self._debug_panel.log_message(
                f"\n--- Execution terminated (exit code: {exit_code}) ---"
            )

        elif event == "frame_variables":
            locals_data = payload.get("locals", [])
            globals_data = payload.get("globals", [])
            self._debug_panel.update_variables(locals_data, globals_data)

        elif event == "error":
            err = payload.get("error", "")
            self._debug_panel.log_message(f"DEBUGGER ERROR: {err}")

    def _send_debug_command(self, cmd_dict: dict[str, Any]) -> None:
        if (
            self._debug_process is not None
            and self._debug_process.state() == QProcess.ProcessState.Running
        ):
            payload = json.dumps(cmd_dict) + "\n"
            self._debug_process.write(payload.encode("utf-8"))

    def _on_debug_key_f5(self) -> None:
        if self._debug_panel._is_paused:
            self._on_debug_continue()
        elif self._debug_panel._is_active:
            pass
        else:
            self._start_visual_debugging()

    def _on_debug_continue(self) -> None:
        self._send_debug_command({"cmd": "continue"})

    def _on_debug_step_over(self) -> None:
        self._send_debug_command({"cmd": "step_over"})

    def _on_debug_step_into(self) -> None:
        self._send_debug_command({"cmd": "step_into"})

    def _on_debug_step_out(self) -> None:
        self._send_debug_command({"cmd": "step_out"})

    def _restart_debugging(self) -> None:
        self._start_visual_debugging()

    def _on_debug_frame_jump(self, file_path_str: str, line: int) -> None:
        p = Path(file_path_str)
        if not self._editor_tabs.editor_for_path(p) and p.exists():
            self._open_file(p)
        editor = self._editor_tabs.editor_for_path(p)
        if editor:
            block = editor.document().findBlockByNumber(max(0, line - 1))
            if block.isValid():
                cursor = QTextCursor(block)
                editor.setTextCursor(cursor)
                editor.centerCursor()

    def _on_debug_frame_selected(self, level: int) -> None:
        self._send_debug_command({"cmd": "get_frame_variables", "level": level})

    def _on_debug_breakpoint_removed(self, file_path_str: str, line: int) -> None:
        p = Path(file_path_str)
        editor = self._editor_tabs.editor_for_path(p)
        if editor:
            bps = editor.get_breakpoints()
            if line in bps:
                editor.toggle_breakpoint(line)

    def _on_breakpoint_toggled(self, file_path: Path, line: int, is_set: bool) -> None:
        all_bps = self._editor_tabs.get_all_breakpoints()
        self._debug_panel.update_breakpoints(all_bps)
        if (
            self._debug_process is not None
            and self._debug_process.state() == QProcess.ProcessState.Running
        ):
            bps_list = [
                {"file": str(fp), "line": ln}
                for fp, lines in all_bps.items()
                for ln in lines
            ]
            self._send_debug_command(
                {"cmd": "set_breakpoints", "breakpoints": bps_list}
            )

    def _toggle_current_line_breakpoint(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor:
            cursor = editor.textCursor()
            line = cursor.blockNumber() + 1
            editor.toggle_breakpoint(line)

    def _stop_debugger(self) -> None:
        if self._debug_process is not None:
            if self._debug_process.state() != QProcess.ProcessState.NotRunning:
                try:
                    self._send_debug_command({"cmd": "stop"})
                    self._debug_process.waitForFinished(500)
                except Exception:
                    pass
                if self._debug_process.state() != QProcess.ProcessState.NotRunning:
                    self._debug_process.kill()
                    self._debug_process.waitForFinished(500)
            self._debug_process = None
        self._debug_panel.set_session_state("inactive")
        self._editor_tabs.clear_all_execution_lines()

    def _run_debugger(self, port: int, wait_for_client: bool) -> None:
        """Launch debugpy listener server for external remote clients."""
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        file_path = editor.file_path()
        if file_path.name.startswith("untitled_") or not file_path.exists():
            QMessageBox.information(
                self, "Debug Error", "Save the file before starting debugging."
            )
            return

        self._debug_panel.clear_console()
        self._debug_panel.log_message(
            f"--- Spawning debugpy server for {file_path.name} ---"
        )

        self._debug_process = QProcess(self)
        self._debug_process.setProgram(self.get_runtime_python())

        arguments = [
            "-Xfrozen_modules=off",
            "-m",
            "debugpy",
            "--listen",
            f"127.0.0.1:{port}",
        ]
        if wait_for_client:
            arguments.append("--wait-for-client")
        arguments.append(str(file_path))

        self._debug_process.setArguments(arguments)
        self._debug_process.setProcessChannelMode(
            QProcess.ProcessChannelMode.SeparateChannels
        )

        self._debug_process.readyReadStandardOutput.connect(self._on_debugpy_stdout_ready)
        self._debug_process.readyReadStandardError.connect(self._on_debugpy_stderr_ready)
        self._debug_process.finished.connect(self._on_debugpy_finished)

        self._debug_process.start()
        self._debug_panel.set_session_active(True, port)
        self._bottom_tabs.setCurrentWidget(self._debug_panel)

    @Slot()
    def _on_debugpy_stdout_ready(self) -> None:
        if self._debug_process is None:
            return
        output_bytes = bytes(self._debug_process.readAllStandardOutput().data())
        self._debug_panel.log_message(output_bytes.decode("utf-8", errors="replace"))

    @Slot()
    def _on_debugpy_stderr_ready(self) -> None:
        if self._debug_process is None:
            return
        error_bytes = bytes(self._debug_process.readAllStandardError().data())
        self._debug_panel.log_message(error_bytes.decode("utf-8", errors="replace"))

    @Slot(int, object)
    def _on_debugpy_finished(self, exit_code: int, _exit_status: object) -> None:
        self._debug_panel.log_message(
            f"\n--- debug process ended (Exit Code: {exit_code}) ---"
        )
        self._debug_panel.set_session_active(False)

    # -------------------------------------------------------------------------
    # Asynchronous Local-First AI Integration Channels
    # -------------------------------------------------------------------------

    @Slot(str, str, str)
    def _on_ai_chat_submitted(self, host: str, model: str, query_text: str) -> None:
        """Post chat conversations asynchronously to local AI sidecar."""
        system_prompt = (
            "You are an expert Python software engineering assistant. Provide direct, highly accurate, "
            "and production-ready answers. Write clear code blocks with full type annotations when providing code."
        )

        def perform_query() -> str:
            return query_local_llm(host, model, system_prompt, query_text)

        def on_query_success(answer: str) -> None:
            self._ai_panel.log_response(f"AI ({model})", answer)
            self._ai_panel.set_busy(False)

        def on_query_failed(error_message: str) -> None:
            self._ai_panel.log_response(
                "Error", f"Could not generate response: {error_message}"
            )
            self._ai_panel.set_busy(False)

        worker = run_in_thread(perform_query)
        worker.signals.result.connect(on_query_success)
        worker.signals.error.connect(on_query_failed)

    @Slot(str, str)
    def _on_ai_refactor_requested(self, host: str, model: str) -> None:
        """Asynchronously query the local AI sidecar to refactor the active Python code."""
        editor = self._editor_tabs.current_editor()
        if editor is None:
            self._ai_panel.log_response(
                "Error", "No active editor tab found to refactor."
            )
            self._ai_panel.set_busy(False)
            return

        original_code = editor.toPlainText()
        if not original_code.strip():
            self._ai_panel.log_response("Error", "Active editor is empty.")
            self._ai_panel.set_busy(False)
            return

        self._ai_panel.log_response(
            "System", f"Analyzing and refactoring {editor.file_path().name} locally..."
        )

        system_prompt = (
            "You are a professional Python refactoring assistant. Your task is to refactor the provided code "
            "to match modern Python best practices, follow PEP 8 styling rules, append clear docstrings and type "
            "annotations, and eliminate syntax logic redundant expressions. Keep all core execution logic, features, and algorithms "
            "strictly identical. Return ONLY the refactored, complete Python code. Do not wrap code in markdown block format "
            "such as ```python ... ```, do not include any conversational lines, and do not append explanations. Output "
            "only the plain valid python source file contents."
        )

        def perform_refactoring() -> str:
            return query_local_llm(host, model, system_prompt, original_code)

        def on_refactor_success(refactored_code: str) -> None:
            self._ai_panel.set_busy(False)
            self._ai_panel.log_response(
                "System",
                "Refactoring complete! Opening side-by-side diff review dialog...",
            )

            # Robust markdown code fence extractor parsing
            cleaned_code = extract_code_from_markdown(refactored_code)

            dialog = DiffDialog(original_code, cleaned_code, self._palette, self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                editor.replace_content(dialog.get_modified_code())
                self._ai_panel.log_response(
                    "System",
                    "Refactored changes successfully applied to active editor!",
                )
                self._scan_dependencies(editor)
            else:
                self._ai_panel.log_response(
                    "System", "Refactoring adjustments discarded by user."
                )

        def on_refactor_failed(error_message: str) -> None:
            self._ai_panel.log_response(
                "Error", f"Refactoring pipeline failed: {error_message}"
            )
            self._ai_panel.set_busy(False)

        worker = run_in_thread(perform_refactoring)
        worker.signals.result.connect(on_refactor_success)
        worker.signals.error.connect(on_refactor_failed)

    # -------------------------------------------------------------------------
    # Thread-Safe Async Linter Handlers
    # -------------------------------------------------------------------------

    def _run_linters(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        file_path = editor.file_path()
        if not file_path.exists():
            QMessageBox.warning(self, "Lint Error", "Save the file before linting.")
            return

        source_content = editor.toPlainText()

        def execute_all_linters() -> list[LintIssue]:
            detected_issues: list[LintIssue] = []
            for linter_tool in LintTool:
                try:
                    detected_issues.extend(
                        self._run_single_linter(linter_tool, file_path, source_content)
                    )
                except Exception as exception:
                    _LOGGER.error(
                        "Linter analysis %s failed: %s",
                        linter_tool.value,
                        exception,
                        exc_info=True,
                    )
            return detected_issues

        self._status_label.setText("Running code analysis...")
        worker = run_in_thread(execute_all_linters)
        worker.signals.result.connect(self._on_lint_completed)
        worker.signals.error.connect(self._on_lint_failed)
        self._bottom_tabs.setCurrentWidget(self._lint_widget)

    @Slot(object)
    def _on_lint_completed(self, issues: list[LintIssue]) -> None:
        self._lint_widget.populate(issues)
        self._status_label.setText(
            f"Lint analysis completed: {len(issues)} issue(s) detected."
        )

    @Slot(str)
    def _on_lint_failed(self, error_message: str) -> None:
        self._status_label.setText("Lint analysis failed.")
        QMessageBox.warning(
            self, "Lint Error", f"Linter execution failed:\n{error_message}"
        )

    @Slot(int, int)
    def _jump_to_lint_issue(self, line: int, column: int) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        editor.jump_to_line(line, max(0, column - 1))
        editor.setFocus()

    # -------------------------------------------------------------------------
    # Code Tools Panel: Diagnose, Format/Clean, Generate, Rename
    # -------------------------------------------------------------------------

    def _show_code_tools_panel(self) -> None:
        self._bottom_tabs.setCurrentWidget(self._code_tools_panel)

    def _preview_and_apply(
        self,
        editor: CodeEditor,
        original_source: str,
        new_source: str,
        operation_label: str,
    ) -> None:
        """Show a diff review for a code-tools mutation and apply it if accepted.

        No-ops (with a log line) if the operation produced no changes, so
        accepting an empty diff can never happen.
        """
        if new_source == original_source:
            self._code_tools_panel.log(f"{operation_label}: no changes needed.")
            return
        dialog = DiffDialog(original_source, new_source, self._palette, self)
        dialog.setWindowTitle(f"Review: {operation_label}")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            editor.replace_content(dialog.get_modified_code())
            self._code_tools_panel.log(f"{operation_label}: changes applied.")
            self._scan_dependencies(editor)
        else:
            self._code_tools_panel.log(f"{operation_label}: changes discarded.")

    @Slot()
    def _on_check_syntax_requested(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        source = editor.toPlainText()
        self._code_tools_panel.set_busy(True)
        self._show_code_tools_panel()

        def perform() -> SyntaxCheckResult:
            return SyntaxAutoFixer.check(source)

        def on_success(result: SyntaxCheckResult) -> None:
            self._code_tools_panel.set_busy(False)
            self._code_tools_panel.populate_issues(result.issues)
            editor.set_syntax_issues(result.issues)
            if result.is_valid:
                self._code_tools_panel.log("✓ No syntax errors found.")
            else:
                self._code_tools_panel.log(
                    f"✗ {len(result.issues)} syntax issue(s) found."
                )

        def on_error(message: str) -> None:
            self._code_tools_panel.set_busy(False)
            self._code_tools_panel.log(f"Syntax check failed: {message}")

        worker = run_in_thread(perform)
        worker.signals.result.connect(on_success)
        worker.signals.error.connect(on_error)

    @Slot()
    def _on_autofix_requested(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        source = editor.toPlainText()
        self._code_tools_panel.set_busy(True)
        self._show_code_tools_panel()

        def perform() -> SyntaxFixResult:
            return SyntaxAutoFixer.autofix(source)

        def on_success(result: SyntaxFixResult) -> None:
            self._code_tools_panel.set_busy(False)
            if result.applied_fixes:
                self._code_tools_panel.log(
                    "Auto-fix applied: " + "; ".join(result.applied_fixes)
                )
            if result.remaining_issue is not None:
                self._code_tools_panel.log(
                    f"Auto-fix stopped at line {result.remaining_issue.line}: "
                    f"{result.remaining_issue.message}"
                )
            editor.set_syntax_issues(
                [result.remaining_issue] if result.remaining_issue else []
            )
            self._preview_and_apply(
                editor, source, result.fixed_source, "Auto-Fix Syntax"
            )

        def on_error(message: str) -> None:
            self._code_tools_panel.set_busy(False)
            self._code_tools_panel.log(f"Auto-fix failed: {message}")

        worker = run_in_thread(perform)
        worker.signals.result.connect(on_success)
        worker.signals.error.connect(on_error)

    @Slot()
    def _on_format_requested(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        source = editor.toPlainText()
        self._code_tools_panel.set_busy(True)
        self._show_code_tools_panel()

        def perform() -> FormatResult:
            return CodeFormatter.format_source(source)

        def on_success(result: FormatResult) -> None:
            self._code_tools_panel.set_busy(False)
            self._preview_and_apply(
                editor, source, result.formatted_source, "Format (Black)"
            )

        def on_error(message: str) -> None:
            self._code_tools_panel.set_busy(False)
            self._code_tools_panel.log(f"Format failed: {message}")

        worker = run_in_thread(perform)
        worker.signals.result.connect(on_success)
        worker.signals.error.connect(on_error)

    @Slot()
    def _on_organize_imports_requested(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        source = editor.toPlainText()
        self._code_tools_panel.set_busy(True)
        self._show_code_tools_panel()

        def perform() -> ImportOrganizeResult:
            return CodeFormatter.organize_imports(source)

        def on_success(result: ImportOrganizeResult) -> None:
            self._code_tools_panel.set_busy(False)
            self._preview_and_apply(
                editor, source, result.organized_source, "Organize Imports"
            )

        def on_error(message: str) -> None:
            self._code_tools_panel.set_busy(False)
            self._code_tools_panel.log(f"Organize imports failed: {message}")

        worker = run_in_thread(perform)
        worker.signals.result.connect(on_success)
        worker.signals.error.connect(on_error)

    @Slot()
    def _on_remove_unused_requested(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        source = editor.toPlainText()
        self._code_tools_panel.set_busy(True)
        self._show_code_tools_panel()

        def perform() -> UnusedCleanupResult:
            return CodeFormatter.remove_unused(source)

        def on_success(result: UnusedCleanupResult) -> None:
            self._code_tools_panel.set_busy(False)
            self._preview_and_apply(
                editor, source, result.cleaned_source, "Remove Unused"
            )

        def on_error(message: str) -> None:
            self._code_tools_panel.set_busy(False)
            self._code_tools_panel.log(f"Remove unused failed: {message}")

        worker = run_in_thread(perform)
        worker.signals.result.connect(on_success)
        worker.signals.error.connect(on_error)

    @Slot()
    def _on_full_pipeline_requested(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        source = editor.toPlainText()
        self._code_tools_panel.set_busy(True)
        self._show_code_tools_panel()

        def perform() -> PipelineResult:
            return CodeToolsPipeline.run_full_pipeline(source)

        def on_success(result: PipelineResult) -> None:
            self._code_tools_panel.set_busy(False)
            for step in result.steps:
                marker = "✓" if step.changed else "·"
                self._code_tools_panel.log(f"{marker} {step.step_name}: {step.detail}")
            self._preview_and_apply(
                editor, source, result.final_source, "Run Full Pipeline"
            )

        def on_error(message: str) -> None:
            self._code_tools_panel.set_busy(False)
            self._code_tools_panel.log(f"Pipeline failed: {message}")

        worker = run_in_thread(perform)
        worker.signals.result.connect(on_success)
        worker.signals.error.connect(on_error)

    @Slot()
    def _on_generate_docstrings_requested(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        source = editor.toPlainText()
        self._code_tools_panel.set_busy(True)
        self._show_code_tools_panel()

        def perform() -> DocstringGenerationResult:
            return CodeGenerator.generate_docstrings(source)

        def on_success(result: DocstringGenerationResult) -> None:
            self._code_tools_panel.set_busy(False)
            if result.inserted_symbols:
                self._code_tools_panel.log(
                    "Generated docstrings for: " + ", ".join(result.inserted_symbols)
                )
            else:
                self._code_tools_panel.log("Generate Docstrings: nothing to document.")
            self._preview_and_apply(
                editor, source, result.generated_source, "Generate Docstrings"
            )

        def on_error(message: str) -> None:
            self._code_tools_panel.set_busy(False)
            self._code_tools_panel.log(f"Docstring generation failed: {message}")

        worker = run_in_thread(perform)
        worker.signals.result.connect(on_success)
        worker.signals.error.connect(on_error)

    @Slot()
    def _on_generate_tests_requested(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        source = editor.toPlainText()
        raw_stem = editor.file_path().stem or "module"
        module_name = (
            raw_stem if raw_stem.isidentifier() else re.sub(r"\W|^(?=\d)", "_", raw_stem)
        )
        self._code_tools_panel.set_busy(True)
        self._show_code_tools_panel()

        def perform() -> TestGenerationResult:
            return CodeGenerator.generate_unit_tests(source, module_name)

        def on_success(result: TestGenerationResult) -> None:
            self._code_tools_panel.set_busy(False)
            if result.covered_symbols:
                self._code_tools_panel.log(
                    "Generated tests covering: " + ", ".join(result.covered_symbols)
                )
            else:
                self._code_tools_panel.log(
                    "Generate Unit Tests: no public symbols found; "
                    "generated an import-only skeleton."
                )
            test_path = Path(f"test_{module_name}.py")
            existing_test_editor = self._editor_tabs.editor_for_path(test_path)
            if existing_test_editor is not None:
                # A test file for this module is already open -- don't
                # silently discard the freshly generated content, route it
                # through the same diff-review as every other code-tools
                # mutation.
                self._editor_tabs.set_current_editor(existing_test_editor)
                self._preview_and_apply(
                    existing_test_editor,
                    existing_test_editor.toPlainText(),
                    result.generated_source,
                    "Regenerate Unit Tests",
                )
                return
            new_test_editor = self._editor_tabs.add_editor(
                test_path, result.generated_source
            )
            # This buffer has no corresponding file on disk yet -- mark it
            # unsaved so closing the tab (or the app) without an explicit
            # Save prompts for confirmation instead of silently discarding
            # the generated test.
            new_test_editor.document().setModified(True)

        def on_error(message: str) -> None:
            self._code_tools_panel.set_busy(False)
            self._code_tools_panel.log(f"Test generation failed: {message}")

        worker = run_in_thread(perform)
        worker.signals.result.connect(on_success)
        worker.signals.error.connect(on_error)

    @Slot(str, str)
    def _on_rename_requested(self, old_name: str, new_name: str) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        source = editor.toPlainText()
        self._code_tools_panel.set_busy(True)
        self._show_code_tools_panel()

        def perform() -> RenameResult:
            return SymbolRenamer.rename(source, old_name, new_name)

        def on_success(result: RenameResult) -> None:
            self._code_tools_panel.set_busy(False)
            if result.occurrence_count == 0:
                self._code_tools_panel.log(
                    f"Rename: no occurrences of '{old_name}' found."
                )
                return
            self._code_tools_panel.log(
                f"Rename: {result.occurrence_count} occurrence(s) of "
                f"'{old_name}' -> '{new_name}'."
            )
            self._preview_and_apply(
                editor,
                source,
                result.renamed_source,
                f"Rename '{old_name}' -> '{new_name}'",
            )

        def on_error(message: str) -> None:
            self._code_tools_panel.set_busy(False)
            self._code_tools_panel.log(f"Rename failed: {message}")

        worker = run_in_thread(perform)
        worker.signals.result.connect(on_success)
        worker.signals.error.connect(on_error)

    # -------------------------------------------------------------------------
    # REPL & Terminal Operations
    # -------------------------------------------------------------------------

    def _show_repl_panel(self) -> None:
        """Switch bottom tabs to REPL and focus input field."""
        self._bottom_tabs.setCurrentWidget(self._repl_panel)
        self._repl_panel._input_field.setFocus()

    def _show_terminal_panel(self) -> None:
        """Switch bottom tabs to Terminal and focus input field."""
        self._bottom_tabs.setCurrentWidget(self._terminal_panel)
        self._terminal_panel._input_field.setFocus()

    def _on_send_selection_menu(self) -> None:
        """Send current editor selection or active line to the REPL."""
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        cursor = editor.textCursor()
        selected_text = cursor.selectedText().replace("\u2029", "\n").strip()
        if not selected_text:
            selected_text = cursor.block().text().strip()
        if selected_text:
            self._on_send_to_repl(selected_text)

    def _on_send_to_repl(self, code_str: str) -> None:
        """Dispatch a code fragment to the REPL, focusing the REPL tab."""
        self._bottom_tabs.setCurrentWidget(self._repl_panel)
        self._repl_panel.execute_code(code_str)

    # -------------------------------------------------------------------------
    # CPython Internals & Profiler Operations
    # -------------------------------------------------------------------------

    def _show_internals_panel(self) -> None:
        """Switch to the CPython Internals dock tab and trigger deep inspection."""
        self._bottom_tabs.setCurrentWidget(self._internals_panel)
        self._inspect_internals()

    def _show_memory_profiler_panel(self) -> None:
        """Switch directly to the Memory Diagnostics Studio tab."""
        self._bottom_tabs.setCurrentWidget(self._internals_panel)
        self._internals_panel._tabs.setCurrentIndex(3)
        if hasattr(self._internals_panel, "_prof_subtabs"):
            self._internals_panel._prof_subtabs.setCurrentIndex(3)

    def _on_internals_file_and_line_activated(self, file_path_str: str, line: int) -> None:
        """Navigate to file and line selected in the profiler or memory studio."""
        if file_path_str and file_path_str not in ("<unknown>", "<string>", "<profile_target>", "<system>"):
            path = Path(file_path_str)
            if not path.is_absolute() and self._project_root:
                path = self._project_root / path
            if path.is_file():
                self._jump_to_source_location(path, line)
                return
        # Fallback to jumping in current editor
        self._jump_to_internals_line(line)

    def _inspect_internals(self) -> None:
        """Run Bytecode disassembly, AST parsing, and Symtable analysis in background."""
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        source = editor.toPlainText()
        file_path_str = str(editor.file_path())

        def analyze() -> tuple[DisassemblyResult, AstInspectionResult, SymtableResult]:
            dis_res = BytecodeDisassembler.disassemble(source, file_path_str)
            ast_res = AstInspector.inspect(source)
            sym_res = SymtableInspector.inspect(source, file_path_str)
            return dis_res, ast_res, sym_res

        def on_success(results: tuple[DisassemblyResult, AstInspectionResult, SymtableResult]) -> None:
            dis_res, ast_res, sym_res = results
            self._internals_panel.populate_bytecode(dis_res)
            self._internals_panel.populate_ast(ast_res)
            self._internals_panel.populate_symtable(sym_res)
            cursor = editor.textCursor()
            current_line = cursor.blockNumber() + 1
            self._internals_panel.highlight_bytecode_for_line(current_line)

        worker = run_in_thread(analyze)
        worker.signals.result.connect(on_success)

    @Slot()
    def _on_profile_code_requested(self) -> None:
        """Profile active editor source under cProfile and tracemalloc in a worker."""
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        source = editor.toPlainText()
        if not source.strip():
            return
        file_name = editor.file_path().name
        self._internals_panel.set_profile_busy(True)

        def run_profile() -> ProfileResult:
            return ExecutionProfiler.profile_code(source, file_name)

        def on_success(result: ProfileResult) -> None:
            self._internals_panel.populate_profile(result)

        def on_error(msg: str) -> None:
            self._internals_panel.set_profile_busy(False)
            _LOGGER.error("Profiling failed: %s", msg)

        worker = run_in_thread(run_profile)
        worker.signals.result.connect(on_success)
        worker.signals.error.connect(on_error)

    @Slot(int)
    def _jump_to_internals_line(self, line: int) -> None:
        """Jump cursor in current editor to the specified line."""
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        editor.jump_to_line(line, 0)
        editor.setFocus()

    @Slot(int, int)
    def _jump_to_internals_node(self, line: int, column: int) -> None:
        """Jump cursor in current editor to the specified AST line and column."""
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        editor.jump_to_line(line, column)
        editor.setFocus()

    @Slot(int)
    def _on_bottom_tab_changed(self, index: int) -> None:
        """Trigger deep CPython internals inspection when the user selects that tab."""
        widget = self._bottom_tabs.widget(index)
        if widget == self._internals_panel:
            self._inspect_internals()
        elif widget == self._dep_panel:
            if not self._dep_panel._graph.all_packages:
                self._refresh_dependencies()

    def _run_single_linter(
        self, tool: LintTool, file_path: Path, source_content: str
    ) -> list[LintIssue]:
        temporary_file_path = file_path.with_suffix(file_path.suffix + ".lint.tmp")
        try:
            temporary_file_path.write_text(source_content, encoding="utf-8")
        except OSError as exception:
            raise LinterError(
                f"Failed to create temporary validation file: {exception}"
            ) from exception

        command_mapping = {
            LintTool.FLAKE8: [
                self._config.python_executable,
                "-m",
                "flake8",
                "--max-line-length=120",
                str(temporary_file_path),
            ],
            LintTool.PYLINT: [
                self._config.python_executable,
                "-m",
                "pylint",
                "--from-stdin",
                "--score=n",
                "--msg-template={path}:{line}:{column}:{msg_id}:{msg}",
                str(temporary_file_path),
            ],
            LintTool.MYPY: [
                self._config.python_executable,
                "-m",
                "mypy",
                "--ignore-missing-imports",
                "--no-error-summary",
                "--show-column-numbers",
                str(temporary_file_path),
            ],
        }

        command = command_mapping[tool]
        try:
            input_content = source_content if tool == LintTool.PYLINT else None
            completed_process = subprocess.run(
                command,
                input=input_content,
                capture_output=True,
                text=True,
                timeout=60,
                # Linters exit nonzero when they find issues -- that is
                # their normal, successful operation, not a process
                # failure -- so this must NOT raise on a nonzero exit code.
                # The output is parsed below regardless of the exit code.
                check=False,
            )
        except FileNotFoundError:
            _LOGGER.warning(
                "Linter tool %s is not installed or not found on system PATH.",
                tool.value,
            )
            return []
        except subprocess.TimeoutExpired as exception:
            _LOGGER.warning("Linter tool %s operation timed out.", tool.value)
            raise ProcessTimeoutError(f"Linter {tool.value} timed out.") from exception
        finally:
            try:
                if temporary_file_path.exists():
                    temporary_file_path.unlink()
            except OSError as exception:
                _LOGGER.debug("Could not clean up temporary lint file: %s", exception)

        detected_issues: list[LintIssue] = []
        regex_pattern = _LINTER_OUTPUT_PATTERNS[tool]
        severity_resolver = _LINTER_SEVERITY_MAPPERS[tool]

        combined_output = completed_process.stdout + completed_process.stderr
        for line in combined_output.splitlines():
            match_object = regex_pattern.match(line)
            if not match_object:
                continue
            group_dictionary = match_object.groupdict()
            code_value = group_dictionary.get("code") or "?"
            try:
                line_number = int(group_dictionary.get("line") or 1)
                column_number = int(group_dictionary.get("col") or 0)
            except (TypeError, ValueError):
                continue

            # Clear temporary formatting file paths from user view
            raw_msg = group_dictionary.get("msg") or ""
            cleaned_msg = raw_msg.replace(".lint.tmp", "").replace(".py.lint.tmp", "")

            detected_issues.append(
                LintIssue(
                    file_path=file_path,
                    line=line_number,
                    column=column_number,
                    code=code_value,
                    tool=tool,
                    severity=severity_resolver(code_value),
                    message=cleaned_msg,
                )
            )
        return detected_issues

    # -------------------------------------------------------------------------
    # Thread-Safe Package Management Handlers (Redirected to uv!)
    # -------------------------------------------------------------------------

    def _refresh_packages(self) -> None:
        self._status_label.setText("Refreshing python environment packages...")
        virtualenv_path = self._get_virtualenv_path()

        def fetch_packages() -> list[PipPackage]:
            cmd = self.get_package_manager_cmd("list")
            try:
                completed_process = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=True,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    "Neither 'uv' nor python 'pip' executable was found on your system PATH."
                ) from exc

            try:
                raw_package_list = json.loads(completed_process.stdout)
            except json.JSONDecodeError as exception:
                raise LinterParseError(
                    "Failed to decode pip packages JSON list."
                ) from exception

            return [
                PipPackage(
                    name=package_item["name"],
                    version=package_item["version"],
                    location=Path(package_item.get("location", "")),
                )
                for package_item in raw_package_list
            ]

        worker = run_in_thread(fetch_packages)
        worker.signals.result.connect(self._on_packages_refreshed)
        worker.signals.error.connect(self._on_package_operation_failed)

    @Slot(object)
    def _on_packages_refreshed(self, packages: list[PipPackage]) -> None:
        self._pkg_widget.populate(packages)
        self._status_label.setText(
            f"Refreshed environment: {len(packages)} packages detected."
        )

    @Slot(str)
    def _on_package_operation_failed(self, error_message: str) -> None:
        self._status_label.setText("Package operation failed.")
        QMessageBox.warning(
            self, "Package Error", f"Package operation failed:\n{error_message}"
        )

    def _install_package(self, package_name: str) -> None:
        if self._offline_service.is_offline() and not self._offline_service.get_local_wheelhouse_dir():
            msg = (
                f"Cannot install '{package_name}': PipViper is running in Offline Mode.\n\n"
                f"To install packages:\n"
                f"• Switch to Online Mode (Opt-in) via the status bar.\n"
                f"• Or configure a local wheelhouse directory for offline installs."
            )
            self._status_label.setText("Install blocked: Offline Mode active.")
            QMessageBox.warning(self, "Offline Mode Active", msg)
            return

        self._status_label.setText(f"Installing package '{package_name}'...")

        def run_install() -> None:
            self._package_service.install_package(self.get_runtime_python(), package_name)

        worker = run_in_thread(run_install)
        worker.signals.finished.connect(self._refresh_packages)
        worker.signals.error.connect(self._on_package_operation_failed)

    def _uninstall_package(self, package_name: str) -> None:
        self._status_label.setText(f"Uninstalling package '{package_name}'...")

        def run_uninstall() -> None:
            self._package_service.uninstall_package(self.get_runtime_python(), package_name)

        worker = run_in_thread(run_uninstall)
        worker.signals.finished.connect(self._refresh_packages)
        worker.signals.error.connect(self._on_package_operation_failed)

    # -------------------------------------------------------------------------
    # Text Search & Cursor Jump Helpers
    # -------------------------------------------------------------------------

    def _find(self) -> None:
        search_term, is_accepted = QInputDialog.getText(self, "Find Text", "Find:")
        if not is_accepted or not search_term:
            return
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        if not editor.find(search_term):
            cursor = editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            editor.setTextCursor(cursor)
            editor.find(search_term)

    def _goto_line(self) -> None:
        editor = self._editor_tabs.current_editor()
        if editor is None:
            return
        maximum_blocks = editor.blockCount()
        line_number, is_accepted = QInputDialog.getInt(
            self, "Go to Line", f"Line (1..{maximum_blocks}):", 1, 1, maximum_blocks
        )
        if is_accepted:
            editor.jump_to_line(line_number, 0)

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "About PipViper IDE",
            "PipViper IDE v7.0.0\n\nA modern development workspace.\n\n"
            "System Dependencies: PySide6, pydantic, jedi.",
        )

    def _show_shortcuts(self) -> None:
        QMessageBox.information(
            self,
            "Keyboard Shortcuts",
            "Ctrl+N        New file\n"
            "Ctrl+O        Open file\n"
            "Ctrl+S        Save\n"
            "Ctrl+F        Find\n"
            "Ctrl+G        Go to line\n"
            "F5            Run current file\n"
            "Shift+F5      Stop execution\n"
            "Ctrl+Shift+R  Reset REPL process\n"
            "(editor)      Typing: live autocomplete popup\n"
            "(editor)      '(' : active parameter description tooltip\n",
        )

    def _open_welcome_buffer(self) -> None:
        welcome_text = (
            '"""Welcome to PipViper IDE."""\n'
            "\n"
            "import os\n"
            "from pathlib import Path\n"
            "\n"
            "\n"
            'def hello(name: str = "World") -> str:\n'
            '    return f"Hello, {name}!"\n'
            "\n"
            "\n"
            'if __name__ == "__main__":\n'
            '    print(hello("PipViper IDE"))\n'
        )
        file_path = Path("welcome.py")
        editor = self._editor_tabs.add_editor(file_path, welcome_text)
        editor.set_file_path(file_path)

    # -------------------------------------------------------------------------
    # Environment & Dependency Management Methods (Phase 8)
    # -------------------------------------------------------------------------

    def _resolve_initial_environment(self) -> PythonEnvironment:
        runtime_exe = Path(self.get_runtime_python())
        envs = self._env_detector.discover_environments(self._project_root)
        for env in envs:
            if env.executable == runtime_exe.resolve() or env.executable == runtime_exe:
                env.is_active = True
                return env

        ver = self._env_detector.probe_python_version(runtime_exe)
        name = (
            runtime_exe.parent.name
            if runtime_exe.parent.name not in ("Scripts", "bin")
            else runtime_exe.parent.parent.name
        )
        return PythonEnvironment(
            name=name,
            env_type=EnvironmentType.VENV if "venv" in str(runtime_exe).lower() else EnvironmentType.SYSTEM,
            executable=runtime_exe,
            version=ver,
            prefix=runtime_exe.parent.parent,
            is_active=True,
            details="Initial active Python interpreter",
        )

    def _show_environment_picker(self) -> None:
        envs = self._env_detector.discover_environments(self._project_root)
        dialog = EnvironmentPickerDialog(envs, self._active_env, self._palette, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            chosen = dialog.get_selected_environment()
            if chosen:
                self._switch_environment(chosen)

    def _switch_environment(self, new_env: PythonEnvironment) -> None:
        self._active_env = new_env
        self._config = self._config.model_copy(update={"python_executable": str(new_env.executable)})
        self._env_selector.set_environment(new_env)
        self._status_label.setText(f"Active environment switched to: {new_env.display_label}")

        try:
            self._pkg_widget._python = str(new_env.executable)
        except Exception:
            pass

        self._refresh_packages()
        self._refresh_dependencies()
        self._run_live_diagnostics()

    def _show_dependencies_panel(self) -> None:
        self._bottom_tabs.setCurrentWidget(self._dep_panel)
        if not self._dep_panel._graph.all_packages:
            self._refresh_dependencies()

    def _refresh_dependencies(self) -> None:
        runtime_py = self.get_runtime_python()

        def worker() -> DependencyGraph:
            return self._dep_scanner.scan_environment(runtime_py)

        def on_done(graph: DependencyGraph) -> None:
            self._dep_panel.set_graph(graph)

        task = run_in_thread(worker)
        task.signals.result.connect(on_done)

    def _upgrade_package(self, package_name: str) -> None:
        if self._offline_service.is_offline() and not self._offline_service.get_local_wheelhouse_dir():
            msg = (
                f"Cannot upgrade '{package_name}': PipViper is running in Offline Mode.\n\n"
                f"To upgrade packages:\n"
                f"• Switch to Online Mode (Opt-in) via the status bar.\n"
                f"• Or configure a local wheelhouse directory for offline installs."
            )
            self._status_label.setText("Upgrade blocked: Offline Mode active.")
            QMessageBox.warning(self, "Offline Mode Active", msg)
            return

        self._status_label.setText(f"Upgrading {package_name}...")
        self._output_panel.append_stdout(f"\n--- Upgrading {package_name} ---\n")
        virtualenv_path = self._get_virtualenv_path()
        runtime_py = self.get_runtime_python()

        def do_upgrade() -> None:
            clean_pkg = self._process_service.validate_package_name(package_name)
            wheelhouse = self._offline_service.get_local_wheelhouse_dir()
            if wheelhouse:
                cmd = [runtime_py, "-m", "pip", "install", "--upgrade", "--no-index", "--find-links", str(wheelhouse), clean_pkg]
            elif shutil.which("uv") and virtualenv_path:
                cmd = ["uv", "pip", "install", "--upgrade", clean_pkg, "--python", virtualenv_path]
            else:
                cmd = [runtime_py, "-m", "pip", "install", "--upgrade", clean_pkg]

            sanitized_cmd = self._process_service.sanitize_arguments(cmd)
            safe_env = self._process_service.build_safe_environment()
            extra_kwargs: dict[str, Any] = {"capture_output": True, "text": True, "timeout": 60, "env": safe_env}
            if sys.platform == "win32":
                extra_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                extra_kwargs["startupinfo"] = startupinfo

            try:
                proc = subprocess.run(sanitized_cmd, **extra_kwargs)
                out = proc.stdout + ("\n" + proc.stderr if proc.stderr else "")
                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, lambda: self._on_upgrade_finished(package_name, out, proc.returncode == 0))
            except Exception as exc:
                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, lambda: self._on_upgrade_finished(package_name, str(exc), False))

        run_in_thread(do_upgrade)

    def _on_upgrade_finished(self, package_name: str, output: str, success: bool) -> None:
        self._output_panel.append_stdout(output)
        if success:
            self._status_label.setText(f"Successfully upgraded {package_name}")
            self._refresh_packages()
            self._refresh_dependencies()
        else:
            self._status_label.setText(f"Failed upgrading {package_name}")

    def _show_requirements_sync_dialog(self) -> None:
        """Scan workspace and prompt user to sync requirements.txt."""
        if not self._project_root:
            QMessageBox.information(self, "Requirements Synchronizer", "No active workspace project root is open.")
            return

        report = self._dep_scanner.scan_workspace_requirements(self._project_root)
        msg_lines = [
            f"Detected Third-Party Imports: {len(report.all_detected_imports)}",
            f"Declared in Requirements: {len(report.declared_requirements)}",
            "",
        ]
        if report.missing_requirements:
            msg_lines.append(f"⚠️ Missing from requirements.txt ({len(report.missing_requirements)}):")
            msg_lines.extend(f"  • {pkg}" for pkg in report.missing_requirements[:10])
            if len(report.missing_requirements) > 10:
                msg_lines.append(f"  ... and {len(report.missing_requirements) - 10} more")
            msg_lines.append("")

        if report.unused_requirements:
            msg_lines.append(f"ℹ️ Declared but unused in code ({len(report.unused_requirements)}):")
            msg_lines.extend(f"  • {pkg}" for pkg in report.unused_requirements[:10])
            if len(report.unused_requirements) > 10:
                msg_lines.append(f"  ... and {len(report.unused_requirements) - 10} more")
            msg_lines.append("")

        if not report.missing_requirements and not report.unused_requirements:
            msg_lines.append("✅ All requirements are in sync with your workspace code!")
            QMessageBox.information(self, "Requirements Synchronizer", "\n".join(msg_lines))
            return

        if report.missing_requirements:
            msg_lines.append("Would you like to append the missing packages to requirements.txt?")
            reply = QMessageBox.question(
                self,
                "Requirements Synchronizer",
                "\n".join(msg_lines),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                req_file = self._project_root / "requirements.txt"
                with req_file.open("a", encoding="utf-8") as f:
                    f.write("\n# Added by PipViper Self-Healing Synchronizer\n")
                    for pkg in report.missing_requirements:
                        f.write(f"{pkg}\n")
                self._status_label.setText(f"Appended {len(report.missing_requirements)} package(s) to requirements.txt")
                self._refresh_dependencies()
        else:
            QMessageBox.information(self, "Requirements Synchronizer", "\n".join(msg_lines))


def install_global_exception_handler() -> None:
    """Install a global exception hook to capture unhandled exceptions gracefully."""
    old_hook = sys.excepthook

    def excepthook(exc_type: type[BaseException], exc_value: BaseException, exc_tb: Any) -> None:
        import traceback

        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        _LOGGER.critical("Unhandled application exception:\n%s", tb_text)

        # In automated test environments, log and return without triggering modal dialogs
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return

        app_inst = QApplication.instance()
        if app_inst:
            try:
                msg_box = QMessageBox()
                msg_box.setIcon(QMessageBox.Icon.Critical)
                msg_box.setWindowTitle("PipViper — Unexpected Error")
                msg_box.setText("An unexpected exception occurred in PipViper.")
                msg_box.setInformativeText(f"{exc_type.__name__}: {exc_value}")
                msg_box.setDetailedText(tb_text)
                msg_box.exec()
            except Exception:
                pass
        old_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = excepthook


def main() -> int:
    """Bootstrap the main window event loop of PipViper IDE."""
    install_global_exception_handler()
    application_config = AppConfig()
    application = QApplication(sys.argv)
    application.setApplicationName("PipViper IDE")
    window = MainWindow(application_config)
    window.show()
    return application.exec()


if __name__ == "__main__":
    sys.exit(main())