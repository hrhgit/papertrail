"""Decode literal patch inputs and committed file-change receipts, without executing code."""

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from ..patches import sections


@dataclass(frozen=True)
class Change:
    path: str
    operation: str
    patch: str | None = None
    after: str | None = None
    before: str | None = None
    moved_from: str | None = None

    def identity(self):
        # Native numbered diffs and literal patches can have different amounts
        # of surrounding context. Compare the actual removed and added lines.
        edits = (
            tuple(op for part in sections(self.patch) for op in part.ops if op[0] != " ")
            if self.patch
            else ()
        )
        return self.operation, self.path, self.moved_from, self.after, edits


def absolute(raw, cwd):
    if not isinstance(raw, str) or not raw or "\0" in raw:
        raise ValueError("invalid_path")
    path = Path(raw)
    if not path.is_absolute() and not cwd:
        raise ValueError("missing_workdir")
    return str((Path(cwd) / path).resolve())


def parse_patch(text, cwd):
    if not isinstance(text, str) or not text:
        raise ValueError("invalid_patch_text")
    rows = text.replace("\r\n", "\n").rstrip("\n").split("\n")
    if rows[0] != "*** Begin Patch" or rows[-1] != "*** End Patch":
        raise ValueError("incomplete_patch")
    operations = []
    i = 1
    while i < len(rows) - 1:
        match = re.fullmatch(r"\*\*\* (Add|Update|Delete) File: (.+)", rows[i])
        if not match:
            raise ValueError("invalid_file_header")
        op, filename = match.groups()
        path = absolute(filename, cwd)
        i += 1
        source = None
        if op == "Update" and rows[i].startswith("*** Move to: "):
            source, path = path, absolute(rows[i][13:], cwd)
            i += 1
        start = i
        while i < len(rows) - 1 and not re.match(r"^\*\*\* (?:Add|Update|Delete) File: ", rows[i]):
            i += 1
        body = rows[start:i]
        if op == "Add" and all(line.startswith("+") for line in body):
            operations.append(
                Change(path, "add", after="".join(s[1:] + "\n" for s in body), before="")
            )
        elif op == "Delete" and not body:
            operations.append(Change(path, "delete"))
        elif op == "Update" and (body or source):
            if body and not body[0].startswith("@@"):
                body.insert(0, "@@")
            patch = "\n".join(line or " " for line in body) or None
            if patch:
                sections(patch)
            operations.append(
                Change(path, "move" if source else "update", patch, moved_from=source)
            )
        else:
            raise ValueError("invalid_file_body")
    if not operations:
        raise ValueError("empty_patch")
    return operations


def event_change(filename, record, cwd):
    path = absolute(filename, cwd)
    kind = record.get("type")
    if kind == "add" and isinstance(record.get("content"), str):
        return Change(path, kind, after=record["content"], before="")
    if kind == "delete" and isinstance(record.get("content", ""), str):
        return Change(path, kind, before=record.get("content"))
    if kind == "update" and isinstance(record.get("unified_diff"), str):
        patch = record["unified_diff"] or None
        if patch:
            sections(patch)
        move = record.get("move_path")
        if move:
            return Change(absolute(move, cwd), "move", patch, moved_from=path)
        return Change(path, kind, patch)
    raise ValueError("unsupported_event_change")


# Only an entire shell command qualifies. Redirecting a patch-shaped string to
# another program or masking the patch's exit code supplies no success receipt.
HEREDOC = re.compile(
    r"\s*(?:command\s+)?apply_patch\s+<<\s*(?P<q>['\"]?)(?P<tag>\w+)(?P=q)[ \t]*\n"
    r"(?P<body>\*\*\* Begin Patch\n[\s\S]*?\n\*\*\* End Patch)\n(?P=tag)\s*\Z"
)


def shell_patch(cmd):
    m = HEREDOC.fullmatch(cmd)
    if m and (m["q"] or not re.search(r"[\\$`]", m["body"])):
        return m["body"]
    return None


def shell_invocation(cmd, cwd):
    """Recognize an optional literal cd guarded by && before the patch command."""
    cd = re.match(r"""\s*cd\s+(?:--\s+)?('[^']+'|"[^"\\$`]+"|[\w./-]+)\s*&&\s*""", cmd)
    if cd and (patch := shell_patch(cmd[cd.end() :])):
        target = shlex.split(cd[1])[0]
        return (patch, absolute(target, cwd)) if target != "-" else (None, cwd)
    return shell_patch(cmd), cwd


def opaque_categories(cmd):
    # Count command signatures, not files. Quoted search patterns and code in
    # a heredoc must not turn a read-only shell command into a redirect/sed.
    header = re.split(r"<<-?\s*['\"]?\w+['\"]?[^\n]*\n", cmd, maxsplit=1)[0]
    unquoted = re.sub(r"""'(?:[^']*)'|"(?:\\.|[^"\\])*" """.strip(), "ARG", header)
    if re.search(r"(?<![<>=])>{1,2}(?![>&=])\s*[^&\s]", unquoted):
        yield "redirect"
    if re.search(r"(?:^|[\s;&|/])sed\s+(?:-[^\s]+\s+)*?-[A-Za-z]*i(?:\s|$|[.'])", unquoted):
        yield "sed_inplace"
    interpreter = re.search(r"(?:^|[\s;&|/])(?:python[.\d]*|node|ruby)\s", unquoted)
    if interpreter and re.search(
        r"\b(?:write_text|write_bytes|writeFileSync|writeFile|writelines)\s*\(|\bopen\([^\n)]*,\s*['\"][wax]",
        cmd,
    ):
        yield "script_write"


