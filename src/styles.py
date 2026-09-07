"""Stylesheet generation for the PipViper IDE.

This module provides the global stylesheet generator function.
It maps ColorPalette fields to a comprehensive QSS (Qt Style Sheet)
string, allowing immediate, unified theme switching across all widgets.
"""

from __future__ import annotations

from . import ColorPalette


def get_stylesheet(color_palette: ColorPalette) -> str:
    """Generate a cohesive Qt Style Sheet (QSS) for the given color palette.

    Styles all standard and custom widgets to match the active theme,
    ensuring seamless, flicker-free transitions during theme switches.

    Args:
        color_palette: The validated ColorPalette containing color token mappings.

    Returns:
        A formatted QSS stylesheet string.
    """
    return f"""
    /* Disable outdated native dotted focus outlines across all controls */
    * {{
        outline: none;
    }}

    QMainWindow, QWidget {{
        background-color: {color_palette.background};
        color: {color_palette.text};
    }}
    QMenuBar {{
        background-color: {color_palette.panel};
        color: {color_palette.text};
        border-bottom: 1px solid {color_palette.border};
    }}
    QMenuBar::item:selected {{
        background-color: {color_palette.selection};
    }}
    QMenu {{
        background-color: {color_palette.panel};
        color: {color_palette.text};
        border: 1px solid {color_palette.border};
    }}
    QMenu::item:selected {{
        background-color: {color_palette.selection};
        color: {color_palette.text};
    }}
    QToolBar {{
        background-color: {color_palette.panel};
        border-bottom: 1px solid {color_palette.border};
        spacing: 4px;
        padding: 4px;
    }}
    QToolButton {{
        background-color: transparent;
        color: {color_palette.text};
        padding: 4px 8px;
        border: 1px solid transparent;
        border-radius: 3px;
    }}
    QToolButton:hover {{
        background-color: {color_palette.selection};
        border: 1px solid {color_palette.border};
    }}
    QStatusBar {{
        background-color: {color_palette.panel};
        color: {color_palette.muted};
        border-top: 1px solid {color_palette.border};
    }}
    QTabWidget::pane {{
        border: 1px solid {color_palette.border};
        background-color: {color_palette.background};
    }}
    QTabBar::tab {{
        background-color: {color_palette.panel};
        color: {color_palette.text};
        padding: 6px 12px;
        border: 1px solid {color_palette.border};
        border-bottom: none;
    }}
    QTabBar::tab:selected {{
        background-color: {color_palette.background};
        color: {color_palette.text};
    }}
    QTabBar::tab:hover {{
        background-color: {color_palette.selection};
    }}
    QTabBar::close-button {{
        border-radius: 2px;
        padding: 2px;
    }}
    QTabBar::close-button:hover {{
        background-color: {color_palette.red};
    }}
    QPlainTextEdit, QTextEdit {{
        background-color: {color_palette.background};
        color: {color_palette.text};
        selection-background-color: {color_palette.selection};
        selection-color: {color_palette.background};
    }}
    QListWidget, QTreeView, QTableWidget {{
        background-color: {color_palette.background};
        color: {color_palette.text};
        border: 1px solid {color_palette.border};
        alternate-background-color: {color_palette.panel};
    }}
    QListWidget::item:hover, QTreeView::item:hover, QTableWidget::item:hover {{
        background-color: {color_palette.current_line};
    }}
    QListWidget::item:selected, QTreeView::item:selected, QTableWidget::item:selected {{
        background-color: {color_palette.selection};
        color: {color_palette.text};
    }}
    QHeaderView::section {{
        background-color: {color_palette.panel};
        color: {color_palette.text};
        border: 1px solid {color_palette.border};
        padding: 4px;
    }}
    QPushButton {{
        background-color: {color_palette.panel};
        color: {color_palette.text};
        border: 1px solid {color_palette.border};
        border-radius: 3px;
        padding: 4px 12px;
    }}
    QPushButton:hover {{
        background-color: {color_palette.selection};
    }}
    QPushButton:pressed {{
        background-color: {color_palette.border};
    }}
    QLineEdit, QSpinBox, QComboBox {{
        background-color: {color_palette.background};
        color: {color_palette.text};
        border: 1px solid {color_palette.border};
        border-radius: 2px;
        padding: 4px;
    }}
    QSplitter::handle {{
        background-color: {color_palette.border};
    }}
    QToolTip {{
        background-color: {color_palette.panel};
        color: {color_palette.text};
        border: 1px solid {color_palette.border};
        padding: 4px;
    }}
    QScrollBar:vertical {{
        background-color: {color_palette.panel};
        width: 12px;
    }}
    QScrollBar::handle:vertical {{
        background-color: {color_palette.border};
        min-height: 20px;
        border-radius: 3px;
    }}
    QScrollBar:horizontal {{
        background-color: {color_palette.panel};
        height: 12px;
    }}
    QScrollBar::handle:horizontal {{
        background-color: {color_palette.border};
        min-width: 20px;
        border-radius: 3px;
    }}
    QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{
        background-color: {color_palette.blue};
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{
        width: 0px;
        height: 0px;
    }}
    QScrollBar::add-page, QScrollBar::sub-page {{
        background: none;
    }}
    QGroupBox {{
        border: 1px solid {color_palette.border};
        border-radius: 5px;
        margin-top: 12px;
        padding: 10px 8px 8px 8px;
        font-weight: bold;
        color: {color_palette.text};
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 10px;
        padding: 0 6px;
        color: {color_palette.blue};
    }}
    QLabel[role="section-title"] {{
        color: {color_palette.blue};
        font-weight: bold;
        padding: 2px 0;
    }}
    QLabel[role="hint"] {{
        color: {color_palette.muted};
        font-style: italic;
    }}
    QPushButton:disabled {{
        color: {color_palette.muted};
        background-color: {color_palette.panel};
        border: 1px solid {color_palette.border};
    }}
    QPushButton[role="primary"] {{
        background-color: {color_palette.blue};
        color: {color_palette.background};
        font-weight: bold;
        border: 1px solid {color_palette.blue};
    }}
    QPushButton[role="primary"]:hover {{
        background-color: {color_palette.selection};
        color: {color_palette.text};
    }}
    QPushButton[role="primary"]:disabled {{
        background-color: {color_palette.panel};
        color: {color_palette.muted};
        border: 1px solid {color_palette.border};
    }}
    QLineEdit:disabled, QComboBox:disabled {{
        color: {color_palette.muted};
        background-color: {color_palette.panel};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus {{
        border: 1px solid {color_palette.blue};
    }}
    QTableWidget {{
        gridline-color: {color_palette.border};
    }}
    QCheckBox {{
        color: {color_palette.text};
        spacing: 6px;
    }}
    QLabel {{
        color: {color_palette.text};
    }}
    """
