"""Render file changes as a side-by-side diff page: one HTML file to open in a browser.

Usage:
  python3 diff_view.py --git [--repo DIR] [--base REF] [PATH ...]   working tree against a git ref (default HEAD)
  python3 diff_view.py --dirs BEFORE AFTER [PATH ...]               two folders, matched by relative path
  python3 diff_view.py --pair OLD NEW [--pair OLD NEW ...]          explicit file pairs

Options:
  --title TEXT       page title (default: "Changes")
  --subtitle TEXT    one line under the title
  --context N        unchanged lines shown around each change (default 3); the rest folds
  --full             show every line, with no folding
  -o, --out FILE     where to write the page (default: a new file in the temp folder)
  --open             open the page in the default browser

It prints the page's file:// address. The page draws each diff with @pierre/diffs (Apache-2.0),
loaded from esm.sh at a pinned version: split or unified, wrapped lines, changed words marked,
folded context that expands on click. Markdown files are drawn as documents (markdown-it and
visual-dom-diff, both MIT), with a Source switch for line diffs. Without network it shows a plain
unified diff instead.
The script itself needs only Python's standard library.
"""
import argparse
import datetime
import difflib
import html
import json
import os
import subprocess
import sys
import tempfile
import webbrowser

# Pinned, so a page renders the same tomorrow. @pierre/diffs 1.5.1 was published on 2026-09-25.
RENDERER = "https://esm.sh/@pierre/diffs@1.5.1"
# Markdown files can also be shown rendered: markdown-it draws each version and visual-dom-diff marks
# what changed between the two drawings. Both load only when a page holds a Markdown file.
MARKDOWN = "https://esm.sh/markdown-it@15.0.2"
DOM_DIFF = "https://esm.sh/visual-dom-diff@0.7.3"
MD_SUFFIXES = (".md", ".markdown")


# ---------- collecting file pairs ----------

def decode(data):
    """Return (text, binary) for raw file bytes."""
    if b"\0" in data:
        return "", True
    return data.decode("utf-8", errors="replace"), False


def read_bytes(path):
    """Return the file's bytes, or None when it does not exist."""
    if path is None or not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def read_file(path):
    """Return (text, binary). The text is None when the file does not exist."""
    data = read_bytes(path)
    return (None, False) if data is None else decode(data)


def entry(name, old, new, before, after):
    (a, a_bin), (b, b_bin) = old, new
    return {"name": name, "old": a, "new": b, "binary": a_bin or b_bin, "before": before, "after": after,
            "md": name.lower().endswith(MD_SUFFIXES) and not (a_bin or b_bin)}


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, check=False).stdout


def pairs_from_git(repo, base, paths):
    repo = os.path.abspath(repo)
    top = git(repo, "rev-parse", "--show-toplevel").decode().strip() or repo
    changed = git(top, "diff", "--name-only", base, "--", *paths).decode().splitlines()
    untracked = git(top, "ls-files", "--others", "--exclude-standard", "--", *paths).decode().splitlines()
    out = []
    for rel in sorted(set(changed) | set(untracked)):
        exists = subprocess.run(["git", "-C", top, "cat-file", "-e", f"{base}:{rel}"],
                                capture_output=True).returncode == 0
        old = decode(git(top, "show", f"{base}:{rel}")) if exists else (None, False)
        out.append(entry(rel, old, read_file(os.path.join(top, rel)), base, "working tree"))
    return out


def pairs_from_dirs(before_dir, after_dir, paths):
    def walk(root):
        found = set()
        for d, _, files in os.walk(root):
            for name in files:
                found.add(os.path.relpath(os.path.join(d, name), root))
        return found
    rels = set(paths) if paths else walk(before_dir) | walk(after_dir)
    before_name, after_name = short_names(before_dir, after_dir)
    out = []
    for rel in sorted(rels):
        # Compare bytes: two binary files read as the same empty text.
        a, b = read_bytes(os.path.join(before_dir, rel)), read_bytes(os.path.join(after_dir, rel))
        if a == b:
            continue
        old = (None, False) if a is None else decode(a)
        new = (None, False) if b is None else decode(b)
        out.append(entry(rel, old, new, before_name, after_name))
    return out


def short_names(a, b):
    """Name two folders by the path below their common parent, such as fx/a and fx/b."""
    a, b = os.path.abspath(a), os.path.abspath(b)
    common = os.path.commonpath([a, b])
    if common in (a, b):
        return a, b
    parent = os.path.dirname(common)
    return os.path.relpath(a, parent), os.path.relpath(b, parent)


