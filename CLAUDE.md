# CLAUDE.md

Guidance for Claude / agents working in this repo.

## Hardware manuals

Source PDFs live in `Hardware Manuals/`. Converted, page-tagged markdown
lives in `docs/manuals/<name>.md`. Page boundaries from the original PDF
are preserved as `## Page N` headings so search hits map back to a real
page in the manual.

Currently converted:

- `docs/manuals/xdj-xz.md` — Pioneer XDJ-XZ (137 pages, ~160 KB)

### Rules — read carefully

- **Never read a manual markdown file in full.** They are 100+ KB and will
  flood the context window. Do not call `Read` without a tight `offset`/
  `limit`, do not `cat` them, do not pass them to other agents wholesale.
- Always go through `tools/search_manual.py` first.
- If you need more than a snippet, fetch a single page with
  `--page N`, not the whole file.
- When citing the manual, reference the page: e.g. "XDJ-XZ manual p.126".

### Searching a manual

```bash
# keyword search (case-insensitive substring), 2 lines of context
python3 tools/search_manual.py "beat sync"

# regex
python3 tools/search_manual.py "MIDI\s+CH\s*\d+" --regex

# pick a different manual (stem of the .md file)
python3 tools/search_manual.py -m xdj-xz "quantize"

# pull one page in full
python3 tools/search_manual.py --page 126

# list available manuals
python3 tools/search_manual.py --list
```

Useful flags: `--context N` (lines around each hit, default 2),
`--max N` (cap total hits printed, default 40).

### Adding a new manual

1. Drop the PDF in `Hardware Manuals/`.
2. Convert: `python3 tools/convert_manual.py "Hardware Manuals/<file>.pdf" docs/manuals/<stem>.md`
3. Add a bullet to the "Currently converted" list above.
4. Once verified, the source PDF can be deleted — the markdown is the
   working copy.

The converter shells out to `pdftotext` (poppler). Install with
`brew install poppler` if missing.
