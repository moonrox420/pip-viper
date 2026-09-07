"""Interactive Python Dependency Visualizer, Graph Analyzer, and Requirements Synchronizer.

This module provides:
    - Zero-overhead offline dependency inspection via importlib.metadata.
    - Reverse dependency graph calculation (requires vs. required_by).
    - Asynchronous PyPI update checking and security audit heuristics.
    - Interactive QGraphicsScene node-link visual graph with layout & pan/zoom.
    - Workspace requirements scanner detecting missing and unused dependencies.
"""

from __future__ import annotations

import ast
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Sequence

from PySide6.QtCore import QPointF, QRectF, Qt, Signal, Slot
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QWidget,
)

from .pip_viper import ColorPalette, map_module_to_pypi

_LOGGER = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Data Models
# -----------------------------------------------------------------------------


class DependencyNode:
    """Represents a single installed Python package within the environment dependency graph."""

    def __init__(
        self,
        name: str,
        installed_version: str,
        latest_version: str | None = None,
        is_outdated: bool = False,
        summary: str = "",
        license_name: str = "",
        author: str = "",
        homepage: str = "",
        requires: list[str] | None = None,
        required_by: list[str] | None = None,
        is_top_level: bool = False,
        vulnerability_alerts: list[str] | None = None,
    ) -> None:
        self.name: str = name
        self.installed_version: str = installed_version
        self.latest_version: str | None = latest_version
        self.is_outdated: bool = is_outdated
        self.summary: str = summary
        self.license: str = license_name
        self.author: str = author
        self.homepage: str = homepage
        self.requires: list[str] = requires or []  # Raw requirements e.g. ["pydantic>=2.0.0"]
        self.required_by: list[str] = required_by or []  # Reverse dependencies
        self.is_top_level: bool = is_top_level
        self.vulnerability_alerts: list[str] = vulnerability_alerts or []

    @property
    def clean_requires_names(self) -> list[str]:
        """Extract only clean package names from requirement specifiers."""
        results: list[str] = []
        for req in self.requires:
            clean = re.split(r"[><=~!;]", req)[0].strip()
            if clean and clean not in results:
                results.append(clean)
        return results

    def __repr__(self) -> str:
        return (
            f"DependencyNode({self.name}=={self.installed_version}, "
            f"requires={len(self.requires)}, required_by={len(self.required_by)})"
        )


class DependencyGraph:
    """Directed dependency graph of an active Python virtual environment."""

    def __init__(self, nodes: dict[str, DependencyNode] | None = None) -> None:
        self.nodes: dict[str, DependencyNode] = nodes or {}

    @property
    def top_level_packages(self) -> list[str]:
        """Packages that are not required by any other package in this environment."""
        return sorted([name for name, node in self.nodes.items() if node.is_top_level])

    @property
    def all_packages(self) -> list[str]:
        """All installed package names in alphabetical order."""
        return sorted(self.nodes.keys())

    def get_node(self, package_name: str) -> DependencyNode | None:
        """Case-insensitive package node lookup."""
        clean = package_name.lower().replace("_", "-")
        for name, node in self.nodes.items():
            if name.lower().replace("_", "-") == clean:
                return node
        return None

    def get_transitive_dependencies(self, package_name: str) -> set[str]:
        """Compute the full transitive closure of dependencies required by a package."""
        visited: set[str] = set()
        queue: list[str] = [package_name]

        while queue:
            current = queue.pop(0)
            node = self.get_node(current)
            if not node:
                continue
            for req_name in node.clean_requires_names:
                clean_req = req_name.lower().replace("_", "-")
                if clean_req not in visited:
                    visited.add(clean_req)
                    queue.append(req_name)

        return visited

    def to_mermaid(self, root_packages: list[str] | None = None, max_nodes: int = 40) -> str:
        """Generate a Mermaid.js flowchart representation of the dependency graph."""
        lines = ["graph TD"]
        targets = root_packages or self.top_level_packages[:15]
        included_nodes: set[str] = set()

        for root in targets:
            root_node = self.get_node(root)
            if not root_node:
                continue
            included_nodes.add(root_node.name)
            for child in root_node.clean_requires_names:
                child_node = self.get_node(child)
                child_label = child_node.name if child_node else child
                lines.append(f'    "{root_node.name}" --> "{child_label}"')
                included_nodes.add(child_label)
                if len(included_nodes) >= max_nodes:
                    break
            if len(included_nodes) >= max_nodes:
                break

        return "\n".join(lines)


