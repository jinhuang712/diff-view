---
name: diff-view
description: Render file changes as a side-by-side diff page and open it in the browser, so the user can review them. Use after editing files the user will review (docs, code, config), before asking for approval, and whenever the user asks to see a diff, a before/after or a side-by-side view.
---

# diff-view

Turns a set of file changes into one HTML page: a list of the changed files with their line counts, then each file's diff with the old text on the left and the new text on the right. Lines that leave are red, lines that arrive are green, the words that changed inside a line are marked, and the code is syntax-highlighted. Unchanged stretches fold to a bar that expands on click. The reader can switch between split and unified layouts, turn line wrapping off, show every line, and collapse files. File headers stay pinned while scrolling. The page follows the system's light or dark theme.

The page draws the diffs with [@pierre/diffs](https://diffs.com) (Apache-2.0), loaded from esm.sh at a pinned version, so it needs a network connection when opened. Without one, it shows a plain unified diff of the same changes.

Use it so the user reviews changes the way they read them best: whole, side by side, in a browser, instead of a diff in the terminal.

## When to use it

- After you edit files the user will review, before you ask for approval or commit.
- When you propose changes you have not applied yet: write the proposal into copies, then compare the originals with the copies.
- When the user asks to see a diff, a before and after, or a side-by-side view.

## How to run it

The script is `scripts/diff_view.py` in this skill's folder. It needs only Python's standard library.

| Situation | Command |
|---|---|
| Changes in a git repo, against the last commit | `python3 scripts/diff_view.py --git --repo <repo> --open` |
| Against an older ref, such as a tag | `python3 scripts/diff_view.py --git --repo <repo> --base v0.1.0 --open` |
| Only some paths | add the paths at the end: `... --git --repo <repo> README.md docs/` |
| Proposed edits kept in copies | `python3 scripts/diff_view.py --pair <original> <copy> --pair <original> <copy> --open` |
| Two folders | `python3 scripts/diff_view.py --dirs <before> <after> --open` |

Options:

| Option | Effect |
|---|---|
| `--title TEXT`, `--subtitle TEXT` | Name the page after the change, for example "README rewrite" |
| `--context N` | Unchanged lines shown around each change (default 3) |
| `--full` | Show every line, with no folding |
| `-o FILE` | Write the page there. By default it goes to a new file in the temp folder |
| `--open` | Open the page in the default browser |

`--git` includes untracked files, so new files show as added. Binary files are listed but not shown.

## What to tell the user

The script prints the page's `file://` address. Put that address in your reply, with one line per file on what changed and why, so the page and the reply read together.

Open the page with `--open` when the user asked to review changes in the browser. When a project's own rules say not to open windows or steal focus, give the address instead and let the user open it.