def pairs_from_list(pairs):
    out = []
    for old, new in pairs:
        # Name the pair after the file being changed: the old path when it exists, else the new one.
        label = os.path.relpath(old if os.path.exists(old) else new)
        out.append(entry(label, read_file(old), read_file(new), "", ""))
    return out


# ---------- rendering ----------

def status_of(f):
    if f["binary"]:
        return "binary"
    if f["old"] is None:
        return "added"
    if f["new"] is None:
        return "deleted"
    return "modified"


def line_counts(f):
    """Lines removed and added. The page recounts with the renderer's own diff once it loads."""
    if f["binary"]:
        return 0, 0
    a, b = (f["old"] or "").splitlines(), (f["new"] or "").splitlines()
    removed = added = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            removed += i2 - i1
            added += j2 - j1
    return removed, added


def stat_bar(removed, added):
    """Five blocks split between added and removed, as on GitHub."""
    total = removed + added
    green = round(5 * added / total) if total else 0
    red = 5 - green if total else 0
    blocks = ["add"] * green + ["del"] * red + ["none"] * (5 - green - red)
    return "".join(f'<i class="{b}"></i>' for b in blocks)


def split_path(name):
    head, _, tail = name.rpartition("/")
    return (head + "/" if head else ""), tail


STATUS_LETTER = {"added": "A", "modified": "M", "deleted": "D", "binary": "B"}


def file_row(i, f):
    status = status_of(f)
    removed, added = line_counts(f)
    folder, base = split_path(f["name"])
    return (f'<li><a href="#f{i}" data-row="{i}"><span class="st {status}" title="{status}">{STATUS_LETTER[status]}</span>'
            f'<span class="path"><span class="dir">{html.escape(folder)}</span>{html.escape(base)}</span>'
            f'<span class="n del{" zero" if not removed else ""}">−{removed}</span>'
            f'<span class="n add{" zero" if not added else ""}">+{added}</span>'
            f'<span class="bar" aria-hidden="true">{stat_bar(removed, added)}</span></a></li>')


def file_section(i, f):
    if f["binary"]:
        return (f'<section class="file binary" id="f{i}"><div class="plain-head"><span class="path">'
                f'{html.escape(f["name"])}</span><span class="note">binary file, not shown</span></div></section>')
    return f'<section class="file" id="f{i}" data-i="{i}"><div class="loading">Loading {html.escape(f["name"])}</div></section>'


def fallback_section(f, context, full):
    """A plain unified diff, shown when the renderer cannot load."""
    head = f'<div class="plain-head"><span class="path">{html.escape(f["name"])}</span></div>'
    if f["binary"]:
        return f'<section class="file">{head}<p class="note pad">Binary file, not shown.</p></section>'
    a, b = (f["old"] or "").splitlines(), (f["new"] or "").splitlines()
    lines = []
    for line in list(difflib.unified_diff(a, b, lineterm="", n=10**9 if full else context))[2:]:
        kind = "hunk" if line.startswith("@@") else "add" if line.startswith("+") else "del" if line.startswith("-") else "ctx"
        lines.append(f'<span class="{kind}">{html.escape(line) or " "}</span>')
    return f'<section class="file">{head}<pre class="udiff">{"".join(lines)}</pre></section>'


