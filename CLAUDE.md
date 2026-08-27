# CLAUDE.md

Guidance for Claude / agents working in this repo.

## What this repo is

Live DJ lighting driven by Pioneer Pro DJ Link. A single Python process
listens to an XDJ-XZ, maps Rekordbox phrase data to lighting modes, and
drives DMX fixtures (Enttec Open DMX USB → 2× Venue Tetra 12 + Tetra Bar)
plus Govee bulbs/strips over LAN UDP (or cloud as fallback).

```
XDJ-XZ ── ethernet (link-local) ──► main.py (vcdj as pn=17)
                                       │
                  Rekordbox PSSI (NFS) ─┤
                                       ▼
                            phrase → mode mapping
                                       │
                ┌──────────────────────┼──────────────────────┐
                ▼                                             ▼
         DMX render thread                              Govee LAN UDP
       (libftdi, 512-byte frames)                  (cloud fallback per-SKU)
```

## Layout

Everything lives under `dj-lights/`:

- `main.py` — entry point. Pro DJ Link client + state machine. Tees stdout
  to `logs/main.log` for the dashboard to tail.
- `dashboard.py` — sidecar HTTP server on **:8787**. Live monitor + scene
  editor. Runs in-process as a background thread by default; can also be
  run standalone.
- `scene_engine.py` — layered renderer (chase, bar_chase, wash_*, pulse,
  strobe). Reads live BPM via `direct_lights.set_bpm_provider`.
- `direct_lights.py` — mode → render thread → DMX + Govee.
- `dmx_controller.py` — Enttec Open DMX USB driver (libftdi, FTDI break
  via baud-rate switching).
- `govee_lan.py` — in-process Govee client. LAN UDP preferred; cloud
  fallback per-device.
- `analysis.py` / `bridge.py` / `scenes_store.py` — PSSI → mode mapping,
  per-track cache (`cache/`), scenes.json CRUD.
- `python-prodj-link/` — vendored Pioneer Pro DJ Link client. Patched for
  XDJ-XZ peer registration — see HANDOFF.md before editing.

The legacy audio-reactive `lightd` stack (the old daemon, `lightctl` CLI,
per-category scene catalogs `scenes_v2.py`, `beat-link.jar` prototype,
etc.) was removed during consolidation but is preserved in git history at
commit `ed6eb7e` and earlier. Restore individual files with
`git show ed6eb7e:dj-lights/<file>`, or the whole tree with
`git checkout ed6eb7e -- dj-lights/` into a working dir.

## Running

Always use the project venv (`.venv/`, Python 3.13). Don't `pip install`
into the user's global Python.

```bash
./.venv/bin/python dj-lights/main.py
```

The dashboard starts in-process; open `http://localhost:8787`.

