"""Menu & Toolbar Controller for PipViper IDE.

Encapsulates menu bar construction, toolbar building, keyboard shortcuts,
and command palette action registration.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QMenu, QMenuBar, QToolBar, QWidget

from ..pip_viper import EditorTheme


class MenuController:
    """Constructs and manages IDE menus, toolbars, and action registries."""

    def __init__(self, parent: QWidget, action_handlers: Mapping[str, Callable[..., Any]]) -> None:
        self._parent = parent
        self._handlers = action_handlers
        self._actions: dict[str, QAction] = {}
        self.toggle_ai_action: Optional[QAction] = None

    def _get_handler(self, name: str) -> Callable[..., Any]:
        handler = self._handlers.get(name)
        if handler is None:
            return lambda *args, **kwargs: None
        return handler

    def _add_action(
        self,
        menu: QMenu,
        name: str,
        text: str,
        shortcut: QKeySequence | QKeySequence.StandardKey | str | None,
        slot_name: str,
    ) -> QAction:
        action = QAction(text, self._parent)
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut))
        handler = self._get_handler(slot_name)
        action.triggered.connect(handler)
        menu.addAction(action)
        self._actions[name] = action
        return action

    def build_menus(self, menu_bar: QMenuBar, command_palette: Optional[Any] = None) -> None:
        """Construct the standard IDE application menu hierarchy."""
        # --- File Menu ---
        file_menu = menu_bar.addMenu("&File")
        self._add_action(file_menu, "new_file", "&New File", QKeySequence.StandardKey.New, "new_file")
        self._add_action(file_menu, "open_file", "&Open File...", QKeySequence.StandardKey.Open, "open_file")
        self._add_action(file_menu, "open_folder", "Open &Folder...", None, "open_folder")
        file_menu.addSeparator()
        self._add_action(file_menu, "quick_open", "&Quick Open...", QKeySequence("Ctrl+P"), "quick_open")
        self._add_action(file_menu, "save", "&Save", QKeySequence.StandardKey.Save, "save")
        self._add_action(file_menu, "save_as", "Save &As...", QKeySequence.StandardKey.SaveAs, "save_as")
        file_menu.addSeparator()
        self._add_action(file_menu, "exit", "E&xit", None, "exit")

        # --- Edit Menu ---
        edit_menu = menu_bar.addMenu("&Edit")
        self._add_action(edit_menu, "command_palette", "&Command Palette...", QKeySequence("Ctrl+Shift+P"), "command_palette")
        self._add_action(edit_menu, "find", "&Find...", QKeySequence.StandardKey.Find, "find")
        self._add_action(edit_menu, "find_in_files", "Find in &Files...", QKeySequence("Ctrl+Shift+H"), "find_in_files")
        self._add_action(edit_menu, "goto_line", "&Go to Line...", QKeySequence("Ctrl+G"), "goto_line")
        self._add_action(edit_menu, "goto_definition", "&Go to Definition", QKeySequence("F12"), "goto_definition")
        edit_menu.addSeparator()
        self._add_action(edit_menu, "select_next", "&Select Next Occurrence", QKeySequence("Ctrl+D"), "select_next")
        self._add_action(edit_menu, "fold_block", "&Fold Block", QKeySequence("Ctrl+Shift+["), "fold_block")
        self._add_action(edit_menu, "unfold_block", "&Unfold Block", QKeySequence("Ctrl+Shift+]"), "unfold_block")
        self._add_action(edit_menu, "fold_all", "Fold &All", None, "fold_all")
        self._add_action(edit_menu, "unfold_all", "Unfold All", None, "unfold_all")

        # --- View Menu ---
        view_menu = menu_bar.addMenu("&View")
        theme_menu = view_menu.addMenu("&Theme")
        set_theme_handler = self._get_handler("set_theme")
        for theme in EditorTheme:
            action = QAction(theme.value.capitalize(), self._parent)
            action.triggered.connect(lambda _checked=False, t=theme: set_theme_handler(t))
            theme_menu.addAction(action)
        view_menu.addSeparator()

        self.toggle_ai_action = QAction("Show AI &Assistant Column", self._parent)
        self.toggle_ai_action.setCheckable(True)
        self.toggle_ai_action.setChecked(True)
        self.toggle_ai_action.setShortcut(QKeySequence("Ctrl+Shift+A"))
        self.toggle_ai_action.triggered.connect(self._get_handler("toggle_ai_panel"))
        view_menu.addAction(self.toggle_ai_action)
        self._actions["toggle_ai"] = self.toggle_ai_action

        view_menu.addSeparator()
        self._add_action(view_menu, "split_right", "Split Editor &Right", QKeySequence("Ctrl+\\"), "split_right")
        self._add_action(view_menu, "split_down", "Split Editor &Down", QKeySequence("Ctrl+Alt+\\"), "split_down")
        self._add_action(view_menu, "close_split", "&Close Split Pane", None, "close_split")
        self._add_action(view_menu, "git_panel", "Open &Git Source Control", QKeySequence("Ctrl+Shift+G"), "git_panel")

        # --- Run Menu ---
        run_menu = menu_bar.addMenu("&Run")
        self._add_action(run_menu, "debug_start", "&Start / Continue Debugging", QKeySequence("F5"), "debug_f5")
        self._add_action(run_menu, "run_current", "&Run (Without Debugging)", QKeySequence("Ctrl+F5"), "run_current")
        self._add_action(run_menu, "debug_step_over", "Step &Over", QKeySequence("F10"), "debug_step_over")
        self._add_action(run_menu, "debug_step_into", "Step &Into", QKeySequence("F11"), "debug_step_into")
        self._add_action(run_menu, "debug_step_out", "Step O&ut", QKeySequence("Shift+F11"), "debug_step_out")
        self._add_action(run_menu, "toggle_breakpoint", "Toggle &Breakpoint", QKeySequence("F9"), "toggle_breakpoint")
        self._add_action(run_menu, "debug_stop", "&Stop Debugging", QKeySequence("Shift+F5"), "debug_stop")
        run_menu.addSeparator()
        self._add_action(run_menu, "inspect_internals", "Inspect &Internals (Bytecode, AST)", QKeySequence("F8"), "inspect_internals")
        run_menu.addSeparator()
        self._add_action(run_menu, "send_to_repl", "Send Selection to REPL", QKeySequence("Ctrl+Return"), "send_to_repl")
        self._add_action(run_menu, "open_repl", "Open &REPL", QKeySequence("Ctrl+Shift+R"), "open_repl")
        self._add_action(run_menu, "open_terminal", "Open &Terminal", QKeySequence("Ctrl+Shift+T"), "open_terminal")
        run_menu.addSeparator()
        self._add_action(run_menu, "run_all_tests", "Run &All Tests", QKeySequence("Ctrl+Shift+U"), "run_all_tests")
        self._add_action(run_menu, "run_file_tests", "Run &Active File Tests", QKeySequence("Ctrl+Shift+F"), "run_file_tests")
        self._add_action(run_menu, "run_mypy", "Run &Mypy Type Check", QKeySequence("F7"), "run_mypy")
        run_menu.addSeparator()
        self._add_action(run_menu, "select_interpreter", "Select &Python Interpreter...", QKeySequence("Ctrl+Shift+I"), "select_interpreter")
        self._add_action(run_menu, "dependencies_panel", "Open &Dependency Studio", None, "dependencies_panel")
        self._add_action(run_menu, "sync_requirements", "&Sync Requirements...", None, "sync_requirements")

        # --- Help Menu ---
        help_menu = menu_bar.addMenu("&Help")
        self._add_action(help_menu, "about", "&About", None, "about")
        self._add_action(help_menu, "shortcuts", "&Keyboard Shortcuts", None, "shortcuts")

        # Index all menus into Universal Command Palette if provided
        if command_palette:
            for menu in [file_menu, edit_menu, view_menu, run_menu, help_menu]:
                command_palette.register_qactions_from_menu(menu.title(), menu.actions())

    def build_toolbar(self) -> QToolBar:
        """Construct the primary top application toolbar."""
        toolbar = QToolBar("Main", self._parent)
        toolbar.setMovable(False)
        items = [
            ("📄 New", "new_file"),
            ("📂 Open", "open_file"),
            ("💾 Save", "save"),
            ("▶ Run", "run_current"),
            ("🧪 Tests", "tests_panel"),
            ("🐞 Debug", "debug_start"),
            ("■ Stop", "stop_run"),
            ("🔬 Internals", "inspect_internals"),
            ("🔍 Lint", "run_linters"),
            ("🛠 Code Tools", "code_tools_panel"),
            ("📦 Packages", "refresh_packages"),
            ("🕸 Dependencies", "dependencies_panel"),
            ("🌿 Git", "git_panel"),
        ]
        for label_text, slot_name in items:
            action = QAction(label_text, self._parent)
            action.triggered.connect(self._get_handler(slot_name))
            toolbar.addAction(action)
        return toolbar
