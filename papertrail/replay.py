"""Replay one path at a time. Each operation uses pre-change line coordinates."""

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime
from difflib import SequenceMatcher
from itertools import groupby, tee
from pathlib import Path
from stat import S_ISREG

from .fragments import apply_replacement, hits, locate_later
from .model import Read, Replacement
from .patches import changes, lines, patch_changes, positions, sections


@dataclass
class Hunk:
    id: str
    kind: str
    ts: str
    added: int
    removed: int
    text: str
    replaced: dict
    edit: str | None
    preimage_unknown: bool = False
    # The lines a pure deletion took out. Empty when any of them was unobserved.
    removed_text: str = ""


@dataclass
class FileHistory:
    path: str
    status: str = ""
    lines: int = 0
    external_events: int = 0
    unapplied: int = 0
    hunks: list[Hunk] = field(default_factory=list)
    ranges: list[tuple[int, int, str]] = field(default_factory=list)
    # Pure deletions still between two current lines: (lines above the spot, hunk).
    gaps: list[tuple[int, str]] = field(default_factory=list)
    reasons: Counter = field(default_factory=Counter)
    unplaced_edits: set = field(default_factory=set)
    unknown_reasons: dict = field(default_factory=dict)
    sha: str = field(default="", repr=False, compare=False)


@dataclass
class DiskImage:
    status: str
    rows: list[str] | None
    sha: str = ""


def unique_block(rows, index, pos, size):
    """A whole matching run can be unique even when each line is repeated."""
    if size == 0:
        return True
    pivot = min(range(pos, pos + size), key=lambda i: len(index[rows[i]]))
    candidates = index[rows[pivot]]
    if len(candidates) == 1:
        return True
    block = rows[pos : pos + size]
    return not any(
        other != pivot and (at := other - pivot + pos) >= 0 and rows[at : at + size] == block
        for other in candidates
    )


def anchors(old, new):
    indexes = []
    for rows in (old, new):
        index = defaultdict(list)
        for i, text in enumerate(rows):
            index[text].append(i)
        indexes.append(index)
    for i, j, n in SequenceMatcher(None, old, new).get_matching_blocks():
        if not n or (unique_block(old, indexes[0], i, n) and unique_block(new, indexes[1], j, n)):
            yield i, j, n


class DeletionPoints:
    """Store marks in the gaps between rows, keeping their original creation order."""

    def __init__(self):
        self.slots = []
        self.next_order = 0

    def add(self, pos, owner, row_count):
        if not self.slots:
            self.slots = [None] * (row_count + 1)
        if self.slots[pos] is None:
            self.slots[pos] = []
        self.slots[pos].append((self.next_order, owner))
        self.next_order += 1

    def pad(self, extra):
        if self.slots:
            self.slots.extend([None] * extra)

    def splice(self, start, end, size):
        if not self.slots:
            return
        if start == end:
            # An insertion leaves the mark at its starting boundary in place.
            self.slots[start + 1 : start + 1] = [None] * size
        elif size:
            self.slots[start + 1 : end] = [None] * (size - 1)
        else:
            # A deletion joins both boundaries and all the gaps between them.
            buckets = [bucket for bucket in self.slots[start : end + 1] if bucket]
            if buckets:
                merged = max(buckets, key=len)
                for bucket in buckets:
                    if bucket is not merged:
                        merged.extend(bucket)
                self.slots[start] = merged
            del self.slots[start + 1 : end + 1]

    def align(self, moves, row_count):
        """Carry marks through the first aligned span covering each gap."""
        if not self.slots:
            return
        kept = [None] * (row_count + 1)
        span = 0
        for pos, bucket in enumerate(self.slots):
            if not bucket:
                continue
            while moves[span][1] < pos:
                span += 1
            old_start, old_end, new_start, new_end, matched = moves[span]
            if matched or pos == old_start:
                at = new_start + pos - old_start
            elif pos == old_end or new_start == new_end:
                at = new_end
            else:
                continue
            if kept[at] is None:
                kept[at] = bucket
            else:
                kept[at].extend(bucket)
        self.slots = kept

    def ordered(self):
        return [
            (pos, owner)
            for _, pos, owner in sorted(
                (order, pos, owner)
                for pos, bucket in enumerate(self.slots) if bucket
                for order, owner in bucket
            )
        ]