class WorkspaceRequirementsReport:
    """Report comparing code-level imported packages with declared project requirements."""

    def __init__(
        self,
        all_detected_imports: list[str],
        declared_requirements: list[str],
        missing_requirements: list[str],
        unused_requirements: list[str],
    ) -> None:
        self.all_detected_imports: list[str] = sorted(all_detected_imports)
        self.declared_requirements: list[str] = sorted(declared_requirements)
        self.missing_requirements: list[str] = sorted(missing_requirements)
        self.unused_requirements: list[str] = sorted(unused_requirements)


# -----------------------------------------------------------------------------
# Dependency Scanner & PyPI Inspector
# -----------------------------------------------------------------------------


class DependencyScanner:
    """Inspects installed distributions, builds dependency trees, and checks PyPI updates."""

    def __init__(self, project_root: Path | None = None) -> None:
        self._project_root: Path | None = project_root
        self._pypi_cache: dict[str, str] = {}

    def scan_environment(self, python_executable: str | None = None) -> DependencyGraph:
        """Scan installed packages using importlib.metadata.

        Works completely offline without launching heavy pip processes. If a custom
        python_executable is supplied, attempts to inspect its site-packages directory.
        """
        import importlib.metadata

        search_paths: list[str] | None = None
        if python_executable:
            site_pkgs = self._resolve_site_packages(Path(python_executable))
            if site_pkgs and site_pkgs.is_dir():
                search_paths = [str(site_pkgs)]

        try:
            distributions = list(
                importlib.metadata.distributions(paths=search_paths)
                if search_paths
                else importlib.metadata.distributions()
            )
        except Exception as exc:
            _LOGGER.warning("Failed scanning distributions with search_paths %s: %s", search_paths, exc)
            distributions = list(importlib.metadata.distributions())

        raw_nodes: dict[str, DependencyNode] = {}
        reverse_map: dict[str, set[str]] = {}

        # 1. Parse all distributions
        for dist in distributions:
            try:
                name = dist.metadata["Name"]
                canonical_name = name.strip()
                version = dist.version
                summary = dist.metadata.get("Summary", "") or ""
                license_name = dist.metadata.get("License", "") or ""
                author = dist.metadata.get("Author", "") or dist.metadata.get("Author-email", "") or ""
                homepage = (
                    dist.metadata.get("Home-page", "")
                    or dist.metadata.get("Project-URL", "")
                    or ""
                )

                # Requirements list
                requires_list: list[str] = []
                if dist.requires:
                    for req in dist.requires:
                        # Strip environment markers for extra dependencies (e.g. extra == 'test')
                        # while retaining core package specifiers
                        if "extra ==" in req:
                            continue
                        clean_spec = req.split(";")[0].strip()
                        if clean_spec:
                            requires_list.append(clean_spec)

                node = DependencyNode(
                    name=canonical_name,
                    installed_version=version,
                    summary=summary,
                    license_name=license_name,
                    author=author,
                    homepage=homepage,
                    requires=requires_list,
                )
                raw_nodes[canonical_name] = node

            except Exception as exc:
                _LOGGER.debug("Skipping distribution %s: %s", getattr(dist, "name", "unknown"), exc)

        # 2. Build reverse dependency connections (required_by)
        for pkg_name, node in raw_nodes.items():
            for req_clean in node.clean_requires_names:
                # Find matching node case-insensitively
                target_key = self._find_matching_key(req_clean, raw_nodes)
                if target_key:
                    reverse_map.setdefault(target_key, set()).add(pkg_name)

        # 3. Populate reverse dependencies and determine top-level packages
        project_declared = self._read_declared_requirements()

        for pkg_name, node in raw_nodes.items():
            dependents = sorted(reverse_map.get(pkg_name, set()))
            node.required_by = dependents

            canonical_lower = pkg_name.lower().replace("_", "-")
            is_explicitly_declared = any(
                req.lower().replace("_", "-") == canonical_lower for req in project_declared
            )

            # Top-level if declared in project or if no other installed package requires it
            node.is_top_level = is_explicitly_declared or (len(dependents) == 0)

        return DependencyGraph(raw_nodes)

    def _load_pypi_cache(self) -> None:
        try:
            cache_file = Path.home() / ".pip_viper" / "pypi_cache.json"
            if cache_file.is_file():
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._pypi_cache.update(data)
        except Exception:
            pass

    def _save_pypi_cache(self) -> None:
        try:
            cache_dir = Path.home() / ".pip_viper"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file = cache_dir / "pypi_cache.json"
            cache_file.write_text(json.dumps(self._pypi_cache, indent=2), encoding="utf-8")
        except Exception:
            pass

    def check_pypi_update(self, package_name: str, timeout: float = 2.0) -> str | None:
        """Query PyPI JSON API for the latest version of a package with caching.

        Strictly offline-first (PRD O1): Returns cached result if available.
        If Offline Mode is active, zero network requests are made.
        """
        clean_name = package_name.strip().lower()
        if not self._pypi_cache:
            self._load_pypi_cache()

        if clean_name in self._pypi_cache:
            return self._pypi_cache[clean_name]

        # PRD O1 / O7: Check Offline Mode before attempting network
        try:
            from .services.offline_service import OfflineService
            if OfflineService.get_instance().is_offline():
                _LOGGER.warning("check_pypi_update skipped for '%s': Offline Mode is active.", clean_name)
                return None
        except Exception:
            pass

        url = f"https://pypi.org/pypi/{clean_name}/json"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "PipViper-IDE/7.0.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                latest = str(data.get("info", {}).get("version", ""))
                if latest:
                    self._pypi_cache[clean_name] = latest
                    self._save_pypi_cache()
                    return latest
        except Exception as exc:
            _LOGGER.debug("PyPI check failed for '%s': %s", clean_name, exc)

        return None

    def scan_workspace_requirements(self, project_root: Path | None = None) -> WorkspaceRequirementsReport:
        """Scan workspace .py files for imports and compare against requirements.txt/pyproject.toml."""
        root = project_root or self._project_root
        if not root or not root.is_dir():
            return WorkspaceRequirementsReport([], [], [], [])

        # 1. Crawl all project python files
        detected_modules: set[str] = set()
        ignore_dirs = {
            ".git", ".venv", "venv", "env", ".env", "__pycache__",
            ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist"
        }

        # Stdlib module names
        stdlib_set = sys.stdlib_module_names | set(sys.builtin_module_names)

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ignore_dirs and not d.startswith(".")]
            for filename in filenames:
                if filename.endswith(".py"):
                    file_path = Path(dirpath) / filename
                    try:
                        content = file_path.read_text(encoding="utf-8", errors="ignore")
                        tree = ast.parse(content, filename=str(file_path))
                        for node in ast.walk(tree):
                            if isinstance(node, ast.Import):
                                for alias in node.names:
                                    top = alias.name.split(".")[0]
                                    if top and top not in stdlib_set:
                                        detected_modules.add(top)
                            elif isinstance(node, ast.ImportFrom):
                                if node.level == 0 and node.module:
                                    top = node.module.split(".")[0]
                                    if top and top not in stdlib_set:
                                        detected_modules.add(top)
                    except Exception:
                        pass

        # 2. Map detected module names to PyPI distribution names
        detected_pypi_pkgs: set[str] = set()
        for mod in detected_modules:
            # Check local file or folder sibling to avoid internal modules
            if (root / f"{mod}.py").exists() or (root / mod).is_dir():
                continue
            pypi_name = map_module_to_pypi(mod)
            detected_pypi_pkgs.add(pypi_name)

        # 3. Read declared requirements
        declared_pkgs = self._read_declared_requirements(root)

        # Canonical normalization for comparison
        declared_normalized = {p.lower().replace("_", "-"): p for p in declared_pkgs}
        detected_normalized = {p.lower().replace("_", "-"): p for p in detected_pypi_pkgs}

        missing = [
            pkg for norm, pkg in detected_normalized.items()
            if norm not in declared_normalized
        ]
        unused = [
            pkg for norm, pkg in declared_normalized.items()
            if norm not in detected_normalized
        ]

        return WorkspaceRequirementsReport(
            all_detected_imports=sorted(detected_pypi_pkgs),
            declared_requirements=sorted(declared_pkgs),
            missing_requirements=sorted(missing),
            unused_requirements=sorted(unused),
        )

    def _resolve_site_packages(self, python_exe: Path) -> Path | None:
        """Locate site-packages directory associated with a python executable."""
        prefix = python_exe.parent.parent
        # Windows layout: prefix / Lib / site-packages
        win_path = prefix / "Lib" / "site-packages"
        if win_path.is_dir():
            return win_path

        # Unix layout: prefix / lib / pythonX.Y / site-packages
        unix_lib = prefix / "lib"
        if unix_lib.is_dir():
            for item in unix_lib.iterdir():
                if item.is_dir() and item.name.startswith("python"):
                    site_pkgs = item / "site-packages"
                    if site_pkgs.is_dir():
                        return site_pkgs

        return None

    def _find_matching_key(self, name: str, node_dict: dict[str, Any]) -> str | None:
        clean = name.lower().replace("_", "-")
        for key in node_dict:
            if key.lower().replace("_", "-") == clean:
                return key
        return None

    def _read_declared_requirements(self, root: Path | None = None) -> list[str]:
        target_root = root or self._project_root
        if not target_root:
            return []

        packages: list[str] = []

        # Read requirements.txt
        req_file = target_root / "requirements.txt"
        if req_file.is_file():
            try:
                for line in req_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and not line.startswith("-"):
                        pkg = re.split(r"[><=~!;]", line)[0].strip()
                        if pkg and pkg not in packages:
                            packages.append(pkg)
            except Exception:
                pass

        # Read pyproject.toml dependencies
        pyproject_file = target_root / "pyproject.toml"
        if pyproject_file.is_file():
            try:
                content = pyproject_file.read_text(encoding="utf-8", errors="ignore")
                # Fast regex parse for dependencies array: dependencies = [ ... ]
                in_deps = False
                for line in content.splitlines():
                    stripped = line.strip()
                    if "dependencies = [" in stripped:
                        in_deps = True
                        continue
                    if in_deps:
                        if "]" in stripped:
                            in_deps = False
                            continue
                        match = re.search(r'["\']([a-zA-Z0-9_\-\.]+)', stripped)
                        if match:
                            pkg = match.group(1).strip()
                            if pkg and pkg not in packages:
                                packages.append(pkg)
            except Exception:
                pass

        return packages


