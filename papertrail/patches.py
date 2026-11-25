"""Decode a single file's patch into old-file coordinates and literal line operations."""

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import groupby


def lines(text):
    parts = text.replace("\r\n", "\n").split("\n")
    return [p + "\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


@dataclass
class Section:
    start: int | None
    ops: list
    scope: str = ""
    eof: bool = False

    @property
    def before(self):
        return [text for sign, text in self.ops if sign != "+"]

    def changes(self, pos):
        for equal, grouped in groupby(self.ops, lambda item: item[0] == " "):
            block = list(grouped)
            end = pos + sum(sign != "+" for sign, _ in block)
            if not equal:
                yield pos, end, [text for sign, text in block if sign == "+"]
            pos = end


def sections(patch):
    raw_sections = []
    for raw in lines(patch):
        line = raw.removesuffix("\n")
        if line.startswith("@@"):
            raw_sections.append([line, [], False])
        elif raw_sections and line.startswith((" ", "-", "+")):
            raw_sections[-1][1].append((line[0], line[1:] + "\n"))
        elif raw_sections and line == "\\ No newline at end of file" and raw_sections[-1][1]:
            op, text = raw_sections[-1][1][-1]
            raw_sections[-1][1][-1] = op, text.rstrip("\n")
        elif line == "*** End of File" and raw_sections:
            raw_sections[-1][2] = True
        elif line == "*** End Patch":
            continue
        elif raw_sections or not line.startswith(
            ("--- ", "+++ ", "*** Begin Patch", "*** Update File: ")
        ):
            raise ValueError("Unsupported patch record.")
    result = []
    previous_end = delta = 0
    for title, ops, eof in raw_sections:
        m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", title)
        pos = None
        if m:
            removed, added = int(m[2] or 1), int(m[4] or 1)
            pos = int(m[1]) - bool(removed)
            if (
                removed != sum(s != "+" for s, _ in ops)
                or added != sum(s != "-" for s, _ in ops)
                or pos < previous_end
                or int(m[3]) - bool(added) != pos + delta
            ):
                raise ValueError("Invalid numbered patch.")
            previous_end = pos + removed
            delta += added - removed
        elif title.startswith("@@ -"):
            raise ValueError("Invalid patch header.")
        result.append(
            Section(
                pos,
                ops,
                title.removeprefix("@@").removeprefix(" ") if not m else "",
                eof,
            )
        )
    if not result:
        raise ValueError("Missing patch hunk header.")
    return result


def positions(old, parts, open_tail=False, postimage=False):
    cursor = 0
    for section in parts:
        pos = section.start
        context = [t for sign, t in section.ops if sign != "-"] if postimage else section.before
        if section.scope:
            scopes = [
                i
                for i in range(cursor, len(old))
                if old[i] is not None and old[i].rstrip("\n") == section.scope
            ]
            if len(scopes) != 1:
                raise ValueError("Patch scope missing or ambiguous.")
            cursor = scopes[0] + 1
            if not section.ops:
                continue
        if (
            pos is None
            or pos < cursor
            or pos > len(old)
            or old[pos : pos + len(context)] != context
        ):
            candidates = [
                i
                for i in range(cursor, len(old) - len(context) + 1)
                if old[i : i + len(context)] == context
                and (not section.eof or (not open_tail and i + len(context) == len(old)))
            ]
            if len(candidates) != 1:
                raise ValueError("Patch context missing or ambiguous.")
            pos = candidates[0]
        yield section, pos
        cursor = pos + len(context)


def patch_changes(old, patch, open_tail=False):
    return [
        op for part, pos in positions(old, sections(patch), open_tail) for op in part.changes(pos)
    ]


def changes(old, new):
    start = 0
    end = min(len(old), len(new))
    while start < end and old[start] == new[start]:
        start += 1
    tail = 0
    while tail < end - start and old[-1 - tail] == new[-1 - tail]:
        tail += 1
    a, b = old[start : len(old) - tail], new[start : len(new) - tail]
    for tag, i, j, k, l in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            yield start + i, start + j, b[k:l]
