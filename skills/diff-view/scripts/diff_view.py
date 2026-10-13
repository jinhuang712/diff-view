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
folded context that expands on click. Without network it shows a plain unified diff instead.
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

# Pinned, so a page renders the same tomorrow. 1.5.1 was published on 2026-09-25.
RENDERER = "https://esm.sh/@pierre/diffs@1.5.1"


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
    return {"name": name, "old": a, "new": b, "binary": a_bin or b_bin, "before": before, "after": after}


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
"""

JS = """
const files = JSON.parse(document.getElementById('diff-data').textContent);
const config = JSON.parse(document.getElementById('diff-config').textContent);
const root = document.documentElement;

const PREFS_KEY = 'diff-view:prefs';
const prefs = { style: 'split', wrap: true, all: config.full };
try { Object.assign(prefs, JSON.parse(localStorage.getItem(PREFS_KEY) || '{}')); } catch (e) {}
if (config.full) prefs.all = true;
function savePrefs() { try { localStorage.setItem(PREFS_KEY, JSON.stringify({ style: prefs.style, wrap: prefs.wrap })); } catch (e) {} }

const CHEVRON = '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 4.5l3 3 3-3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const views = [];

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
function apply(v) { v.diff.setOptions(optionsFor(v)); v.diff.rerender(); }
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
  document.getElementById('opt-wrap').checked = prefs.wrap;
  document.getElementById('opt-all').checked = prefs.all;
}
document.addEventListener('click', e => {
  const s = e.target.closest('[data-style]');
  if (s) { prefs.style = s.dataset.style; savePrefs(); syncToolbar(); applyAll(); return; }
  const f = e.target.closest('[data-files]');
  if (f) views.forEach(v => setCollapsed(v, f.dataset.files === 'collapse'));
});
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
  const { FileDiff, parseDiffFromFile } = await Promise.race([import(config.renderer), timeout]);
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
    const v = { status: f.old == null ? 'added' : f.new == null ? 'deleted' : 'modified', collapsed: false };
    v.diff = new FileDiff(optionsFor(v));
    section.replaceChildren();
    v.diff.render({ fileDiff, containerWrapper: section });
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
    config = json.dumps({"renderer": RENDERER, "context": context, "full": full})
    if not files:
        body = '<p class="empty-page">No changes.</p>'
    else:
        body = f"""<ol class="filelist">{"".join(file_row(i, f) for i, f in enumerate(files))}</ol>
<div class="toolbar">
  <div class="group">
    <div class="seg" role="group" aria-label="Layout"><button type="button" data-style="split">Split</button><button type="button" data-style="unified">Unified</button></div>
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