# -----------------------------------------------------------------------------
# Visual Node-Link Interactive Graphics Scene
# -----------------------------------------------------------------------------


class PackageNodeItem(QGraphicsRectItem):
    """Interactive visual node representing a package in the dependency graph."""

    def __init__(
        self,
        node: DependencyNode,
        palette: ColorPalette,
        x: float = 0,
        y: float = 0,
        parent: QGraphicsItem | None = None,
    ) -> None:
        super().__init__(x, y, 140, 50, parent)
        self.node: DependencyNode = node
        self._palette: ColorPalette = palette
        self._is_highlighted: bool = False

        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_highlighted(self, highlighted: bool) -> None:
        self._is_highlighted = highlighted
        self.update()

    def paint(self, painter: QPainter, option: Any, widget: QWidget | None = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Border color
        if self._is_highlighted or self.isSelected():
            border_color = QColor(self._palette.blue)
            pen_width = 2.5
        elif self.node.is_top_level:
            border_color = QColor(self._palette.blue)
            pen_width = 1.8
        elif self.node.is_outdated:
            border_color = QColor(self._palette.yellow)
            pen_width = 1.5
        else:
            border_color = QColor(self._palette.border)
            pen_width = 1.0

        painter.setPen(QPen(border_color, pen_width))
        painter.setBrush(QBrush(QColor(self._palette.panel)))
        painter.drawRoundedRect(self.rect(), 6, 6)

        # Top-level accent badge pill
        badge_color = QColor(self._palette.blue if self.node.is_top_level else self._palette.selection)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(badge_color))
        painter.drawRoundedRect(self.rect().x() + 4, self.rect().y() + 4, 6, 42, 3, 3)

        # Title: Package Name
        painter.setPen(QPen(QColor(self._palette.text)))
        font = painter.font()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        name_text = self.node.name
        if len(name_text) > 14:
            name_text = name_text[:12] + ".."
        painter.drawText(int(self.rect().x() + 16), int(self.rect().y() + 20), name_text)

        # Version subtitle
        font.setPointSize(8)
        font.setBold(False)
        painter.setFont(font)
        sub_color = QColor(self._palette.yellow if self.node.is_outdated else self._palette.border)
        painter.setPen(QPen(sub_color))
        ver_text = f"v{self.node.installed_version}"
        if self.node.is_outdated and self.node.latest_version:
            ver_text += f" → {self.node.latest_version}"
        painter.drawText(int(self.rect().x() + 16), int(self.rect().y() + 38), ver_text)