CSS = """
:root {
  color-scheme: light dark;
  --bg: #fafafa; --surface: #ffffff; --fg: #0a0a0a; --muted: #6b6b6b; --faint: #a3a3a3;
  --line: #e5e5e5; --hover: #f2f2f2; --press: #ebebeb;
  --accent: #0077cc; --add: #16a34a; --del: #dc2626; --mod: #0077cc;
  --ui: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0a0a0a; --surface: #0a0a0a; --fg: #fafafa; --muted: #a3a3a3; --faint: #6b6b6b;
    --line: #262626; --hover: #171717; --press: #1f1f1f;
    --accent: #4db8ff; --add: #5ecc71; --del: #ff6762; --mod: #69b1ff;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 14px/1.5 var(--ui); -webkit-font-smoothing: antialiased; }
.page { max-width: 1680px; margin: 0 auto; padding: 36px 24px 96px; }
@media (max-width: 640px) { .page { padding: 20px 16px 64px; } }

header h1 { margin: 0; font-size: 22px; font-weight: 650; letter-spacing: -0.01em; }
header .sub { margin: 6px 0 0; color: var(--muted); max-width: 76ch; }
.meta { display: flex; flex-wrap: wrap; gap: 4px 16px; margin: 12px 0 0; color: var(--muted); font-size: 13px; }
.meta .refs { font-family: var(--mono); font-size: 12px; }
.meta .add, .n.add { color: var(--add); }
.meta .del, .n.del { color: var(--del); }

.filelist { list-style: none; margin: 20px 0 0; padding: 6px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface); }
.filelist a { display: grid; grid-template-columns: 16px minmax(0, 1fr) auto auto auto; align-items: center; gap: 12px;
  padding: 5px 10px; border-radius: 6px; color: inherit; text-decoration: none; }
.filelist a:hover { background: var(--hover); }
.filelist a:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.st { font: 600 11px var(--mono); text-align: center; }
.st.added { color: var(--add); } .st.modified { color: var(--mod); } .st.deleted { color: var(--del); } .st.binary { color: var(--muted); }
.path { font: 12.5px var(--mono); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.path .dir { color: var(--muted); }
.n { font: 12px var(--mono); font-variant-numeric: tabular-nums; text-align: right; min-width: 3ch; }
.n.zero { color: var(--faint); }
.bar { display: inline-flex; gap: 2px; }
.bar i { width: 7px; height: 7px; border-radius: 2px; background: var(--line); }
.bar i.add { background: var(--add); } .bar i.del { background: var(--del); }
@media (max-width: 640px) { .bar { display: none; } .filelist a { grid-template-columns: 16px minmax(0, 1fr) auto auto; gap: 8px; } }

.toolbar { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px 16px; margin: 24px 0 12px; }
.group { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.seg { display: inline-flex; padding: 2px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); }
.seg button, .ghost { font: 500 12.5px var(--ui); color: var(--muted); background: transparent; border: 0; border-radius: 6px; padding: 4px 12px; cursor: pointer; }
.seg button[aria-pressed="true"] { color: var(--fg); background: var(--press); }
.seg button:hover, .ghost:hover { color: var(--fg); }
.ghost { border: 1px solid var(--line); background: var(--surface); padding: 4px 10px; }
.ghost:hover { background: var(--hover); }
button:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.toggle { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--muted); cursor: pointer; user-select: none; padding: 4px 4px; }
.toggle input { accent-color: var(--accent); margin: 0; }

section.file { margin: 0 0 16px; border: 1px solid var(--line); border-radius: 10px; overflow: clip; background: var(--surface); scroll-margin-top: 12px;
  --diffs-font-family: var(--mono); --diffs-header-font-family: var(--ui); --diffs-font-size: 12.5px; --diffs-line-height: 20px; }
.loading { padding: 14px 16px; color: var(--muted); font-size: 13px; }
.plain-head { display: flex; align-items: baseline; gap: 12px; padding: 10px 16px; }
.plain-head .path { font-weight: 600; }
.note { color: var(--muted); font-size: 13px; }
.pad { margin: 0; padding: 0 16px 14px; }
.fold { display: inline-grid; place-items: center; width: 22px; height: 22px; margin-right: 2px; padding: 0; border: 0; border-radius: 5px;
  background: transparent; color: var(--muted); cursor: pointer; }
.fold:hover { background: var(--hover); color: var(--fg); }
.fold svg { transition: transform .12s ease; }
.fold[aria-expanded="false"] svg { transform: rotate(-90deg); }
.tag { margin-left: 8px; padding: 1px 7px; border-radius: 999px; font: 500 11px var(--ui); border: 1px solid currentColor; }
.tag.added { color: var(--add); } .tag.deleted { color: var(--del); }

.fallback, html.offline main, html.offline .toolbar { display: none; }
html.offline .fallback { display: block; }
.fallback .notice { margin: 24px 0 12px; padding: 10px 14px; border: 1px solid var(--line); border-radius: 8px; color: var(--muted); font-size: 13px; }
.udiff { margin: 0; padding: 4px 0 8px; font: 12.5px/20px var(--mono); white-space: pre-wrap; overflow-wrap: anywhere; }
.udiff span { display: block; padding: 0 16px; }
.udiff .add { background: color-mix(in srgb, var(--add) 14%, transparent); }
.udiff .del { background: color-mix(in srgb, var(--del) 14%, transparent); }
.udiff .hunk { color: var(--muted); background: var(--hover); margin: 4px 0; }
.empty-page { color: var(--muted); margin-top: 24px; }

.lbl { color: var(--muted); font-size: 12.5px; margin-left: 10px; }
.md-head { position: sticky; top: 0; z-index: 2; display: flex; align-items: center; gap: 8px; min-height: 44px; padding: 0 16px 0 14px;
  background: var(--surface); font: 500 13px var(--ui); }
.md-head .name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.md-head .grow { flex: 1; }
.md-head .ico { color: var(--mod); display: inline-flex; }
.md-head .ico.added { color: var(--add); } .md-head .ico.deleted { color: var(--del); }
.md-head .cnt { font: 12.5px var(--mono); }
.md-head .cnt.del { color: var(--del); } .md-head .cnt.add { color: var(--add); margin-left: 8px; }
.md-note { margin: 0; padding: 10px 16px 14px; color: var(--muted); font-size: 13px; }
.md-row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
.md-grid.unified .md-row { grid-template-columns: minmax(0, 1fr); }
.md-cell { min-width: 0; padding: 7px 18px; overflow-wrap: anywhere; font: 15px/1.65 var(--ui); }
.md-row .md-cell + .md-cell { border-left: 1px solid var(--line); }
.md-cell > * { margin-block: 0; }
.md-cell > .vdd-added, .md-cell > .vdd-removed, .md-cell > .vdd-modified { background: none; }
.md-cell h1, .md-cell h2, .md-cell h3, .md-cell h4, .md-cell h5, .md-cell h6 { font-weight: 650; line-height: 1.3; letter-spacing: -0.01em; }
.md-cell h1 { font-size: 1.7em; } .md-cell h2 { font-size: 1.4em; } .md-cell h3 { font-size: 1.15em; }
.md-cell h4, .md-cell h5, .md-cell h6 { font-size: 1em; }
.md-cell ul, .md-cell ol { padding-left: 1.5em; }
.md-cell li + li { margin-top: 2px; }
.md-cell blockquote { margin-inline: 0; padding: 2px 14px; border-left: 3px solid var(--line); color: var(--muted); }
.md-cell a { color: var(--accent); text-decoration-thickness: 1px; text-underline-offset: 2px; }
.md-cell code { padding: 1px 5px; border-radius: 4px; background: var(--hover); font: 0.88em var(--mono); }
.md-cell pre code { padding: 0; border-radius: 0; background: none; font: inherit; }
.md-cell pre { padding: 10px 14px; overflow: auto; border-radius: 8px; background: var(--hover); font: 12.5px/1.6 var(--mono); }
.md-cell table { border-collapse: collapse; font-size: 0.93em; }
.md-cell th, .md-cell td { padding: 4px 10px; border: 1px solid var(--line); text-align: left; vertical-align: top; }
.md-cell th { background: var(--hover); font-weight: 600; }
.md-cell hr { border: 0; border-top: 1px solid var(--line); }
.md-cell img { max-width: 100%; }
.md-cell ins { text-decoration: none; background: color-mix(in srgb, var(--add) 24%, transparent); border-radius: 2px; }
.md-cell ins.vdd-modified { background: color-mix(in srgb, var(--mod) 20%, transparent); }
.md-cell del { text-decoration: none; background: color-mix(in srgb, var(--del) 24%, transparent); border-radius: 2px; }
.md-grid.unified del { text-decoration: line-through; text-decoration-thickness: 1px; }
.md-row.mod > .md-cell:first-child { box-shadow: inset 3px 0 0 var(--del); }
.md-row.mod > .md-cell:last-child { box-shadow: inset 3px 0 0 var(--add); }
.md-row.del > .md-cell:first-child, .md-cell.del { background: color-mix(in srgb, var(--del) 9%, transparent); box-shadow: inset 3px 0 0 var(--del); }
.md-row.add > .md-cell:last-child, .md-cell.add { background: color-mix(in srgb, var(--add) 9%, transparent); box-shadow: inset 3px 0 0 var(--add); }
.md-grid.unified .md-row.mod > .md-cell { box-shadow: inset 3px 0 0 var(--mod); }
.md-cell.empty { background: repeating-linear-gradient(135deg, transparent 0 6px, var(--line) 6px 7px); opacity: .5; }
.md-fold { display: block; width: calc(100% - 16px); margin: 4px 8px; padding: 5px 10px; border: 0; border-radius: 6px; background: var(--hover);
  color: var(--muted); font: 12.5px var(--ui); text-align: left; cursor: pointer; }
.md-fold:hover { color: var(--fg); background: var(--press); }
@media (max-width: 640px) { .md-row { grid-template-columns: minmax(0, 1fr); } .md-row .md-cell + .md-cell { border-left: 0; } .md-cell.empty { display: none; } }
"""

