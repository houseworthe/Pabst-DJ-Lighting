"""Convert a PDF manual to a page-tagged markdown file.

Page boundaries from `pdftotext` output (form-feed `\f`) become `## Page N`
headings so a searcher can return precise page references.

Usage: python tools/convert_manual.py <input.pdf> <output.md>
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


def pdf_to_pages(pdf_path: Path) -> list[str]:
    result = subprocess.run(
        ["pdftotext", "-layout", str(pdf_path), "-"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.split("\f")


def clean_page(text: str) -> str:
    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    out: list[str] = []
    blank = False
    for ln in lines:
        if not ln.strip():
            if not blank:
                out.append("")
            blank = True
        else:
            out.append(ln)
            blank = False
    return "\n".join(out)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 1
    pdf_path = Path(sys.argv[1])
    md_path = Path(sys.argv[2])
    md_path.parent.mkdir(parents=True, exist_ok=True)

    pages = pdf_to_pages(pdf_path)
    title = re.sub(r"\s+", " ", pdf_path.stem).strip()
    parts = [f"# {title}\n", f"_Source: {pdf_path.name} ({len(pages)} pages)_\n"]
    for i, page in enumerate(pages, start=1):
        body = clean_page(page)
        if not body:
            continue
        parts.append(f"\n\n## Page {i}\n\n{body}\n")
    md_path.write_text("".join(parts), encoding="utf-8")
    print(f"Wrote {md_path} ({len(pages)} pages, {md_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
