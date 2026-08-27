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

BASE = os.path.dirname(__file__)

# Mirror everything written to stdout/stderr into <repo>/logs/main.log so the
# dashboard's tail (dashboard.py:LOG_PATH) sees live state regardless of how
# the shell launched us. Without this, redirecting stdout (e.g. nohup ... >>
# /tmp/x.log) leaves the dashboard parsing a stale file and showing "idle".
_LOG_FILE_PATH = os.path.abspath(os.path.join(BASE, "..", "logs", "main.log"))


class _Tee:
    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass


try:
    os.makedirs(os.path.dirname(_LOG_FILE_PATH), exist_ok=True)
    _log_fh = open(_LOG_FILE_PATH, "a", buffering=1)  # line-buffered
    sys.stdout = _Tee(sys.__stdout__, _log_fh)
    sys.stderr = _Tee(sys.__stderr__, _log_fh)
except Exception as _e:
    print(f"[main] could not open {_LOG_FILE_PATH}: {_e}", flush=True)

# Show "New Player ..." + status packet logs from prodj so we can tell whether
# decks are even being registered. Our own prints use [tag] so they still stand
# out. basicConfig binds to the *current* sys.stderr — install the Tee first so
# logger output is captured too.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
PRODJ_PATH = os.path.abspath(os.path.join(BASE, "python-prodj-link"))
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
# prodj's PDBProvider writes to "databases/player-{pn}-{slot}.pdb" relative to
# CWD ([pdbprovider.py:52](python-prodj-link/prodj/data/pdbprovider.py:52)).
# Resolve dynamically against the same cwd prodj uses, and pick the slot of
# the loaded track — pinning to "sd" silently broke USB-loaded tracks, and
# pinning to <repo>/databases/ silently broke launches from anywhere else.
def _pdb_path_for(player_number: int, slot: Any) -> Optional[str]:
    if not slot:
        return None
    return os.path.abspath(
        os.path.join("databases", f"player-{player_number}-{slot}.pdb")
    )

_state_lock = threading.Lock()
_active_deck: Optional[int] = None
_active_track_by_deck: dict[int, int] = {}
_analysis_by_track: dict[int, dict[str, Any]] = {}
_pssi_cache: dict[int, dict[str, Any]] = {}
_last_mode: Optional[str] = None
# Per-deck, the track_id we last committed to when that deck became active.
# Flap-back guard: once we've flipped to a (deck, track_id) we won't flip to
# it again during this track — a brief pause on the outgoing deck can't
# bounce us between tracks. The next flip requires a NEW track loaded.
_committed_track_by_deck: dict[int, int] = {}

# Fire mode changes this many milliseconds BEFORE the beat boundary so lights
# lead the transition instead of lagging it. Converted to a beat offset on each
# tick using the live BPM.
SCENE_LOOKAHEAD_MS = 1000

# If the active deck stops playing for this long without another deck picking
# up, blackout. Guards against the lights freezing on (say) the last outro
# mode after the DJ just... stops. Crossfades are unaffected — the hysteresis
# in on_client_change promotes the incoming deck before the timer expires.
PAUSE_BLACKOUT_S = 5.0
_pause_since: Optional[float] = None

prodj: Optional[ProDj] = None