**Launch from the repo root, not from inside `dj-lights/`.** `main.py`
resolves `databases/player-{pn}-{slot}.pdb` relative to CWD (vendored
prodj's PDBProvider does the same). Running from the repo root reads/
writes `<repo>/databases/`, which is canonical. A stale duplicate sits at
`dj-lights/databases/` from before the consolidation — `cd dj-lights &&
python main.py` would silently use *that* dir and miss recently-cached
PDBs. Don't.

Only **one** `main.py` at a time — ProDJ Link binds UDP 50000–50002. A
second instance silently fails or fights the first for status packets.

`./SETUP.md` is the source of truth for `brew install libftdi portaudio`,
venv creation, and the verify-imports one-liner. Re-read it if anything
imports-broken.

## Demoing the lights (`dj-lights/demo.py`)

This is the agent-facing "show me the lights" tool. **No DJ gear required** —
it drives the *same* renderer the live set uses (`direct_lights` →
`SceneEngine`), so what it shows is exactly what plays during a track. To
demo anything, read this section; you don't need to understand the ProDJ Link
stack.

```bash
# from the repo root, with the venv:
./.venv/bin/python dj-lights/demo.py            # full soundcheck (~1 min)
./.venv/bin/python dj-lights/demo.py list       # every fixture/primitive/category/scene
./.venv/bin/python dj-lights/demo.py scene Meteor       # one named scene (id or name)
./.venv/bin/python dj-lights/demo.py category drop      # all scenes in a category, w/ its intensity curve
./.venv/bin/python dj-lights/demo.py layer bar_chase    # one layer-type primitive in isolation
./.venv/bin/python dj-lights/demo.py fixture wash_1     # one physical fixture (wiring/addressing check)
```

The no-arg **soundcheck** sweeps every physical fixture (each Tetra 12 wash,
each Tetra Bar zone, Govee) then every DMX layer primitive, ~2.5 s each.

Knobs: `--secs N` (dwell per item), `--color RRGGBB` (fixture-check color),
`--bpm N` (BPM-locked rates, local only), `--intensity 0..1` (pin intensity),
`--hold` (leave the last look on instead of blacking out), `--gap MS`.

**It picks how to reach the lights automatically** (`--driver auto`):

- **Dashboard up on :8787** → routes through the dashboard's preview API. No
  fight for the DMX device, so this is **safe to run during a live set** (each
  item hot-swaps as a preview; the set's live mode resumes when the demo ends).
- **Nothing on :8787** → drives `direct_lights` in-process and owns DMX +
  Govee directly. `main.py` must **not** be running (it holds the FTDI
  device). If DMX is unavailable the demo warns and runs Govee-only.

Force a transport with `--driver local|http` if needed.

The fixture, primitive, and category lists are derived live from
`scene_engine.DMX_LAYER_RENDERERS` and `scenes.json`, so new layer types and
scenes appear in `demo.py` automatically — no edit needed when you add one.

## Load-bearing ProDJ Link invariants

The XDJ only unicasts status to us when we obey a specific handshake. If
status packets stop ~60 s after startup while our vcdj keeps ticking
outbound, one of these regressed. The canonical write-up is in
[HANDOFF.md](HANDOFF.md) and `dj-lights/README.md`; the short list:

1. Announce as **rekordbox, player_number 17 (0x11)**,
   `device_type=rekordbox`, `is_rekordbox=True`. CDJ-range pn=5 is silently
   dropped by the XDJ-XZ.
2. Send **DJM-class status (type 0x29)** every 200 ms, `u2=1`,
   `remaining_bytes=0x14`, unicast from source port 50002 to each peer IP.
3. **Reply to every `rekordbox_hello` (0x10) with a `rekordbox_reply` (0x11)**.
   Look for `Sent first rekordbox_reply to 169.254.x.x` in the log — if
   every `rekordbox_hello` line says `ignoring`, the interceptor in
   `dj-lights/python-prodj-link/prodj/core/prodj.py` isn't firing.
4. **Pin the send socket** via `IP_BOUND_IF` on macOS (two `169.254/16`
   routes are common across en0/en8). `vcdj.set_interface_data` does this;
   `configure_vcdj_interface()` in `main.py` picks the right NIC.

Don't refactor vcdj identity, the rekordbox-reply path, or the
`IP_BOUND_IF` plumbing without a hardware test. There is no useful unit
test for "XDJ trusts us as a peer."

## Reading logs

`main.py` tees stdout/stderr to `logs/main.log` (line-buffered, inode-aware
tail in `dashboard.py`). Operator-visible state is in tagged prints:
`[main] [vcdj] [deck] [track] [phrases] [mode] [dmx] [govee] [pause] [pssi]`.
Keep that vocabulary — the dashboard parses it and so does the eye.

A healthy first ~10 s after `main.py` start, in order: prodj sockets
bound → `[vcdj] bound en8 …` → `New Player 1: XDJ-XZ` → status packets →
`Sent first rekordbox_reply` → `[track] … N phrases` → `[mode] beat=N`.

## Editing rules of thumb

- **Mode dispatch lives in `main.py::on_client_change`.** Active-deck
  hysteresis, dual-deck PSSI preload, lookahead, and pause-blackout all
  hang off this function. Read the comments before touching it — each
  guard exists to fix a specific live-set bug.
- **Scene rendering lives in `dj-lights/scene_engine.py` and
  `direct_lights.py`.** Layer types: chase, bar_chase, wash_*, pulse,
  strobe (see recent commits and `scenes.json`). BPM-locked rates read
  the live BPM via `direct_lights.set_bpm_provider`.
- **Govee state**: `dj-lights/govee_lan.py`. LAN UDP whenever a device IP
  is cached in `~/.config/govee/devices.json`; cloud otherwise. Error
  messages live under `msg` (control) or `message` (device list) — read
  both.
- **Scenes config**: `dj-lights/scenes.json`, edited via the dashboard's
  `/editor` or by hand.
- **Don't add features uninvited.** `TODO.md` lists the known wants — if
  a change isn't in there or in the active conversation, ask first.

## Hardware reference

- DMX wiring, addressing, and channel maps: `dj-lighting-system.md` (the
  software section there describes the archived `lightd` stack and is
  retained for historical context only).
- Pioneer XDJ-XZ manual: see "Hardware manuals" below — use the search
  tool, don't slurp the file.

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