class State:
    """Indexed physical lines. None is an unobserved line; an open tail has unknown length.

    A deletion leaves a mark between two rows. Marks move with the rows around them.
    A rewrite of those rows takes the spot away, and with it the mark.
    """

    def __init__(self, path):
        self.file = FileHistory(path)
        self.rows = []
        self.owners = []
        self.marks = DeletionPoints()
        self.known_owners = set()
        self.open_tail = True
        self.edited = False
        self.observed = False
        self.last_ts = None
        self.external_at = None
        self.uncertain_edit = False
        self.unplaced_outputs = set()
        self.soft_endings = set()

    def hunk(self, kind, ts, added, removed, edit=None, unknown_before=False, removed_rows=()):
        key = f"{self.file.path}#{len(self.file.hunks)}"
        prior = Counter(owner for owner in removed if owner in self.known_owners)
        deleted = not added and removed_rows and None not in removed_rows
        self.file.hunks.append(
            Hunk(
                key,
                kind,
                ts,
                len(added),
                len(removed),
                "" if kind == "unknown" else "".join(added),
                dict(prior),
                edit,
                unknown_before,
                "".join(removed_rows) if deleted else "",
            )
        )
        if kind == "unknown":
            self.file.unknown_reasons[key] = "first_observation_after_edit"
        else:
            self.known_owners.add(key)
        if kind == "external" and self.external_at != ts:
            self.file.external_events += 1
            self.external_at = ts
        return key

    def splice(self, start, end, added, kind, ts, edit=None, unknown_before=False, fill=False):
        """Replace rows[start:end]. `fill` only supplies text for the same rows."""
        rows = self.rows[start:end]
        owner = self.hunk(kind, ts, added, self.owners[start:end], edit, unknown_before, rows)
        if self.soft_endings:
            delta = len(added) - (end - start)
            self.soft_endings = {
                i if i < start else i + delta for i in self.soft_endings if i < start or i >= end
            }
        if not fill:
            self.marks.splice(start, end, len(added))
        self.rows[start:end] = added
        self.owners[start:end] = [owner] * len(added)
        if end > start and not added:
            self.marks.add(start, owner, len(self.rows))

    def forget(self, first, end, count):
        self.marks.splice(first, end, count)
        self.rows[first:end] = [None] * count
        self.owners[first:end] = [None] * count
        self.soft_endings = {
            i if i < first else i + count - (end - first)
            for i in self.soft_endings
            if i < first or i >= end
        }

    def pad(self, count):
        extra = max(0, count - len(self.rows))
        self.rows.extend([None] * extra)
        self.owners.extend([None] * extra)
        self.marks.pad(extra)

    def reveal(self, start, observed, ts):
        """Apply an observation at a known address, preserving the owner of equal lines."""
        if observed and start + len(observed) > len(self.rows):
            self.open_tail = True
        self.pad(start + len(observed))
        for i in self.soft_endings.intersection(range(start, start + len(observed))):
            if self.rows[i].removesuffix("\n") == observed[i - start].removesuffix("\n"):
                self.rows[i] = observed[i - start]
        self.soft_endings.difference_update(range(start, start + len(observed)))
        missing = "unknown" if self.edited else "baseline"

        def kind(i):
            old = self.rows[start + i]
            if old is None:
                return missing
            return "external" if old != observed[i] else None

        for source, items in groupby(range(len(observed)), kind):
            indexes = list(items)
            if source:
                a, b = indexes[0], indexes[-1] + 1
                self.splice(start + a, start + b, observed[a:b], source, ts, fill=True)
        self.observed = True

    def read(self, snapshot, ts):
        if snapshot.complete:
            self.sync(snapshot.lines, "external", ts)
            self.uncertain_edit = False
            self.unplaced_outputs.clear()
            return
        start = snapshot.start - 1
        observed = snapshot.lines
        soft = snapshot.final_ending_unspecified and bool(observed)
        if soft:
            last = start + len(observed) - 1
            if (
                last < len(self.rows)
                and self.rows[last] is not None
                and self.rows[last].removesuffix("\n") == observed[-1].removesuffix("\n")
            ):
                observed = [*observed[:-1], self.rows[last]]
                soft = last in self.soft_endings
            elif observed[-1] == "\n":
                # An empty last display row could be the formatter's EOF marker.
                observed = observed[:-1]
                soft = False
        # A bounded read may show an insertion or deletion that shifted the
        # rest of the file. A unique matching right boundary fixes the old span.
        stop = start + len(observed)
        if (
            observed
            and not self.open_tail
            and None not in self.rows
            and self.rows[start:stop] != observed
            and (start == 0 or self.rows[start : start + 1] == observed[:1])
        ):
            for old_pos, new_pos, count in anchors(self.rows, observed):
                if count and old_pos >= start and new_pos + count == len(observed):
                    old_stop = old_pos + count
                    if old_stop != stop:
                        self.sync(
                            self.rows[:start] + observed + self.rows[old_stop:],
                            "external",
                            ts,
                        )
                    break
        self.reveal(start, observed, ts)
        if soft:
            self.soft_endings.add(start + len(observed) - 1)

    def sync(self, target, kind, ts, edit=None):
        """Match unique known runs. Wildcard gaps absorb any number of disk lines."""
        old, owners = self.rows, self.owners
        if target and not target[-1].endswith("\n"):
            final = target[-1]
            final_with_lf = final + "\n"
            candidates = [i for i in self.soft_endings if old[i] == final_with_lf]
            if (
                len(candidates) == 1
                and old.count(final_with_lf) == 1
                and target.count(final_with_lf) == 0
            ):
                i = candidates[0]
                old[i] = final
        self.soft_endings.clear()
        disputed = set()
        for fragment in self.unplaced_outputs:
            for start, span, _ in hits([(0, target)], fragment):
                disputed.update(range(start, start + len(span)))
        if old == target and None not in old:
            self.open_tail = False
            self.observed = True
            return
        assigned = []
        # Old spans and the new spans they became, in order, to carry the marks.
        moves, deletions = [], []
        a = b = 0
        for i, j, n in anchors(old, target):
            moves.append((a, i, b, j, False))
            moves.append((i, i + n, j, j + n, True))
            if i > a or j > b:
                gap = None in old[a:i] or (i == len(old) and self.open_tail)
                values = target[b:j]
                # Equal text in an unmatched gap could belong to multiple old rows.
                uncertain = set(old[a:i])
                if kind in ("external", "baseline") and not self.edited:
                    missing = "baseline"
                else:
                    missing = "unknown"

                observed_values = set(values) if kind == "external" and gap else set()
                origins = []
                for k, value in enumerate(values, b):
                    if k in disputed:
                        origins.append("unknown")
                    elif gap:
                        previous_pos = a + k - b
                        # A numbered full observation can contradict a known cell
                        # in a hole. Preserve the observation, without guessing a
                        # replacement edge across the rest of the unknown interval.
                        contradiction = (
                            kind == "external"
                            and previous_pos < i
                            and old[previous_pos] is not None
                            and old[previous_pos] not in observed_values
                            and value not in uncertain
                        )
                        origins.append("external" if contradiction else missing)
                    else:
                        origins.append("unknown" if value in uncertain else kind)
                groups = [
                    (src, list(items))
                    for src, items in groupby(zip(origins, values), key=lambda pair: pair[0])
                ]
                for src, group in groups:
                    part = [v for _, v in group]
                    # Only a fully bounded, unambiguous replacement has known predecessors.
                    removed = (
                        owners[a:i] if len(groups) == 1 and not gap and src != "unknown" else []
                    )
                    owner = self.hunk(src, ts, part, removed, edit if src == "agent" else None)
                    if src == "unknown":
                        if disputed.intersection(range(b, j)):
                            reason = "unplaced_output"
                        elif gap:
                            reason = "unobserved_window"
                        else:
                            reason = "ambiguous_alignment"
                        self.file.unknown_reasons[owner] = reason
                    assigned.extend([owner] * len(part))
                if not values and i > a and not gap:
                    deletions.append((b, self.hunk(kind, ts, [], owners[a:i], edit, False, old[a:i])))
            assigned.extend(owners[i : i + n])
            a, b = i + n, j + n
        self.marks.align(moves, len(target))
        for pos, owner in deletions:
            self.marks.add(pos, owner, len(target))
        self.rows, self.owners = target.copy(), assigned
        self.open_tail = False
        self.observed = True

    def ops(self, changeset, edit):
        offset = 0
        for i, j, added in changeset:
            self.splice(i + offset, j + offset, added, "agent", edit.ts, edit.id)
            offset += len(added) - (j - i)

    def snapshot_time(self, ts):
        if self.last_ts:
            a, b = map(datetime.fromisoformat, (self.last_ts, ts))
            return (a + (b - a) / 2).isoformat()
        return ts


