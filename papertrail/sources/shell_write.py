"""Recognize literal, top-level shell writes without interpreting shell programs."""

import re
import shlex
from pathlib import Path

from ..model import Edit
from .paths import resolved, special_file

WORD = r"""(?:'[^'\n]*'|"[^"\n]*"|[^\s<>&;|'"()]+)"""
DOC = re.compile(r"""<<(?P<tabs>-?)[ \t]*(?P<quote>['"]?)(?P<tag>[\w.-]+)(?P=quote)(?=\s|[;>]|$)""")
DEST = rf"(?P<op>>>?)[ \t]*(?P<path>{WORD})"
HERE = DOC.pattern
HEADERS = [
    re.compile(rf"cat\s+{DEST}\s*{HERE}"),
    re.compile(rf"cat\s+{HERE}\s*{DEST}"),
    re.compile(rf"tee\s+(?P<append>-a\s+)?(?P<path>{WORD})\s*{HERE}"),
]
# A standalone echo with one literal argument and no options writes that
# argument followed by a newline. Backslashes are shell-dependent.
ECHO_WRITE = re.compile(
    rf"""[ \t\n]*echo[ \t]+(?P<value>'[^'\\\n]*'|"[^"$`!\\\n]*"|[A-Za-z0-9_./]+)"""
    rf"[ \t]+{DEST}[ \t\n]*"
)
MKDIR_PREFIX = re.compile(rf"[ \t\n]*mkdir[ \t]+-p[ \t]+({WORD})[ \t]*&&[ \t]*")
CD = re.compile(rf"cd\s+(?:--\s+)?({WORD})")
CONTROL = re.compile(
    r"(?:^|[;&|]\s*)(?:if|for|while|until|case|function|alias|eval|source|exec|exit|return|read|trap|set|pushd|popd|setopt|unsetopt|\.)(?:\s|$)|^\s*[({]|(?<!\w)\w+\s*\(\s*\)"
)
SCRIPT_METHOD = re.compile(r"\b(?:write_text|write_bytes|writeFileSync|writeFile|writelines)\s*\(")
SCRIPT_OPEN = re.compile(r"\bopen\(")
OPEN_MODE_OR_END = re.compile(r"""(?P<mode>,\s*['"][wax])|[)\n]""")
HEADER_SEPARATORS = re.compile(r";|&&")
QUOTES = re.compile("""['"]""")
DOUBLE_QUOTE_END = re.compile(r'(?<!\\)(?:\\\\)*(?:"|\\(?=\n))')


def script_write(text):
    if SCRIPT_METHOD.search(text):
        return True
    pos = 0
    while opening := SCRIPT_OPEN.search(text, pos):
        # A later open() within the same run cannot reach a mode the first
        # opening could not already match. A comma's whitespace may cross lines.
        mode_or_end = OPEN_MODE_OR_END.search(text, opening.end())
        if mode_or_end is None:
            break
        if mode_or_end.lastgroup == "mode":
            return True
        pos = mode_or_end.end()
    return False


def mask_quoted(text):
    """Replace complete quoted arguments, including double-quoted escapes."""
    pieces = []
    pos = copied = failed_until = 0
    while quote := QUOTES.search(text, pos):
        pos = quote.end()
        if quote[0] == "'":
            end = text.find("'", pos) + 1
        else:
            if pos <= failed_until:
                continue
            closing = DOUBLE_QUOTE_END.search(text, pos)
            if closing is None or text[closing.end() - 1] != '"':
                # Starting at an escaped quote before the same invalid newline
                # or EOF cannot make the double-quoted match succeed.
                failed_until = closing.end() if closing else len(text)
                continue
            end = closing.end()
        if end:
            pieces.extend((text[copied:quote.start()], "ARG"))
            pos = copied = end
    return "".join((*pieces, text[copied:])) if pieces else text


def split_headers(header):
    return [part.strip() for part in HEADER_SEPARATORS.split(header)]


def literal_path(token, cwd):
    if not token or token.startswith("#") or re.search(r"[$`*?\[\]{}\\]", token):
        return None
    try:
        (name,) = shlex.split(token)
    except ValueError:
        return None
    if not name or name.startswith(("~", "-", "=")) or "\0" in name:
        return None
    path = Path(name)
    if not path.is_absolute() and not cwd:
        return None
    try:
        return resolved(Path(cwd or "/") / path)
    except (OSError, RuntimeError):
        return None


def shell_status(status, diagnostic):
    # In a command list a later success can mask a redirection or cd failure.
    if (
        status == "success"
        and isinstance(diagnostic, str)
        and re.search(
            r"(?im)^(?:[^\n]*?: )?(?:no such file or directory|permission denied|read-only file system|"
            r"cannot create|is a directory|no space left on device|file exists|command not found)\b|"
            r"^(?:cat|tee|cd|bash|zsh|sh):[^\n]*(?:denied|failed|not found|no such file)",
            diagnostic,
        )
    ):
        return "write_diagnostic"
    return status


def shell_commands(command):
    rows = iter(command.split("\n"))
    for raw in rows:
        header = raw.strip()
        if not header or header.startswith("#"):
            continue
        docs = list(DOC.finditer(header)) if "<<" in header else []
        if not docs:
            yield header, "", True, False
            continue
        # Shells read multiple heredocs on a command line from left to right.
        parts = split_headers(header)
        split = len(parts) == len(docs) and all(
            any(p.fullmatch(part) for p in HEADERS) for part in parts
        )
        body, closed = "", True
        for index, doc in enumerate(docs):
            contents = []
            for line in rows:
                value = line.lstrip("\t") if doc["tabs"] else line
                if value == doc["tag"]:
                    break
                contents.append(value + "\n")
            else:
                closed = False
            body = "".join(contents)
            if split:
                yield parts[index], body, closed, True
        if not split:
            yield header, body, closed, bool(docs)


