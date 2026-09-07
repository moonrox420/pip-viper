"""Git Version Control System (VCS) engine and service for PipViper.

Provides high-performance, asynchronous-capable Git operations, repository
discovery, branch management, commit history logging, status parsing,
and in-memory diff hunk analysis using standard library difflib.
"""

from __future__ import annotations

import difflib
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

_LOGGER = logging.getLogger("pip_viper.vcs")


class GitFileStatus(str, Enum):
    """Classification of working tree and index file states."""

    MODIFIED = "modified"
    ADDED = "added"
    DELETED = "deleted"
    UNTRACKED = "untracked"
    RENAMED = "renamed"
    COPIED = "copied"
    CONFLICT = "conflict"
    TYPECHANGE = "typechange"
    UNMODIFIED = "unmodified"

    @property
    def badge(self) -> str:
        """Return a single-character badge representing this status."""
        return {
            GitFileStatus.MODIFIED: "M",
            GitFileStatus.ADDED: "A",
            GitFileStatus.DELETED: "D",
            GitFileStatus.UNTRACKED: "?",
            GitFileStatus.RENAMED: "R",
            GitFileStatus.COPIED: "C",
            GitFileStatus.CONFLICT: "U",
            GitFileStatus.TYPECHANGE: "T",
            GitFileStatus.UNMODIFIED: " ",
        }.get(self, " ")


@dataclass(frozen=True)
class GitStatusEntry:
    """Represents a single changed file in the Git working tree or index."""

    file_path: Path
    status: GitFileStatus
    staged: bool
    old_path: Optional[Path] = None

    @property
    def display_name(self) -> str:
        """Return relative path string or old -> new for renames."""
        if self.old_path:
            return f"{self.old_path.as_posix()} ➜ {self.file_path.as_posix()}"
        return self.file_path.as_posix()


@dataclass(frozen=True)
class GitBranch:
    """Represents a local or remote Git branch."""

    name: str
    is_current: bool = False
    upstream: Optional[str] = None
    ahead: int = 0
    behind: int = 0


@dataclass(frozen=True)
class GitCommit:
    """Represents a single Git commit record."""

    hash: str
    short_hash: str
    author: str
    email: str
    relative_date: str
    message: str


class GitDiffType(str, Enum):
    """Classification of line-level diff hunk changes."""

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"


@dataclass(frozen=True)
class GitDiffHunk:
    """Represents a contiguous change hunk between two text revisions."""

    diff_type: GitDiffType
    start_line: int  # 1-indexed line number in current buffer
    line_count: int  # Number of affected lines in current buffer
    original_content: str  # Content from base/HEAD
    modified_content: str  # Content in current buffer
    original_start_line: int = 1


