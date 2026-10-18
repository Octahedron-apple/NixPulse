"""ANSI-safe incident detection for stderr files and captured tmux output."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Callable


ANSI_RE = re.compile(
    r"(?:\x1B\[[0-?]*[ -/]*[@-~])|(?:\x1B\][^\x07]*(?:\x07|\x1B\\))|\x1B[@-_]"
)
SIGNATURES = (
    ("missing-header", re.compile(r"fatal error: ([^\s:]+\.h): No such file or directory", re.I)),
    ("missing-command", re.compile(r"(?:command not found|No such file or directory).*", re.I)),
    ("linker-error", re.compile(r"(?:cannot find -l([\w.+-]+)|undefined reference to)", re.I)),
    ("port-collision", re.compile(r"(?:EADDRINUSE|Address already in use)", re.I)),
)


def strip_ansi(value: str) -> str:
    return ANSI_RE.sub("", value).replace("\r", "")


def truncate_output(value: str, *, lines: int = 30, max_chars: int = 1600) -> str:
    """Keep only bounded trailing output for both matching and model context."""

    cleaned = strip_ansi(value)
    trailing = "\n".join(cleaned.splitlines()[-lines:])
    return trailing[-max_chars:]


@dataclass(frozen=True)
class Incident:
    kind: str
    summary: str
    evidence: str
    suggested_package: str | None = None


def detect_incident(output: str, *, exit_code: int | None = None) -> Incident | None:
    bounded = truncate_output(output)
    for kind, pattern in SIGNATURES:
        match = pattern.search(bounded)
        if match:
            evidence = match.group(0).strip()
            suggested = None
            if kind == "missing-header" and match.group(1).lower() == "zlib.h":
                suggested = "zlib"
            elif kind == "linker-error" and match.group(1):
                suggested = {"z": "zlib"}.get(match.group(1).lower())
            return Incident(kind, evidence, bounded, suggested)
    if exit_code == 127:
        return Incident("missing-command", "command exited with status 127", bounded)
    return None


class StderrWatcher:
    """Poll a regular log or FIFO and emit each new failure at most once."""

    def __init__(self, source: str | Path, *, on_incident: Callable[[Incident], None] | None = None) -> None:
        self.source = Path(source)
        self.on_incident = on_incident
        self._seen: set[str] = set()
        self._offset = 0

    def poll_once(self, *, exit_code: int | None = None) -> Incident | None:
        if not self.source.exists():
            return None
        try:
            if stat.S_ISFIFO(self.source.stat().st_mode):
                descriptor = os.open(self.source, os.O_RDONLY | os.O_NONBLOCK)
                try:
                    pieces: list[bytes] = []
                    while True:
                        try:
                            piece = os.read(descriptor, 4096)
                        except BlockingIOError:
                            break
                        if not piece:
                            break
                        pieces.append(piece)
                    chunk = b"".join(pieces).decode("utf-8", errors="replace")
                finally:
                    os.close(descriptor)
            else:
                with self.source.open("r", encoding="utf-8", errors="replace") as stream:
                    stream.seek(self._offset)
                    chunk = stream.read()
                    self._offset = stream.tell()
        except OSError:
            return None
        if not chunk.strip():
            return None
        incident = detect_incident(chunk, exit_code=exit_code)
        if incident is None:
            return None
        fingerprint = hashlib.sha256(incident.evidence.encode()).hexdigest()
        if fingerprint in self._seen:
            return None
        self._seen.add(fingerprint)
        if self.on_incident:
            self.on_incident(incident)
        return incident
