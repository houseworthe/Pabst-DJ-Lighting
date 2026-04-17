"""
MVP2 main — single-process Pro DJ Link bridge + scene driver.

Flow:
  1. Watch XDJ via ProDJ Link.
  2. On new track loaded on the active deck: fetch Rekordbox PSSI phrase data,
     normalize into six modes (intro/groove/buildup/breakdown/drop/outro).
  3. On each beat update from the active deck: look up the current mode and
     call direct_lights.apply_mode(mode). direct_lights spawns a render thread
     that drives DMX + Govee until the mode changes.

No HTTP, no dashboard, no manual scene control — mode -> lights, that's it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import struct
import sys
import threading
import time
from typing import Any, Optional

# Show "New Player ..." + status packet logs from prodj so we can tell whether
# decks are even being registered. Our own prints use [tag] so they still stand out.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

BASE = os.path.dirname(__file__)
PRODJ_PATH = os.path.abspath(os.path.join(BASE, "..", "dj-lights", "python-prodj-link"))
if PRODJ_PATH not in sys.path:
    sys.path.insert(0, PRODJ_PATH)

import netifaces as ni

from prodj.core.prodj import ProDj  # type: ignore
from prodj.pdblib.usbanlzdatabase import UsbAnlzDatabase  # type: ignore

from bridge import ingest_track_payload
import direct_lights
import dashboard

# 0x11 = rekordbox convention. XDJ-XZ accepts us as a peer only when we
# announce as rekordbox (device_type=3) at pn=17; pn=5 (CDJ range) was silently
# rejected. See vcdj.py for the full identity fix.
VCDJ_PLAYER_NUMBER = 17
# link-local auto-assigns a new IP on each interface bringup, so pick en8's
# current address at runtime rather than hardcoding.
PREFERRED_IFACE_NAME = "en8"
PDB_PATH = os.path.abspath(os.path.join(BASE, "..", "dj-lights", "databases", "player-1-sd.pdb"))

_state_lock = threading.Lock()
_active_deck: Optional[int] = None
_active_track_by_deck: dict[int, int] = {}
_analysis_by_track: dict[int, dict[str, Any]] = {}
_pssi_cache: dict[int, dict[str, Any]] = {}
_last_mode: Optional[str] = None

prodj: Optional[ProDj] = None


def find_anlz_path_in_pdb(track_id: int) -> Optional[str]:
    if not os.path.exists(PDB_PATH):
        return None
    with open(PDB_PATH, "rb") as f:
        data = f.read()
    tid_bytes = struct.pack("<I", track_id)
    pattern = rb"/PIONEER/USBANLZ/P[0-9a-fA-F]{3}/[0-9a-fA-F]{8}/ANLZ0000\.DAT"
    pos = 0
    while True:
        pos = data.find(tid_bytes, pos)
        if pos == -1:
            return None
        m = re.search(pattern, data[pos:pos + 800])
        if m:
            return m.group(0).decode("ascii")
        pos += 1


def fetch_pssi(loaded_player_number: int, slot: Any, track_id: int) -> Optional[dict[str, Any]]:
    if track_id in _pssi_cache:
        return _pssi_cache[track_id]
    dat_path = find_anlz_path_in_pdb(track_id)
    if not dat_path or prodj is None:
        return None
    ext_path = dat_path.replace(".DAT", ".EXT")
    client = next(
        (c for c in prodj.cl.clients
         if getattr(c, "player_number", None) == loaded_player_number and hasattr(c, "ip_addr")),
        None,
    ) or next(
        (c for c in prodj.cl.clients
         if getattr(c, "player_number", 99) <= 4 and hasattr(c, "ip_addr")),
        None,
    )
    if client is None:
        return None
    ext_data = prodj.nfs.enqueue_buffer_download(client.ip_addr, slot, ext_path)
    if not ext_data:
        return None
    db = UsbAnlzDatabase()
    db.load_ext_buffer(ext_data)
    if "song_structure" not in db:
        return None
    ss = db["song_structure"]
    phrases = [
        {"type": e.get("kind"), "start_beat": e.get("beat", 0), "end_beat": 0}
        for e in ss.get("entries", [])
    ]
    end_beat = int(ss.get("end_beat", 0) or 0)
    for i, p in enumerate(phrases):
        p["end_beat"] = phrases[i + 1]["start_beat"] if i + 1 < len(phrases) else end_beat
    payload = {"mood": ss.get("mood", "low"), "phrases": phrases}
    _pssi_cache[track_id] = payload
    return payload


def load_track(deck: int, loaded_player_number: int, slot: Any, track_id: int, metadata: dict) -> None:
    pssi = fetch_pssi(loaded_player_number, slot, track_id)
    if pssi is None:
        pssi = {
            "mood": "low",
            "phrases": [
                {"type": 1, "start_beat": 0, "end_beat": 32},
                {"type": 2, "start_beat": 32, "end_beat": 96},
                {"type": 5, "start_beat": 96, "end_beat": 160},
                {"type": 7, "start_beat": 160, "end_beat": 192},
            ],
        }
    track = {
        "track_id": str(track_id),
        "title": metadata.get("title", "Unknown"),
        "artist": metadata.get("artist", "Unknown"),
        "deck": deck,
        "duration": metadata.get("duration", 0),
        "bpm": metadata.get("bpm") or 0,
    }
    analysis = ingest_track_payload(track, pssi, [])
    with _state_lock:
        _analysis_by_track[track_id] = analysis
    print(f"[track] deck={deck} id={track_id} \"{track['title']}\" — "
          f"{len(analysis['phrases'])} phrases", flush=True)
    # Full phrase timeline as JSON so the dashboard can render the scene map.
    # Kept on a separate line so [track] stays human-readable at a glance.
    print(f"[phrases] deck={deck} id={track_id} {json.dumps(analysis['phrases'])}",
          flush=True)


def section_for_beat(phrases: list[dict], beat: int) -> Optional[dict]:
    if not phrases:
        return None
    if beat <= phrases[0]["start_beat"]:
        return phrases[0]
    for p in phrases:
        if p["start_beat"] <= beat < p["end_beat"]:
            return p
    return phrases[-1]


def on_client_change(player_number: int) -> None:
    global _active_deck, _last_mode
    if prodj is None:
        return
    client = prodj.cl.getClient(player_number)
    if client is None:
        return

    play_state = getattr(client, "play_state", "stopped")
    is_playing = play_state in {"playing", "cue_play", "looping"}
    track_id = getattr(client, "track_id", None)
    beat_count = getattr(client, "beat_count", None)
    loaded_slot = getattr(client, "loaded_slot", None)
    loaded_player_number = getattr(client, "loaded_player_number", None) or player_number

    # Hysteresis on active-deck selection. During a crossfade both decks are
    # `is_playing`; without this, every status packet from the non-active deck
    # flips active_deck and lights strobe between two tracks' modes. Rule:
    #   * If nothing is active yet, the first playing deck wins.
    #   * Otherwise only switch when the current active deck has stopped
    #     playing (the DJ has faded out the outgoing track). That means lights
    #     follow the outgoing track through the crossfade until its deck is
    #     silent, then cleanly hand off.
    if is_playing:
        if _active_deck is None:
            _active_deck = player_number
            _last_mode = None
            print(f"[deck] active -> {player_number}", flush=True)
        elif _active_deck != player_number:
            current_cl = prodj.cl.getClient(_active_deck)
            current_playing = bool(current_cl) and getattr(
                current_cl, "play_state", "stopped"
            ) in {"playing", "cue_play", "looping"}
            if not current_playing:
                _active_deck = player_number
                _last_mode = None
                print(f"[deck] active -> {player_number} "
                      f"(previous deck stopped)", flush=True)

    # Pre-load analysis for ANY deck that loads a track, not just the active
    # one — so when a crossfade promotes the other deck, we already have its
    # PSSI phrase map and can compute the correct mode on the very next beat
    # instead of staying stuck on the previous track's last mode.
    if track_id and track_id > 0 and loaded_slot is not None:
        previous = _active_track_by_deck.get(player_number)
        _active_track_by_deck[player_number] = track_id
        if previous != track_id and track_id not in _analysis_by_track:
            def _load():
                meta = {}
                done = threading.Event()

                def cb(request, pn, slot_in, item_id, data):
                    if request == "metadata" and data:
                        meta.update(data)
                    done.set()

                try:
                    prodj.data.get_metadata(loaded_player_number, loaded_slot, track_id, cb)
                    done.wait(timeout=4.0)
                except Exception:
                    pass
                load_track(player_number, loaded_player_number, loaded_slot, track_id, meta)

            threading.Thread(target=_load, daemon=True).start()

    if _active_deck != player_number:
        return
    if not is_playing:
        return
    if not isinstance(beat_count, int) or beat_count < 0:
        return

    analysis = _analysis_by_track.get(_active_track_by_deck.get(player_number, -1))
    if not analysis:
        return
    section = section_for_beat(analysis["phrases"], beat_count)
    if not section:
        return
    mode = section["mode"]
    if mode != _last_mode:
        _last_mode = mode
        print(f"[mode] beat={beat_count} -> {mode}", flush=True)
        direct_lights.apply_mode(mode)


def configure_vcdj_interface() -> bool:
    if prodj is None:
        return False
    # Prefer PREFERRED_IFACE_NAME; fall back to any 169.254/16 interface.
    candidates = [PREFERRED_IFACE_NAME] + [
        iface for iface in ni.interfaces() if iface != PREFERRED_IFACE_NAME
    ]
    for iface in candidates:
        try:
            addrs = ni.ifaddresses(iface)
        except Exception:
            continue
        inet = addrs.get(ni.AF_INET, [])
        link = addrs.get(ni.AF_LINK, [])
        for entry in inet:
            ip = entry.get("addr")
            netmask = entry.get("netmask")
            if not ip or not netmask or not link:
                continue
            # Must be link-local (169.254.0.0/16) to talk to the XDJ
            if not ip.startswith("169.254."):
                continue
            mac_str = link[0].get("addr", "")
            if mac_str and mac_str.count(":") == 5:
                prodj.vcdj.set_interface_data(ip, netmask, mac_str)
                print(f"[vcdj] bound {iface} {ip}/{netmask} mac={mac_str}", flush=True)
                return True
    print("[vcdj] no link-local interface found — using default", flush=True)
    return False


_shutdown = threading.Event()


def handle_signal(signum, frame):
    _shutdown.set()


def main() -> None:
    global prodj
    print("[main] starting dj-lights mvp2", flush=True)
    direct_lights.warm_up()
    # Dashboard runs in-process so /editor's preview button can call
    # direct_lights.apply_scene_preview() directly (no IPC).
    dashboard.start_background()

    prodj = ProDj()
    prodj.set_client_keepalive_callback(on_client_change)
    prodj.set_client_change_callback(on_client_change)
    prodj.start()
    prodj.cl.auto_request_beatgrid = True
    prodj.vcdj_set_player_number(VCDJ_PLAYER_NUMBER)
    configure_vcdj_interface()
    prodj.vcdj_enable()
    print(f"[main] vcdj player {VCDJ_PLAYER_NUMBER} — waiting for decks", flush=True)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        while not _shutdown.is_set():
            time.sleep(0.25)
    finally:
        print("[main] shutting down", flush=True)
        try:
            direct_lights.blackout()
        except Exception:
            pass
        try:
            prodj.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
