"""Match literal edits against known windows and recover their positions from later evidence."""

from bisect import bisect_left, bisect_right
from itertools import groupby

from .model import Read
from .patches import changes, lines, sections


def runs(rows):
    for known, grouped in groupby(enumerate(rows), lambda item: item[1] is not None):
        if known:
            chunk = list(grouped)
            yield chunk[0][0], [r for _, r in chunk]


def occurrences(text, needle):
    at = text.find(needle)
    while at >= 0:
        yield at
        at = text.find(needle, at + 1)


def hits(windows, needle, replacement=""):
    found = []
    if not needle:
        return found
    for start, rows in windows:
        text = "".join(rows)
        offsets = [0]
        for row in rows:
            offsets.append(offsets[-1] + len(row))
        for at in occurrences(text, needle):
            a = bisect_right(offsets, at) - 1
            # Include a following line when a substitution can join physical lines.
            stop = at + len(needle)
            b = bisect_left(offsets, stop)
            prefix = text[offsets[a] : at] + replacement
            if offsets[b] == stop and prefix and not prefix.endswith("\n") and b < len(rows):
                b += 1
            found.append((start + a, rows[a:b], at - offsets[a]))
    return found


def substitute(state, edit, found):
    """Apply literal matches to their original line spans, including inline edits."""
    r = edit.replacement
    groups = []
    for first, span, at in found:
        if groups and first <= groups[-1][1]:
            groups[-1][1] = max(groups[-1][1], first + len(span))
            groups[-1][2].append((first, at))
        else:
            groups.append([first, first + len(span), [(first, at)]])
    for first, end, positions in reversed(groups):
        rows = state.rows[first:end]
        text = "".join(rows)
        offsets = [0]
        for row in rows:
            offsets.append(offsets[-1] + len(row))
        for row, at in reversed(positions):
            at += offsets[row - first]
            text = text[:at] + r.new + text[at + len(r.old) :]
        # Several adjacent deletions can join a line even if their individual
        # replacements did not. Include the surviving suffix in that physical line.
        if text and not text.endswith("\n") and rows[-1].endswith("\n") and end < len(state.rows):
            suffix = state.rows[end]
            if suffix is not None:
                text += suffix
                rows = [*rows, suffix]
        for i, j, new in reversed(list(changes(rows, lines(text)))):
            state.splice(first + i, first + j, new, "agent", edit.ts, edit.id)


def preimage_windows(event):
    if isinstance(event, Read):
        return [(event.start - 1, event.lines)]
    if event.before is not None:
        return [(0, lines(event.before))]
    if event.patch:
        return [(s.start, s.before) for s in sections(event.patch) if s.start is not None]
    return []


def translate_back(start, size, changesets):
    """A witness may cross later numbered edits only through untouched lines."""
    for ops in reversed(changesets):
        delta = 0
        for i, j, added in ops:
            pos = i + delta
            if pos >= start + size:
                break
            if pos + len(added) > start or (not added and start < pos):
                return None
            delta += len(added) - (j - i)
        start -= delta
    return start


def before_later_changes(state, first_edit, following, disk):
    """Rewind a complete witness through consecutive independently invertible changes."""
    pending = [first_edit]
    for later in following:
        if isinstance(later, Read):
            if later.complete:
                image = later.lines.copy()
                break
            continue
        if later.before is not None:
            image = lines(later.before)
            break
        if not (later.replacement or later.patch):
            return None
        pending.append(later)
    else:
        if disk is None:
            return None
        image = disk.copy()

    for change in reversed(pending):
        if change.replacement:
            pattern = change.replacement
            # Earlier occurrences of the postimage would make its history ambiguous.
            if not pattern.new or hits(runs(state.rows), pattern.new):
                return None
            text = "".join(image)
            at = text.find(pattern.new)
            if at < 0 or text.find(pattern.new, at + 1) >= 0:
                return None
            image = lines(text[:at] + pattern.old + text[at + len(pattern.new) :])
        else:
            try:
                parts = sections(change.patch)
            except ValueError:
                return None
            for part in parts:
                if part.start is None:
                    return None
                after = [t for sign, t in part.ops if sign != "-"]
                before = part.before
                # Previous sections have already been restored to old coordinates.
                start = part.start
                if start > len(image) or image[start : start + len(after)] != after:
                    return None
                image[start : start + len(after)] = before
    return image


