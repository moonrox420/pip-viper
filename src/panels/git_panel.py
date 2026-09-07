"""Subpanel module extracted from src.panels."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, ClassVar, Optional, Sequence, cast

from pydantic import BaseModel, ConfigDict, Field
from PySide6.QtCore import (
    QEvent,
    QItemSelectionModel,
    QObject,
    QPoint,
    QProcess,
    QProcessEnvironment,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QFont, QGuiApplication, QKeyEvent, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..vcs import (
    GitBranch,
    GitCommit,
    GitFileStatus,
    GitService,
    GitStatusEntry,
)
from ..code_tools import CodeIssue
from ..internals import (
    AstInspectionResult,
    AstNodeInfo,
    DisassemblyInstruction,
    DisassemblyResult,
    MemoryHotspot,
    ProfileRecord,
    ProfileResult,
    ScopeInfo,
    ScopeSymbol,
    SymtableResult,
)
from ..testing import TestItem, TestStatus, TestSuiteSummary
from ..dependencies import (
    DependencyGraph,
    DependencyGraphView,
    DependencyNode,
    DependencyScanner,
)
from ..profiler_visualizer import FlameGraphWidget, CallHierarchyView
from ..memory_tracker import MemoryTrackerWidget
from .. import (
    ColorPalette,
    FileOperationError,
    LintIssue,
    LintSeverity,
    PipPackage,
    ProcessTimeoutError,
    run_in_thread,
)
from .base import (
    _LOGGER,
    _SEVERITY_COLOR_KEYS,
    _severity_color,
    _section_label,
    _qcolor,
)

# -----------------------------------------------------------------------------
# Git Panel
# -----------------------------------------------------------------------------


class GitPanel(QWidget):
    """Elite Git Version Control panel supporting staging, unstaging, committing,
    branch management, commit history logging, and visual diff inspection.
    """

    command_output = Signal(str)
    file_activated = Signal(Path)
    diff_requested = Signal(Path, bool)  # path, is_staged
    branch_changed = Signal(str)
    status_refreshed = Signal()

    def __init__(self, palette: ColorPalette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._project_root: Path | None = None
        self._git = GitService()

        self._current_branch: str = ""
        self._branches: list[GitBranch] = []
        self._staged_entries: list[GitStatusEntry] = []
        self._unstaged_entries: list[GitStatusEntry] = []
        self._commits: list[GitCommit] = []

        self._setup_ui()
        self._apply_colors()

    def _setup_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(6)

        # 1. Top Toolbar (Branch + Sync controls)
        toolbar = QWidget(self)
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(6)

        branch_icon = QLabel("🌿", toolbar)
        toolbar_layout.addWidget(branch_icon)

        self._branch_combo = QComboBox(toolbar)
        self._branch_combo.setMinimumWidth(180)
        self._branch_combo.activated.connect(self._on_branch_combo_activated)
        toolbar_layout.addWidget(self._branch_combo)

        toolbar_layout.addStretch(1)

        self._btn_refresh = QPushButton("🔄 Refresh", toolbar)
        self._btn_refresh.clicked.connect(self.refresh_status)
        toolbar_layout.addWidget(self._btn_refresh)

        self._btn_pull = QPushButton("⬇ Pull", toolbar)
        self._btn_pull.clicked.connect(self._pull)
        toolbar_layout.addWidget(self._btn_pull)

        self._btn_push = QPushButton("⬆ Push", toolbar)
        self._btn_push.clicked.connect(self._push)
        toolbar_layout.addWidget(self._btn_push)

        main_layout.addWidget(toolbar)

        # 2. Main content splitter: Top (Commit & Changes) / Bottom (History & Output)
        self._v_splitter = QSplitter(Qt.Orientation.Vertical, self)

        # Upper container: Commit controls + Changes Trees
        changes_container = QWidget(self._v_splitter)
        changes_layout = QVBoxLayout(changes_container)
        changes_layout.setContentsMargins(0, 0, 0, 0)
        changes_layout.setSpacing(6)

        # Commit controls
        commit_box = QWidget(changes_container)
        commit_layout = QHBoxLayout(commit_box)
        commit_layout.setContentsMargins(0, 0, 0, 0)
        commit_layout.setSpacing(6)

        self._commit_message_input = QLineEdit(commit_box)
        self._commit_message_input.setPlaceholderText("Commit message (Ctrl+Enter to commit)")
        self._commit_message_input.returnPressed.connect(self._commit)
        commit_layout.addWidget(self._commit_message_input, 1)

        self._amend_checkbox = QCheckBox("Amend", commit_box)
        commit_layout.addWidget(self._amend_checkbox)

        self._commit_btn = QPushButton("✓ Commit", commit_box)
        self._commit_btn.setProperty("role", "primary")
        self._commit_btn.clicked.connect(self._commit)
        commit_layout.addWidget(self._commit_btn)

        self._stage_all_btn = QPushButton("➕ Stage All", commit_box)
        self._stage_all_btn.clicked.connect(self._stage_all)
        commit_layout.addWidget(self._stage_all_btn)

        self._unstage_all_btn = QPushButton("➖ Unstage All", commit_box)
        self._unstage_all_btn.clicked.connect(self._unstage_all)
        commit_layout.addWidget(self._unstage_all_btn)

        changes_layout.addWidget(commit_box)

        # Splitter holding Staged and Unstaged Changes side-by-side
        self._h_changes_splitter = QSplitter(Qt.Orientation.Horizontal, changes_container)

        # Staged Changes group
        staged_group = QGroupBox("Staged Changes", self._h_changes_splitter)
        staged_vbox = QVBoxLayout(staged_group)
        staged_vbox.setContentsMargins(4, 4, 4, 4)
        self._staged_tree = QTreeWidget(staged_group)
        self._staged_tree.setHeaderLabels(["", "File", "Action"])
        self._staged_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._staged_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._staged_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._staged_tree.itemDoubleClicked.connect(self._on_staged_item_double_clicked)
        self._staged_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._staged_tree.customContextMenuRequested.connect(self._on_staged_context_menu)
        staged_vbox.addWidget(self._staged_tree)
        self._staged_group = staged_group

        # Unstaged Changes group
        unstaged_group = QGroupBox("Changes", self._h_changes_splitter)
        unstaged_vbox = QVBoxLayout(unstaged_group)
        unstaged_vbox.setContentsMargins(4, 4, 4, 4)
        self._unstaged_tree = QTreeWidget(unstaged_group)
        self._unstaged_tree.setHeaderLabels(["", "File", "Actions"])
        self._unstaged_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._unstaged_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._unstaged_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._unstaged_tree.itemDoubleClicked.connect(self._on_unstaged_item_double_clicked)
        self._unstaged_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._unstaged_tree.customContextMenuRequested.connect(self._on_unstaged_context_menu)
        unstaged_vbox.addWidget(self._unstaged_tree)
        self._unstaged_group = unstaged_group

        self._h_changes_splitter.addWidget(staged_group)
        self._h_changes_splitter.addWidget(unstaged_group)
        self._h_changes_splitter.setSizes([350, 450])

        changes_layout.addWidget(self._h_changes_splitter, 1)

        # Lower container: Tabs for Recent Commits and Command Output
        self._bottom_notebook = QTabWidget(self._v_splitter)

        # Tab 1: Recent Commits Table
        self._commits_table = QTableWidget(self._bottom_notebook)
        self._commits_table.setColumnCount(4)
        self._commits_table.setHorizontalHeaderLabels(["Hash", "Message", "Author", "Date"])
        self._commits_table.verticalHeader().setVisible(False)
        self._commits_table.setShowGrid(False)
        self._commits_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._commits_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._commits_table.setFont(QFont("Consolas", 9))
        self._commits_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._commits_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._commits_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._commits_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._bottom_notebook.addTab(self._commits_table, "📜 Recent Commits")

        # Tab 2: Command Output View (preserves existing _output_view)
        self._output_view = QPlainTextEdit(self._bottom_notebook)
        self._output_view.setReadOnly(True)
        self._output_view.setFont(QFont("Consolas", 10))
        self._bottom_notebook.addTab(self._output_view, "📃 Command Output")

        self._v_splitter.addWidget(changes_container)
        self._v_splitter.addWidget(self._bottom_notebook)
        self._v_splitter.setSizes([360, 160])

        main_layout.addWidget(self._v_splitter, 1)

    def set_palette(self, palette: ColorPalette) -> None:
        """Dynamically apply colors on theme switches."""
        self._palette = palette
        self._apply_colors()
        self._repopulate_trees()

    def _apply_colors(self) -> None:
        bg = self._palette.background
        fg = self._palette.text
        border = self._palette.border
        panel = self._palette.panel

        tree_style = f"""
            QTreeWidget, QTableWidget {{
                background-color: {bg};
                color: {fg};
                border: 1px solid {border};
            }}
            QHeaderView::section {{
                background-color: {panel};
                color: {self._palette.muted};
                border: none;
                border-bottom: 1px solid {border};
                padding: 3px 6px;
                font-weight: bold;
            }}
        """
        self._staged_tree.setStyleSheet(tree_style)
        self._unstaged_tree.setStyleSheet(tree_style)
        self._commits_table.setStyleSheet(tree_style)
        self._output_view.setStyleSheet(f"background-color: {bg}; color: {fg}; border: 1px solid {border};")

    def set_project_root(self, root: Path) -> None:
        """Specify the directory target context for git calls."""
        self._project_root = root
        self.refresh_status()

    def refresh_status(self) -> None:
        """Asynchronously query Git status, branches, and commit log."""
        if self._project_root is None or not self._git.is_git_repo(self._project_root):
            self._branch_combo.clear()
            self._branch_combo.addItem("Not a git repository")
            self._staged_tree.clear()
            self._unstaged_tree.clear()
            self._commits_table.setRowCount(0)
            return

        root = self._project_root

        def fetch_git_data() -> dict[str, Any]:
            status_entries = self._git.get_status(root)
            curr_branch, branches = self._git.get_branches(root)
            commits = self._git.get_log(root, max_count=25)
            return {
                "status": status_entries,
                "current_branch": curr_branch,
                "branches": branches,
                "commits": commits,
            }

        worker = run_in_thread(fetch_git_data)

        @Slot(object)
        def on_data(data: dict[str, Any]) -> None:
            self._current_branch = data.get("current_branch", "")
            self._branches = data.get("branches", [])
            entries: list[GitStatusEntry] = data.get("status", [])
            self._commits = data.get("commits", [])

            self._staged_entries = [e for e in entries if e.staged]
            self._unstaged_entries = [e for e in entries if not e.staged]

            self._update_branch_ui()
            self._repopulate_trees()
            self._repopulate_commits()
            self.status_refreshed.emit()

        worker.signals.result.connect(on_data)
        worker.signals.error.connect(self._append)

    def _update_branch_ui(self) -> None:
        self._branch_combo.blockSignals(True)
        self._branch_combo.clear()
        selected_idx = 0
        for idx, branch in enumerate(self._branches):
            display = f"🌿 {branch.name}"
            if branch.upstream:
                indicators = []
                if branch.ahead > 0:
                    indicators.append(f"↑{branch.ahead}")
                if branch.behind > 0:
                    indicators.append(f"↓{branch.behind}")
                if indicators:
                    display += f" ({' '.join(indicators)})"
            self._branch_combo.addItem(display, branch.name)
            if branch.is_current or branch.name == self._current_branch:
                selected_idx = idx

        self._branch_combo.insertSeparator(self._branch_combo.count())
        self._branch_combo.addItem("➕ New Branch...", "__new__")
        self._branch_combo.setCurrentIndex(selected_idx)
        self._branch_combo.blockSignals(False)

    def _on_branch_combo_activated(self, index: int) -> None:
        data = self._branch_combo.itemData(index)
        if data == "__new__":
            self._create_new_branch()
            return

        branch_name = str(data)
        if branch_name and branch_name != self._current_branch and self._project_root:
            def do_switch() -> str:
                self._git.switch_branch(self._project_root, branch_name)
                return f"Switched to branch {branch_name}"

            worker = run_in_thread(do_switch)
            worker.signals.result.connect(lambda res: (self._append(res), self.refresh_status(), self.branch_changed.emit(branch_name)))
            worker.signals.error.connect(self._append)

    def _create_new_branch(self) -> None:
        if self._project_root is None:
            return
        branch_name, ok = QInputDialog.getText(self, "New Branch", "Enter branch name:")
        if ok and branch_name.strip():
            bname = branch_name.strip()

            def do_create() -> str:
                self._git.create_branch(self._project_root, bname)
                return f"Created and switched to branch {bname}"

            worker = run_in_thread(do_create)
            worker.signals.result.connect(lambda res: (self._append(res), self.refresh_status(), self.branch_changed.emit(bname)))
            worker.signals.error.connect(self._append)

    def _repopulate_trees(self) -> None:
        # 1. Staged tree
        self._staged_group.setTitle(f"Staged Changes ({len(self._staged_entries)})")
        self._staged_tree.clear()
        for entry in self._staged_entries:
            item = QTreeWidgetItem(self._staged_tree)
            item.setText(0, entry.status.badge)
            item.setText(1, entry.display_name)
            item.setData(0, Qt.ItemDataRole.UserRole, entry)

            color = self._get_status_color(entry.status)
            item.setForeground(0, QColor(color))

            # Action button: Unstage (-)
            btn_unstage = QPushButton("➖", self._staged_tree)
            btn_unstage.setToolTip("Unstage file")
            btn_unstage.setMaximumWidth(28)
            btn_unstage.clicked.connect(lambda _c=False, p=entry.file_path: self._unstage_file(p))
            self._staged_tree.setItemWidget(item, 2, btn_unstage)

        # 2. Unstaged tree
        self._unstaged_group.setTitle(f"Changes ({len(self._unstaged_entries)})")
        self._unstaged_tree.clear()
        for entry in self._unstaged_entries:
            item = QTreeWidgetItem(self._unstaged_tree)
            item.setText(0, entry.status.badge)
            item.setText(1, entry.display_name)
            item.setData(0, Qt.ItemDataRole.UserRole, entry)

            color = self._get_status_color(entry.status)
            item.setForeground(0, QColor(color))

            # Action buttons widget (+ Stage, ↺ Discard)
            actions_widget = QWidget(self._unstaged_tree)
            act_layout = QHBoxLayout(actions_widget)
            act_layout.setContentsMargins(0, 0, 0, 0)
            act_layout.setSpacing(2)

            btn_stage = QPushButton("➕", actions_widget)
            btn_stage.setToolTip("Stage file")
            btn_stage.setMaximumWidth(26)
            btn_stage.clicked.connect(lambda _c=False, p=entry.file_path: self._stage_file(p))
            act_layout.addWidget(btn_stage)

            btn_discard = QPushButton("↺", actions_widget)
            btn_discard.setToolTip("Discard changes")
            btn_discard.setMaximumWidth(26)
            btn_discard.clicked.connect(lambda _c=False, p=entry.file_path: self._discard_file(p))
            act_layout.addWidget(btn_discard)

            self._unstaged_tree.setItemWidget(item, 2, actions_widget)

    def _get_status_color(self, status: GitFileStatus) -> str:
        if status in (GitFileStatus.ADDED, GitFileStatus.UNTRACKED):
            return self._palette.green
        elif status == GitFileStatus.MODIFIED:
            return self._palette.blue
        elif status == GitFileStatus.DELETED:
            return self._palette.red
        elif status == GitFileStatus.CONFLICT:
            return self._palette.yellow
        return self._palette.text

    def _repopulate_commits(self) -> None:
        self._commits_table.setRowCount(len(self._commits))
        for row, commit in enumerate(self._commits):
            hash_item = QTableWidgetItem(commit.short_hash)
            hash_item.setForeground(QColor(self._palette.blue))
            msg_item = QTableWidgetItem(commit.message)
            author_item = QTableWidgetItem(commit.author)
            author_item.setForeground(QColor(self._palette.muted))
            date_item = QTableWidgetItem(commit.relative_date)
            date_item.setForeground(QColor(self._palette.muted))

            self._commits_table.setItem(row, 0, hash_item)
            self._commits_table.setItem(row, 1, msg_item)
            self._commits_table.setItem(row, 2, author_item)
            self._commits_table.setItem(row, 3, date_item)

    # -------------------------------------------------------------------------
    # Operations
    # -------------------------------------------------------------------------

    def _stage_file(self, path: Path) -> None:
        if self._project_root is None:
            return

        def do_stage() -> str:
            self._git.stage_files(self._project_root, [path])
            return f"Staged {path}"

        worker = run_in_thread(do_stage)
        worker.signals.result.connect(lambda res: (self._append(res), self.refresh_status()))
        worker.signals.error.connect(self._append)

    def _unstage_file(self, path: Path) -> None:
        if self._project_root is None:
            return

        def do_unstage() -> str:
            self._git.unstage_files(self._project_root, [path])
            return f"Unstaged {path}"

        worker = run_in_thread(do_unstage)
        worker.signals.result.connect(lambda res: (self._append(res), self.refresh_status()))
        worker.signals.error.connect(self._append)

    def _discard_file(self, path: Path) -> None:
        if self._project_root is None:
            return
        reply = QMessageBox.question(
            self,
            "Confirm Discard",
            f"Are you sure you want to discard all changes in '{path}'?\nThis operation cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        def do_discard() -> str:
            self._git.discard_changes(self._project_root, [path])
            return f"Discarded changes in {path}"

        worker = run_in_thread(do_discard)
        worker.signals.result.connect(lambda res: (self._append(res), self.refresh_status()))
        worker.signals.error.connect(self._append)

    def _stage_all(self) -> None:
        if self._project_root is None:
            return

        def do_stage_all() -> str:
            self._git.stage_files(self._project_root, [])
            return "Staged all changes"

        worker = run_in_thread(do_stage_all)
        worker.signals.result.connect(lambda res: (self._append(res), self.refresh_status()))
        worker.signals.error.connect(self._append)

    def _unstage_all(self) -> None:
        if self._project_root is None:
            return

        def do_unstage_all() -> str:
            self._git.unstage_files(self._project_root, [])
            return "Unstaged all changes"

        worker = run_in_thread(do_unstage_all)
        worker.signals.result.connect(lambda res: (self._append(res), self.refresh_status()))
        worker.signals.error.connect(self._append)

    def _commit(self) -> None:
        msg = self._commit_message_input.text().strip()
        if not msg:
            self._append("Cannot commit: Commit message is empty.")
            return
        if self._project_root is None:
            self._append("No project folder open.")
            return

        amend = self._amend_checkbox.isChecked()

        def do_commit() -> str:
            # If nothing is staged, auto-stage all tracked changes first
            status = self._git.get_status(self._project_root)
            staged = [e for e in status if e.staged]
            if not staged and not amend:
                self._git.stage_files(self._project_root, [])
            out = self._git.commit(self._project_root, msg, amend=amend)
            return out or "Committed changes successfully."

        worker = run_in_thread(do_commit)

        @Slot(str)
        def on_commit_done(out: str) -> None:
            self._append(out)
            self._commit_message_input.clear()
            self._amend_checkbox.setChecked(False)
            self.refresh_status()

        worker.signals.result.connect(on_commit_done)
        worker.signals.error.connect(self._append)

    def _push(self) -> None:
        if self._project_root is None:
            return

        def do_push() -> str:
            return self._git.push(self._project_root)

        worker = run_in_thread(do_push)
        worker.signals.result.connect(lambda res: (self._append(res), self.refresh_status()))
        worker.signals.error.connect(self._append)

    def _pull(self) -> None:
        if self._project_root is None:
            return

        def do_pull() -> str:
            return self._git.pull(self._project_root)

        worker = run_in_thread(do_pull)
        worker.signals.result.connect(lambda res: (self._append(res), self.refresh_status()))
        worker.signals.error.connect(self._append)

    # -------------------------------------------------------------------------
    # Context Menus & Double Click
    # -------------------------------------------------------------------------

    def _on_staged_item_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        entry: Optional[GitStatusEntry] = item.data(0, Qt.ItemDataRole.UserRole)
        if entry:
            self.diff_requested.emit(entry.file_path, True)
            self.file_activated.emit(entry.file_path)

    def _on_unstaged_item_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        entry: Optional[GitStatusEntry] = item.data(0, Qt.ItemDataRole.UserRole)
        if entry:
            self.diff_requested.emit(entry.file_path, False)
            self.file_activated.emit(entry.file_path)

    def _on_staged_context_menu(self, pos: QPoint) -> None:
        item = self._staged_tree.itemAt(pos)
        if not item:
            return
        entry: Optional[GitStatusEntry] = item.data(0, Qt.ItemDataRole.UserRole)
        if not entry:
            return

        menu = QMenu(self)
        diff_action = menu.addAction("🔍 View Diff")
        unstage_action = menu.addAction("➖ Unstage File")
        open_action = menu.addAction("📄 Open File")

        action = menu.exec(self._staged_tree.viewport().mapToGlobal(pos))
        if action == diff_action:
            self.diff_requested.emit(entry.file_path, True)
        elif action == unstage_action:
            self._unstage_file(entry.file_path)
        elif action == open_action:
            self.file_activated.emit(entry.file_path)

    def _on_unstaged_context_menu(self, pos: QPoint) -> None:
        item = self._unstaged_tree.itemAt(pos)
        if not item:
            return
        entry: Optional[GitStatusEntry] = item.data(0, Qt.ItemDataRole.UserRole)
        if not entry:
            return

        menu = QMenu(self)
        diff_action = menu.addAction("🔍 View Diff")
        stage_action = menu.addAction("➕ Stage File")
        discard_action = menu.addAction("↺ Discard Changes")
        open_action = menu.addAction("📄 Open File")

        action = menu.exec(self._unstaged_tree.viewport().mapToGlobal(pos))
        if action == diff_action:
            self.diff_requested.emit(entry.file_path, False)
        elif action == stage_action:
            self._stage_file(entry.file_path)
        elif action == discard_action:
            self._discard_file(entry.file_path)
        elif action == open_action:
            self.file_activated.emit(entry.file_path)

    # -------------------------------------------------------------------------
    # Backwards Compatibility API
    # -------------------------------------------------------------------------

    def _run_git(self, command_arguments: list[str]) -> None:
        """Backwards-compatible generic git executor."""
        if self._project_root is None:
            self._append("No project folder open.")
            return

        def execute_git() -> str:
            try:
                from ..services.process_service import ProcessService
                proc = ProcessService.get_instance().run_command(
                    ["git", *command_arguments],
                    cwd=self._project_root,
                    timeout=30,
                    check=False,
                )
                out = (proc.stdout or "").strip()
                err = (proc.stderr or "").strip()
                combined = []
                if out:
                    combined.append(out)
                if err:
                    combined.append(err)
                return "\n".join(combined)
            except Exception as ex:
                return str(ex)

        worker = run_in_thread(execute_git)
        worker.signals.result.connect(lambda res: (self._append(res), self.refresh_status()))
        worker.signals.error.connect(self._append)

    @Slot(str)
    def _append(self, text: str) -> None:
        """Append log text to output view and emit command_output signal."""
        self._output_view.appendPlainText(text)
        self.command_output.emit(text)


# Backwards compatibility alias
GitWidget = GitPanel



