"use strict";

// A small tokenizer for coloring code, with no dependencies. It reads one line at
// a time and carries only what can span lines: an open block comment or string.
// Tokens are [start, end, type] within the line. It aims to be right for ordinary
// code and quietly plain where a language gets unusual.
//
// Types: comment, string, keyword, const, number, def (a name being defined),
// call (a name being called), prop (a key or attribute), tag, meta.

const SYNTAX = (() => {
  const words = (s) => new Set(s.split(" "));
  const C_STRINGS = [{ q: '"' }, { q: "'", char: true }];
  const SLASHES = { line: ["//"], block: [["/*", "*/"]] };

  const LANGS = {
    python: {
      line: ["#"],
      strings: [{ q: '"""', multi: true }, { q: "'''", multi: true }, { q: '"' }, { q: "'" }],
      prefix: /^[rbfuRBFU]{1,2}(?=["'])/,
      keywords: words("and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield"),
      consts: words("True False None self cls"),
      defs: words("def class"),
      decorator: true,
    },
    javascript: {
      ...SLASHES,
      strings: [{ q: '"' }, { q: "'" }, { q: "`", multi: true }],
      regex: true,
      keywords: words("async await break case catch class const continue debugger default delete do else enum export extends finally for function if implements import in instanceof interface let new return static super switch throw try typeof var void while with yield"),
      consts: words("true false null undefined NaN Infinity this"),
      defs: words("function class interface enum"),
    },
    json: { strings: [{ q: '"' }], consts: words("true false null"), keywords: new Set(), keys: true },
    css: {
      block: [["/*", "*/"]],
      strings: [{ q: '"' }, { q: "'" }],
      word: /^-{0,2}[A-Za-z_][\w-]*/,
      keywords: new Set(),
      consts: words("important inherit initial unset none auto"),
      at: true,
      hex: true,
      props: true,
    },
    shell: {
      line: ["#"],
      strings: [{ q: '"' }, { q: "'", raw: true }],
      keywords: words("if then else elif fi for while until do done case esac in function select return exit export local readonly set unset shift source break continue"),
      consts: words("true false"),
      defs: words("function"),
    },
    yaml: {
      line: ["#"],
      strings: [{ q: '"' }, { q: "'", raw: true }],
      keywords: new Set(),
      consts: words("true false null yes no on off True False Null"),
      start: [[/^\s*(?:-\s+)*([\w.\/$-]+)(?=\s*:(?:\s|$))/d, "prop"]],
    },
    toml: {
      line: ["#"],
      strings: [{ q: '"""', multi: true }, { q: "'''", multi: true, raw: true }, { q: '"' }, { q: "'", raw: true }],
      keywords: new Set(),
      consts: words("true false"),
      start: [[/^\s*(\[\[?[^\]]*\]?\]?)(?=\s*(?:#|$))/d, "keyword"], [/^\s*([\w."-]+)(?=\s*=)/d, "prop"]],
    },
    sql: {
      line: ["--"],
      block: [["/*", "*/"]],
      strings: [{ q: "'", raw: true }, { q: '"' }],
      fold: true,
      keywords: words("select from where and or not null is in as join left right inner outer full cross on group by order having limit offset insert into values update set delete create table index view drop alter add column primary key foreign references unique default case when then else end distinct union all exists between like asc desc with match optional return unwind call merge detach count sum min max avg"),
      consts: words("true false"),
    },
    c: clike("auto break case char const continue default do double else enum extern float for goto if inline int long register restrict return short signed sizeof static struct switch typedef union unsigned void volatile while class namespace template typename public private protected virtual override using new delete try catch throw bool operator friend constexpr noexcept explicit final", "struct class enum union namespace"),
    java: clike("abstract assert boolean break byte case catch char class const continue default do double else enum extends final finally float for if implements import instanceof int interface long native new package private protected public return short static strictfp super switch synchronized throw throws transient try var void volatile while record sealed permits fun val when object companion override open internal data suspend lateinit is in as by", "class interface enum record fun object"),
    go: { ...clike("break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var", "func type"), strings: [{ q: '"' }, { q: "`", multi: true, raw: true }, { q: "'", char: true }], consts: words("true false nil iota") },
    rust: clike("as async await break const continue crate dyn else enum extern fn for if impl in let loop match mod move mut pub ref return static struct super trait type unsafe use where while", "fn struct enum trait mod type impl"),
    swift: clike("actor associatedtype async await break case catch class continue default defer deinit do else enum extension fallthrough fileprivate for func guard if import in init inout internal is let open operator private protocol public repeat rethrows return some static struct subscript switch throw throws try typealias var where while", "func class struct enum protocol extension actor"),
    csharp: clike("abstract as async await base bool break byte case catch char checked class const continue decimal default delegate do double else enum event explicit extern finally fixed float for foreach goto if implicit in int interface internal is lock long namespace new object operator out override params private protected public readonly record ref return sbyte sealed short sizeof stackalloc static string struct switch throw try typeof uint ulong unchecked unsafe ushort using var virtual void volatile while", "class struct interface enum record namespace"),
    php: { ...clike("abstract and array as break callable case catch class clone const continue declare default do echo else elseif empty enddeclare endfor endforeach endif endswitch endwhile extends final finally fn for foreach function global goto if implements include include_once instanceof insteadof interface isset list match namespace new or print private protected public readonly require require_once return static switch throw trait try unset use var while xor yield", "function class interface trait"), line: ["//", "#"], strings: [{ q: '"' }, { q: "'" }] },
    ruby: {
      line: ["#"],
      strings: [{ q: '"' }, { q: "'" }],
      keywords: words("alias and begin break case class def defined do else elsif end ensure for if in module next not or redo rescue retry return super then undef unless until when while yield require require_relative attr_reader attr_writer attr_accessor"),
      consts: words("true false nil self"),
      defs: words("def class module"),
    },
  };

  function clike(keywords, defs) {
    return { ...SLASHES, strings: C_STRINGS, keywords: words(keywords), consts: words("true false null nil nullptr NULL None Some Ok Err self Self this"), defs: words(defs) };
  }

  const BY_EXT = {
    py: "python", pyi: "python",
    js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript", ts: "javascript", tsx: "javascript", mts: "javascript",
    json: "json", jsonl: "json", ipynb: "json", rl: null,
    css: "css", scss: "css", less: "css",
    sh: "shell", bash: "shell", zsh: "shell", fish: "shell",
    yml: "yaml", yaml: "yaml", toml: "toml", lock: "toml", ini: "toml", cfg: "toml",
    sql: "sql", cypher: "sql", cql: "sql",
    c: "c", h: "c", cc: "c", cpp: "c", cxx: "c", hpp: "c", m: "c", mm: "c",
    java: "java", kt: "java", kts: "java", scala: "java", groovy: "java", gradle: "java",
    go: "go", rs: "rust", swift: "swift", cs: "csharp", php: "php", rb: "ruby",
    html: "markup", htm: "markup", xml: "markup", svg: "markup", vue: "markup", plist: "markup",
    md: "markdown", markdown: "markdown", mdx: "markdown",
  };
  const BY_NAME = { Dockerfile: "shell", Makefile: "shell", "uv.lock": "toml", "Cargo.lock": "toml", "poetry.lock": "toml", ".gitignore": "shell", ".env": "shell" };

  function language(path, firstLine = "") {
    const name = (path || "").split("/").pop();
    if (name in BY_NAME) return BY_NAME[name];
    const ext = name.includes(".") ? name.split(".").pop().toLowerCase() : "";
    if (ext in BY_EXT) return BY_EXT[ext];
    const bang = /^#!.*?\b(python|node|bash|sh|zsh|ruby)\b/.exec(firstLine)?.[1];
    return bang ? { python: "python", node: "javascript", ruby: "ruby" }[bang] || "shell" : null;
  }

  // Where a string or comment that opened earlier closes, or -1.
  function closing(text, from, open) {
    for (let i = from; i < text.length; i++) {
      if (!open.raw && text[i] === "\\") i++;
      else if (text.startsWith(open.close, i)) return i + open.close.length;
    }
    return -1;
  }

  const NUMBER = /^(?:0[xX][\da-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d+)?)\w*/;
  const WORD = /^[A-Za-z_$][\w$]*/;
  const REGEX = /^\/(?![*\/])(?:\\.|\[(?:\\.|[^\]\\])*\]|[^\/\\\[])+\/[dgimsuvy]*/;
  const CHAR = /^'(?:\\[^']+|[^\\'])'/;
  // A slash after one of these starts a regular expression, not a division.
  const BEFORE_REGEX = new Set([..."(,=:[!&|?{};+-*%<>~^"]);
  const REGEX_WORDS = words("return typeof case do else in of void delete throw new yield await");

  function scanCode(text, L, st) {
    const out = [];
    const n = text.length;
    const push = (a, b, type) => b > a && out.push([a, b, type]);
    let i = 0;
    let last = ""; // the last character or word that wasn't space or a comment
    let define = false;

    if (st.open) {
      const end = closing(text, 0, st.open);
      push(0, end < 0 ? n : end, st.open.type);
      if (end < 0) return out;
      i = end;
      st.open = null;
      last = "x";
    } else {
      for (const [re, type] of L.start || []) {
        const m = re.exec(text);
        if (!m) continue;
        push(m.indices[1][0], m.indices[1][1], type);
        i = m[0].length;
        break;
      }
    }

    scan: while (i < n) {
      const c = text[i];
      if (c === " " || c === "\t") {
        i++;
        continue;
      }
      for (const mark of L.line || []) {
        // In a shell, a # inside a word (like $#) is not a comment.
        if (!text.startsWith(mark, i) || (mark === "#" && i && /\S/.test(text[i - 1]) && L === LANGS.shell)) continue;
        push(i, n, "comment");
        return out;
      }
      for (const [open, close] of L.block || []) {
        if (!text.startsWith(open, i)) continue;
        const end = closing(text, i + open.length, { close, raw: true });
        push(i, end < 0 ? n : end, "comment");
        if (end < 0) {
          st.open = { close, raw: true, type: "comment" };
          return out;
        }
        i = end;
        continue scan;
      }
      const prefix = L.prefix?.exec(text.slice(i))?.[0].length || 0;
      for (const s of L.strings || []) {
        if (!text.startsWith(s.q, i + prefix)) continue;
        if (s.char) {
          const m = CHAR.exec(text.slice(i));
          if (m) push(i, i + m[0].length, "string");
          i += m ? m[0].length : 1;
          last = "x";
          continue scan;
        }
        const open = { close: s.q, raw: s.raw || /r/i.test(text.slice(i, i + prefix)), type: "string" };
        const end = closing(text, i + prefix + s.q.length, open);
        const key = L.keys && end > 0 && /^\s*:/.test(text.slice(end));
        push(i, end < 0 ? n : end, key ? "prop" : "string");
        if (end < 0) {
          if (s.multi) st.open = open;
          return out;
        }
        i = end;
        last = "x";
        continue scan;
      }
      if (L.regex && c === "/" && (!last || BEFORE_REGEX.has(last) || REGEX_WORDS.has(last))) {
        const m = REGEX.exec(text.slice(i));
        if (m) {
          push(i, i + m[0].length, "string");
          i += m[0].length;
          last = "x";
          continue;
        }
      }
      if (L.hex && c === "#") {
        const m = /^#[\da-fA-F]{3,8}\b/.exec(text.slice(i));
        if (m && /[\s:,(]/.test(text[i - 1] || " ") && i) {
          push(i, i + m[0].length, "number");
          i += m[0].length;
          continue;
        }
      }
      if ((L.decorator || L.at) && c === "@") {
        const m = /^@[\w.-]+/.exec(text.slice(i));
        if (m && (L.at || !last)) {
          push(i, i + m[0].length, L.at ? "keyword" : "meta");
          i += m[0].length;
          last = "x";
          continue;
        }
      }
      if ((c >= "0" && c <= "9") || (c === "." && text[i + 1] >= "0" && text[i + 1] <= "9")) {
        const m = NUMBER.exec(text.slice(i));
        push(i, i + m[0].length, "number");
        i += m[0].length;
        last = "x";
        continue;
      }
      const m = (L.word || WORD).exec(text.slice(i));
      if (!m) {
        // `function (a)` defines nothing by name.
        define = false;
        last = c;
        i++;
        continue;
      }
      const word = m[0];
      const end = i + word.length;
      const key = L.fold ? word.toLowerCase() : word;
      const member = last === ".";
      if (L.props && text[end] === ":" && (!last || last === "{" || last === ";") && !text.includes("{", end)) push(i, end, "prop");
      else if (!member && L.keywords.has(key)) {
        push(i, end, "keyword");
        define = !!L.defs?.has(key);
        last = word;
        i = end;
        continue;
      } else if (!member && L.consts?.has(key)) push(i, end, "const");
      else if (define) push(i, end, "def");
      else if (text[end] === "(" && !L.props) push(i, end, "call");
      define = false;
      last = word;
      i = end;
    }
    return out;
  }

  function scanMarkup(text, st) {
    const out = [];
    const n = text.length;
    const push = (a, b, type) => b > a && out.push([a, b, type]);
    let i = 0;
    while (i < n) {
      if (st.open) {
        const end = closing(text, i, st.open);
        push(i, end < 0 ? n : end, st.open.type);
        if (end < 0) return out;
        i = end;
        st.open = null;
      } else if (!st.tag) {
        // Inside <script> or <style>, a < is not a tag until the closing one.
        const at = text.indexOf(st.until || "<", i);
        if (at < 0) return out;
        i = at;
        st.until = null;
        if (text.startsWith("<!--", i)) {
          st.open = { close: "-->", raw: true, type: "comment" };
          continue;
        }
        const m = /^<[\/!?]?[A-Za-z][\w:.-]*/.exec(text.slice(i));
        if (m) {
          push(i, i + m[0].length, "tag");
          st.tag = true;
          st.body = /^<(script|style)$/i.exec(m[0])?.[1];
        }
        i += m ? m[0].length : 1;
      } else {
        const c = text[i];
        const m = /^[\w:@.#-]+/.exec(text.slice(i));
        if (c === '"' || c === "'") {
          st.open = { close: c, raw: true, type: "string" };
          push(i, i + 1, "string");
          i++;
        } else if (c === ">" || text.startsWith("/>", i) || text.startsWith("?>", i)) {
          const size = c === ">" ? 1 : 2;
          push(i, i + size, "tag");
          i += size;
          st.tag = false;
          if (st.body && c === ">") st.until = "</" + st.body;
          st.body = null;
        } else if (m) {
          push(i, i + m[0].length, "prop");
          i += m[0].length;
        } else i++;
      }
    }
    return out;
  }

  function scanMarkdown(text, st) {
    const fence = /^\s*(```|~~~)/.exec(text);
    if (fence) {
      st.fence = st.fence ? null : fence[1];
      return [[0, text.length, "meta"]];
    }
    if (st.fence) return [];
    if (/^#{1,6}\s/.test(text)) return [[0, text.length, "keyword"]];
    if (/^\s*>/.test(text)) return [[0, text.length, "comment"]];
    const out = [];
    const bullet = /^\s*(?:[-*+]|\d+[.)])\s/.exec(text);
    if (bullet) out.push([0, bullet[0].length, "meta"]);
    for (const m of text.matchAll(/`[^`]+`|\[[^\]]+\]\([^)\s]+\)|\*\*[^*]+\*\*/g)) {
      out.push([m.index, m.index + m[0].length, m[0][0] === "`" ? "string" : m[0][0] === "[" ? "call" : "def"]);
    }
    return out;
  }

  // Keep the lexical state across lines and across slices of the same file.
  function scanner(lang) {
    if (!lang) return null;
    const st = {};
    const scan = lang === "markup" ? (t) => scanMarkup(t, st) : lang === "markdown" ? (t) => scanMarkdown(t, st) : (t) => scanCode(t, LANGS[lang], st);
    // A minified line is not worth the time, and nobody reads it for its colors.
    return (text) => text.length > 2000 ? [] : scan(text);
  }

  // One token list per line, or null when the file's language isn't known.
  function tokens(lines, path) {
    const scan = scanner(language(path, lines[0]));
    return scan ? lines.map(scan) : null;
  }

  return { tokens, scanner, language, TYPES: ["comment", "string", "keyword", "const", "number", "def", "call", "prop", "tag", "meta"] };
})();

if (typeof module !== "undefined") module.exports = SYNTAX;
