"""Unit tests for the Git Version Control System (VCS) engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.vcs import (
    GitBranch,
    GitCommit,
    GitDiffHunk,
    GitDiffType,
    GitFileStatus,
    GitService,
    GitStatusEntry,
)


def test_git_file_status_badges() -> None:
    """Verify badges for all GitFileStatus variants."""
    assert GitFileStatus.MODIFIED.badge == "M"
    assert GitFileStatus.ADDED.badge == "A"
    assert GitFileStatus.DELETED.badge == "D"
    assert GitFileStatus.UNTRACKED.badge == "?"
    assert GitFileStatus.RENAMED.badge == "R"
    assert GitFileStatus.CONFLICT.badge == "U"


def test_git_status_entry_display_name() -> None:
    """Verify display names for normal files and renames."""
    entry = GitStatusEntry(file_path=Path("src/app.py"), status=GitFileStatus.MODIFIED, staged=False)
    assert entry.display_name == "src/app.py"

    renamed = GitStatusEntry(
        file_path=Path("src/new.py"),
        status=GitFileStatus.RENAMED,
        staged=True,
        old_path=Path("src/old.py"),
    )
    assert "src/old.py ➜ src/new.py" in renamed.display_name


def test_compute_file_diff_hunks_additions() -> None:
    """Verify hunks for added lines."""
    base = "line1\nline2\n"
    curr = "line1\nline1.5\nline2\nline3\n"
    hunks = GitService.compute_file_diff_hunks(base, curr)

    assert len(hunks) == 2
    assert hunks[0].diff_type == GitDiffType.ADDED
    assert hunks[0].start_line == 2
    assert hunks[0].line_count == 1
    assert hunks[0].modified_content == "line1.5"

    assert hunks[1].diff_type == GitDiffType.ADDED
    assert hunks[1].start_line == 4
    assert hunks[1].line_count == 1
    assert hunks[1].modified_content == "line3"


def test_compute_file_diff_hunks_modifications() -> None:
    """Verify hunks for modified lines."""
    base = "alpha\nbeta\ngamma\n"
    curr = "alpha\nBETA\ngamma\n"
    hunks = GitService.compute_file_diff_hunks(base, curr)

    assert len(hunks) == 1
    assert hunks[0].diff_type == GitDiffType.MODIFIED
    assert hunks[0].start_line == 2
    assert hunks[0].line_count == 1
    assert hunks[0].original_content == "beta"
    assert hunks[0].modified_content == "BETA"


def test_compute_file_diff_hunks_deletions() -> None:
    """Verify hunks for deleted lines."""
    base = "one\ntwo\nthree\n"
    curr = "one\nthree\n"
    hunks = GitService.compute_file_diff_hunks(base, curr)

    assert len(hunks) == 1
    assert hunks[0].diff_type == GitDiffType.DELETED
    assert hunks[0].start_line == 2
    assert hunks[0].original_content == "two"
    assert hunks[0].modified_content == ""


def test_compute_file_diff_hunks_identical() -> None:
    """Identical content yields no diff hunks."""
    text = "def hello():\n    return 42\n"
    hunks = GitService.compute_file_diff_hunks(text, text)
    assert hunks == []


def test_git_service_repo_inspection(tmp_path: Path) -> None:
    """Verify GitService repository queries on real workspace and non-repo directory."""
    import tempfile

    svc = GitService()

    # System has git installed
    assert svc.is_git_installed() is True

    # Real workspace is a git repository
    workspace_root = Path(".").resolve()
    assert svc.is_git_repo(workspace_root) is True
    assert svc.find_repo_root(workspace_root) is not None

    # Strict check on empty sub-directory confirms it is not a standalone repo root
    non_repo = tmp_path / "empty_dir"
    non_repo.mkdir()
    assert svc.is_git_repo(non_repo, strict=True) is False

    # Standalone temp directory outside repository hierarchy
    outside_temp = Path(tempfile.gettempdir()) / "pip_viper_non_repo_test"
    outside_temp.mkdir(exist_ok=True)
    try:
        assert svc.is_git_repo(outside_temp) is False
        assert svc.get_status(outside_temp) == []
        assert svc.get_branches(outside_temp) == ("", [])
        assert svc.get_log(outside_temp) == []
    finally:
        try:
            outside_temp.rmdir()
        except OSError:
            pass


def test_git_service_live_repo_queries() -> None:
    """Verify GitService queries active PipViper repository."""
    svc = GitService()
    root = Path(".").resolve()

    curr_branch, branches = svc.get_branches(root)
    assert curr_branch == "main"
    assert any(b.name == "main" for b in branches)

    commits = svc.get_log(root, max_count=5)
    assert len(commits) > 0
    assert commits[0].hash != ""
    assert commits[0].short_hash != ""
    assert commits[0].author != ""

    # Check reading tracked file at HEAD
    head_content = svc.get_file_at_head(root, Path("README.md"))
    assert head_content is not None
    assert len(head_content) > 0

    # Untracked/nonexistent file returns None
    assert svc.get_file_at_head(root, Path("untracked_sample_file.xyz")) is None