def apply_output(state, edit, target):
    if edit.before is None and state.open_tail and all(row is None for row in state.rows):
        state.splice(0, len(state.rows), target, "agent", edit.ts, edit.id, True)
        state.open_tail = False
    elif edit.before is not None and not state.open_tail and None not in state.rows:
        state.ops(list(changes(state.rows, target)), edit)
    else:
        state.sync(target, "agent", edit.ts, edit.id)


def later_context(state, edit, parts, following, disk):
    """Invert unique whole-line postimages against a complete later observation."""
    check, remaining = tee(following)
    image = disk
    for event in check:
        if isinstance(event, Read):
            if not event.complete:
                continue
            image = event.lines
        else:
            image = lines(event.before) if event.before is not None else None
        break
    if image is not None:
        anchors = []
        delta = 0
        try:
            for part, pos in positions(image, parts, postimage=True):
                after = [t for sign, t in part.ops if sign != "-"]
                if not after or any(
                    row == after[0] and state.rows[i : i + len(after)] == after
                    for i, row in enumerate(state.rows)
                ):
                    raise ValueError
                first = pos - delta
                if first < 0 or any(
                    row is not None and row != wanted
                    for row, wanted in zip(state.rows[first:], part.before)
                ):
                    raise ValueError
                anchors.append((part, first))
                delta += len(after) - len(part.before)
        except ValueError:
            pass
        else:
            return anchors
    if len(parts) == 1 and not parts[0].scope and not parts[0].eof:
        part = parts[0]
        r = Replacement("".join(part.before), "".join(t for sign, t in part.ops if sign != "-"))
        anchor = locate_later(state, replace(edit, replacement=r), remaining, disk)
        if anchor and anchor[2] == 0 and anchor[1] == part.before:
            return [(part, anchor[0])]
    raise ValueError("Unplaced patch context.")