def js_literal(raw):
    """A deliberately small grammar: no evaluation, substitutions or guessed escapes."""
    raw = raw.strip()
    if raw.startswith("String.raw`") and raw.endswith("`"):
        value = raw[11:-1]
        return value if "${" not in value and "`" not in value else None
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
            return value if isinstance(value, str) else None
        except ValueError:
            return None
    if len(raw) >= 2 and raw[0] in "'`" and raw[-1] == raw[0]:
        body = raw[1:-1]
        out = []
        i = 0
        while i < len(body):
            if raw[0] == "`" and body.startswith("${", i):
                return None
            if body[i] != "\\":
                out.append(body[i])
                i += 1
                continue
            i += 1
            if i >= len(body):
                return None
            ch = body[i]
            if ch in "xu":
                n = 2 if ch == "x" else 4
                digits = body[i + 1 : i + 1 + n]
                if len(digits) != n or not re.fullmatch("[0-9a-fA-F]+", digits):
                    return None
                value = int(digits, 16)
                if 0xD800 <= value <= 0xDFFF:
                    return None
                out.append(chr(value))
                i += n
            elif ch != "\n":
                escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v"}
                if ch.isdigit():
                    return None
                out.append(escapes.get(ch, ch))
            i += 1
        return "".join(out)
    return None


JS_STRING = r"""(?:String\.raw)?`(?:\\.|[^`\\])*`|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'"""


def wrapper_command(code):
    """Decode one literal tools.exec_command object. Require an inner receipt."""
    match = re.fullmatch(
        r"\s*(?:text\(\s*)?await\s+tools\.exec_command\(\s*\{(.*)\}\s*\)\s*\)?\s*;?\s*",
        code,
        re.DOTALL,
    )
    if not match:
        return None
    body, args = match[1], {}
    field = re.compile(
        r"""\s*["']?(\w+)["']?\s*:\s*(""" + JS_STRING + r"""|\d+|true|false)\s*(?:,|$)""",
        re.DOTALL,
    )
    pos = 0
    while pos < len(body.rstrip()):
        token = field.match(body, pos)
        if not token or token[1] in args:
            return None
        args[token[1]] = js_literal(token[2])
        pos = token.end()
    if not isinstance(args.get("cmd"), str) or ("workdir" in args and not args["workdir"]):
        return None
    return args


def wrapper_patch(code):
    # A FileChange receipt also covers arbitrary JS, including branches and loops.
    literal = JS_STRING
    code = code.strip()
    arg = r"(?P<arg>" + literal + r"|[\w$]+)"
    m = re.fullmatch(
        r"(?:(?:const|let)\s+(?P<name>[\w$]+)\s*=\s*(?P<init>" + literal + r")\s*;\s*)?"
        r"(?:text\(\s*)?await\s+tools\.apply_patch\(\s*" + arg + r"\s*\)\s*\)?\s*;?",
        code,
        re.DOTALL,
    )
    if not m:
        return None
    return js_literal(m["init"] if m["name"] and m["arg"] == m["name"] else m["arg"])


def command_string(command):
    if isinstance(command, str):
        return command
    if isinstance(command, list) and all(isinstance(x, str) for x in command):
        if (
            len(command) == 3
            and Path(command[0]).name in ("bash", "sh", "zsh")
            and command[1] in ("-lc", "-c")
        ):
            return command[2]
        return shlex.join(command)
    return ""


def shell_calls(code):
    """Decode one direct call or an ordered Promise array printed as a whole."""
    code = code.strip().removesuffix(";").strip()
    assignment = re.fullmatch(
        r"const\s+(\w+)\s*=\s*(await\s+.*?)\s*;\s*text\(\1\)", code, re.DOTALL
    )
    if assignment:
        code = assignment[2]
    elif code.startswith("text(") and code.endswith(")"):
        code = code[5:-1].strip()
    code = code.removeprefix("await ").strip()
    parallel = re.fullmatch(r"Promise\.all(?:Settled)?\(\s*\[(.*)\]\s*\)", code, re.DOTALL)
    raw = parallel[1] if parallel else code
    field = rf"""["']?\w+["']?\s*:\s*(?:{JS_STRING}|\d+|true|false)"""
    obj = rf"\{{\s*(?:{field}\s*,\s*)*(?:{field})?\s*\}}"
    expr = re.compile(r"\s*tools\.(exec_command|write_stdin)\(\s*(" + obj + r")\s*\)\s*", re.DOTALL)
    result = []
    while raw:
        m = expr.match(raw)
        if not m:
            return []
        name, args = m.groups()
        values = {}
        for key, value in re.findall(
            r"""["']?(\w+)["']?\s*:\s*(""" + JS_STRING + r"|\d+|true|false)", args, re.DOTALL
        ):
            if key in values:
                return []
            values[key] = int(value) if value.isdecimal() else js_literal(value)
        result.append((name, values))
        raw = raw[m.end() :].strip()
        if raw:
            if not parallel or not raw.startswith(","):
                return []
            raw = raw[1:].strip()
    return result
