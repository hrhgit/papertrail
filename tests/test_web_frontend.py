"""Run the reader's browser script in Node without starting the page."""

import json
import shutil
import subprocess

import pytest

from papertrail import web


def run_page(js, data=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("needs node to run the page script")
    script = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const dir = process.argv[1];
const page = fs.readFileSync(dir + "/app.js", "utf8");
const ctx = vm.createContext({
  console, navigator: {}, document: { querySelector: () => null },
  history: {}, sessionStorage: { getItem: () => null },
  URLSearchParams, input: JSON.parse(process.argv[2]),
});
vm.runInContext(fs.readFileSync(dir + "/syntax.js", "utf8"), ctx);
vm.runInContext(page.replace(/\nstart\(\);\s*$/, ""), ctx);
vm.runInContext(process.argv[3], ctx);
"""
    result = subprocess.run(
        [node, "-e", script, str(web.STATIC), json.dumps(data), js],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_inferred_line_label_and_basis():
    rows = [
        {"kind": "inferred", "agent": "codex", "basis": "command"},
        {"kind": "inferred", "agent": "claude-code", "basis": "nearby"},
    ]
    assert run_page("console.log(JSON.stringify(input.map(r => [kindLabel(r), kindTip(r)])))", rows) == [
        ["Likely Codex", "A command in this session named the file"],
        ["Likely Claude Code", "Nearest edit to this file by this session"],
    ]


@pytest.mark.parametrize("path,source,expected", [
    ("example.js", """\
function f() {}
async function waitForIt() {
if (ready) {
  foo(bar) {
  }
}
for (;;) {}
while (ready) {}
switch (value) {}
try {} catch (error) {}
// } ignored

return "}";
}
const arrow = () => {}
const asyncArrow = async (a) => {}
const expression = function () {}
const named = function inner() {}
export function namedExport() {}
export default function primary() {}
export const exported = () => {}
class Box {
constructor() {}
static make() {}
async fill() {}
get value() {}
set value(v) {}
work() {
  foo(bar) {
  }
  function nested() {}
}
}
const obj = {
  shorthand() {},
  async fetch() {},
  get value() {},
};
  foo(bar) {
  }
""", [
        ["f", "function", 1, 1, 0], ["waitForIt", "function", 2, 14, 0],
        ["arrow", "function", 15, 15, 0], ["asyncArrow", "function", 16, 16, 0],
        ["expression", "function", 17, 17, 0], ["named", "function", 18, 18, 0],
        ["namedExport", "function", 19, 19, 0], ["primary", "function", 20, 20, 0],
        ["exported", "function", 21, 21, 0], ["Box", "class", 22, 33, 0],
        ["constructor", "constructor", 23, 23, 1], ["make", "method", 24, 24, 1],
        ["fill", "method", 25, 25, 1], ["value", "method", 26, 26, 1],
        ["value", "method", 27, 27, 1], ["work", "method", 28, 32, 1],
        ["nested", "function", 31, 31, 2], ["shorthand", "method", 35, 35, 0],
        ["fetch", "method", 36, 36, 0], ["value", "method", 37, 37, 0],
    ]),
    ("example.ts", """\
export default function () {}
export class Box<T> {
  public static async read(
    key: string = makeKey(")"),
  ): Promise<string> {
    return key;
  }
}
export const from = async (v: string): Promise<string> => {
  return v;
}
""", [
        ["default", "function", 1, 1, 0], ["Box", "class", 2, 8, 0],
        ["read", "method", 3, 7, 1], ["from", "function", 9, 11, 0],
    ]),
    ("example.go", """\
func F() {}
func (r *T) M(
    value int,
    more func() int,
) (int, error) {
// } ignored

return value + more(), nil
}
type T struct {}
type I interface {
    Read() int
}
func Next() {}
""", [
        ["F", "func", 1, 1, 0], ["M", "func", 2, 9, 0],
        ["T", "type", 10, 10, 0], ["I", "type", 11, 13, 0],
        ["Next", "func", 14, 14, 0],
    ]),
    ("Example.java", """\
public class Example {
    public Example() {}
    @Override
    public static <T> List<T> f(
        T value
    ) throws IOException {
// } ignored

    if (ready) {}
    for (;;) {}
    while (ready) {}
    switch (value) {}
    try {} catch (Exception error) {}
    synchronized (lock) {}
    new Foo(value) {
    };
    return foo(value);
    }
    void next() {}
}
interface Contract {
    List<String> read();
}
""", [
        ["Example", "class", 1, 20, 0], ["Example", "constructor", 2, 2, 1],
        ["f", "method", 4, 18, 1], ["next", "method", 19, 19, 1],
        ["Contract", "interface", 21, 23, 0], ["read", "method", 22, 22, 1],
    ]),
    ("example.py", """\
class Box:
    def method(self):
        def nested():
            pass

        return nested()
def next():
    pass
""", [
        ["Box", "class", 1, 6, 0], ["method", "def", 2, 6, 1],
        ["nested", "def", 3, 4, 2], ["next", "def", 7, 8, 0],
    ]),
])
def test_outline_names_and_ranges(path, source, expected):
    actual = run_page("""
const { path, lines } = input;
console.log(JSON.stringify(outlineOf({ path, lines }).map(
  ({ name, kind, line, end, depth }) => [name, kind, line, end, depth],
)));
""", {"path": path, "lines": source.splitlines()})
    assert actual == expected, actual


def test_browser_history_restores_panels_and_edit_targets():
    assert run_page("""
const assert = (condition, message) => { if (!condition) throw Error(message); };
(async () => {
  const entries = [{ data: { i: 0 }, hash: "" }];
  let at = 0, pushes = 0;
  globalThis.location = { hash: "" };
  Object.defineProperty(history, "state", { get: () => entries[at].data });
  history.replaceState = (data, _, hash = location.hash) => {
    location.hash = hash;
    entries[at] = { data: JSON.parse(JSON.stringify(data)), hash };
  };
  history.pushState = (data, _, hash) => {
    entries.splice(++at);
    pushes++;
    history.replaceState(data, "", hash);
  };
  const travel = async (offset) => {
    at += offset;
    location.hash = entries[at].hash;
    await restoreHash();
    await Promise.resolve();
  };

  const elements = new Map();
  document.getElementById = (id) => {
    if (!elements.has(id)) elements.set(id, {
      querySelector: () => null, replaceChildren() {}, setAttribute() {}, scrollIntoView() {},
    });
    return elements.get(id);
  };
  document.querySelectorAll = () => [];
  globalThis.CSS = { escape: (value) => value };
  h = () => ({ replaceChildren() {} });
  icon = () => null;
  dots = () => null;
  renderFile = renderTabs = renderFiles = renderCrossings = renderSessions = markSelected = record = () => {};
  setPanel = redrawGap = () => {};

  const stops = [
    { kind: "agent", session: "first", edit: "e1", chain: ["h1"] },
    { kind: "agent", session: "second", edit: "e2", chain: ["h2"] },
  ];
  const file = (path) => ({
    path, lines: Array(50).fill("code"), tracks: [],
    gaps: [{ after: 4, kind: "agent", session: "first", edit: "cut", removed: 1,
      text: "gone", wrote: [{ kind: "agent", session: "second", edit: "wrote", lines: 1 }] }],
  });
  let lastChat = null;
  showConversation = async (_, id, target) => { lastChat = { id, target }; };
  let lateTrace;
  api = async (route, args) => {
    if (route === "file") return file(args.path);
    if (route === "trace" && args.line === 3) return new Promise((resolve) => { lateTrace = resolve; });
    return { rows: stops, ...args };
  };
  state.workspace = { project: "/demo" };
  state.sessions = new Map([["first", {}], ["second", {}]]);

  await openFile("/demo/a.js", 2);
  await openSession("second", { edit: "target", prompt: "2025-01-02" });
  assert(pushes === 0 && history.state.panel.id === "second", "same-position chat must replace");
  selectLine(40);
  await travel(-1);
  assert(state.line === 2 && state.focus === "second", "line and focus restored");
  assert(lastChat.id === "second" && lastChat.target.edit === "target" &&
    lastChat.target.prompt === "2025-01-02" && state.backTo.line === 2, "chat target restored");
  await travel(1);
  assert(state.line === 40 && state.panel.view === "trace", "forward restored line trace");

  selectLine(40, { span: { line: 40, end: 44, name: "f", kind: "function" } });
  await Promise.resolve();
  showStop(1);
  await openFile("/demo/b.js", 1);
  await travel(-1);
  assert(state.file.path === "/demo/a.js" && state.trace.span.end === 44, "span restored across files");
  assert(lastChat.id === "second" && lastChat.target.edit === "e2", "chosen span edit restored");
  await travel(1);
  assert(state.file.path === "/demo/b.js" && state.panel.view === "trace", "forward across files");

  selectGap(4, { open: true });
  let pickWriter;
  h = (tag, attrs) => {
    if (tag === "button" && attrs?.class === "writer") pickWriter = attrs.onclick;
    return { replaceChildren() {} };
  };
  renderTraceView();
  pickWriter();
  await openFile("/demo/c.js", null, { keepPanel: true });
  await openFile("/demo/d.js", 20);
  await travel(-1);
  assert(state.file.path === "/demo/c.js" && state.line == null, "location restored apart from panel");
  assert(state.trace.gap === 4 && lastChat.id === "second" && lastChat.target.edit === "wrote",
    "deletion's writer and original edit restored");

  history.replaceState({ i: at }, "", "#file=/demo/c.js&line=5");
  await restoreHash();
  await Promise.resolve();
  assert(state.line === 5 && state.panel.view === "trace" && lastChat.id === "first",
    "old entry falls back to the line trace");
  selectLine(3);
  await openSession("second");
  lateTrace({ rows: stops });
  await Promise.resolve();
  await Promise.resolve();
  assert(state.panel.view === "conversation" && !state.trace && lastChat.id === "second",
    "late trace does not replace a newer conversation at the same line");
  assert(pushes === 4 && entries.length === 5, "restores and panel changes do not push entries");
  console.log(JSON.stringify(true));
})();
""")