def apply_edit(state, edit, following, disk):
    ts = state.snapshot_time(edit.ts)
    if edit.before is not None:
        state.sync(lines(edit.before), "external" if state.observed else "baseline", ts)
        state.uncertain_edit = False
        state.unplaced_outputs.clear()
    target = lines(edit.after) if edit.after is not None else None
    if edit.patch:
        parts = sections(edit.patch)
        numbered = all(p.start is not None for p in parts)
        if target is not None and edit.before is not None:
            ops = patch_changes(state.rows, edit.patch)
            check = state.rows.copy()
            for a, b, t in reversed(ops):
                check[a:b] = t
            if check == target:
                state.ops(ops, edit)
                return
        elif numbered:
            if edit.patch.startswith("--- /dev/null\n"):
                state.sync([], "external", ts)
            for part in parts:
                state.reveal(part.start, part.before, ts)
            state.ops([op for p in parts for op in p.changes(p.start)], edit)
            if target is not None:
                state.sync(target, "external", ts)
            return
        elif target is None:
            try:
                if state.open_tail and any(not p.before and p.ops for p in parts):
                    raise ValueError("Missing insertion address.")
                ops = patch_changes(state.rows, edit.patch, state.open_tail)
            except ValueError:
                if not edit.operation:
                    raise
                anchors = later_context(state, edit, parts, following, disk)
                for part, first in anchors:
                    state.reveal(first, part.before, ts)
                ops = [op for part, first in anchors for op in part.changes(first)]
            state.ops(ops, edit)
            return
    if target is not None:
        apply_output(state, edit, target)
        return
    apply_replacement(state, edit, following, disk)