JS = """
const files = JSON.parse(document.getElementById('diff-data').textContent);
const config = JSON.parse(document.getElementById('diff-config').textContent);
const root = document.documentElement;

const PREFS_KEY = 'diff-view:prefs';
const prefs = { style: 'split', wrap: true, all: config.full, md: 'rendered' };
try { Object.assign(prefs, JSON.parse(localStorage.getItem(PREFS_KEY) || '{}')); } catch (e) {}
if (config.full) prefs.all = true;
function savePrefs() { try { localStorage.setItem(PREFS_KEY, JSON.stringify({ style: prefs.style, wrap: prefs.wrap, md: prefs.md })); } catch (e) {} }

const CHEVRON = '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 4.5l3 3 3-3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const views = [];
let FileDiff, parseDiffFromFile;

function optionsFor(v) {
  return {
    diffStyle: prefs.style, overflow: prefs.wrap ? 'wrap' : 'scroll', expandUnchanged: prefs.all,
    collapsed: v.collapsed, stickyHeader: true, lineDiffType: 'word-alt', hunkSeparators: 'line-info',
    themeType: 'system', theme: { dark: 'pierre-dark', light: 'pierre-light' },
    renderHeaderPrefix: () => {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'fold'; b.innerHTML = CHEVRON;
      b.setAttribute('aria-expanded', String(!v.collapsed));
      b.title = v.collapsed ? 'Expand file' : 'Collapse file';
      b.addEventListener('click', e => { e.stopPropagation(); setCollapsed(v, !v.collapsed); });
      return b;
    },
    renderHeaderFilenameSuffix: () => {
      if (v.status !== 'added' && v.status !== 'deleted') return null;
      const t = document.createElement('span');
      t.className = 'tag ' + v.status; t.textContent = v.status === 'added' ? 'new' : 'deleted';
      return t;
    },
  };
}
// ---- Markdown, drawn instead of shown as source ----
const mdState = { libs: null, loading: null, failed: false };
function ensureMd() {
  if (!mdState.loading) {
    const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error('timed out after 20 s')), 20000));
    mdState.loading = Promise.race([Promise.all([import(config.markdown), import(config.domDiff)]), timeout]).then(([m, d]) => {
      // Raw HTML in a document is shown as text, and comments are hidden, as on GitHub.
      const md = new m.default({ html: true, linkify: false });
      const raw = (tokens, i) => /^\\s*<!--/.test(tokens[i].content) ? '' : md.utils.escapeHtml(tokens[i].content);
      md.renderer.rules.html_block = raw;
      md.renderer.rules.html_inline = raw;
      mdState.libs = { md, visualDomDiff: d.visualDomDiff };
    }).catch(e => {
      mdState.failed = true;
      document.getElementById('md-fail').textContent = 'Markdown could not be drawn (' + (e && e.message || e) + '), so these files show as source.';
    });
  }
  return mdState.loading;
}
function mdOn(v) { return v.md && prefs.md === 'rendered' && !!mdState.libs; }

function bodyOf(src) {
  // A leading front-matter block is shown as code, as in the source.
  const text = (src || '').replace(/\\r\\n/g, '\\n').replace(/^---\\n([\\s\\S]*?)\\n---(?=\\n|$)/, (m, y) => '```yaml\\n' + y + '\\n```');
  const e = document.createElement('div');
  e.innerHTML = mdState.libs.md.render(text);
  return e;
}
function mdRows(v) {
  if (v.rows) return v.rows;
  const f = files[v.i];
  const merged = mdState.libs.visualDomDiff(bodyOf(f.old), bodyOf(f.new)).firstElementChild;
  const mark = '.vdd-added, .vdd-removed, .vdd-modified';
  v.rows = [];
  for (const k of merged.children) {
    if (!k.textContent.trim() && !k.matches('hr, img') && !k.querySelector('img, hr')) continue;  // empty wrappers
    const added = k.classList.contains('vdd-added'), removed = k.classList.contains('vdd-removed');
    let left = null, right = null;
    if (!added) { left = k.cloneNode(true); left.querySelectorAll('.vdd-added').forEach(n => n.remove()); }
    if (!removed) { right = k.cloneNode(true); right.querySelectorAll('.vdd-removed').forEach(n => n.remove()); }
    const changed = added || removed || k.matches(mark) || !!k.querySelector(mark);
    v.rows.push({ merged: k, left, right, kind: added ? 'add' : removed ? 'del' : changed ? 'mod' : 'same', changed });
  }
  return v.rows;
}
function cell(node, cls) {
  const c = document.createElement('div');
  c.className = 'md-cell ' + cls;
  if (node) { c.append(node.cloneNode(true)); c.querySelectorAll('a[href]').forEach(a => { a.title = a.getAttribute('href'); }); }
  return c;
}
function mdRow(r, split) {
  const row = document.createElement('div');
  row.className = 'md-row ' + r.kind;
  if (split) row.append(cell(r.left, r.left ? '' : 'empty'), cell(r.right, r.right ? '' : 'empty'));
  else row.append(cell(r.merged, r.kind === 'add' || r.kind === 'del' ? r.kind : ''));
  return row;
}
function mdBody(v) {
  const rows = mdRows(v), split = prefs.style === 'split';
  const grid = document.createElement('div');
  grid.className = 'md-grid ' + (split ? 'split' : 'unified');
  if (!rows.some(r => r.changed)) {
    const n = document.createElement('p');
    n.className = 'md-note';
    n.textContent = 'Nothing changed in the drawn text: only comments, spacing or markup. Choose Source to see every line.';
    grid.append(n);
    return grid;
  }
  const keep = 1;  // unchanged blocks kept around a change
  for (let i = 0; i < rows.length;) {
    if (rows[i].changed) { grid.append(mdRow(rows[i++], split)); continue; }
    let j = i;
    while (j < rows.length && !rows[j].changed) j++;
    const head = i === 0 ? 0 : keep, tail = j === rows.length ? 0 : keep, hidden = j - i - head - tail;
    if (prefs.all || hidden < 3) { for (let k = i; k < j; k++) grid.append(mdRow(rows[k], split)); }
    else {
      for (let k = i; k < i + head; k++) grid.append(mdRow(rows[k], split));
      const bar = document.createElement('button');
      bar.type = 'button'; bar.className = 'md-fold'; bar.textContent = hidden + ' unchanged blocks';
      const folded = rows.slice(i + head, j - tail);
      bar.addEventListener('click', () => bar.replaceWith(...folded.map(r => mdRow(r, split))));
      grid.append(bar);
      for (let k = j - tail; k < j; k++) grid.append(mdRow(rows[k], split));
    }
    i = j;
  }
  return grid;
}
function mdHeader(v) {
  const h = document.createElement('div');
  h.className = 'md-head';
  const b = document.createElement('button');
  b.type = 'button'; b.className = 'fold'; b.innerHTML = CHEVRON;
  b.setAttribute('aria-expanded', String(!v.collapsed));
  b.title = v.collapsed ? 'Expand file' : 'Collapse file';
  b.addEventListener('click', () => setCollapsed(v, !v.collapsed));
  const sym = { added: 'M5 8h6M8 5v6', deleted: 'M5 8h6', modified: '' }[v.status];
  const ico = document.createElement('span');
  ico.className = 'ico ' + v.status;
  ico.innerHTML = '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><rect x="1.5" y="1.5" width="13" height="13" rx="3.5" fill="none" stroke="currentColor" stroke-width="1.5"/>'
    + (sym ? '<path d="' + sym + '" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" fill="none"/>' : '<circle cx="8" cy="8" r="2" fill="currentColor"/>') + '</svg>';
  const name = document.createElement('span');
  name.className = 'name'; name.textContent = files[v.i].name;
  h.append(b, ico, name);
  if (v.status === 'added' || v.status === 'deleted') {
    const t = document.createElement('span');
    t.className = 'tag ' + v.status; t.textContent = v.status === 'added' ? 'new' : 'deleted';
    h.append(t);
  }
  const grow = document.createElement('span');
  grow.className = 'grow';
  const del = document.createElement('span'), add = document.createElement('span');
  del.className = 'cnt del'; del.textContent = '\\u2212' + v.removed;
  add.className = 'cnt add'; add.textContent = '+' + v.added;
  h.append(grow);
  if (v.removed) h.append(del);
  if (v.added) h.append(add);
  return h;
}
function renderMd(v) {
  v.section.replaceChildren(mdHeader(v));
  if (!v.collapsed) v.section.append(mdBody(v));
}

// Build a file's view from nothing, in the mode the page is in now.
function mount(v) {
  if (v.diff) { try { v.diff.cleanUp(); } catch (e) {} v.diff = null; }
  v.section.replaceChildren();
  if (mdOn(v)) { v.mounted = 'md'; renderMd(v); return; }
  v.mounted = 'code';
  v.diff = new FileDiff(optionsFor(v));
  v.diff.render({ fileDiff: v.fileDiff, containerWrapper: v.section });
}
function apply(v) {
  if (v.mounted === 'md') renderMd(v);
  else { v.diff.setOptions(optionsFor(v)); v.diff.rerender(); }
}
function setCollapsed(v, collapsed) { v.collapsed = collapsed; apply(v); }
function applyAll() { views.forEach(apply); }

function bar(removed, added) {
  const total = removed + added, green = total ? Math.round(5 * added / total) : 0, red = total ? 5 - green : 0;
  return Array.from({ length: 5 }, (_, k) => `<i class="${k < green ? 'add' : k < green + red ? 'del' : 'none'}"></i>`).join('');
}
function setCounts(i, removed, added) {
  const row = document.querySelector(`[data-row="${i}"]`);
  if (!row) return;
  const del = row.querySelector('.n.del'), add = row.querySelector('.n.add');
  del.textContent = '\\u2212' + removed; del.classList.toggle('zero', !removed);
  add.textContent = '+' + added; add.classList.toggle('zero', !added);
  row.querySelector('.bar').innerHTML = bar(removed, added);
}

function syncToolbar() {
  document.querySelectorAll('[data-style]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.style === prefs.style)));
  document.querySelectorAll('[data-md]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.md === prefs.md)));
  document.getElementById('opt-wrap').checked = prefs.wrap;
  document.getElementById('opt-all').checked = prefs.all;
}
document.addEventListener('click', e => {
  const s = e.target.closest('[data-style]');
  if (s) { prefs.style = s.dataset.style; savePrefs(); syncToolbar(); applyAll(); return; }
  const m = e.target.closest('[data-md]');
  if (m) { switchMd(m.dataset.md); return; }
  const f = e.target.closest('[data-files]');
  if (f) views.forEach(v => setCollapsed(v, f.dataset.files === 'collapse'));
  // Links in a drawn document would resolve against this page, so only web links open, in a new tab.
  const a = e.target.closest('.md-cell a[href]');
  if (a) { e.preventDefault(); if (/^https?:/i.test(a.getAttribute('href'))) window.open(a.href, '_blank', 'noopener'); }
});
async function switchMd(mode) {
  prefs.md = mode; savePrefs(); syncToolbar();
  if (mode === 'rendered') await ensureMd();
  views.filter(v => v.md).forEach(mount);
}
document.getElementById('opt-wrap').addEventListener('change', e => { prefs.wrap = e.target.checked; savePrefs(); applyAll(); });
document.getElementById('opt-all').addEventListener('change', e => { prefs.all = e.target.checked; applyAll(); });
syncToolbar();

function goOffline(reason) {
  root.classList.add('offline');
  const n = document.getElementById('offline-reason');
  if (n) n.textContent = reason;
}

try {
  const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error('timed out after 20 s')), 20000));
  ({ FileDiff, parseDiffFromFile } = await Promise.race([import(config.renderer), timeout]));
  if (prefs.md === 'rendered' && files.some(f => f.md)) await ensureMd();
  let removedAll = 0, addedAll = 0;
  files.forEach((f, i) => {
    if (f.binary) return;
    const section = document.getElementById('f' + i);
    const oldFile = f.old == null ? null : { name: f.name, contents: f.old };
    const newFile = f.new == null ? null : { name: f.name, contents: f.new };
    const fileDiff = parseDiffFromFile(oldFile, newFile, { context: config.context });
    let removed = 0, added = 0;
    for (const h of fileDiff.hunks) { removed += h.deletionLines; added += h.additionLines; }
    removedAll += removed; addedAll += added;
    setCounts(i, removed, added);
    const v = { i, section, fileDiff, removed, added, md: !!f.md, collapsed: false,
      status: f.old == null ? 'added' : f.new == null ? 'deleted' : 'modified' };
    mount(v);
    views.push(v);
  });
  const total = document.getElementById('total');
  if (total) total.innerHTML = `<span class="add">+${addedAll}</span> <span class="del">\\u2212${removedAll}</span>`;
} catch (e) {
  goOffline(String(e && e.message || e));
}
"""


