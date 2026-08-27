"""Offline scan of rekordbox PSSI across the local library.

Reads every ANLZ .EXT in the rekordbox share dir, decodes PSSI via the
vendored parser, runs the live `normalize_phrases`, and reports real mode
sequences — focused on breakdown<->buildup adjacency (the combine target).

Read-only. Writes nothing. Run from repo root with the venv.
"""
from __future__ import annotations

import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "python-prodj-link"))

import logging
logging.disable(logging.CRITICAL)  # silence per-track PSSI info logs

from prodj.pdblib.usbanlz import AnlzFile, AnlzTagSongStructure  # noqa: E402
from analysis import normalize_phrases  # noqa: E402

SHARE = os.path.expanduser(
    "~/Library/Pioneer/rekordbox/share/PIONEER/USBANLZ"
)

_XOR_BASE = bytes([0xCB, 0xE1, 0xEE, 0xFA, 0xE5, 0xEE, 0xAD, 0xEE,
                   0xE9, 0xD2, 0xE9, 0xEB, 0xE1, 0xE9, 0xF3, 0xE8,
                   0xE9, 0xF4, 0xE1])


def _decode_pssi(raw: bytes):
    """Parse a PSSI body. These local exports are PLAINTEXT (not masked):
    the vendored loader's unconditional XOR corrupts them. Parse raw first;
    only fall back to the rekordbox-6 XOR unmask if the plaintext mood is
    not a valid 1/2/3."""
    import struct
    try:
        ss = AnlzTagSongStructure.parse(raw)
        if ss.mood in (1, 2, 3):
            return ss
    except Exception:
        pass
    # masked fallback
    try:
        le = struct.unpack(">H", raw[4:6])[0]
        mask = bytes([(b + le) & 0xFF for b in _XOR_BASE])
        buf = bytearray(raw)
        for i in range(6, len(buf)):
            buf[i] ^= mask[(i - 6) % len(mask)]
        ss = AnlzTagSongStructure.parse(bytes(buf))
        return ss
    except Exception:
        return None


def pssi_from_ext(path: str):
    """Return a normalize_phrases-shaped pssi dict, or None if no PSSI."""
    try:
        with open(path, "rb") as fh:
            parsed = AnlzFile.parse_stream(fh)
    except Exception:
        return None
    tag = next((t for t in parsed.tags if t.type == "PSSI"), None)
    if tag is None:
        return None
    ss = _decode_pssi(tag.content.raw_data)
    if ss is None or not ss.entries:
        return None
    entries = sorted(ss.entries, key=lambda e: e.beat)
    end_beat = ss.end_beat
    phrases = []
    for i, e in enumerate(entries):
        start = e.beat
        stop = entries[i + 1].beat if i + 1 < len(entries) else end_beat
        phrases.append({"type": e.kind, "start_beat": start, "end_beat": stop})
    return {"mood": ss.mood, "phrases": phrases}


SHARE_BASE = os.path.expanduser("~/Library/Pioneer/rekordbox/share")


def ext_files_from_playlist(name: str):
    """EXT analysis paths for every track in the named rekordbox playlist."""
    import logging as _lg
    _lg.disable(_lg.CRITICAL)
    from pyrekordbox import Rekordbox6Database
    db = Rekordbox6Database()
    pl = next((p for p in db.get_playlist()
               if (getattr(p, "Name", "") or "").strip().lower() == name.lower()),
              None)
    if pl is None:
        raise SystemExit(f"playlist {name!r} not found")
    from pyrekordbox.db6 import tables
    rows = (db.query(tables.DjmdSongPlaylist)
              .filter(tables.DjmdSongPlaylist.PlaylistID == str(pl.ID)).all())
    out = []
    for r in rows:
        c = r.Content
        adp = getattr(c, "AnalysisDataPath", None)
        if not adp:
            continue
        ext = SHARE_BASE + adp[:-4] + ".EXT" if adp.upper().endswith(".DAT") \
            else SHARE_BASE + adp
        if os.path.exists(ext):
            out.append(ext)
    return out


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg and not arg.isdigit():
        # treat as playlist name, e.g.  _scan_phrases.py All
        ext_files = ext_files_from_playlist(arg)
        print(f"playlist {arg!r}: {len(ext_files)} tracks with EXT on disk\n")
    else:
        ext_files = []
        for root, _dirs, files in os.walk(SHARE):
            for f in files:
                if f.upper().endswith(".EXT"):
                    ext_files.append(os.path.join(root, f))
        ext_files.sort()
        limit = int(arg) if arg else len(ext_files)
        ext_files = ext_files[:limit]

    tracks_total = 0
    tracks_with_pssi = 0
    mood_counter = Counter()
    transition_counter = Counter()      # (modeA -> modeB) over normalized phrases
    bd_bu_adjacent = 0                  # breakdown<->buildup either direction
    bu_then_drop = 0
    bd_then_drop = 0
    sample_sequences = []

    for path in ext_files:
        tracks_total += 1
        pssi = pssi_from_ext(path)
        if pssi is None:
            continue
        norm = normalize_phrases(pssi)
        if not norm:
            continue
        tracks_with_pssi += 1
        mood_counter[str(pssi["mood"])] += 1
        seq = [p["mode"] for p in norm]

        for a, b in zip(seq, seq[1:]):
            transition_counter[(a, b)] += 1
            if {a, b} == {"breakdown", "buildup"}:
                bd_bu_adjacent += 1
            if a == "buildup" and b == "drop":
                bu_then_drop += 1
            if a == "breakdown" and b == "drop":
                bd_then_drop += 1

        if len(sample_sequences) < 25:
            sample_sequences.append(seq)

    print(f"EXT files scanned:      {tracks_total}")
    print(f"with usable PSSI:       {tracks_with_pssi}")
    print(f"mood distribution:      {dict(mood_counter)}")
    print()
    print(f"breakdown<->buildup adjacencies (either dir): {bd_bu_adjacent}")
    print(f"buildup -> drop transitions:                  {bu_then_drop}")
    print(f"breakdown -> drop transitions:                {bd_then_drop}")
    print()
    print("Top 20 mode transitions (normalized phrases):")
    for (a, b), n in transition_counter.most_common(20):
        print(f"  {a:>10} -> {b:<10}  {n}")
    print()
    print("Sample normalized mode sequences (first 25 tracks):")
    for seq in sample_sequences:
        print("  " + " ".join(seq))


if __name__ == "__main__":
    main()