def disk_image(path):
    try:
        location = Path(path)
        if not S_ISREG(location.stat().st_mode):
            raise OSError()
        raw = location.read_bytes()
        text = raw.decode("utf-8")
        sha = hashlib.sha256(raw).hexdigest()
        del raw
        return DiskImage("", lines(text), sha)
    except FileNotFoundError:
        return DiskImage("missing", None)
    except (OSError, UnicodeError):
        return DiskImage("unreadable", None)


def append_output(state, edit, following, disk):
    content = edit.appended
    if not content:
        return
    if state.open_tail or (state.rows and state.rows[-1] is None):
        # Consecutive appends have one verifiable combined suffix. A different
        # mutation is a barrier unless it supplies a complete preimage.
        suffix = [content]
        image = disk
        for later in following:
            if isinstance(later, Read):
                if not later.complete:
                    continue
                image = later.lines
            elif later.operation == "append" and later.appended is not None:
                suffix.append(later.appended)
                continue
            else:
                image = lines(later.before) if later.before is not None else None
            break
        text = "".join(image or [])
        ending = "".join(suffix)
        if not text.endswith(ending):
            raise ValueError("unknown_append_boundary")
        before = lines(text[: -len(ending)])
        if len(before) < len(state.rows) or any(
            a is not None and a != b for a, b in zip(state.rows, before)
        ):
            raise ValueError("unknown_append_boundary")
        state.edited = True
        state.reveal(0, before, edit.ts)
        state.open_tail = False
    end = len(state.rows)
    if end and state.rows[-1] is not None and not state.rows[-1].endswith("\n"):
        state.splice(end - 1, end, lines(state.rows[-1] + content), "agent", edit.ts, edit.id)
    else:
        state.splice(end, end, lines(content), "agent", edit.ts, edit.id)


def apply_event(state, event, following, disk):
    if isinstance(event, Read):
        state.read(event, state.snapshot_time(event.ts))
    else:
        skipped = state.file.unapplied
        try:
            if event.operation == "append":
                append_output(state, event, following, disk)
            elif event.operation == "delete":
                if event.before is not None:
                    state.sync(lines(event.before), "external", state.snapshot_time(event.ts))
                state.ops([(0, len(state.rows), [])], event)
                state.open_tail = False
            elif event.operation in ("move", "update") and not event.patch and event.after is None:
                pass  # A committed empty diff (including a pure move).
            else:
                if event.replacement:
                    r = event.replacement
                    event = replace(
                        event,
                        replacement=replace(
                            r,
                            old=r.old.replace("\r\n", "\n"),
                            new=r.new.replace("\r\n", "\n"),
                        ),
                    )
                apply_edit(state, event, following, disk)
        except ValueError as exc:
            if event.after is not None:
                apply_output(state, event, lines(event.after))
            else:
                reason = (
                    str(exc)
                    if event.replacement or event.operation == "append"
                    else "unplaced_patch"
                )
                if event.replacement and event.replacement.new:
                    state.unplaced_outputs.add(event.replacement.new)
                if event.operation and event.patch:
                    for part in sections(event.patch):
                        for _, _, added in part.changes(0):
                            if added:
                                state.unplaced_outputs.add("".join(added))
                state.uncertain_edit = True
                state.file.reasons[reason] += 1
                state.file.unapplied += 1
        if state.file.unapplied > skipped:
            state.file.unplaced_edits.add(event.id)
        if event.after is not None or event.operation == "delete":
            state.uncertain_edit = False
            state.unplaced_outputs.clear()
        state.edited = state.observed = True
    state.last_ts = event.ts