def build_page(title, subtitle, files, context, full):
    removed_all = added_all = 0
    for f in files:
        r, a = line_counts(f)
        removed_all += r
        added_all += a
    refs = ""
    if files and files[0]["before"]:
        refs = f'<span class="refs">{html.escape(files[0]["before"])} → {html.escape(files[0]["after"])}</span>'
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    sub = f'<p class="sub">{html.escape(subtitle)}</p>' if subtitle else ""
    count = f"{len(files)} file{'s' if len(files) != 1 else ''}"
    data = json.dumps(files, ensure_ascii=False).replace("<", "\\u003c")
    config = json.dumps({"renderer": RENDERER, "markdown": MARKDOWN, "domDiff": DOM_DIFF, "context": context, "full": full})
    md_control = ""
    if any(f["md"] for f in files):
        md_control = ('<span class="lbl">Markdown</span>'
                      '<div class="seg" role="group" aria-label="Markdown files">'
                      '<button type="button" data-md="rendered">Rendered</button>'
                      '<button type="button" data-md="source">Source</button></div>'
                      '<span class="note" id="md-fail"></span>')
    if not files:
        body = '<p class="empty-page">No changes.</p>'
    else:
        body = f"""<ol class="filelist">{"".join(file_row(i, f) for i, f in enumerate(files))}</ol>
<div class="toolbar">
  <div class="group">
    <div class="seg" role="group" aria-label="Layout"><button type="button" data-style="split">Split</button><button type="button" data-style="unified">Unified</button></div>
    {md_control}
    <label class="toggle"><input type="checkbox" id="opt-wrap"> Wrap lines</label>
    <label class="toggle"><input type="checkbox" id="opt-all"> Show all lines</label>
  </div>
  <div class="group"><button type="button" class="ghost" data-files="expand">Expand all</button><button type="button" class="ghost" data-files="collapse">Collapse all</button></div>
</div>
<main>{"".join(file_section(i, f) for i, f in enumerate(files))}</main>
<div class="fallback">
  <p class="notice">The diff renderer did not load (<span id="offline-reason">no script</span>), so this is a plain unified diff. It loads from esm.sh and needs a network connection.</p>
  {"".join(fallback_section(f, context, full) for f in files)}
</div>
<noscript><style>.fallback {{ display: block; }} main, .toolbar {{ display: none; }}</style></noscript>"""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><div class="page">
<header><h1>{html.escape(title)}</h1>{sub}
<p class="meta"><span>{count}</span><span id="total"><span class="add">+{added_all}</span> <span class="del">−{removed_all}</span></span>{refs}<span>{stamp}</span></p>
</header>
{body}
</div>
<script type="application/json" id="diff-data">{data}</script>
<script type="application/json" id="diff-config">{config}</script>
<script type="module">{JS if files else ""}</script></body></html>
"""


def main(argv):
    p = argparse.ArgumentParser(description="Render file changes as a side-by-side diff page.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--git", action="store_true", help="working tree against a git ref")
    src.add_argument("--dirs", nargs=2, metavar=("BEFORE", "AFTER"), help="two folders, matched by relative path")
    src.add_argument("--pair", nargs=2, action="append", metavar=("OLD", "NEW"), help="an explicit file pair")
    p.add_argument("paths", nargs="*", help="limit --git or --dirs to these paths")
    p.add_argument("--repo", default=".", help="repository for --git (default: current folder)")
    p.add_argument("--base", default="HEAD", help="git ref to compare against (default: HEAD)")
    p.add_argument("--title", default="Changes")
    p.add_argument("--subtitle", default="")
    p.add_argument("--context", type=int, default=3)
    p.add_argument("--full", action="store_true")
    p.add_argument("-o", "--out")
    p.add_argument("--open", action="store_true")
    args = p.parse_args(argv)

    if args.git:
        files = pairs_from_git(args.repo, args.base, args.paths)
    elif args.dirs:
        files = pairs_from_dirs(args.dirs[0], args.dirs[1], args.paths)
    else:
        files = pairs_from_list(args.pair)

    out = args.out or os.path.join(tempfile.gettempdir(),
                                   f"diff-view-{datetime.datetime.now():%Y%m%d-%H%M%S}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(build_page(args.title, args.subtitle, files, max(args.context, 0), args.full))
    url = "file://" + os.path.abspath(out)
    print(url)
    if args.open:
        webbrowser.open(url)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