def balanced(header):
    try:
        shlex.split(header, comments=True)
    except ValueError:
        return False
    return True


def shell_edits(
    command, cwd, status, counts, *, session, key, seq, ts, prompt, model, effort, tool,
    standalone=False,
):
    if not isinstance(command, str):
        return []
    edits = []
    safe = True
    # Quote balance only matters once a later line is a write, so it is checked
    # then. shlex reads a character at a time, and agents run very long commands.
    unchecked = []
    unknown_directory = False
    echo = ECHO_WRITE.fullmatch(command)
    commands = [(command.strip(), "", True, False)] if echo else shell_commands(command)
    setup = MKDIR_PREFIX.match(command)
    if setup and literal_path(setup[1], cwd):
        # Success after && confirms the write when it is the last command.
        following = list(shell_commands(command[setup.end() :]))
        if len(following) == 1 and any(p.fullmatch(following[0][0]) for p in HEADERS):
            commands = following
    if standalone:
        commands = list(commands)
        if len(commands) != 1:
            return []
    for i, (header, body, closed, has_doc) in enumerate(commands):
        # Handle a literal cd on its own or guarding the next command with &&.
        if header == "cd":
            cwd = None
            unknown_directory = True
            continue
        prefix, guard, rest = header.partition("&&")
        if not guard and ";" in prefix:
            prefix, guard, rest = header.partition(";")
        if re.match(r"^cd\s", prefix):
            cd = CD.fullmatch(prefix.strip())
            cwd = literal_path(cd[1], cwd) if cd else None
            unknown_directory = not cwd
            if not guard:
                continue
            if standalone and guard != "&&":
                return []
            header = rest.strip()
        if header in ("set -e", "set -eu", "set -euo pipefail"):
            continue
        if CONTROL.search(header) or re.search(r"\b(?:alias|function)\s+(?:cat|tee)\b", header):
            safe = False
        match = echo or next((m for pattern in HEADERS if (m := pattern.fullmatch(header))), None)
        candidate = bool(re.search(r"(?:^|[;&|]\s*)(?:cat|tee)\s", header) and "<<" in header)
        if match:
            if safe and unchecked:
                safe = all(map(balanced, unchecked))
                unchecked.clear()
            path = literal_path(match["path"], cwd)
            reason = None
            if not safe:
                reason = "shell_context"
            elif unknown_directory:
                reason = "unknown_cwd"
            elif not closed:
                reason = "unclosed_heredoc"
            elif not path:
                reason = (
                    "unknown_cwd"
                    if not cwd and not Path(match["path"]).is_absolute()
                    else "dynamic_path"
                )
            elif special_file(path):
                reason = "special_file"
            elif not echo and not match["quote"] and re.search(r"[$`]|\\[\\$`\n]", body):
                reason = "body_expansion"
            elif status != "success":
                reason = status
            elif echo:
                (value,) = shlex.split(echo["value"])
                if value.startswith("-"):
                    # Even a quoted "-n", "-e", or "-E" is an echo option.
                    reason = "echo_option"
                else:
                    body = value + "\n"
            if reason:
                counts["shell_rejected_" + reason] += 1
                continue
            append = match.groupdict().get("op") == ">>" or match.groupdict().get("append")
            op = "append" if append else "overwrite"
            counts["shell_" + op] += 1
            edits.append(
                Edit(
                    f"{session}:sh:{key}:{i:08}",
                    seq,
                    ts,
                    tool,
                    path,
                    prompt,
                    model,
                    after=body if not append else None,
                    effort=effort,
                    operation=op,
                    call_id=key,
                    appended=body if append else None,
                )
            )
        elif candidate:
            counts["shell_rejected_unsupported_header"] += 1
        else:
            # A search pattern or code string can itself contain ">" or "sed -i".
            words = mask_quoted(header)
            if re.search(r"(?:^|[;&|]\s*)(?:cp|mv)\s", words):
                counts["shell_opaque_copy_move"] += 1
            if re.search(r"(?:^|[;&|]\s*)sed\s+(?:-\w+\s+)*-[A-Za-z]*i(?:\s|$|[.'])", words):
                counts["shell_opaque_sed"] += 1
            if re.search(r"(?:^|[;&|]\s*)(?:python[.\d]*|node|ruby)\s", header) and script_write(
                header + "\n" + body
            ):
                counts["shell_opaque_script"] += 1
            if re.search(r"(?<![<>=])>{1,2}(?![>&=])", words):
                counts["shell_opaque_redirect"] += 1
        # Unparsed multiline syntax, subshells, skipped branches and directory
        # changes can change the meaning of later physical lines.
        if header.count("`") % 2 or "$(" in header:
            safe = False
        if re.search(r"(?:\\|&&|\|\||\||\()$", header) or (not has_doc and "<<" in header):
            safe = False
        if safe:
            unchecked.append(header)
        if re.search(r"(?:[;&|]\s*|^\s*)(?:cd|pushd|popd)\b", header):
            cwd = None
            unknown_directory = True
    return edits