def finish(state, snapshot):
    f = state.file
    f.status, f.sha = snapshot.status, snapshot.sha
    disk = snapshot.rows
    if disk is None:
        return f
    complete = not state.open_tail and None not in state.rows
    exact = complete and state.rows == disk
    # A numbered window can cover the whole disk, but cannot prove its prior EOF.
    state.sync(disk, "drift", "")
    f.status = "exact" if exact else "drift"
    if state.uncertain_edit or not complete:
        f.status = "unresolved"
    f.lines = len(disk)
    start = 1
    for owner, group in groupby(state.owners):
        end = start + sum(1 for _ in group)
        f.ranges.append((start, end - 1, owner))
        start = end
    f.gaps = state.marks.ordered()
    return f


def order(e):
    return e.ts, e.seq, getattr(e, "id", "")


def replay(path, edits, reads=()):
    state = State(path)
    events = sorted([*edits, *reads], key=order)
    disk = disk_image(path)
    states = {path: state}
    replay_component(states, events, {path: disk})
    return finish(states[path], disk)


def following_events(segment, idx):
    for i in range(idx + 1, len(segment)):
        yield segment[i]


def replay_component(states, events, disks):
    # A future observation only belongs to this path until its next lifetime
    # boundary. Build the intervals once, without copying every event suffix.
    intervals = defaultdict(list)
    locations = {}
    for event in events:
        if getattr(event, "operation", "") in ("move", "delete", "add"):
            for path in {event.path, getattr(event, "moved_from", None)} - {None}:
                intervals[path].append([])
        if not intervals[event.path]:
            intervals[event.path].append([])
        segment = intervals[event.path][-1]
        locations[id(event)] = (segment, len(segment))
        segment.append(event)

    def reset(path):
        prior = states[path]
        fresh = State(path)
        fresh.file = prior.file
        fresh.open_tail = False
        states[path] = fresh

    for event in events:
        path = event.path
        op = getattr(event, "operation", "")
        if op == "move" and event.moved_from != path:
            old = states[event.moved_from]
            dest_history = states[path].file
            reset(event.moved_from)
            old.file = dest_history
            states[path] = old
        elif op == "add":
            reset(path)
        segment, idx = locations[id(event)]
        disk = disks[path].rows if segment is intervals[path][-1] else None
        apply_event(states[path], event, following_events(segment, idx), disk)


def histories(sessions):
    by_path, reads, neighbors = defaultdict(list), defaultdict(list), defaultdict(set)
    for session in sessions:
        for e in session.edits:
            by_path[e.path].append(e)
            if e.moved_from:
                neighbors[e.path].add(e.moved_from)
                neighbors[e.moved_from].add(e.path)
                by_path.setdefault(e.moved_from, [])
        for r in session.reads:
            reads[r.path].append(r)
    seen = set()
    for first in sorted(by_path):
        if first in seen:
            continue
        group, pending = set(), [first]
        while pending:
            path = pending.pop()
            if path not in group:
                group.add(path)
                pending.extend(neighbors[path] - group)
        seen.update(group)
        states = {p: State(p) for p in group}
        disks = {p: disk_image(p) for p in group}
        events = sorted((e for p in group for e in (*by_path[p], *reads[p])), key=order)
        replay_component(states, events, disks)
        for path in sorted(group):
            yield finish(states[path], disks[path])