def locate_later(state, edit, following, disk):
    r = edit.replacement
    if not r.new or hits(runs(state.rows), r.new):
        return None
    changesets = []
    candidate = None
    following = iter(following)
    for event in following:
        try:
            windows = preimage_windows(event)
        except ValueError:
            return candidate
        found = hits(windows, r.new, r.old)
        if len(found) > 1:
            return None
        if found:
            mapped = witness(state, r, found, changesets)
            if mapped and candidate and mapped != candidate:
                return None
            candidate = candidate or mapped
        if isinstance(event, Read):
            if event.complete:
                # A complete observation closes this interval, including negative evidence.
                return candidate
            continue
        if event.before is not None:
            return candidate
        if event.patch:
            parts = sections(event.patch)
            if all(p.start is not None for p in parts):
                changesets.append([op for p in parts for op in p.changes(p.start)])
                continue
        if event.replacement and candidate is None:
            image = before_later_changes(state, event, following, disk)
            if image is not None:
                return witness(state, r, hits([(0, image)], r.new, r.old), changesets)
        return candidate
    if candidate:
        return candidate
    if disk is not None:
        return witness(state, r, hits([(0, disk)], r.new, r.old), changesets)
    return None


def witness(state, r, found, changesets):
    if len(found) != 1:
        return None
    first, span, at = found[0]
    text = "".join(span)
    before = lines(text[:at] + r.old + text[at + len(r.new) :])
    first = translate_back(first, len(span), changesets)
    if first is None:
        return None
    # The reconstructed old text must agree with every known line it overlaps.
    for i, row in enumerate(before, first):
        if i < len(state.rows) and state.rows[i] is not None and state.rows[i] != row:
            return None
    return first, before, at


def apply_replacement(state, edit, following, disk):
    ts = state.snapshot_time(edit.ts)
    r = edit.replacement
    if not r:
        raise ValueError("missing_change")
    if not r.old:
        if not state.open_tail and not state.rows:
            state.splice(0, 0, lines(r.new), "agent", edit.ts, edit.id)
            return
        raise ValueError("empty_search_unknown_preimage")
    found = hits(runs(state.rows), r.old, r.new)
    usable = []
    for first, span, at in found:
        body = "".join(span)
        result = body[:at] + r.new + body[at + len(r.old) :]
        end = first + len(span)
        needs_neighbor = (end < len(state.rows) and state.rows[end] is None) or (
            end == len(state.rows) and state.open_tail
        )
        if result and not result.endswith("\n") and body.endswith("\n") and needs_neighbor:
            continue
        usable.append((first, span, at))
    if r.all and usable:
        # Python/Claude replace-all takes nonoverlapping occurrences.
        accepted = []
        last = (-1, -1)
        for item in usable:
            first, span, at = item
            if (first, at) >= last:
                accepted.append(item)
                consumed = ("".join(span)[:at] + r.old).split("\n")
                last = (first + len(consumed) - 1, len(consumed[-1]))
        substitute(state, edit, accepted)
        if state.open_tail or None in state.rows:
            state.file.reasons["replace_all_unknown_region"] += 1
            state.file.unapplied += 1
        return
    if len(found) == len(usable) == 1:
        substitute(state, edit, usable)
        return
    anchor = locate_later(state, edit, following, disk)
    if anchor:
        first, span, at = anchor
        state.reveal(first, span, ts)
        substitute(state, edit, [anchor])
        return
    if found:
        # When a replacement changes line count, even the rows between two
        # possible sites have uncertain addresses. The suffix moves by one
        # known delta when every candidate yields the same number of lines.
        deltas = set()
        for first, span, at in found:
            body = "".join(span)
            changed = body[:at] + r.new + body[at + len(r.old) :]
            deltas.add(len(lines(changed)) - len(span))
        if deltas == {0}:
            for first, span, _ in found:
                state.forget(first, first + len(span), len(span))
        else:
            first = found[0][0]
            end = max(i + len(span) for i, span, _ in found)
            if len(deltas) == 1:
                state.forget(first, end, max(0, end - first + deltas.pop()))
            else:
                state.forget(first, len(state.rows), len(state.rows) - first)
                state.open_tail = True
    raise ValueError("ambiguous_replacement" if found else "unlocated_replacement")
