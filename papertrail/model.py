"""Source-neutral, JSON-serializable records. Times are ISO 8601 UTC."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class Prompt:
    id: str
    seq: int
    ts: str
    text: str


@dataclass(frozen=True)
class Replacement:
    old: str
    new: str
    all: bool = False


@dataclass
class Read:
    path: str
    ts: str
    seq: int
    start: int
    lines: list[str]
    complete: bool
    final_ending_unspecified: bool = False


@dataclass
class Edit:
    id: str
    seq: int
    ts: str
    tool: str
    path: str
    prompt: str | None
    model: str
    before: str | None = None
    after: str | None = None
    patch: str | None = None
    replacement: Replacement | None = None
    # Reasoning effort the agent ran with for this call, as the source names it. Empty if unrecorded.
    effort: str = ""
    operation: str = ""
    moved_from: str | None = None
    call_id: str = ""
    appended: str | None = None


@dataclass
class Session:
    id: str
    agent: str
    log_path: str
    model: str = ""
    title: str = ""
    cwd: str = ""
    branch: str = ""
    started_at: str = ""
    ended_at: str = ""
    parent: str | None = None
    resume: str | None = None
    resume_cwd: str = ""
    prompts: list[Prompt] = field(default_factory=list)
    edits: list[Edit] = field(default_factory=list)
    reads: list[Read] = field(default_factory=list)


class Source(Protocol):
    def sessions(self, root: Path) -> Iterator[Session]: ...
