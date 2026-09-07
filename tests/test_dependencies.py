"""Tests for Phase 8: Dependency Studio, Graph Visualizer, and Requirements Synchronizer.

Covers:
    - DependencyNode requirements parsing and reverse dependents
    - DependencyGraph top-level package identification and Mermaid export
    - DependencyScanner environment scanning and workspace requirements audit
    - DependencyStudioPanel tree hierarchy, flat list, and package inspector details
    - DependencyGraphView zoom and pan interactions
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidgetItem

from src import (
    ColorPalette,
    DependencyGraph,
    DependencyGraphView,
    DependencyNode,
    DependencyScanner,
    DependencyStudioPanel,
    EditorTheme,
    WorkspaceRequirementsReport,
    get_palette,
)


@pytest.fixture
def palette() -> ColorPalette:
    return get_palette(EditorTheme.DARK)


def test_dependency_node_and_graph_transitive_resolution() -> None:
    # Build a small dependency tree:
    # app -> fastapi -> pydantic -> typing-extensions
    # app -> requests -> urllib3
    node_app = DependencyNode(
        name="my-app",
        installed_version="1.0.0",
        requires=["fastapi>=0.100.0", "requests"],
        is_top_level=True,
    )
    node_fastapi = DependencyNode(
        name="fastapi",
        installed_version="0.110.0",
        requires=["pydantic>=2.0.0; python_version >= '3.8'"],
        required_by=["my-app"],
    )
    node_pydantic = DependencyNode(
        name="pydantic",
        installed_version="2.6.0",
        requires=["typing-extensions>=4.6.0"],
        required_by=["fastapi"],
    )
    node_typing = DependencyNode(
        name="typing-extensions",
        installed_version="4.10.0",
        required_by=["pydantic"],
    )
    node_requests = DependencyNode(
        name="requests",
        installed_version="2.31.0",
        requires=["urllib3<3.0"],
        required_by=["my-app"],
    )
    node_urllib3 = DependencyNode(
        name="urllib3",
        installed_version="2.2.0",
        required_by=["requests"],
    )

    graph = DependencyGraph({
        "my-app": node_app,
        "fastapi": node_fastapi,
        "pydantic": node_pydantic,
        "typing-extensions": node_typing,
        "requests": node_requests,
        "urllib3": node_urllib3,
    })

    assert graph.top_level_packages == ["my-app"]
    assert len(graph.all_packages) == 6

    # Test clean requirement names
    assert node_fastapi.clean_requires_names == ["pydantic"]
    assert node_requests.clean_requires_names == ["urllib3"]

    # Test transitive dependencies
    transitive = graph.get_transitive_dependencies("my-app")
    assert "fastapi" in transitive
    assert "pydantic" in transitive
    assert "typing-extensions" in transitive
    assert "requests" in transitive
    assert "urllib3" in transitive

    # Test Mermaid output
    mermaid = graph.to_mermaid(["my-app"])
    assert "graph TD" in mermaid
    assert '"my-app" --> "fastapi"' in mermaid
    assert '"my-app" --> "requests"' in mermaid


def test_dependency_scanner_workspace_requirements(tmp_path: Path) -> None:
    # Create mock workspace:
    # file1.py imports requests and yaml
    # file2.py imports json (stdlib) and local_helper
    # requirements.txt has requests and unused_lib
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    (src_dir / "main.py").write_text(
        "import requests\n"
        "import yaml\n"
        "from pathlib import Path\n"
        "import local_helper\n",
        encoding="utf-8",
    )
    (src_dir / "local_helper.py").write_text("def run(): pass\n", encoding="utf-8")

    req_file = tmp_path / "requirements.txt"
    req_file.write_text("requests>=2.28.0\nunused-lib==1.0\n", encoding="utf-8")

    scanner = DependencyScanner(project_root=tmp_path)
    report: WorkspaceRequirementsReport = scanner.scan_workspace_requirements(tmp_path)

    # 'yaml' maps to 'pyyaml'
    assert "requests" in report.all_detected_imports
    assert "pyyaml" in report.all_detected_imports
    assert "pyyaml" in report.missing_requirements  # Imported but missing from requirements.txt
    assert "unused-lib" in report.unused_requirements  # In requirements.txt but unused in code


def test_dependency_studio_panel_rendering_and_inspector(
    qtbot: Any, palette: ColorPalette
) -> None:
    panel = DependencyStudioPanel(palette)
    getattr(qtbot, "addWidget")(panel)

    node1 = DependencyNode(
        name="flask",
        installed_version="3.0.0",
        latest_version="3.0.2",
        is_outdated=True,
        summary="A lightweight WSGI web application framework.",
        license_name="BSD-3-Clause",
        author="Armin Ronacher",
        homepage="https://palletsprojects.com/p/flask/",
        requires=["werkzeug>=3.0.0", "jinja2>=3.1.2"],
        is_top_level=True,
    )
    node2 = DependencyNode(
        name="jinja2",
        installed_version="3.1.2",
        summary="A full-featured template engine for Python.",
        required_by=["flask"],
    )

    graph = DependencyGraph({"flask": node1, "jinja2": node2})
    panel.set_graph(graph)

    # Test Tree population in Hierarchy Mode
    assert panel._tree.topLevelItemCount() == 1
    root_item = panel._tree.topLevelItem(0)
    assert root_item is not None
    assert "flask" in root_item.text(0)
    assert root_item.childCount() == 2

    # Click item to inspect
    panel._on_tree_item_clicked(root_item, 0)
    assert "flask" in panel._lbl_pkg_name.text()
    assert "v3.0.0" in panel._lbl_pkg_version.text()
    assert "v3.0.2" in panel._lbl_pkg_version.text()
    assert panel._btn_upgrade.isEnabled() is True  # Outdated package allows upgrade

    # Test View Mode switch to Flat Inventory
    panel._view_mode_combo.setCurrentIndex(1)
    assert panel._tree.topLevelItemCount() == 2

    # Test View Mode switch to Outdated Only
    panel._view_mode_combo.setCurrentIndex(2)
    assert panel._tree.topLevelItemCount() == 1
    assert "flask" in panel._tree.topLevelItem(0).text(0)

    # Test Graph View zoom
    panel._graph_view.zoom_in()
    panel._graph_view.zoom_out()
    panel._graph_view.reset_view()
