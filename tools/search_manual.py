"""Search a converted manual without loading it whole.

Returns matches grouped by page, with a few lines of surrounding context.
The markdown produced by `convert_manual.py` uses `## Page N` headings, so
we can attribute every hit to a page.

Examples:
    python tools/search_manual.py "MIDI channel"
    python tools/search_manual.py -m xdj-xz "beat sync" --context 4
    python tools/search_manual.py "DMX" --max 20 --regex

Tip for agents: prefer this over reading the .md directly. The full file
is ~160 KB and will burn context. Use `--page N` to fetch one page only.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANUALS_DIR = ROOT / "docs" / "manuals"


def resolve_manual(name: str) -> Path:
    p = Path(name)
    if p.is_file():
        return p
    cand = MANUALS_DIR / (name if name.endswith(".md") else f"{name}.md")
    if cand.is_file():
        return cand
    raise SystemExit(f"manual not found: {name} (looked in {MANUALS_DIR})")


def iter_pages(path: Path):
    page_re = re.compile(r"^## Page (\d+)\s*$")
    current_page: int | None = None
    buf: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            m = page_re.match(line)
            if m:
                if current_page is not None:
                    yield current_page, buf
                current_page = int(m.group(1))
                buf = []
            else:
                if current_page is not None:
                    buf.append(line.rstrip("\n"))
        if current_page is not None:
            yield current_page, buf


def search(path: Path, pattern: re.Pattern, context: int, max_hits: int):
    hits = 0
    for page, lines in iter_pages(path):
        matched_idx = [i for i, ln in enumerate(lines) if pattern.search(ln)]
        if not matched_idx:
            continue
        print(f"\n── Page {page} ─ {len(matched_idx)} match(es) ──")
        shown: set[int] = set()
        for idx in matched_idx:
            lo = max(0, idx - context)
            hi = min(len(lines), idx + context + 1)
            for j in range(lo, hi):
                if j in shown:
                    continue
                shown.add(j)
                marker = ">" if j == idx else " "
                print(f"  {marker} {lines[j]}")
            print("  ...")
            hits += 1
            if hits >= max_hits:
                print(f"\n[reached --max {max_hits}; stopping]")
                return
    if hits == 0:
        print("(no matches)")


def show_page(path: Path, page_num: int) -> None:
    for page, lines in iter_pages(path):
        if page == page_num:
            print(f"## Page {page}\n")
            print("\n".join(lines))
            return
    raise SystemExit(f"page {page_num} not found in {path}")


def list_manuals() -> None:
    if not MANUALS_DIR.is_dir():
        print(f"(no manuals dir: {MANUALS_DIR})")
        return
    for p in sorted(MANUALS_DIR.glob("*.md")):
        size_kb = p.stat().st_size // 1024
        print(f"  {p.stem:<20} {size_kb:>5} KB  {p}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="?", help="search term (case-insensitive substring by default)")
    ap.add_argument("-m", "--manual", default="xdj-xz", help="manual stem or path (default: xdj-xz)")
    ap.add_argument("-c", "--context", type=int, default=2, help="lines of context around each hit (default 2)")
    ap.add_argument("--max", type=int, default=40, help="max hits to print (default 40)")
    ap.add_argument("--regex", action="store_true", help="treat query as a regex")
    ap.add_argument("--page", type=int, help="print one page in full and exit")
    ap.add_argument("--list", action="store_true", help="list available manuals and exit")
    args = ap.parse_args()

    if args.list:
        list_manuals()
        return 0

    path = resolve_manual(args.manual)

    if args.page is not None:
        show_page(path, args.page)
        return 0

    if not args.query:
        ap.error("query required (or pass --page N / --list)")

    flags = 0 if args.regex else re.IGNORECASE
    pattern = re.compile(args.query if args.regex else re.escape(args.query), flags)
    search(path, pattern, args.context, args.max)
    return 0


if __name__ == "__main__":
    sys.exit(main())