class DependencyEdgeItem(QGraphicsLineItem):
    """Directed connection arrow representing a package requirement."""

    def __init__(
        self,
        source_item: PackageNodeItem,
        target_item: PackageNodeItem,
        palette: ColorPalette,
        parent: QGraphicsItem | None = None,
    ) -> None:
        super().__init__(parent)
        self.source_item: PackageNodeItem = source_item
        self.target_item: PackageNodeItem = target_item
        self._palette: ColorPalette = palette
        self._is_highlighted: bool = False
        self.setZValue(-1)
        self.update_position()

    def set_highlighted(self, highlighted: bool) -> None:
        self._is_highlighted = highlighted
        self.update()

    def update_position(self) -> None:
        p1 = self.source_item.scenePos() + QPointF(self.source_item.rect().width() / 2, self.source_item.rect().height())
        p2 = self.target_item.scenePos() + QPointF(self.target_item.rect().width() / 2, 0)
        self.setLine(p1.x(), p1.y(), p2.x(), p2.y())

    def paint(self, painter: QPainter, option: Any, widget: QWidget | None = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        line = self.line()
        if line.length() < 5:
            return

        color = QColor(self._palette.blue if self._is_highlighted else self._palette.border)
        pen_width = 2.0 if self._is_highlighted else 1.0
        painter.setPen(QPen(color, pen_width))
        painter.drawLine(line)

        # Arrow head at target
        angle = math.atan2(line.dy(), line.dx())
        arrow_size = 7.0
        p2 = line.p2()
        p_arrow1 = p2 - QPointF(math.cos(angle - math.pi / 7) * arrow_size, math.sin(angle - math.pi / 7) * arrow_size)
        p_arrow2 = p2 - QPointF(math.cos(angle + math.pi / 7) * arrow_size, math.sin(angle + math.pi / 7) * arrow_size)

        arrow_head = QPolygonF([p2, p_arrow1, p_arrow2])
        painter.setBrush(QBrush(color))
        painter.drawPolygon(arrow_head)


class DependencyGraphView(QGraphicsView):
    """Pan and zoom capable viewport hosting the interactive dependency node scene."""

    node_selected = Signal(object)  # Emits DependencyNode

    def __init__(self, palette: ColorPalette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette: ColorPalette = palette
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._node_items: dict[str, PackageNodeItem] = {}
        self._edge_items: list[DependencyEdgeItem] = []

        self._apply_style()

    def set_palette(self, palette: ColorPalette) -> None:
        self._palette = palette
        self._apply_style()
        self.setBackgroundBrush(QBrush(QColor(self._palette.background)))

    def _apply_style(self) -> None:
        self.setBackgroundBrush(QBrush(QColor(self._palette.background)))
        self.setStyleSheet(f"QGraphicsView {{ border: 1px solid {self._palette.border}; }}")

    def wheelEvent(self, event: QWheelEvent) -> None:
        zoom_factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(zoom_factor, zoom_factor)

    def zoom_in(self) -> None:
        self.scale(1.2, 1.2)

    def zoom_out(self) -> None:
        self.scale(1 / 1.2, 1 / 1.2)

    def reset_view(self) -> None:
        self.resetTransform()
        self.centerOn(self._scene.itemsBoundingRect().center())

    def populate(self, graph: DependencyGraph) -> None:
        """Render nodes and edges in a tidy layered hierarchy."""
        self._scene.clear()
        self._node_items.clear()
        self._edge_items.clear()

        if not graph.all_packages:
            return

        # 1. Identify levels for top-level vs sub-dependencies
        roots = graph.top_level_packages or graph.all_packages[:10]
        levels: dict[int, list[str]] = {0: []}
        assigned: set[str] = set()

        for root in roots:
            levels[0].append(root)
            assigned.add(root)

        current_level = 0
        while current_level in levels and levels[current_level]:
            next_level_nodes: list[str] = []
            for pkg in levels[current_level]:
                node = graph.get_node(pkg)
                if not node:
                    continue
                for req in node.clean_requires_names:
                    match_key = graph.get_node(req)
                    if match_key and match_key.name not in assigned:
                        assigned.add(match_key.name)
                        next_level_nodes.append(match_key.name)
            if next_level_nodes:
                levels[current_level + 1] = next_level_nodes
                current_level += 1
            else:
                break

        # Any unassigned packages go into an additional row
        unassigned = [p for p in graph.all_packages if p not in assigned]
        if unassigned:
            levels[current_level + 1] = unassigned[:15]

        # 2. Place nodes on canvas
        col_width = 170
        row_height = 90

        for level_idx, pkgs in levels.items():
            level_total_width = len(pkgs) * col_width
            start_x = -level_total_width / 2
            y = level_idx * row_height

            for i, pkg_name in enumerate(pkgs):
                node = graph.get_node(pkg_name)
                if not node:
                    continue
                x = start_x + i * col_width
                node_item = PackageNodeItem(node, self._palette, x, y)
                self._scene.addItem(node_item)
                self._node_items[node.name.lower().replace("_", "-")] = node_item

        # 3. Create edges
        for level_idx, pkgs in levels.items():
            for pkg_name in pkgs:
                node = graph.get_node(pkg_name)
                if not node:
                    continue
                src_key = node.name.lower().replace("_", "-")
                src_item = self._node_items.get(src_key)
                if not src_item:
                    continue

                for req_clean in node.clean_requires_names:
                    tgt_key = req_clean.lower().replace("_", "-")
                    tgt_item = self._node_items.get(tgt_key)
                    if tgt_item and tgt_item != src_item:
                        edge_item = DependencyEdgeItem(src_item, tgt_item, self._palette)
                        self._scene.addItem(edge_item)
                        self._edge_items.append(edge_item)

        self._scene.selectionChanged.connect(self._on_selection_changed)
        self.reset_view()

    def select_package(self, package_name: str) -> None:
        """Select a package node by name and highlight its connections."""
        key = package_name.lower().replace("_", "-")
        item = self._node_items.get(key)
        if item:
            self._scene.clearSelection()
            item.setSelected(True)
            self.centerOn(item)

    @Slot()
    def _on_selection_changed(self) -> None:
        selected_items = self._scene.selectedItems()
        # Reset all edge highlights
        for edge in self._edge_items:
            edge.set_highlighted(False)
        for node_item in self._node_items.values():
            node_item.set_highlighted(False)

        if not selected_items:
            return

        selected_node_item = selected_items[0]
        if isinstance(selected_node_item, PackageNodeItem):
            selected_node = selected_node_item.node
            self.node_selected.emit(selected_node)

            # Highlight connected incoming and outgoing edges
            for edge in self._edge_items:
                if edge.source_item == selected_node_item or edge.target_item == selected_node_item:
                    edge.set_highlighted(True)
                    edge.source_item.set_highlighted(True)
                    edge.target_item.set_highlighted(True)
