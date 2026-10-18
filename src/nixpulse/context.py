"""Bounded, workspace-local context ingestion.

This module is deliberately independent from the model and orchestration
layers.  It turns a workspace into small, deterministic text fragments while
ensuring that every filesystem access resolves below the configured root.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Iterable


DEFAULT_TOKEN_BUDGET = 2048
DEFAULT_MAX_LINES = 200
DEFAULT_TREE_DEPTH = 2
DEFAULT_TREE_ENTRIES = 120
TRUNCATION_MARKER = "\n...[context truncated]..."


class ContextError(ValueError):
    """Base error for invalid or unavailable workspace context."""


class WorkspaceBoundaryError(ContextError):
    """Raised when a path escapes the configured workspace root."""


def estimate_tokens(text: str) -> int:
    """Estimate tokens conservatively using the common four-character ratio.

    NixPulse does not depend on a tokenizer.  The same estimate is used by
    :func:`truncate_to_budget`, making the bound deterministic and cheap.
    """

    if not text:
        return 0
    return math.ceil(len(text) / 4)


def truncate_to_budget(text: str, token_budget: int = DEFAULT_TOKEN_BUDGET) -> str:
    """Return text whose conservative token estimate fits ``token_budget``.

    Both the beginning and end are retained so a model sees file headers and
    trailing diagnostics.  The marker is included in the budget.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if isinstance(token_budget, bool) or not isinstance(token_budget, int):
        raise TypeError("token_budget must be an integer")
    if token_budget < 1:
        raise ValueError("token_budget must be positive")

    character_budget = token_budget * 4
    if len(text) <= character_budget:
        return text

    marker = TRUNCATION_MARKER
    if len(marker) >= character_budget:
        return marker[:character_budget]

    available = character_budget - len(marker)
    head_size = (available + 1) // 2
    tail_size = available - head_size
    return f"{text[:head_size]}{marker}{text[-tail_size:] if tail_size else ''}"


def truncate_for_model(text: str, token_budget: int = DEFAULT_TOKEN_BUDGET) -> str:
    """Readable alias for callers that build model prompts explicitly."""

    return truncate_to_budget(text, token_budget)