class GitService:
    """Thread-safe interface for Git repository inspection and execution."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout

    @staticmethod
    def is_git_installed() -> bool:
        """Return True if the git executable is discoverable on system PATH."""
        return shutil.which("git") is not None

    @staticmethod
    def is_git_repo(path: Path, strict: bool = False) -> bool:
        """Return True if path is a Git repository (or resides inside one if strict=False)."""
        if not path.exists():
            return False
        search_dir = path if path.is_dir() else path.parent
        if strict:
            return (search_dir / ".git").exists()
        # Check parent hierarchy for .git
        curr = search_dir.resolve()
        while True:
            if (curr / ".git").exists():
                return True
            parent = curr.parent
            if parent == curr:
                break
            curr = parent
        return False

    @staticmethod
    def find_repo_root(path: Path) -> Optional[Path]:
        """Find the root directory containing the .git repository folder."""
        if not path.exists():
            return None
        curr = (path if path.is_dir() else path.parent).resolve()
        while True:
            if (curr / ".git").exists():
                return curr
            parent = curr.parent
            if parent == curr:
                break
            curr = parent
        return None

    def _run(
        self,
        args: list[str],
        cwd: Path,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a git command with timeout and safe string decoding."""
        repo_dir = cwd if cwd.is_dir() else cwd.parent
        return subprocess.run(
            ["git", *args],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self._timeout,
            check=check,
        )

    def init_repo(self, path: Path) -> None:
        """Initialize a new Git repository at the specified directory."""
        self._run(["init"], cwd=path)

    def get_status(self, repo_root: Path) -> list[GitStatusEntry]:
        """Parse git status --porcelain=v1 into structured GitStatusEntry records.

        Porcelain format specifies:
        XY PATH [-> ORIG_PATH]
        X: Staged status (Index)
        Y: Unstaged status (Working Tree)
        """
        if not self.is_git_repo(repo_root):
            return []

        try:
            result = self._run(["status", "--porcelain=v1", "-uall"], cwd=repo_root, check=False)
            if result.returncode != 0:
                _LOGGER.warning("git status returned code %d: %s", result.returncode, result.stderr)
                return []
        except Exception as exc:
            _LOGGER.error("Failed to execute git status: %s", exc)
            return []

        entries: list[GitStatusEntry] = []
        for line in result.stdout.splitlines():
            if len(line) < 3:
                continue

            index_char = line[0]
            work_char = line[1]
            raw_path_part = line[3:].strip()

            # Handle renames e.g. "old_name.py -> new_name.py"
            old_path: Optional[Path] = None
            if " -> " in raw_path_part:
                parts = raw_path_part.split(" -> ", 1)
                old_path = Path(parts[0].strip().strip('"'))
                file_path = Path(parts[1].strip().strip('"'))
            else:
                file_path = Path(raw_path_part.strip('"'))

            # Map index (staged) change if present
            if index_char not in (" ", "?"):
                staged_status = self._char_to_status(index_char)
                entries.append(
                    GitStatusEntry(
                        file_path=file_path,
                        status=staged_status,
                        staged=True,
                        old_path=old_path,
                    )
                )

            # Map working tree (unstaged) change if present
            if work_char not in (" ",):
                if work_char == "?" and index_char == "?":
                    unstaged_status = GitFileStatus.UNTRACKED
                else:
                    unstaged_status = self._char_to_status(work_char)

                entries.append(
                    GitStatusEntry(
                        file_path=file_path,
                        status=unstaged_status,
                        staged=False,
                        old_path=old_path,
                    )
                )

        return entries

    @staticmethod
    def _char_to_status(char: str) -> GitFileStatus:
        mapping = {
            "M": GitFileStatus.MODIFIED,
            "A": GitFileStatus.ADDED,
            "D": GitFileStatus.DELETED,
            "R": GitFileStatus.RENAMED,
            "C": GitFileStatus.COPIED,
            "U": GitFileStatus.CONFLICT,
            "T": GitFileStatus.TYPECHANGE,
            "?": GitFileStatus.UNTRACKED,
        }
        return mapping.get(char, GitFileStatus.MODIFIED)

    def get_branches(self, repo_root: Path) -> tuple[str, list[GitBranch]]:
        """Return active branch name and list of all local Git branches."""
        if not self.is_git_repo(repo_root):
            return "", []

        try:
            result = self._run(
                ["branch", "--list", "-vv", "--no-color"],
                cwd=repo_root,
                check=False,
            )
            if result.returncode != 0:
                return "", []
        except Exception as exc:
            _LOGGER.error("Failed to query git branches: %s", exc)
            return "", []

        current_branch = ""
        branches: list[GitBranch] = []

        for line in result.stdout.splitlines():
            line_str = line.strip()
            if not line_str:
                continue

            is_curr = line.startswith("*")
            # Format: [*] <name> <hash> [<upstream>: ahead X, behind Y] <commit subject>
            clean_line = line[1:].strip() if is_curr else line_str
            parts = clean_line.split()
            if not parts:
                continue

            name = parts[0]
            if is_curr:
                current_branch = name

            upstream: Optional[str] = None
            ahead = 0
            behind = 0

            # Parse tracking upstream in brackets e.g. [origin/main: ahead 1]
            if "[" in clean_line and "]" in clean_line:
                tracking_chunk = clean_line[clean_line.find("[") + 1 : clean_line.find("]")]
                tracking_parts = tracking_chunk.split(":")
                upstream = tracking_parts[0].strip()
                if len(tracking_parts) > 1:
                    extra = tracking_parts[1]
                    if "ahead" in extra:
                        try:
                            ahead_str = extra.split("ahead")[1].split(",")[0].strip()
                            ahead = int(ahead_str.split()[0])
                        except Exception:
                            pass
                    if "behind" in extra:
                        try:
                            behind_str = extra.split("behind")[1].split(",")[0].strip()
                            behind = int(behind_str.split()[0])
                        except Exception:
                            pass

            branches.append(
                GitBranch(
                    name=name,
                    is_current=is_curr,
                    upstream=upstream,
                    ahead=ahead,
                    behind=behind,
                )
            )

        # Fallback if no branches exist yet (e.g. empty repository)
        if not current_branch and not branches:
            try:
                res = self._run(["branch", "--show-current"], cwd=repo_root, check=False)
                if res.returncode == 0 and res.stdout.strip():
                    current_branch = res.stdout.strip()
                    branches.append(GitBranch(name=current_branch, is_current=True))
            except Exception:
                pass

        return current_branch, branches

    def create_branch(self, repo_root: Path, branch_name: str) -> None:
        """Create and check out a new branch."""
        self._run(["checkout", "-b", branch_name], cwd=repo_root)

    def switch_branch(self, repo_root: Path, branch_name: str) -> None:
        """Check out an existing branch."""
        self._run(["checkout", branch_name], cwd=repo_root)

    def get_log(self, repo_root: Path, max_count: int = 25) -> list[GitCommit]:
        """Fetch the most recent commit history records."""
        if not self.is_git_repo(repo_root):
            return []

        # Format: hash|short_hash|author|email|relative_date|subject
        format_spec = "%H|%h|%an|%ae|%ar|%s"
        try:
            result = self._run(
                ["log", f"-n{max_count}", f"--pretty=format:{format_spec}"],
                cwd=repo_root,
                check=False,
            )
            if result.returncode != 0:
                return []
        except Exception as exc:
            _LOGGER.error("Failed to query git log: %s", exc)
            return []

        commits: list[GitCommit] = []
        for line in result.stdout.splitlines():
            parts = line.split("|", 5)
            if len(parts) == 6:
                commits.append(
                    GitCommit(
                        hash=parts[0],
                        short_hash=parts[1],
                        author=parts[2],
                        email=parts[3],
                        relative_date=parts[4],
                        message=parts[5],
                    )
                )
        return commits

    def stage_files(self, repo_root: Path, paths: list[Path]) -> None:
        """Stage the specified files or all files if paths is empty."""
        if not paths:
            self._run(["add", "-A"], cwd=repo_root)
            return

        rel_paths = [p.as_posix() for p in paths]
        self._run(["add", "--", *rel_paths], cwd=repo_root)

    def unstage_files(self, repo_root: Path, paths: list[Path]) -> None:
        """Unstage the specified files or all files if paths is empty."""
        if not paths:
            self._run(["restore", "--staged", "."], cwd=repo_root, check=False)
            return

        rel_paths = [p.as_posix() for p in paths]
        # Try 'git restore --staged', fallback to 'git reset HEAD'
        res = self._run(["restore", "--staged", "--", *rel_paths], cwd=repo_root, check=False)
        if res.returncode != 0:
            self._run(["reset", "HEAD", "--", *rel_paths], cwd=repo_root, check=False)

    def discard_changes(self, repo_root: Path, paths: list[Path]) -> None:
        """Discard working tree changes for the specified files."""
        if not paths:
            return

        for p in paths:
            full_path = (repo_root / p) if not p.is_absolute() else p
            if not full_path.exists():
                # Deleted file: restore it
                self._run(["checkout", "--", p.as_posix()], cwd=repo_root, check=False)
                continue

            # Check if file is untracked
            status_entries = self.get_status(repo_root)
            is_untracked = any(
                e.file_path == p and e.status == GitFileStatus.UNTRACKED for e in status_entries
            )
            if is_untracked:
                try:
                    if full_path.is_file():
                        full_path.unlink()
                    elif full_path.is_dir():
                        shutil.rmtree(full_path)
                except Exception as exc:
                    _LOGGER.warning("Failed to remove untracked file %s: %s", full_path, exc)
            else:
                self._run(["checkout", "HEAD", "--", p.as_posix()], cwd=repo_root, check=False)

    def commit(self, repo_root: Path, message: str, amend: bool = False) -> str:
        """Create a commit with the specified message."""
        if not message.strip():
            raise ValueError("Commit message cannot be empty.")

        args = ["commit", "-m", message]
        if amend:
            args.append("--amend")

        result = self._run(args, cwd=repo_root)
        return (result.stdout or "").strip()

    def push(self, repo_root: Path) -> str:
        """Push commits to the default upstream remote."""
        result = self._run(["push"], cwd=repo_root)
        return (result.stdout or result.stderr or "Pushed successfully.").strip()

    def pull(self, repo_root: Path) -> str:
        """Pull commits from the default upstream remote."""
        result = self._run(["pull"], cwd=repo_root)
        return (result.stdout or result.stderr or "Pulled successfully.").strip()

    def get_file_at_head(self, repo_root: Path, file_path: Path) -> Optional[str]:
        """Fetch the contents of file_path at Git HEAD.

        Returns None if file is untracked or does not exist at HEAD.
        """
        if not self.is_git_repo(repo_root):
            return None

        # Convert to relative posix path for git show
        try:
            rel = (
                file_path.relative_to(repo_root)
                if file_path.is_absolute()
                else file_path
            ).as_posix()
        except ValueError:
            return None

        try:
            result = self._run(["show", f"HEAD:{rel}"], cwd=repo_root, check=False)
            if result.returncode == 0:
                return result.stdout
            return None
        except Exception:
            return None

    def get_diff(
        self,
        repo_root: Path,
        file_path: Optional[Path] = None,
        staged: bool = False,
    ) -> str:
        """Return unified diff output for a file or entire repository."""
        if not self.is_git_repo(repo_root):
            return ""

        args = ["diff", "--no-color"]
        if staged:
            args.append("--staged")
        if file_path is not None:
            try:
                rel = (
                    file_path.relative_to(repo_root)
                    if file_path.is_absolute()
                    else file_path
                ).as_posix()
                args.extend(["--", rel])
            except ValueError:
                pass

        try:
            result = self._run(args, cwd=repo_root, check=False)
            return result.stdout or ""
        except Exception:
            return ""

    @staticmethod
    def compute_file_diff_hunks(base_text: str, current_text: str) -> list[GitDiffHunk]:
        """Compute line-level diff hunks comparing base_text against current_text.

        Uses standard library difflib.SequenceMatcher for fast, 100% in-memory
        resolution with zero subprocess overhead.
        """
        base_lines = base_text.splitlines()
        current_lines = current_text.splitlines()

        matcher = difflib.SequenceMatcher(None, base_lines, current_lines)
        hunks: list[GitDiffHunk] = []

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue

            orig_chunk = "\n".join(base_lines[i1:i2])
            mod_chunk = "\n".join(current_lines[j1:j2])

            if tag == "insert":
                # Current lines [j1:j2] were inserted (1-indexed: j1 + 1)
                hunks.append(
                    GitDiffHunk(
                        diff_type=GitDiffType.ADDED,
                        start_line=j1 + 1,
                        line_count=max(1, j2 - j1),
                        original_content="",
                        modified_content=mod_chunk,
                        original_start_line=i1 + 1,
                    )
                )
            elif tag == "replace":
                # Lines modified
                hunks.append(
                    GitDiffHunk(
                        diff_type=GitDiffType.MODIFIED,
                        start_line=j1 + 1,
                        line_count=max(1, j2 - j1),
                        original_content=orig_chunk,
                        modified_content=mod_chunk,
                        original_start_line=i1 + 1,
                    )
                )
            elif tag == "delete":
                # Lines deleted from base before current line j1 + 1
                hunks.append(
                    GitDiffHunk(
                        diff_type=GitDiffType.DELETED,
                        start_line=max(1, j1 + 1),
                        line_count=max(1, i2 - i1),
                        original_content=orig_chunk,
                        modified_content="",
                        original_start_line=i1 + 1,
                    )
                )

        return hunks
