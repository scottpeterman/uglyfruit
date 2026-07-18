#!/usr/bin/env python3
"""Apply a review-first substitution map to a source tree.

Companion to the leak scanner. Reads a map of ``real => lab`` string pairs and
rewrites them across the tree. Defaults to DRY-RUN (shows a unified diff and
per-rule hit counts); nothing is written until ``--write`` is passed.

Substitutions are literal (not regex) and applied in file order, so put the
most specific strings first (FQDN before bare site code). Per-file global
replace keeps data rows and their assertions in sync automatically.

Map file syntax:
    real string => lab string      # trailing comments ok
    # full-line comments and blanks ignored
"""

from __future__ import annotations

import argparse
import difflib
import os
import sys

SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "vendor", "screenshots", ".mypy_cache", ".pytest_cache",
    "dist", "build", ".eggs",
}
SKIP_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
    ".pyc", ".so", ".o", ".bin", ".drawio", ".zip", ".gz", ".tar",
    ".woff", ".woff2", ".ttf",
}
SKIP_FILES = {"scrub_scan.py", "scrub_apply.py", ".scrub_terms", "scrub_map.txt"}


def load_map(path: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            # strip trailing comment (only after the separator, to be safe)
            if "=>" not in s:
                continue
            left, _, right = s.partition("=>")
            # allow trailing "# comment" on the value side
            right = right.split("#", 1)[0]
            real, lab = left.strip(), right.strip()
            if real:
                pairs.append((real, lab))
    return pairs


def apply_to_text(text: str, pairs: list[tuple[str, str]]) -> tuple[str, dict]:
    counts: dict[str, int] = {}
    for real, lab in pairs:
        n = text.count(real)
        if n:
            counts[real] = counts.get(real, 0) + n
            text = text.replace(real, lab)
    return text, counts


def iter_files(root: str):
    if os.path.isfile(root):
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name in SKIP_FILES or name.startswith(".scrub_terms"):
                continue
            if os.path.splitext(name)[1].lower() in SKIP_EXTS:
                continue
            yield os.path.join(dirpath, name)


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply a review-first scrub map.")
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--map", default="scrub_map.txt", help="substitution map file")
    ap.add_argument("--write", action="store_true",
                    help="actually write changes (default: dry-run diff only)")
    ap.add_argument("--quiet", action="store_true", help="counts only, no diffs")
    args = ap.parse_args()

    if not os.path.exists(args.path):
        print(f"error: path does not exist: {args.path!r}", file=sys.stderr)
        return 2
    if not os.path.exists(args.map):
        print(f"error: map file not found: {args.map!r}", file=sys.stderr)
        return 2

    pairs = load_map(args.map)
    if not pairs:
        print(f"error: no substitutions loaded from {args.map!r}", file=sys.stderr)
        return 2

    total_counts: dict[str, int] = {}
    changed_files = 0
    for path in iter_files(args.path):
        try:
            with open(path, "rb") as fh:
                if b"\x00" in fh.read(2048):
                    continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                original = fh.read()
        except (OSError, UnicodeError):
            continue

        new, counts = apply_to_text(original, pairs)
        if new == original:
            continue
        changed_files += 1
        for k, v in counts.items():
            total_counts[k] = total_counts.get(k, 0) + v

        if not args.quiet:
            rel = os.path.relpath(path, args.path if os.path.isdir(args.path)
                                  else os.path.dirname(args.path) or ".")
            diff = difflib.unified_diff(
                original.splitlines(), new.splitlines(),
                fromfile=rel, tofile=rel, lineterm="", n=0)
            for dl in diff:
                print(dl)

        if args.write:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new)

    mode = "WROTE" if args.write else "DRY-RUN (no files changed)"
    print("\n" + "=" * 52, file=sys.stderr)
    print(f"{mode}: {changed_files} file(s) affected", file=sys.stderr)
    for real, n in sorted(total_counts.items(), key=lambda x: -x[1]):
        lab = next(l for r, l in pairs if r == real)
        print(f"  {n:4d}  {real}  =>  {lab}", file=sys.stderr)
    if not args.write:
        print("\nRe-run with --write to apply.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())