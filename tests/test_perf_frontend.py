"""Check the reader's layout and incremental parsing in Node."""

from test_web_frontend import run_page


def test_track_lookup_is_inclusive_and_handles_unowned_lines():
    assert run_page("""
const first = { start: 2, end: 4 };
const second = { start: 6, end: 100000 };
const tracks = [first, second];
const lines = [0, 1, 2, 4, 5, 6, 100000, 100001];
console.log(JSON.stringify(lines.map((line) => tracks.indexOf(trackAt(tracks, line)))));
""") == [-1, -1, 0, 0, -1, 1, 1, -1]


def test_virtual_rows_include_gaps_and_bound_the_mounted_window():
    assert run_page("""
const layout = fileLayout(100000, 20, [0, 500, 100000]);
layout.setHeight(0, 44);
layout.setHeight(500, 84);
layout.setHeight(100000, 64);
const results = [
  layout.offset(0), layout.offset(1), layout.offset(500), layout.offset(501),
  layout.offset(100000), layout.offset(100001),
  visibleRows(layout, 0, 400, 320),
  visibleRows(layout, layout.offset(500) + 30, 0, 0),
  visibleRows(layout, layout.offset(100000) + 20, 400, 320),
];
layout.setHeight(500, 0);
results.push(layout.offset(501), layout.at(layout.offset(501)));
console.log(JSON.stringify(results));
""") == [0, 64, 10044, 10148, 2000128, 2000192,
         [0, 34], [500, 501], [99985, 100001], 10064, 501]


def test_markdown_outline_ranges_and_fences():
    assert run_page("""
const lines = [
  "# First", "", "### Nested", "detail", "", "```md", "# Ignore", "```",
  "", "## Sibling", "", "# Last", "", "  ",
];
const tracks = [{start: 1, end: 10, kind: "agent", lanes: [{session: "writer"}]}];
const items = outlineOf({path: "notes.md", lines, tracks});
console.log(JSON.stringify(items.map(({name, line, end, depth, owners}) => [name, line, end, depth, owners])));
""") == [
        ["First", 1, 10, 0, [["writer", 10]]],
        ["Nested", 3, 8, 1, [["writer", 6]]],
        ["Sibling", 10, 10, 1, [["writer", 1]]],
        ["Last", 12, 12, 0, []],
    ]


def test_incremental_syntax_retains_multiline_state():
    assert run_page("""
const text = ["const a = `hello", "world", "`;", "/* note", "still here */", "return a;"];
const scan = SYNTAX.scanner("javascript");
const first = text.slice(0, 2).map(scan);
const rest = text.slice(2).map(scan);
const actual = first.concat(rest);
console.log(JSON.stringify([
  JSON.stringify(actual) === JSON.stringify(SYNTAX.tokens(text, "code.js")),
  actual[1], actual[4], actual[5][0],
]));
""") == [True, [[0, 5, "string"]], [[0, 13, "comment"]], [0, 6, "keyword"]]


def test_outline_cache_requires_same_source_and_ownership():
    assert run_page("""
const path = "notes.md";
const lines = ["# Heading", "body"];
const tracks = [{start: 1, end: 2, kind: "agent", lanes: [{session: "first"}]}];
const items = outlineOf({path, lines, tracks});
outlines.set(path, {lines, tracks, items});
console.log(JSON.stringify([
  cachedOutline({path, lines: [...lines], tracks: [{...tracks[0]}]}) === items,
  cachedOutline({path, lines: ["# Changed", "body"], tracks}),
  cachedOutline({path, lines, tracks: [{...tracks[0], lanes: [{session: "second"}]}]}),
]));
""") == [True, None, None]