def _positive_integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContextError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class WorkspaceContext:
    """Read-only context view rooted at one workspace directory.

    ``max_lines`` bounds every file request and ``token_budget`` bounds every
    returned fragment.  Missing output is represented by an empty string; an
    invalid path or a directory passed to ``read_file`` raises ``ContextError``.
    """

    root: Path | str
    token_budget: int = DEFAULT_TOKEN_BUDGET
    max_lines: int = DEFAULT_MAX_LINES

    def __post_init__(self) -> None:
        root = Path(self.root).expanduser().resolve(strict=False)
        if not root.exists() or not root.is_dir():
            raise ContextError(f"workspace root is not a directory: {self.root!s}")
        _positive_integer(self.token_budget, "token_budget")
        _positive_integer(self.max_lines, "max_lines")
        object.__setattr__(self, "root", root)

    def safe_path(self, path: str | Path, *, must_exist: bool = False) -> Path:
        """Resolve ``path`` and reject paths outside this workspace.

        Absolute paths are accepted only when they are already below the root.
        Resolving symlinks before the boundary check prevents an in-workspace
        link from exposing an outside file.
        """

        raw = Path(path) if isinstance(path, (str, Path)) else None
        if raw is None:
            raise ContextError("path must be a string or Path")
        if "\x00" in str(raw):
            raise ContextError("path cannot contain NUL")
        candidate = raw if raw.is_absolute() else self.root / raw
        try:
            resolved = candidate.resolve(strict=must_exist)
        except FileNotFoundError as exc:
            raise ContextError(f"path does not exist: {path!s}") from exc
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceBoundaryError(
                f"path is outside workspace root: {path!s}"
            ) from exc
        if must_exist and not resolved.exists():
            raise ContextError(f"path does not exist: {path!s}")
        return resolved

    def read_file(
        self,
        path: str | Path,
        *,
        start_line: int = 1,
        end_line: int | None = None,
        max_lines: int | None = None,
    ) -> str:
        """Read an inclusive, bounded 1-based line range from a text file."""

        start_line = _positive_integer(start_line, "start_line")
        if end_line is not None:
            end_line = _positive_integer(end_line, "end_line")
            if end_line < start_line:
                raise ContextError("end_line must be greater than or equal to start_line")
        requested_limit = self.max_lines if max_lines is None else _positive_integer(
            max_lines, "max_lines"
        )
        line_limit = min(requested_limit, self.max_lines)
        bounded_end = start_line + line_limit - 1
        if end_line is not None:
            bounded_end = min(end_line, bounded_end)

        target = self.safe_path(path, must_exist=True)
        if not target.is_file():
            raise ContextError(f"context path is not a file: {path!s}")

        selected: list[str] = []
        try:
            with target.open("r", encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, start=1):
                    if number < start_line:
                        continue
                    if number > bounded_end:
                        break
                    selected.append(line.rstrip("\r\n"))
        except OSError as exc:
            raise ContextError(f"could not read file: {path!s}") from exc
        return truncate_to_budget("\n".join(selected), self.token_budget)

    def read_range(
        self,
        path: str | Path,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> str:
        """Positional-friendly alias for :meth:`read_file`."""

        return self.read_file(path, start_line=start_line, end_line=end_line)

    def snippet(
        self,
        path: str | Path,
        line: int | None = None,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        before: int = 3,
        after: int = 3,
    ) -> str:
        """Read a small focused snippet around a line or an explicit range.

        Passing ``line`` creates ``before``/``after`` context around that
        1-based line.  Passing ``start_line`` and optionally ``end_line``
        expands that range by the same context counts.
        """

        before = _positive_integer(before, "before") if before else 0
        after = _positive_integer(after, "after") if after else 0
        if line is not None:
            if start_line is not None or end_line is not None:
                raise ContextError("line cannot be combined with start_line or end_line")
            line = _positive_integer(line, "line")
            start_line = max(1, line - before)
            end_line = line + after
        else:
            if start_line is None:
                raise ContextError("snippet requires line or start_line")
            start_line = _positive_integer(start_line, "start_line")
            if end_line is None:
                end_line = start_line
            end_line = _positive_integer(end_line, "end_line")
            if end_line < start_line:
                raise ContextError("end_line must be greater than or equal to start_line")
            start_line = max(1, start_line - before)
            end_line += after
        return self.read_file(path, start_line=start_line, end_line=end_line)

    def read_snippet(self, path: str | Path, line: int, *, before: int = 3, after: int = 3) -> str:
        """Explicit alias for :meth:`snippet` using a center line."""

        return self.snippet(path, line, before=before, after=after)

    def directory_tree(
        self,
        path: str | Path = ".",
        *,
        max_depth: int = DEFAULT_TREE_DEPTH,
        max_entries: int = DEFAULT_TREE_ENTRIES,
    ) -> str:
        """Return a compact, deterministic tree without following symlinks."""

        if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0:
            raise ContextError("max_depth must be a non-negative integer")
        max_entries = _positive_integer(max_entries, "max_entries")
        directory = self.safe_path(path, must_exist=True)
        if not directory.is_dir():
            raise ContextError(f"tree path is not a directory: {path!s}")

        display_root = "." if Path(path) in {Path("."), Path("")} else directory.name
        lines = [f"{display_root}/"]
        seen_entries = 0
        truncated = False

        def visit(current: Path, prefix: str, depth: int) -> None:
            nonlocal seen_entries, truncated
            if depth > max_depth or truncated:
                return
            try:
                entries = sorted(current.iterdir(), key=lambda item: (not item.is_dir(), item.name))
            except OSError as exc:
                raise ContextError(f"could not list directory: {current}") from exc
            for entry in entries:
                if seen_entries >= max_entries:
                    truncated = True
                    return
                seen_entries += 1
                is_link = entry.is_symlink()
                is_dir = entry.is_dir() and not is_link
                suffix = "/" if is_dir else ("@" if is_link else "")
                lines.append(f"{prefix}└── {entry.name}{suffix}")
                if is_dir and depth < max_depth:
                    visit(entry, prefix + "    ", depth + 1)

        visit(directory, "", 1)
        if truncated:
            lines.append("... (directory entries truncated)")
        return truncate_to_budget("\n".join(lines), self.token_budget)

    def tree(self, path: str | Path = ".", **kwargs: int) -> str:
        """Short alias for :meth:`directory_tree`."""

        return self.directory_tree(path, **kwargs)

    def build_context(
        self,
        files: Iterable[str | Path] = (),
        *,
        include_tree: bool = True,
        tree_depth: int = DEFAULT_TREE_DEPTH,
    ) -> str:
        """Build one bounded context document from a tree and file fragments."""

        sections: list[str] = []
        if include_tree:
            sections.append("Directory tree:\n" + self.directory_tree(max_depth=tree_depth))
        for path in files:
            path_text = str(path)
            sections.append(f"File: {path_text}\n{self.read_file(path_text)}")
        return truncate_to_budget("\n\n".join(sections), self.token_budget)


ContextReader = WorkspaceContext


def ingest_context(
    workspace_root: str | Path,
    files: Iterable[str | Path] = (),
    *,
    include_tree: bool = True,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    max_lines: int = DEFAULT_MAX_LINES,
    tree_depth: int = DEFAULT_TREE_DEPTH,
) -> str:
    """Convenience entry point for one bounded workspace context document."""

    return WorkspaceContext(
        workspace_root, token_budget=token_budget, max_lines=max_lines
    ).build_context(files, include_tree=include_tree, tree_depth=tree_depth)


__all__ = [
    "ContextError",
    "ContextReader",
    "DEFAULT_MAX_LINES",
    "DEFAULT_TOKEN_BUDGET",
    "WorkspaceBoundaryError",
    "WorkspaceContext",
    "estimate_tokens",
    "ingest_context",
    "truncate_for_model",
    "truncate_to_budget",
]