def find_anlz_path_in_pdb(track_id: int, pdb_path: str) -> Optional[str]:
    if not os.path.exists(pdb_path):
        return None
    with open(pdb_path, "rb") as f:
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
    pdb_path = _pdb_path_for(loaded_player_number, slot)
    if not pdb_path:
        return None
    dat_path = find_anlz_path_in_pdb(track_id, pdb_path)
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
    # NFS + EXT parsing both raise on timeout / malformed data — catch here so
    # callers see None and the groove fallback in load_track engages, instead
    # of crashing the preload thread and leaving the track unanalyzed.
    try:
        ext_data = prodj.nfs.enqueue_buffer_download(client.ip_addr, slot, ext_path)
        if not ext_data:
            return None
        db = UsbAnlzDatabase()
        db.load_ext_buffer(ext_data)
        if "song_structure" not in db:
            return None
        ss = db["song_structure"]
    except Exception as e:
        print(f"[pssi] track {track_id} fetch/parse failed: {e}", flush=True)
        return None
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
    fallback = pssi is None
    if fallback:
        # No Rekordbox phrase analysis for this track. Old behavior was to skip
        # entirely so failures were obvious — but that left the dance floor
        # dark on any unanalyzed track, which is worse than a wrong scene.
        # Compromise: synthesize a single `groove` phrase covering the track.
        # We deliberately do NOT fake intro/drop/outro boundaries (the original
        # objection to a "generic template") — groove just keeps lights moving
        # on the beat. The log line below makes the fallback visible so the
        # operator knows to re-analyze the track in Rekordbox.
        bpm = metadata.get("bpm") or 0
        duration = metadata.get("duration") or 0
        end_beat = int(duration * bpm / 60) if duration and bpm else 99999
        pssi = {
            "mood": "low",
            "phrases": [{"type": 2, "start_beat": 0, "end_beat": max(end_beat, 64)}],
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
    suffix = " [groove fallback — no phrase data]" if fallback else ""
    print(f"[track] deck={deck} id={track_id} \"{track['title']}\" — "
          f"{len(analysis['phrases'])} phrases{suffix}", flush=True)
    # Full phrase timeline as JSON so the dashboard can render the scene map.
    # Kept on a separate line so [track] stays human-readable at a glance.
    print(f"[phrases] deck={deck} id={track_id} {json.dumps(analysis['phrases'])}",
          flush=True)


def section_for_beat(phrases: list[dict], beat: float) -> Optional[dict]:
    if not phrases:
        return None
    if beat <= phrases[0]["start_beat"]:
        return phrases[0]
    for p in phrases:
        if p["start_beat"] <= beat < p["end_beat"]:
            return p
    return phrases[-1]


def on_client_change(player_number: int) -> None:
    global _active_deck, _last_mode, _pause_since
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
            if track_id and track_id > 0:
                _committed_track_by_deck[player_number] = track_id
            _last_mode = None
            print(f"[deck] active -> {player_number}", flush=True)
        elif _active_deck != player_number:
            # Flap-back guard: once we've already flipped to this (deck,
            # track_id) during this track's life, don't flip back to it on a
            # momentary pause of the outgoing deck. Only a NEW track loaded on
            # this deck (different track_id) clears the guard.
            already_committed = (
                track_id
                and _committed_track_by_deck.get(player_number) == track_id
            )
            if not already_committed:
                current_cl = prodj.cl.getClient(_active_deck)
                current_playing = bool(current_cl) and getattr(
                    current_cl, "play_state", "stopped"
                ) in {"playing", "cue_play", "looping"}
                # Follow the mixer. The XDJ reports which channels are on-air
                # (channel fader up + crossfader). During a crossfade BOTH decks
                # are `is_playing`, so play-state alone can't tell which track
                # the room actually hears — that's how the lights latch the
                # wrong (outgoing) deck. Hand off when this deck is on-air and
                # the current active deck is NOT (the DJ has brought this one
                # up), or when the current deck has stopped entirely. When both
                # are on-air (mid-blend) we keep the current deck — no flap.
                # on_air defaults False, so on rigs that don't report it this
                # falls back to the old stop-based handoff (no regression).
                this_on_air = bool(getattr(client, "on_air", False))
                current_on_air = bool(current_cl) and bool(
                    getattr(current_cl, "on_air", False)
                )
                # Tempo-master flag (Pioneer "MASTER"). The XDJ-XZ doesn't
                # broadcast ch_on_air, so on_air is always False here; the
                # master flag is the reliable "which deck is driving" signal,
                # and it's exactly what the DJ means by "the master deck".
                this_master = "master" in (getattr(client, "state", None) or [])
                current_master = bool(current_cl) and (
                    "master" in (getattr(current_cl, "state", None) or [])
                )
                if (
                    (not current_playing)
                    or (this_on_air and not current_on_air)
                    or (this_master and not current_master)
                ):
                    _active_deck = player_number
                    if track_id and track_id > 0:
                        _committed_track_by_deck[player_number] = track_id
                    _last_mode = None
                    reason = ("previous deck stopped" if not current_playing
                              else "on-air handoff" if this_on_air
                              else "master handoff")
                    print(f"[deck] active -> {player_number} ({reason})", flush=True)
                else:
                    # Diagnostic: a playing deck we DIDN'T switch to, and why —
                    # reveals what flags the XDJ actually reports during a blend.
                    print(f"[deck] keep {_active_deck}; deck {player_number} "
                          f"playing but not switched "
                          f"(this master={this_master} on_air={this_on_air}; "
                          f"active master={current_master} on_air={current_on_air})",
                          flush=True)

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

    # Pause-blackout tracker — the watchdog thread fires the actual blackout.
    if _active_deck == player_number:
        if is_playing:
            _pause_since = None
        elif _pause_since is None:
            _pause_since = time.monotonic()

    if _active_deck != player_number:
        return
    if not is_playing:
        return
    if not isinstance(beat_count, int) or beat_count < 0:
        return

    analysis = _analysis_by_track.get(_active_track_by_deck.get(player_number, -1))
    if not analysis:
        return
    # Fire the next section ~SCENE_LOOKAHEAD_MS early so lights lead the beat.
    # beat_count is an integer that only ticks on whole beats, but we translate
    # the lookahead into beats via live BPM and add it before the section
    # lookup — so the last quarter-second of a phrase resolves to the NEXT
    # phrase's mode.
    bpm = getattr(client, "bpm", None) or analysis["track"].get("bpm") or 120
    beats_ahead = SCENE_LOOKAHEAD_MS / 1000.0 * float(bpm) / 60.0
    section = section_for_beat(analysis["phrases"], beat_count + beats_ahead)
    if not section:
        return
    mode = section["mode"]
    # Scene pick uses `mode` (e.g. a whole breakdown→buildup climb is one
    # held `build`), but intensity follows the finer `intensity_mode` sub-phrase
    # so the curve dips on the breakdown and ramps on the buildup within that
    # single scene. Falls back to `mode` for ordinary phrases.
    intensity_mode = section.get("intensity_mode", mode)
    # Push phrase boundaries every status packet (cheap), so the auto-curve
    # always reads the right span — even if the lookahead grabbed a future
    # phrase or PSSI shifted under us.
    direct_lights.set_intensity_phrase(
        section["start_beat"], section["end_beat"], intensity_mode
    )
    if mode != _last_mode:
        _last_mode = mode
        scene = direct_lights.apply_mode(mode)
        scene_name = (scene or {}).get("name") or (scene or {}).get("id") or "—"
        print(f"[mode] beat={beat_count} -> {mode} | scene: {scene_name}",
              flush=True)


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


def _pause_watchdog() -> None:
    """Blackout if the active deck stays paused past PAUSE_BLACKOUT_S.

    Skips if a preview is running — the editor drives lights directly and we
    shouldn't stomp on it. Resets _last_mode so the next live beat picks a
    fresh scene instead of being suppressed by the mode-change guard.
    """
    global _pause_since, _last_mode
    while not _shutdown.wait(0.5):
        since = _pause_since
        if since is None:
            continue
        if time.monotonic() - since < PAUSE_BLACKOUT_S:
            continue
        if prodj is None or _active_deck is None:
            continue
        cl = prodj.cl.getClient(_active_deck)
        if cl is not None and getattr(cl, "play_state", "stopped") in {
            "playing", "cue_play", "looping"
        }:
            _pause_since = None
            continue
        if direct_lights.status().get("preview"):
            _pause_since = None
            continue
        print(f"[pause] active deck {_active_deck} idle "
              f"{PAUSE_BLACKOUT_S:.0f}s — blackout", flush=True)
        try:
            direct_lights.blackout()
        except Exception:
            pass
        _last_mode = None
        _pause_since = None


def get_active_bpm() -> Optional[float]:
    """Live BPM for whichever deck is currently active, or None.

    Read from the prodj client object — the same field main.py uses for
    SCENE_LOOKAHEAD_MS conversion. direct_lights filters out garbage values
    and caches the last good reading, so returning None during transient
    states (deck loading, no client yet) is safe.
    """
    if prodj is None or _active_deck is None:
        return None
    try:
        cl = prodj.cl.getClient(_active_deck)
    except Exception:
        return None
    if cl is None:
        return None
    bpm = getattr(cl, "bpm", None)
    if bpm is None:
        return None
    try:
        return float(bpm)
    except (TypeError, ValueError):
        return None


def get_active_beat() -> Optional[int]:
    """Live beat count for the active deck, or None during transients."""
    if prodj is None or _active_deck is None:
        return None
    try:
        cl = prodj.cl.getClient(_active_deck)
    except Exception:
        return None
    if cl is None:
        return None
    beat = getattr(cl, "beat_count", None)
    if not isinstance(beat, int) or beat < 0:
        return None
    return beat


def main() -> None:
    global prodj
    print("[main] starting dj-lights", flush=True)
    direct_lights.set_bpm_provider(get_active_bpm)
    direct_lights.set_beat_provider(get_active_beat)
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

    threading.Thread(target=_pause_watchdog, name="pause-watchdog", daemon=True).start()

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
