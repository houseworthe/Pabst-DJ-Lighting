"""Live dashboard + scene editor for dj-lights.

Two pages served on http://localhost:8787:

  /        — live monitor (tails logs/main.log for status)
  /editor  — scene editor (CRUD on scenes.json + live preview)

Runs inside main.py as a background thread so the editor's Play button can
call direct_lights.apply_scene_preview() directly, no IPC. When main.py is
not running, run this module standalone: only the monitor page needs main,
the editor works fine (preview will be a no-op until direct_lights is reachable).
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

BASE = Path(__file__).resolve().parent
LOG_PATH = BASE.parent / "logs" / "main.log"
PORT = int(os.environ.get("DJ_DASHBOARD_PORT", "8787"))

# direct_lights + scenes_store are imported lazily so this module can also run
# standalone (e.g. to edit scenes when main.py is offline).
_direct_lights = None
_scenes_store = None


def _lazy_direct_lights():
    global _direct_lights
    if _direct_lights is None:
        try:
            sys.path.insert(0, str(BASE))
            import direct_lights  # type: ignore
            _direct_lights = direct_lights
        except Exception as e:
            print(f"[dashboard] direct_lights unavailable ({e}) — preview disabled", flush=True)
            _direct_lights = False
    return _direct_lights or None


def _lazy_store():
    global _scenes_store
    if _scenes_store is None:
        sys.path.insert(0, str(BASE))
        from scenes_store import get_store  # type: ignore
        _scenes_store = get_store()
    return _scenes_store


# --- Build two-step runner ---------------------------------------------------
# Plays one build scene through the live breakdown→buildup intensity curve as a
# timed preview (no DJ gear): stage 1 follows the breakdown curve (DMX add-ons
# gated off), stage 2 follows the buildup curve (add-ons accrete + ramp to the
# drop). Runs in a daemon thread so the HTTP request returns immediately.
_build_thread: Optional[threading.Thread] = None
_build_stop = threading.Event()
_build_status: dict = {"running": False, "scene": None, "stage": None}


def _build_ramp(dl, lo: float, hi: float, secs: float) -> bool:
    """Drive manual intensity lo→hi over `secs`. Returns False if interrupted."""
    steps = max(1, int(secs / 0.3))
    for i in range(steps):
        if _build_stop.is_set():
            return False
        dl.set_intensity_manual(lo + (hi - lo) * (i / max(1, steps - 1)))
        if _build_stop.wait(secs / steps):
            return False
    return True


def _run_build_two_step(scene: dict, stage_secs: float) -> None:
    dl = _lazy_direct_lights()
    if dl is None:
        return
    bd = dl._INTENSITY_CURVE_BY_MODE["breakdown"]
    bu = dl._INTENSITY_CURVE_BY_MODE["buildup"]
    try:
        dl.apply_scene_preview(scene)
        _build_status.update(scene=scene.get("name"), stage="breakdown")
        if not _build_ramp(dl, bd[0], bd[1], stage_secs):
            return
        _build_status.update(stage="buildup")
        _build_ramp(dl, bu[0], bu[1], stage_secs)
    finally:
        dl.set_intensity_manual(None)
        try:
            dl.stop_preview(resume_mode=True)
        except Exception:
            pass
        _build_status.update(running=False, stage=None)


def _start_build(scene: dict, stage_secs: float) -> None:
    global _build_thread
    _build_stop.set()
    if _build_thread and _build_thread.is_alive():
        _build_thread.join(timeout=1.0)
    _build_stop.clear()
    _build_status.update(running=True, scene=scene.get("name"), stage="breakdown")
    _build_thread = threading.Thread(
        target=_run_build_two_step, args=(scene, stage_secs),
        name="dashboard-build", daemon=True,
    )
    _build_thread.start()


_state_lock = threading.Lock()
_state: dict[str, Any] = {
    "deck": None,
    # Per-deck track analysis. Keyed by deck number; the dashboard surfaces the
    # ACTIVE deck's entry so the phrase timeline can't drift onto a track that
    # isn't currently driving the lights.
    "tracks_by_deck": {},
    "beat": None,
    "mode": None,
    "scene": None,
    "last_beat_ts": None,
    "status_packets": 0,
    "vcdj_ticks": 0,
    "govee_errors": [],
    "dmx_open": False,
    "vcdj_bound": None,
    "started_at": None,
}

RE_LOG_PREFIX = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ [A-Z]+ [^:]+: (.*)$"
)
RE_STARTED = re.compile(r"^\[main\] starting")
RE_VCDJ = re.compile(r"^\[vcdj\] bound (\S+) (\S+) mac=(\S+)")
RE_DECK = re.compile(r"^\[deck\] active -> (\d+)")
RE_TRACK = re.compile(
    r'^\[track\] deck=(\d+) id=(\d+) "([^"]*)" \S+ (\d+) phrases'
)
RE_PHRASES = re.compile(r"^\[phrases\] deck=(\d+) id=(\d+) (.+)$")
RE_MODE = re.compile(r"^\[mode\] beat=(\d+) -> (\w+)(?: \| scene: (.+))?$")
RE_STATUS = re.compile(r"STATUS PACKET deck (\d+): beat_count \S+ -> (\d+)")
RE_TICK = re.compile(r"vcdj status tick #(\d+)")
RE_GOVEE = re.compile(r"^\[govee\] cloud (\S+) -> (\d+) (.*)$")
RE_DMX = re.compile(r"^\[dmx\] opened")


def _reset_on_main_restart() -> None:
    _state["tracks_by_deck"] = {}
    _state["mode"] = None
    _state["scene"] = None
    _state["beat"] = None
    _state["status_packets"] = 0
    _state["vcdj_ticks"] = 0
    _state["dmx_open"] = False
    _state["govee_errors"] = []


def apply_line(line: str) -> None:
    body = line.rstrip("\n")
    m = RE_LOG_PREFIX.match(body)
    if m:
        body = m.group(1)
    now = time.time()

    with _state_lock:
        if RE_STARTED.match(body):
            _state["started_at"] = now
            _reset_on_main_restart()
            return
        if m := RE_VCDJ.match(body):
            _state["vcdj_bound"] = {
                "iface": m.group(1), "ip": m.group(2), "mac": m.group(3),
            }
            return
        if m := RE_DECK.match(body):
            _state["deck"] = int(m.group(1))
            return
        if m := RE_TRACK.match(body):
            deck, tid, title, n = m.groups()
            deck_i = int(deck)
            _state["tracks_by_deck"][deck_i] = {
                "deck": deck_i, "id": tid, "title": title,
                "phrase_count": int(n), "phrases": [],
            }
            # Only clear mode/scene if THIS deck is active — a track load on
            # the other deck shouldn't blank the badge for the deck currently
            # driving lights.
            if _state["deck"] == deck_i:
                _state["mode"] = None
                _state["scene"] = None
            return
        if m := RE_PHRASES.match(body):
            deck, tid, payload = m.groups()
            deck_i = int(deck)
            try:
                phrases = json.loads(payload)
            except json.JSONDecodeError:
                return
            entry = _state["tracks_by_deck"].get(deck_i)
            if entry and entry["id"] == tid:
                entry["phrases"] = phrases
            return
        if m := RE_MODE.match(body):
            _state["beat"] = int(m.group(1))
            _state["mode"] = m.group(2)
            _state["scene"] = m.group(3)  # None on legacy lines
            return
        if m := RE_STATUS.search(body):
            _state["status_packets"] += 1
            _state["beat"] = int(m.group(2))
            _state["last_beat_ts"] = now
            return
        if m := RE_TICK.search(body):
            _state["vcdj_ticks"] = int(m.group(1))
            return
        if m := RE_GOVEE.match(body):
            _state["govee_errors"].append({
                "device": m.group(1), "code": m.group(2),
                "msg": m.group(3), "ts": now,
            })
            _state["govee_errors"] = _state["govee_errors"][-20:]
            return
        if RE_DMX.match(body):
            _state["dmx_open"] = True
            return


def tail_log() -> None:
    while True:
        if not LOG_PATH.exists():
            time.sleep(0.5)
            continue
        try:
            with LOG_PATH.open() as f:
                start_inode = os.fstat(f.fileno()).st_ino
                for line in f:
                    apply_line(line)
                while True:
                    line = f.readline()
                    if line:
                        apply_line(line)
                        continue
                    try:
                        stat = LOG_PATH.stat()
                    except FileNotFoundError:
                        break
                    if stat.st_ino != start_inode or stat.st_size < f.tell():
                        break
                    time.sleep(0.1)
        except Exception:
            time.sleep(1)


# Descriptor of every DMX target and Govee SKU available to the editor.
# The UI consumes this via /api/fixtures to build dropdowns dynamically.
FIXTURES = {
    "dmx_targets": [
        {"id": "all", "name": "All fixtures (wash + bar)"},
        {"id": "wash", "name": "Tetra 12 wash pars (x2, both sides)"},
        {"id": "wash_1", "name": "Tetra 12 wash — left (d.001)"},
        {"id": "wash_2", "name": "Tetra 12 wash — right (d.007)"},
        {"id": "bar_all", "name": "Tetra Bar — all 4 bulbs"},
        {"id": "bar_z1", "name": "Tetra Bar — bulb 1 (far left)"},
        {"id": "bar_z2", "name": "Tetra Bar — bulb 2 (center-left)"},
        {"id": "bar_z3", "name": "Tetra Bar — bulb 3 (center-right)"},
        {"id": "bar_z4", "name": "Tetra Bar — bulb 4 (far right)"},
    ],
    "categories": ["intro", "groove", "build", "breakdown", "drop", "outro"],
    # Two Govee groups and one DMX group. The editor shows each as a togglable
    # card; the underlying scene.layers is projected to/from these on the fly.
    "device_groups": [
        {
            "id": "cob",
            "sku": "H61E5",
            "name": "COB strips",
            "subtitle": "Govee Glide / tall light bars",
        },
        {
            "id": "bulbs",
            "sku": "H6010",
            "name": "Smart bulbs",
            "subtitle": "Govee color bulbs (spheres)",
        },
    ],
    "dmx_group": {
        "name": "DMX fixtures",
        "subtitle": "Tetra 12 wash (×2) + Tetra Bar (4 zones)",
    },
    "dmx_effects": [
        {
            "type": "solid",
            "label": "Solid color",
            "description": "Single color held indefinitely.",
            "params": [
                {"key": "target", "kind": "target", "label": "target"},
                {"key": "rgb", "kind": "color", "label": "color"},
                {"key": "amber", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "amber", "unit": "/ 255"},
                {"key": "dim", "kind": "int", "min": 0, "max": 255, "default": 128,
                 "label": "brightness", "unit": "/ 255"},
            ],
        },
        {
            "type": "breathe",
            "label": "Breathe",
            "description": "Dimmer pulses up and down. Add multiple colors to have them cycle one per breath.",
            "params": [
                {"key": "target", "kind": "target", "label": "target"},
                {"key": "colors", "kind": "color_list", "label": "colors"},
                {"key": "amber", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "amber", "unit": "/ 255"},
                {"key": "hz", "kind": "float", "min": 0.01, "max": 5.0, "step": 0.01,
                 "default": 0.25, "label": "speed", "unit": "Hz"},
                {"key": "dim_min", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "brightness min", "unit": "/ 255"},
                {"key": "dim_max", "kind": "int", "min": 0, "max": 255, "default": 128,
                 "label": "brightness max", "unit": "/ 255"},
            ],
        },
        {
            "type": "chase",
            "label": "Chase (wash ↔ bar)",
            "description": "Tetra 12 wash and Tetra Bar alternate. With two colors, wash/bar swap colors on each toggle. With one color, the off group goes dark.",
            "params": [
                {"key": "colors", "kind": "color_list", "label": "colors",
                 "default": [[255, 255, 255]]},
                {"key": "amber", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "amber", "unit": "/ 255"},
                {"kind": "rate", "label": "speed",
                 "hz_min": 0.1, "hz_max": 20.0, "hz_step": 0.1, "hz_default": 1.0,
                 "hz_unit": "toggles/s",
                 "beats_chips": [0.25, 0.5, 1, 2, 4, 8], "beats_default": 1},
                {"key": "dim_active", "kind": "int", "min": 0, "max": 255, "default": 128,
                 "label": "on brightness", "unit": "/ 255"},
                {"key": "dim_rest", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "off brightness", "unit": "/ 255"},
            ],
        },
        {
            "type": "bar_chase",
            "label": "Bar chase (per-zone sweep)",
            "description": "A single bright zone walks across bulbs 1→4. With multiple colors, each zone-step picks the next color. Add a tail for a comet trail.",
            "params": [
                {"key": "colors", "kind": "color_list", "label": "colors",
                 "default": [[255, 80, 0]]},
                {"key": "amber", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "amber", "unit": "/ 255"},
                {"kind": "rate", "label": "speed",
                 "hz_min": 0.5, "hz_max": 20.0, "hz_step": 0.1, "hz_default": 4.0,
                 "hz_unit": "zones/s",
                 "beats_chips": [0.25, 0.5, 1, 2, 4, 8], "beats_default": 1},
                {"key": "direction", "kind": "select", "label": "direction",
                 "default": "wrap",
                 "options": [
                     {"value": "wrap", "label": "wrap (1→2→3→4→1)"},
                     {"value": "pingpong", "label": "pingpong (1→2→3→4→3→2→1)"},
                 ]},
                {"key": "tail", "kind": "int", "min": 0, "max": 3, "default": 0,
                 "label": "tail length", "unit": "zones"},
                {"key": "dim_active", "kind": "int", "min": 0, "max": 255, "default": 200,
                 "label": "head brightness", "unit": "/ 255"},
                {"key": "dim_rest", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "rest brightness", "unit": "/ 255"},
                {"key": "wash", "kind": "select", "label": "wash",
                 "default": "off",
                 "options": [
                     {"value": "off", "label": "off (Tetra 12s dark)"},
                     {"value": "match", "label": "match head color"},
                     {"value": "include", "label": "include in chase (5th position)"},
                 ]},
            ],
        },
        {
            "type": "bar_shoot",
            "label": "Bar shootout (launch + recoil)",
            "description": "Light shoots out across the 4 bar zones super fast, then retracts by pulling the brightness back. Pick a direction and whether the retract recoils or fades. Multiple colors fire a different color each launch.",
            "params": [
                {"key": "colors", "kind": "color_list", "label": "colors",
                 "default": [[255, 80, 0], [0, 120, 255]]},
                {"key": "mode", "kind": "select", "label": "direction",
                 "default": "out",
                 "options": [
                     {"value": "out", "label": "out (left→right, recoil right→left)"},
                     {"value": "in", "label": "in (right→left, recoil left→right)"},
                     {"value": "center", "label": "center (middle→edges)"},
                     {"value": "split", "label": "split (edges→middle)"},
                 ]},
                {"key": "retract", "kind": "select", "label": "retract style",
                 "default": "recede",
                 "options": [
                     {"value": "recede", "label": "recede (light recoils back)"},
                     {"value": "fade", "label": "fade (uniform brightness drain)"},
                 ]},
                {"key": "shoot_ms", "kind": "int", "min": 10, "max": 2000, "default": 120,
                 "label": "shoot time", "unit": "ms"},
                {"key": "hold_ms", "kind": "int", "min": 0, "max": 2000, "default": 40,
                 "label": "hold time", "unit": "ms"},
                {"key": "retract_ms", "kind": "int", "min": 10, "max": 3000, "default": 220,
                 "label": "retract time", "unit": "ms"},
                {"key": "gap_ms", "kind": "int", "min": 0, "max": 5000, "default": 180,
                 "label": "gap before relaunch", "unit": "ms"},
                {"key": "amber", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "amber", "unit": "/ 255"},
                {"key": "dim_active", "kind": "int", "min": 0, "max": 255, "default": 255,
                 "label": "peak brightness", "unit": "/ 255"},
                {"key": "dim_rest", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "rest brightness", "unit": "/ 255"},
                {"key": "strobe", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "strobe", "unit": "/ 255"},
                {"key": "wash", "kind": "select", "label": "wash",
                 "default": "off",
                 "options": [
                     {"value": "off", "label": "off (Tetra 12s dark)"},
                     {"value": "match", "label": "match (wash follows the launch)"},
                 ]},
            ],
        },
        {
            "type": "bar_flow",
            "label": "Bar flow (smooth glide)",
            "description": "A soft blob of light glides continuously across the 4 bar zones, crossfading between them instead of snapping — the smooth 'liquid' movement. Slow it down for a calm drifting glow; widen it to light more zones at once. Multiple colors advance one per pass.",
            "params": [
                {"key": "colors", "kind": "color_list", "label": "colors",
                 "default": [[0, 120, 255], [255, 0, 180]]},
                {"key": "amber", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "amber", "unit": "/ 255"},
                {"kind": "rate", "label": "speed",
                 "hz_min": 0.1, "hz_max": 8.0, "hz_step": 0.05, "hz_default": 0.7,
                 "hz_unit": "zones/s",
                 "beats_chips": [1, 2, 4, 8, 16], "beats_default": 4},
                {"key": "direction", "kind": "select", "label": "direction",
                 "default": "wrap",
                 "options": [
                     {"value": "wrap", "label": "wrap (loops 1→4→1, seamless)"},
                     {"value": "pingpong", "label": "pingpong (bounces end to end)"},
                 ]},
                {"key": "width", "kind": "float", "min": 0.3, "max": 3.0, "step": 0.1,
                 "default": 1.2, "label": "glow width", "unit": "zones"},
                {"key": "dim_active", "kind": "int", "min": 0, "max": 255, "default": 255,
                 "label": "peak brightness", "unit": "/ 255"},
                {"key": "dim_rest", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "rest brightness", "unit": "/ 255"},
                {"key": "strobe", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "strobe", "unit": "/ 255"},
                {"key": "wash", "kind": "select", "label": "wash",
                 "default": "off",
                 "options": [
                     {"value": "off", "label": "off (Tetra 12s dark)"},
                     {"value": "match", "label": "match (wash follows the glow)"},
                     {"value": "include", "label": "include (flow across the whole room)"},
                 ]},
            ],
        },
        {
            "type": "wash_pingpong",
            "label": "Wash ping-pong",
            "description": "Hard ping-pong between left and right wash pars. Multiple colors cycle one per toggle.",
            "params": [
                {"key": "colors", "kind": "color_list", "label": "colors",
                 "default": [[255, 0, 0], [0, 0, 255]]},
                {"key": "amber", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "amber", "unit": "/ 255"},
                {"key": "rate_hz", "kind": "float", "min": 0.1, "max": 20.0, "step": 0.1,
                 "default": 2.0, "label": "speed", "unit": "toggles/s"},
                {"key": "dim_active", "kind": "int", "min": 0, "max": 255, "default": 200,
                 "label": "on brightness", "unit": "/ 255"},
                {"key": "dim_rest", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "off brightness", "unit": "/ 255"},
                {"key": "strobe", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "strobe", "unit": "/ 255"},
            ],
        },
        {
            "type": "wash_chase",
            "label": "Wash chase (crossfade)",
            "description": "Smooth sinusoidal crossfade between left and right wash. Color advances per fade — pair colors swap sides each round trip.",
            "params": [
                {"key": "colors", "kind": "color_list", "label": "colors",
                 "default": [[255, 80, 0], [0, 80, 255]]},
                {"key": "amber", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "amber", "unit": "/ 255"},
                {"key": "hz", "kind": "float", "min": 0.05, "max": 5.0, "step": 0.05,
                 "default": 0.5, "label": "speed", "unit": "Hz"},
                {"key": "dim_min", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "brightness min", "unit": "/ 255"},
                {"key": "dim_max", "kind": "int", "min": 0, "max": 255, "default": 200,
                 "label": "brightness max", "unit": "/ 255"},
                {"key": "strobe", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "strobe", "unit": "/ 255"},
            ],
        },
        {
            "type": "dual_wash",
            "label": "Dual wash (asymmetric)",
            "description": "Static split: left and right wash hold different colors. Use as a base under a busy chase, or standalone as a room-color split.",
            "params": [
                {"key": "rgb_left", "kind": "color", "label": "left color (wash 1)",
                 "default": [255, 0, 180]},
                {"key": "rgb_right", "kind": "color", "label": "right color (wash 2)",
                 "default": [0, 180, 255]},
                {"key": "amber", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "amber", "unit": "/ 255"},
                {"key": "dim", "kind": "int", "min": 0, "max": 255, "default": 200,
                 "label": "brightness", "unit": "/ 255"},
                {"key": "strobe", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "strobe", "unit": "/ 255"},
            ],
        },
        {
            "type": "pulse",
            "label": "Pulse (color cycle)",
            "description": "Flashes on/off, cycling through the colors list one per cycle.",
            "params": [
                {"key": "target", "kind": "target", "label": "target"},
                {"key": "colors", "kind": "color_list", "label": "colors"},
                {"key": "amber", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "amber", "unit": "/ 255"},
                {"key": "on_ms", "kind": "int", "min": 10, "max": 5000, "default": 80,
                 "label": "on time", "unit": "ms"},
                {"kind": "pulse_period", "label": "off time / period",
                 "off_min": 0, "off_max": 5000, "off_default": 40, "off_unit": "ms",
                 "beats_chips": [0.25, 0.5, 1, 2, 4, 8], "beats_default": 1},
                {"key": "dim", "kind": "int", "min": 0, "max": 255, "default": 128,
                 "label": "brightness", "unit": "/ 255"},
            ],
        },
        {
            "type": "random_flash",
            "label": "Random flash",
            "description": "Sporadic bright flashes at random intervals, occasionally doubled. Dark between flashes.",
            "params": [
                {"key": "target", "kind": "target", "label": "target"},
                {"key": "rgb", "kind": "color", "label": "color", "default": [255, 255, 255]},
                {"key": "amber", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "amber", "unit": "/ 255"},
                {"key": "dim", "kind": "int", "min": 0, "max": 255, "default": 80,
                 "label": "brightness", "unit": "/ 255"},
                {"key": "min_gap_s", "kind": "float", "min": 0.2, "max": 30.0, "step": 0.1,
                 "default": 4.0, "label": "min gap", "unit": "s"},
                {"key": "max_gap_s", "kind": "float", "min": 0.3, "max": 60.0, "step": 0.1,
                 "default": 8.0, "label": "max gap", "unit": "s"},
                {"key": "double_chance", "kind": "int", "min": 0, "max": 100, "default": 30,
                 "label": "double chance", "unit": "%"},
                {"key": "flash_ms", "kind": "int", "min": 20, "max": 500, "default": 90,
                 "label": "flash length", "unit": "ms"},
            ],
        },
        {
            "type": "popcorn",
            "label": "Popcorn (random pop + decay)",
            "description": "Each fixture pops to max brightness at random and decays back to a baseline. Tunable peak, baseline, pop rate, and decay time. With multiple colors, each pop picks a fresh color.",
            "params": [
                {"key": "target", "kind": "select", "label": "scope",
                 "default": "all",
                 "options": [
                     {"value": "all", "label": "all (2 wash + 4 bar zones, 6 units)"},
                     {"value": "wash", "label": "wash only (2 units)"},
                     {"value": "bar_all", "label": "bar zones only (4 units)"},
                 ]},
                {"key": "mode", "kind": "select", "label": "mode",
                 "default": "solo",
                 "options": [
                     {"value": "solo", "label": "solo (one pop at a time, no overlap)"},
                     {"value": "overlap", "label": "overlap (multiple pops can be active)"},
                 ]},
                {"key": "colors", "kind": "color_list", "label": "colors",
                 "default": [[255, 80, 0], [255, 0, 200], [0, 180, 255]]},
                {"key": "amber", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "amber", "unit": "/ 255"},
                {"key": "max_brightness", "kind": "int", "min": 0, "max": 255, "default": 255,
                 "label": "peak brightness", "unit": "/ 255"},
                {"key": "min_brightness", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "baseline brightness", "unit": "/ 255"},
                {"key": "flash_rate_hz", "kind": "float", "min": 0.1, "max": 30.0, "step": 0.1,
                 "default": 4.0, "label": "pop rate", "unit": "pops/s (combined)"},
                {"key": "decay_ms", "kind": "int", "min": 30, "max": 3000, "default": 700,
                 "label": "decay time", "unit": "ms"},
                {"key": "strobe", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "strobe", "unit": "/ 255"},
            ],
        },
        {
            "type": "strobe",
            "label": "Strobe",
            "description": "Dark baseline with brief bright flashes. Rate 0 = dark; slide up for more frequent flashes.",
            "params": [
                {"key": "target", "kind": "target", "label": "target"},
                {"key": "rgb", "kind": "color", "label": "color", "default": [255, 255, 255]},
                {"key": "amber", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "amber", "unit": "/ 255"},
                {"key": "dim", "kind": "int", "min": 0, "max": 255, "default": 80,
                 "label": "brightness", "unit": "/ 255"},
                {"key": "rate", "kind": "int", "min": 0, "max": 255, "default": 0,
                 "label": "rate", "unit": "/ 255"},
            ],
        },
        {
            "type": "acid_kaleidoscope",
            "label": "🌈 Acid: Kaleidoscope Meltdown",
            "description": "A full rainbow rotates across the whole rig (wash → bar → wash). Each segment is hue-offset and breathes on its own detuned sine, so it shimmers and never lands on a flat frame.",
            "params": [
                {"key": "spin_dps", "kind": "float", "min": -180, "max": 180, "step": 1,
                 "default": 40, "label": "spin speed", "unit": "°/s"},
                {"key": "spread_deg", "kind": "float", "min": 0, "max": 180, "step": 1,
                 "default": 47, "label": "hue spread", "unit": "° / segment"},
                {"key": "breathe_hz", "kind": "float", "min": 0, "max": 3, "step": 0.01,
                 "default": 0.3, "label": "breathe speed", "unit": "Hz"},
                {"key": "sat", "kind": "int", "min": 0, "max": 255, "default": 255,
                 "label": "saturation", "unit": "/ 255"},
                {"key": "dim_min", "kind": "int", "min": 0, "max": 255, "default": 20,
                 "label": "brightness min", "unit": "/ 255"},
                {"key": "dim_max", "kind": "int", "min": 0, "max": 255, "default": 220,
                 "label": "brightness max", "unit": "/ 255"},
            ],
        },
        {
            "type": "acid_bloom",
            "label": "🧠 Acid: Fractal Seizure Bloom",
            "description": "Per-segment hue random-walk with random brightness blooms, white sparks, periodic whole-rig complementary inversions, and random color-cannon stabs. Controlled chaos — no two frames repeat.",
            "params": [
                {"key": "walk_speed", "kind": "float", "min": 0, "max": 360, "step": 5,
                 "default": 70, "label": "hue drift", "unit": "°/s"},
                {"key": "spike_rate", "kind": "float", "min": 0, "max": 30, "step": 0.5,
                 "default": 4, "label": "bloom rate", "unit": "/s"},
                {"key": "spark_rate", "kind": "float", "min": 0, "max": 20, "step": 0.5,
                 "default": 1.5, "label": "white spark rate", "unit": "/s"},
                {"key": "cannon_rate", "kind": "float", "min": 0, "max": 10, "step": 0.1,
                 "default": 0.8, "label": "color cannon rate", "unit": "/s"},
                {"key": "invert_period_s", "kind": "float", "min": 0.5, "max": 30, "step": 0.5,
                 "default": 4, "label": "invert every", "unit": "s"},
                {"key": "base_dim", "kind": "int", "min": 0, "max": 255, "default": 40,
                 "label": "resting brightness", "unit": "/ 255"},
                {"key": "sat", "kind": "int", "min": 0, "max": 255, "default": 255,
                 "label": "saturation", "unit": "/ 255"},
                {"key": "decay_ms", "kind": "int", "min": 30, "max": 3000, "default": 300,
                 "label": "bloom decay", "unit": "ms"},
            ],
        },
    ],
}

# Every DMX layer can be an "add-on": `enter_at` (0..1) gates the layer off
# until the live build intensity reaches it, then fades it in (see
# scene_engine._enter_gate). 0 = base layer (always on from the bottom). Appended
# here so it shows on every effect's editor without repeating it in each spec.
for _spec in FIXTURES["dmx_effects"]:
    _spec["params"].append(
        {"key": "enter_at", "kind": "float", "min": 0.0, "max": 1.0, "step": 0.05,
         "default": 0.0, "label": "add-on: enter at intensity",
         "unit": "i  (0 = always on)"}
    )


def _govee_presets_for_ui() -> dict:
    """Return {sku: [{param_id, name}, ...]} for the preset picker."""
    dl = _lazy_direct_lights()
    out: dict[str, list] = {}
    if dl is None:
        return out
    try:
        govee = dl.govee_client()
    except Exception:
        return out
    for sku, scenes in govee.scenes_by_sku.items():
        out[sku] = [{"param_id": int(s["paramId"]), "name": s.get("name", "")} for s in scenes]
    return out


def _govee_skus_known() -> list[str]:
    dl = _lazy_direct_lights()
    if dl is None:
        return ["H61E5", "H6010"]  # safe defaults
    try:
        govee = dl.govee_client()
        return sorted({s for s in govee.scenes_by_sku.keys()} | {d.get("sku") for d in govee.devices if d.get("sku")})
    except Exception:
        return ["H61E5", "H6010"]


INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>dj-lights monitor</title>
<style>
  :root { color-scheme: dark; }
  body { font: 14px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #0a0a0a; color: #e0e0e0; margin: 0; padding: 24px; }
  .nav { max-width: 1200px; margin: 0 auto 16px; display: flex; gap: 10px; }
  .nav a { color: #888; text-decoration: none; padding: 6px 12px; border-radius: 6px;
           border: 1px solid #222; font-size: 12px; text-transform: uppercase;
           letter-spacing: 1px; }
  .nav a.active { color: #e0e0e0; border-color: #444; background: #151515; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px;
          max-width: 1200px; margin: 0 auto; }
  .card { background: #151515; border: 1px solid #242424; border-radius: 8px;
          padding: 18px 20px; }
  .full { grid-column: 1 / -1; }
  h2 { margin: 0 0 12px; font-size: 11px; text-transform: uppercase;
       color: #888; letter-spacing: 1.5px; font-weight: 600; }
  .mode-badge { padding: 28px 20px; border-radius: 8px; text-align: center;
                font-size: 48px; font-weight: 700; letter-spacing: 2px;
                text-transform: uppercase; transition: background 0.3s; }
  .mode-badge .scene { display: block; font-size: 18px; letter-spacing: 1px;
                       text-transform: none; font-weight: 500; opacity: 0.85;
                       margin-top: 6px; }
  .mode-intro     { background: #1a2b4a; }
  .mode-groove    { background: #1a4a2b; }
  .mode-build     { background: #8a5500; }
  .mode-drop      { background: #c8142b; }
  .mode-breakdown { background: #4a1a6a; }
  .mode-outro     { background: #333; }
  .mode-unknown   { background: #222; color: #555; }
  .phrases { display: flex; gap: 3px; margin-top: 8px; flex-wrap: nowrap;
             overflow-x: auto; padding-bottom: 4px; }
  .phrase { flex: 0 0 auto; min-width: 70px; padding: 10px 6px;
            font-size: 11px; border-radius: 4px; text-align: center;
            opacity: 0.45; transition: opacity 0.2s, transform 0.2s;
            border: 2px solid transparent; }
  .phrase.current { opacity: 1; border-color: #fff; transform: scale(1.05); }
  .phrase .pm { font-weight: 700; text-transform: uppercase; letter-spacing: 1px; }
  .phrase .pb { opacity: 0.7; margin-top: 3px; font-variant-numeric: tabular-nums; }
  .kv { display: grid; grid-template-columns: 120px 1fr; gap: 6px 14px; }
  .kv .k { color: #888; font-size: 12px; }
  .kv .v { font-variant-numeric: tabular-nums; word-break: break-all; }
  .beat-big { font-size: 34px; font-weight: 700; }
  .healthy { color: #6c6; }
  .stale   { color: #da0; }
  .bad     { color: #e55; }
  .muted   { color: #555; font-style: italic; }
  .pill    { display: inline-block; padding: 2px 8px; border-radius: 10px;
             font-size: 11px; }
  .pill.open   { background: #14321c; color: #6c6; }
  .pill.closed { background: #241414; color: #c66; }
  ul.errors { margin: 0; padding: 0; list-style: none; font-size: 12px;
              max-height: 140px; overflow-y: auto;
              font-variant-numeric: tabular-nums; }
  ul.errors li { padding: 3px 0; border-bottom: 1px solid #222; }
  .controls { display: flex; gap: 12px; }
  .controls button { flex: 1; padding: 14px 18px; font-size: 14px;
                     font-weight: 600; letter-spacing: 1px; text-transform: uppercase;
                     background: #1c1c1c; color: #e0e0e0; border: 1px solid #333;
                     border-radius: 6px; cursor: pointer; transition: background 0.15s; }
  .controls button:hover:not(:disabled) { background: #252525; border-color: #444; }
  .controls button:disabled { opacity: 0.4; cursor: not-allowed; }
  .controls button.danger  { background: #4a1a1a; border-color: #7a2a2a; color: #f4d4d4; }
  .controls button.danger:hover:not(:disabled)  { background: #691a1a; }
  .controls button.primary { background: #1c5220; border-color: #2a7a30; color: #d4f4d8; }
  .controls button.primary:hover:not(:disabled) { background: #22692a; }
  .intensity-row { display: flex; gap: 12px; align-items: center; }
  .intensity-row input[type="range"] { flex: 1; height: 26px; }
  .intensity-readout { min-width: 130px; font-size: 13px; font-variant-numeric: tabular-nums;
                       color: #cfcfcf; }
  .intensity-row button { padding: 8px 14px; font-size: 12px; font-weight: 600;
                          letter-spacing: 1px; text-transform: uppercase;
                          background: #1c1c1c; color: #e0e0e0; border: 1px solid #333;
                          border-radius: 6px; cursor: pointer; }
  .intensity-row button:hover { background: #252525; border-color: #444; }
  .intensity-row button.active { background: #1c3a52; border-color: #2a5a7a; color: #d4e8f4; }
  .intensity-hint { font-size: 11px; margin-top: 6px; }
</style></head>
<body>
<div class="nav">
  <a href="/" class="active">Monitor</a>
  <a href="/editor">Scene Editor</a>
</div>
<div class="grid">
  <div class="card full">
    <h2>current mode</h2>
    <div id="mode" class="mode-badge mode-unknown">idle</div>
  </div>
  <div class="card full"><h2>controls</h2>
    <div class="controls">
      <button id="btn-blackout" class="danger" type="button">Blackout</button>
      <button id="btn-refresh" class="primary" type="button">Refresh scene</button>
    </div>
  </div>
  <div class="card full"><h2>intensity</h2>
    <div class="intensity-row">
      <input id="intensity-slider" type="range" min="0" max="100" value="100">
      <div id="intensity-readout" class="intensity-readout">auto &mdash; 100%</div>
      <button id="btn-intensity-auto" type="button">Auto</button>
    </div>
    <div id="intensity-hint" class="muted intensity-hint">drag to override; Auto resumes the per-mode curve (buildup ramps up, drop fades down).</div>
  </div>
  <div class="card full"><h2>build two-step demo</h2>
    <div class="intensity-row">
      <select id="build-scene" style="flex:1; padding:8px; background:#1c1c1c; color:#e0e0e0; border:1px solid #333; border-radius:6px; font-size:13px;">
        <option value="">random build scene</option>
      </select>
      <label style="font-size:12px; color:#888;">stage&nbsp;<input id="build-secs" type="number" min="2" max="120" value="15" style="width:54px; padding:6px; background:#1c1c1c; color:#e0e0e0; border:1px solid #333; border-radius:6px;">s</label>
      <button id="btn-build-run" class="active" type="button">Run</button>
      <button id="btn-build-stop" type="button">Stop</button>
    </div>
    <div id="build-status" class="muted intensity-hint">plays one build scene through breakdown (DMX gated) &rarr; buildup (DMX accretes to the drop). Safe during a live set &mdash; resumes after.</div>
  </div>
  <div class="card"><h2>track</h2><div class="kv" id="track-info"></div></div>
  <div class="card"><h2>position</h2><div class="kv" id="live-info"></div></div>
  <div class="card full"><h2>phrase timeline &mdash; scene map</h2>
    <div id="phrases" class="phrases"></div></div>
  <div class="card full"><h2>health</h2><div class="kv" id="health-info"></div></div>
  <div class="card full"><h2>govee errors (most recent 20)</h2>
    <ul class="errors" id="errors"></ul></div>
</div>
<script>
const MODE_COLORS = {
  intro: '#1a2b4a', groove: '#1a4a2b', build: '#8a5500',
  drop: '#c8142b', breakdown: '#4a1a6a', outro: '#333',
};
const KNOWN_MODES = new Set(Object.keys(MODE_COLORS));
function el(tag, cls) { const e = document.createElement(tag); if (cls) e.className = cls; return e; }
function addKV(parent, key, textVal, cls) {
  const k = el('div', 'k'); k.textContent = key; parent.appendChild(k);
  const v = el('div', 'v ' + (cls || ''));
  if (textVal instanceof Node) v.appendChild(textVal);
  else v.textContent = textVal == null ? '\u2014' : String(textVal);
  parent.appendChild(v);
}
function clear(n){while(n.firstChild)n.removeChild(n.firstChild);}
function muted(t){const d=el('div','muted');d.textContent=t;return d;}
async function poll() {
  try { const r = await fetch('/api/state'); const s = await r.json(); render(s); }
  catch(_e){}
  setTimeout(poll, 400);
}
async function postJson(url) {
  const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
  return r.json().catch(() => ({}));
}
function wireControls() {
  const blackoutBtn = document.getElementById('btn-blackout');
  const refreshBtn = document.getElementById('btn-refresh');
  blackoutBtn.onclick = async () => {
    blackoutBtn.disabled = true;
    try { await postJson('/api/blackout'); }
    finally { blackoutBtn.disabled = false; }
  };
  refreshBtn.onclick = async () => {
    refreshBtn.disabled = true;
    try { await postJson('/api/scene/refresh'); }
    finally { refreshBtn.disabled = false; }
  };
}
wireControls();

// ---- Build two-step demo ----
async function postJsonBody(url, obj) {
  const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                             body: JSON.stringify(obj || {})});
  return r.json().catch(() => ({}));
}
async function wireBuild() {
  const sel = document.getElementById('build-scene');
  const secs = document.getElementById('build-secs');
  const runBtn = document.getElementById('btn-build-run');
  const stopBtn = document.getElementById('btn-build-stop');
  const status = document.getElementById('build-status');
  if (!sel || !runBtn) return;
  try {
    const r = await fetch('/api/scenes'); const data = await r.json();
    (data.scenes || []).filter(s => s.category === 'build').forEach(s => {
      const o = document.createElement('option'); o.value = s.id; o.textContent = s.name;
      sel.appendChild(o);
    });
  } catch(_e) {}
  runBtn.onclick = async () => {
    runBtn.disabled = true;
    const total = Math.round((parseFloat(secs.value) || 15) * 2);
    try {
      const res = await postJsonBody('/api/build/start',
        {scene_id: sel.value || null, stage_secs: parseFloat(secs.value) || 15});
      if (res.error) { status.textContent = 'error: ' + res.error; }
      else { status.textContent = '▶ ' + res.scene + ' — breakdown → buildup (' + total + 's). DMX gated, then accretes to the drop.'; }
    } finally { setTimeout(() => { runBtn.disabled = false; }, 600); }
  };
  stopBtn.onclick = async () => {
    await postJsonBody('/api/build/stop', {});
    status.textContent = 'stopped.';
  };
}
wireBuild();

// ---- Intensity slider ----
// The slider is the manual override surface. Dragging it pins intensity to a
// fixed value; the Auto button releases the pin so the per-mode curve drives
// (e.g. buildup ramps up over the phrase). While in auto mode the slider
// follows the live value but the user isn't holding it, so we don't push
// updates on every poll — only on user interaction.
let _intensityDragging = false;
let _intensityIsManual = false;

function fmtIntensity(value, manual) {
  const pct = Math.round(value * 100);
  return (manual ? 'manual' : 'auto') + ' — ' + pct + '%';
}

async function pollIntensity() {
  try {
    const r = await fetch('/api/intensity');
    const s = await r.json();
    const slider = document.getElementById('intensity-slider');
    const readout = document.getElementById('intensity-readout');
    const autoBtn = document.getElementById('btn-intensity-auto');
    if (!slider || !readout || !autoBtn) return;
    _intensityIsManual = s.manual !== null && s.manual !== undefined;
    if (!_intensityDragging) {
      const v = (typeof s.value === 'number') ? s.value : 1.0;
      slider.value = String(Math.round(v * 100));
      readout.textContent = fmtIntensity(v, _intensityIsManual);
    }
    autoBtn.classList.toggle('active', !_intensityIsManual);
  } catch (_e) {}
  setTimeout(pollIntensity, 500);
}

function wireIntensity() {
  const slider = document.getElementById('intensity-slider');
  const readout = document.getElementById('intensity-readout');
  const autoBtn = document.getElementById('btn-intensity-auto');
  if (!slider || !readout || !autoBtn) return;

  let pending = null;
  let inflight = false;
  // Coalesce drag events: at most one POST in flight; the latest pending
  // value gets sent as soon as the current request resolves.
  async function flush() {
    if (inflight || pending === null) return;
    const v = pending; pending = null; inflight = true;
    try {
      await fetch('/api/intensity', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({value: v}),
      });
    } catch (_e) {}
    inflight = false;
    if (pending !== null) flush();
  }

  slider.addEventListener('input', () => {
    _intensityDragging = true;
    const v = Number(slider.value) / 100;
    readout.textContent = fmtIntensity(v, true);
    pending = v; flush();
  });
  slider.addEventListener('change', () => {
    _intensityDragging = false;
  });
  autoBtn.onclick = async () => {
    _intensityDragging = false;
    try {
      await fetch('/api/intensity', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({value: null}),
      });
    } catch (_e) {}
  };
}
wireIntensity();
pollIntensity();
function render(s) {
  const modeEl = document.getElementById('mode');
  const mode = s.mode && KNOWN_MODES.has(s.mode) ? s.mode : (s.mode ? 'unknown' : 'unknown');
  modeEl.className = 'mode-badge mode-' + mode;
  clear(modeEl);
  modeEl.appendChild(document.createTextNode(s.mode || 'idle'));
  if (s.scene) {
    const sc = el('span', 'scene'); sc.textContent = s.scene; modeEl.appendChild(sc);
  }
  const t = document.getElementById('track-info'); clear(t);
  if (s.track) {
    addKV(t, 'deck', s.track.deck); addKV(t, 'id', s.track.id);
    addKV(t, 'title', s.track.title || '(unknown)');
    addKV(t, 'phrases', s.track.phrase_count);
  } else { t.appendChild(muted('no track loaded')); }
  const lv = document.getElementById('live-info'); clear(lv);
  if (s.beat != null) { const b = el('span','beat-big'); b.textContent = s.beat; addKV(lv,'beat',b); }
  else { addKV(lv,'beat',null); }
  addKV(lv,'mode',s.mode); addKV(lv,'active deck',s.deck);
  const pEl = document.getElementById('phrases'); clear(pEl);
  if (s.track && Array.isArray(s.track.phrases) && s.track.phrases.length) {
    s.track.phrases.forEach(p => {
      const cur = s.beat != null && s.beat >= p.start_beat && s.beat < p.end_beat;
      const d = el('div','phrase'+(cur?' current':''));
      d.style.background = MODE_COLORS[p.mode] || '#222';
      const m = el('div','pm'); m.textContent = p.mode;
      const r = el('div','pb'); r.textContent = p.start_beat + '\u2013' + p.end_beat;
      d.appendChild(m); d.appendChild(r); pEl.appendChild(d);
    });
  } else { pEl.appendChild(muted('no phrase data yet')); }
  const h = document.getElementById('health-info'); clear(h);
  const beatAge = s.last_beat_ts ? (Date.now()/1000 - s.last_beat_ts) : null;
  const beatClass = beatAge == null ? 'muted' : beatAge < 2 ? 'healthy' : beatAge < 10 ? 'stale' : 'bad';
  addKV(h,'last beat', beatAge==null?'never':beatAge.toFixed(1)+'s ago', beatClass);
  addKV(h,'status packets', s.status_packets);
  addKV(h,'vcdj ticks', s.vcdj_ticks);
  const dmxPill = el('span','pill ' + (s.dmx_open?'open':'closed'));
  dmxPill.textContent = s.dmx_open ? 'open' : 'closed';
  addKV(h,'dmx', dmxPill);
  addKV(h,'vcdj bound', s.vcdj_bound ? (s.vcdj_bound.iface+' '+s.vcdj_bound.ip) : 'no');
  const u = document.getElementById('errors'); clear(u);
  if (!s.govee_errors || !s.govee_errors.length) {
    const li = document.createElement('li'); li.className='muted'; li.textContent='none \u2014 all clean'; u.appendChild(li);
  } else {
    s.govee_errors.slice().reverse().forEach(e => {
      const li = document.createElement('li');
      const ts = new Date(e.ts*1000).toLocaleTimeString();
      li.textContent = ts+'  ['+e.device+'] '+e.code+' '+e.msg;
      u.appendChild(li);
    });
  }
}
poll();
</script>
</body></html>
"""


EDITOR_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>dj-lights scene editor</title>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { font: 14px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #0a0a0a; color: #e0e0e0; margin: 0; padding: 20px; }
.nav { max-width: 1600px; margin: 0 auto 16px; display: flex; gap: 10px; align-items: center; }
.nav a { color: #888; text-decoration: none; padding: 6px 12px; border-radius: 6px;
         border: 1px solid #222; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; }
.nav a.active { color: #e0e0e0; border-color: #444; background: #151515; }
.nav .spacer { flex: 1; }
.nav .status { font-size: 11px; color: #888; padding: 6px 10px; }
.nav .status.dirty { color: #da0; }
button { background: #1c1c1c; color: #e0e0e0; border: 1px solid #333;
         border-radius: 5px; padding: 6px 12px; font-size: 12px; cursor: pointer;
         letter-spacing: 0.5px; }
button:hover:not(:disabled) { background: #252525; border-color: #444; }
button:disabled { opacity: 0.4; cursor: not-allowed; }
button.primary { background: #1c5220; border-color: #2a7a30; color: #d4f4d8; }
button.primary:hover:not(:disabled) { background: #22692a; }
button.danger { background: #4a1a1a; border-color: #7a2a2a; color: #f4d4d4; }
button.danger:hover:not(:disabled) { background: #691a1a; }
button.playing { background: #b88900; border-color: #e0a800; color: #fff; }

.layout { display: grid; grid-template-columns: 320px 1fr; gap: 16px;
          max-width: 1600px; margin: 0 auto; }
.cat-col { background: #101010; border: 1px solid #242424; border-radius: 8px;
           padding: 10px; margin-bottom: 10px; }
.cat-col h3 { font-size: 11px; text-transform: uppercase; color: #888; letter-spacing: 1.5px;
              margin: 0 0 8px; padding-left: 4px; font-weight: 600;
              display: flex; align-items: center; gap: 8px; }
.cat-col h3 .count { color: #555; font-weight: 400; }
.cat-col.drop-target { border-color: #6cf; background: #0a1822; }
.scene-card { background: #181818; border: 1px solid #2a2a2a; border-radius: 6px;
              padding: 8px 10px; margin-bottom: 6px; cursor: grab; user-select: none;
              display: flex; align-items: center; gap: 8px;
              transition: border-color 0.12s, background 0.12s; }
.scene-card:hover { border-color: #444; background: #202020; }
.scene-card.selected { border-color: #6cf; background: #142233; }
.scene-card .dot { width: 10px; height: 10px; border-radius: 50%; flex: 0 0 auto; }
.scene-card .name { flex: 1; font-size: 13px; overflow: hidden;
                    text-overflow: ellipsis; white-space: nowrap; }
.scene-card.dragging { opacity: 0.4; }
.cat-dot-intro     { background: #1a2b4a; }
.cat-dot-groove    { background: #1a4a2b; }
.cat-dot-build     { background: #8a5500; }
.cat-dot-drop      { background: #c8142b; }
.cat-dot-breakdown { background: #4a1a6a; }
.cat-dot-outro     { background: #777; }

.editor { background: #101010; border: 1px solid #242424; border-radius: 8px;
          padding: 20px; min-height: 600px; }
.editor .empty { color: #555; font-style: italic; padding: 40px; text-align: center; }
.editor h2 { font-size: 14px; margin: 0 0 14px; color: #ccc; letter-spacing: 0.5px; }

.head-row { display: flex; gap: 10px; align-items: center; margin-bottom: 18px;
            padding-bottom: 14px; border-bottom: 1px solid #222; }
.head-row input[type="text"] { background: #181818; border: 1px solid #2a2a2a;
      color: #e0e0e0; padding: 8px 10px; border-radius: 5px; font-size: 14px; width: 260px; }
.head-row select { background: #181818; border: 1px solid #2a2a2a; color: #e0e0e0;
      padding: 8px 10px; border-radius: 5px; font-size: 13px; }
.head-row .spacer { flex: 1; }

.group-card { background: #141414; border: 1px solid #2a2a2a; border-radius: 8px;
              padding: 14px 16px; margin-bottom: 12px; transition: opacity 0.15s; }
.group-card.inactive { opacity: 0.55; }
.group-head { display: flex; align-items: center; gap: 12px;
              padding-bottom: 10px; border-bottom: 1px solid #222; margin-bottom: 12px; }
.group-card.inactive .group-head { border-bottom-color: transparent; margin-bottom: 0; }
.group-title { display: flex; flex-direction: column; gap: 2px; }
.group-title h3 { margin: 0; font-size: 13px; font-weight: 600; color: #ddd;
                  letter-spacing: 0.3px; }
.group-title .group-sub { color: #666; font-size: 11px; }
.group-head .spacer { flex: 1; }
.toggle { display: inline-flex; align-items: center; gap: 6px;
          padding: 3px 10px; border-radius: 14px; cursor: pointer;
          background: #222; color: #888; font-size: 11px;
          text-transform: uppercase; letter-spacing: 1px; user-select: none;
          border: 1px solid #333; }
.toggle.on { background: #1c5220; color: #d4f4d8; border-color: #2a7a30; }
.toggle input { margin: 0; }
.group-inactive-note { color: #555; font-style: italic; font-size: 12px;
                      padding: 4px 0; }

.mode-row { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.row-label { color: #888; font-size: 12px; min-width: 80px; }
.mode-row select { background: #0f0f0f; border: 1px solid #333; color: #e0e0e0;
                   padding: 5px 10px; border-radius: 4px; font-size: 12px; }
.mode-row .spacer { flex: 1; }

.param-grid { display: grid; grid-template-columns: 130px 1fr 100px; gap: 6px 12px;
              align-items: center; }
.param-grid .k { color: #888; font-size: 12px; }
.param-grid input[type="range"] { width: 100%; accent-color: #6cf; }
.param-grid input[type="color"] { width: 60px; height: 28px; padding: 0;
      border: 1px solid #333; background: #0f0f0f; border-radius: 4px; cursor: pointer; }
.param-grid input[type="number"] { width: 70px; background: #0f0f0f; border: 1px solid #333;
      color: #e0e0e0; padding: 4px 6px; border-radius: 4px; font-size: 12px; }
.param-grid select { background: #0f0f0f; border: 1px solid #333; color: #e0e0e0;
      padding: 5px 8px; border-radius: 4px; font-size: 12px; max-width: 100%; }
.param-grid .val { color: #aaa; font-size: 11px; font-variant-numeric: tabular-nums;
                   text-align: right; }
.color-list { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.color-list input[type="color"] { width: 38px; height: 26px; }
.color-list button { padding: 2px 8px; font-size: 11px; }

.effect-card { background: #181818; border: 1px solid #2a2a2a; border-radius: 6px;
               padding: 10px 14px; margin-bottom: 8px; }
.effect-head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px;
               padding-bottom: 8px; border-bottom: 1px dashed #222; }
.effect-type { font-size: 11px; font-weight: 700; color: #ccc;
               text-transform: uppercase; letter-spacing: 1px; flex: 0 0 auto; }
.effect-desc { color: #666; font-size: 11px; flex: 1; }
.effect-head button { padding: 2px 8px; font-size: 11px; }
.effect-head select { background: #0f0f0f; border: 1px solid #333; color: #e0e0e0;
                      padding: 4px 8px; border-radius: 4px; font-size: 12px; }

.add-effect { display: flex; gap: 8px; padding: 8px; border: 1px dashed #333;
              border-radius: 6px; background: #101010; margin-top: 8px;
              align-items: center; }
.add-effect select { flex: 1; background: #0f0f0f; border: 1px solid #333;
                     color: #e0e0e0; padding: 5px 8px; border-radius: 4px;
                     font-size: 12px; }

.bar { display: flex; gap: 8px; margin-top: 16px; padding-top: 14px; border-top: 1px solid #222; }
.toast { position: fixed; top: 20px; right: 20px; background: #1c5220; color: #d4f4d8;
         padding: 10px 16px; border-radius: 6px; font-size: 13px; opacity: 0;
         transition: opacity 0.2s; pointer-events: none; z-index: 1000; }
.toast.show { opacity: 1; }
.toast.err { background: #4a1a1a; color: #f4d4d4; }
</style></head>
<body>
<div class="nav">
  <a href="/">Monitor</a>
  <a href="/editor" class="active">Scene Editor</a>
  <span class="spacer"></span>
  <span id="status" class="status">loaded</span>
  <button id="btn-save" class="primary">Save</button>
</div>
<div class="layout">
  <div id="cat-rail"></div>
  <div id="editor" class="editor">
    <div class="empty">Select or create a scene to edit.</div>
  </div>
</div>
<div id="toast" class="toast"></div>
<script>
// ---- State ----
let FIXTURES = null;
let PRESETS = {};
let SKUS = [];
let CATALOG = {version: 1, scenes: []};
let selectedId = null;
let dirty = false;
let previewing = false;
let debouncePreview = null;

function $(id) { return document.getElementById(id); }
function el(tag, cls) { const e = document.createElement(tag); if (cls) e.className = cls; return e; }
function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
function toast(msg, err) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast show' + (err ? ' err' : '');
  setTimeout(() => { t.classList.remove('show'); }, 1800);
}
function setDirty(d) {
  dirty = d;
  $('status').textContent = d ? 'unsaved changes' : 'saved';
  $('status').classList.toggle('dirty', d);
}
function rgbToHex(rgb) {
  if (!rgb) return '#000000';
  const [r,g,b] = rgb;
  return '#' + [r,g,b].map(v => Math.max(0,Math.min(255,v|0)).toString(16).padStart(2,'0')).join('');
}
function hexToRgb(hex) {
  const h = hex.replace('#','');
  return [parseInt(h.slice(0,2),16), parseInt(h.slice(2,4),16), parseInt(h.slice(4,6),16)];
}
function newId() { return 'scene-' + Math.random().toString(36).slice(2, 10); }

// 12 vibrant DJ-friendly basics — used by palette pickers in the color
// editors. Picked for separation on the color wheel + visibility on Tetras.
const BASIC_PALETTE = [
  {name: 'red',      rgb: [255, 0, 0]},
  {name: 'orange',   rgb: [255, 80, 0]},
  {name: 'amber',    rgb: [255, 180, 0]},
  {name: 'lime',     rgb: [120, 255, 0]},
  {name: 'green',    rgb: [0, 255, 40]},
  {name: 'cyan',     rgb: [0, 200, 255]},
  {name: 'blue',     rgb: [0, 80, 255]},
  {name: 'indigo',   rgb: [75, 0, 255]},
  {name: 'purple',   rgb: [180, 0, 255]},
  {name: 'magenta',  rgb: [255, 0, 180]},
  {name: 'hot pink', rgb: [255, 40, 100]},
  {name: 'white',    rgb: [255, 255, 255]},
];

// Build a row of palette swatches. `onPick` fires with a fresh RGB array.
// `extras` (optional) is a list of {label, onClick} buttons appended after.
function renderPaletteRow(onPick, extras) {
  const row = el('div');
  row.style.display = 'flex';
  row.style.flexWrap = 'wrap';
  row.style.gap = '4px';
  row.style.marginTop = '6px';
  row.style.alignItems = 'center';
  BASIC_PALETTE.forEach(c => {
    const sw = el('button');
    sw.type = 'button';
    sw.title = c.name;
    sw.style.background = rgbToHex(c.rgb);
    sw.style.width = '20px';
    sw.style.height = '20px';
    sw.style.padding = '0';
    sw.style.border = '1px solid #555';
    sw.style.borderRadius = '3px';
    sw.style.cursor = 'pointer';
    sw.onclick = (e) => { e.preventDefault(); onPick(c.rgb.slice()); };
    row.appendChild(sw);
  });
  (extras || []).forEach(x => {
    const b = el('button');
    b.type = 'button';
    b.textContent = x.label;
    b.style.marginLeft = '6px';
    b.style.fontSize = '11px';
    b.onclick = (e) => { e.preventDefault(); x.onClick(); };
    row.appendChild(b);
  });
  return row;
}

// ---- Initial load ----
async function boot() {
  const [fx, presets, cat] = await Promise.all([
    fetch('/api/fixtures').then(r => r.json()),
    fetch('/api/govee/presets').then(r => r.json()),
    fetch('/api/scenes').then(r => r.json()),
  ]);
  FIXTURES = fx;
  PRESETS = presets.presets || {};
  SKUS = presets.skus || [];
  CATALOG = cat;
  renderAll();
}
boot();

// ---- Rendering ----
function renderAll() { renderRail(); renderEditor(); }

function renderRail() {
  const rail = $('cat-rail');
  clear(rail);
  const byCat = {};
  FIXTURES.categories.forEach(c => byCat[c] = []);
  CATALOG.scenes.forEach(s => {
    const c = byCat[s.category] ? s.category : 'uncategorized';
    if (!byCat[c]) byCat[c] = [];
    byCat[c].push(s);
  });
  Object.entries(byCat).forEach(([cat, scenes]) => {
    const col = el('div', 'cat-col');
    col.dataset.category = cat;
    col.addEventListener('dragover', (e) => { e.preventDefault(); col.classList.add('drop-target'); });
    col.addEventListener('dragleave', () => col.classList.remove('drop-target'));
    col.addEventListener('drop', (e) => {
      e.preventDefault();
      col.classList.remove('drop-target');
      const sid = e.dataTransfer.getData('text/plain');
      const scene = CATALOG.scenes.find(s => s.id === sid);
      if (!scene || scene.category === cat) return;
      scene.category = cat;
      setDirty(true);
      renderRail();
    });
    const h = el('h3');
    h.appendChild(document.createTextNode(cat));
    const count = el('span', 'count'); count.textContent = '(' + scenes.length + ')';
    h.appendChild(count);
    col.appendChild(h);
    scenes.forEach(s => col.appendChild(sceneCard(s)));
    const add = el('button'); add.textContent = '+ add';
    add.style.fontSize = '11px'; add.style.width = '100%'; add.style.marginTop = '2px';
    add.onclick = () => createScene(cat);
    col.appendChild(add);
    rail.appendChild(col);
  });
}

function sceneCard(scene) {
  const c = el('div', 'scene-card' + (scene.id === selectedId ? ' selected' : ''));
  c.draggable = true;
  c.dataset.id = scene.id;
  c.addEventListener('dragstart', (e) => {
    e.dataTransfer.setData('text/plain', scene.id);
    c.classList.add('dragging');
  });
  c.addEventListener('dragend', () => c.classList.remove('dragging'));
  c.onclick = () => { selectedId = scene.id; renderAll(); };
  const dot = el('span', 'dot cat-dot-' + scene.category);
  const name = el('span', 'name'); name.textContent = scene.name || scene.id;
  c.appendChild(dot); c.appendChild(name);
  return c;
}

function renderEditor() {
  const ed = $('editor');
  clear(ed);
  const scene = CATALOG.scenes.find(s => s.id === selectedId);
  if (!scene) {
    const e = el('div', 'empty');
    e.textContent = 'Select a scene from the left rail, or click + add under any category.';
    ed.appendChild(e); return;
  }
  scene.layers = scene.layers || [];
  ed.appendChild(renderSceneHead(scene));
  if (scene.blackout) {
    const note = el('div', 'empty');
    note.textContent = 'Blackout scene — DMX zeroed, Govee off. Layers ignored.';
    ed.appendChild(note);
    return;
  }
  FIXTURES.device_groups.forEach(g => ed.appendChild(renderGoveeGroup(scene, g)));
  ed.appendChild(renderDmxGroup(scene));
}

function renderSceneHead(scene) {
  const head = el('div', 'head-row');
  const nameI = el('input'); nameI.type = 'text'; nameI.value = scene.name || '';
  nameI.placeholder = 'Scene name';
  nameI.oninput = () => { scene.name = nameI.value; setDirty(true); renderRail(); };
  head.appendChild(nameI);
  const catS = el('select');
  FIXTURES.categories.forEach(c => {
    const o = el('option'); o.value = c; o.textContent = c;
    if (c === scene.category) o.selected = true;
    catS.appendChild(o);
  });
  catS.onchange = () => { scene.category = catS.value; setDirty(true); renderRail(); };
  head.appendChild(catS);
  const boLabel = el('label');
  boLabel.style.display = 'flex'; boLabel.style.alignItems = 'center'; boLabel.style.gap = '6px';
  boLabel.style.fontSize = '12px'; boLabel.style.color = '#aaa';
  const boCb = el('input'); boCb.type = 'checkbox'; boCb.checked = !!scene.blackout;
  boCb.onchange = () => {
    scene.blackout = boCb.checked;
    setDirty(true);
    if (previewing) maybePushPreview(scene);
    renderEditor();
  };
  boLabel.appendChild(boCb);
  boLabel.appendChild(document.createTextNode('Blackout scene'));
  head.appendChild(boLabel);
  head.appendChild(el('div', 'spacer'));
  const playBtn = el('button', previewing ? 'playing' : 'primary');
  playBtn.textContent = previewing ? 'Stop Preview' : 'Play Preview';
  playBtn.onclick = () => previewing ? stopPreview() : startPreview(scene);
  head.appendChild(playBtn);
  const twoStepBtn = el('button', twoStepRunning ? 'playing' : '');
  twoStepBtn.textContent = twoStepRunning ? 'Stop Two-step' : 'Two-step ▶';
  twoStepBtn.title = 'Preview this scene through breakdown → buildup so staged (enter_at) layers come in as it climbs';
  twoStepBtn.onclick = () => twoStepRunning ? stopTwoStep() : startTwoStep(scene);
  head.appendChild(twoStepBtn);
  const dupBtn = el('button'); dupBtn.textContent = 'Duplicate';
  dupBtn.onclick = () => duplicate(scene); head.appendChild(dupBtn);
  const delBtn = el('button', 'danger'); delBtn.textContent = 'Delete';
  delBtn.onclick = () => deleteScene(scene); head.appendChild(delBtn);
  return head;
}

// ---- Govee device groups (COB / Bulbs) ----
//
// Each group owns the H* SKU's entry inside the scene's govee_preset layer
// (merged format: {presets: {sku: param_id}}) OR a govee_rgb layer scoped to
// that SKU. Toggling the group off strips both. Internal helpers below keep
// the scene.layers array coherent after each user action.

function goveePresetLayer(scene) {
  return scene.layers.find(l => l.type === 'govee_preset' && l.presets && typeof l.presets === 'object');
}
function goveeRgbLayerFor(scene, sku) {
  return scene.layers.find(l => l.type === 'govee_rgb' && (l.skus || []).length === 1 && l.skus[0] === sku);
}
function ensurePresetLayer(scene) {
  let p = goveePresetLayer(scene);
  if (!p) { p = {type: 'govee_preset', presets: {}}; scene.layers.push(p); }
  if (!p.presets) p.presets = {};
  return p;
}
function clearGroupLayers(scene, sku) {
  // Remove this SKU from any govee_preset.presets dict; drop empty preset layer.
  scene.layers = scene.layers.filter(l => {
    if (l.type === 'govee_preset' && l.presets) {
      if (sku in l.presets) {
        delete l.presets[sku];
        if (Object.keys(l.presets).length === 0) return false;
      }
      return true;
    }
    if (l.type === 'govee_rgb' && (l.skus || []).includes(sku)) {
      l.skus = (l.skus || []).filter(s => s !== sku);
      if (l.skus.length === 0) return false;
    }
    return true;
  });
}
function setGroupMode(scene, sku, mode) {
  clearGroupLayers(scene, sku);
  if (mode === 'preset') {
    const layer = ensurePresetLayer(scene);
    layer.presets[sku] = null; // user picks one
  } else if (mode === 'solid') {
    scene.layers.push({type: 'govee_rgb', skus: [sku], rgb: [255, 120, 20], brightness: 80});
  }
  // mode === 'off' already handled by clearGroupLayers
}

function renderGoveeGroup(scene, g) {
  const presetLayer = goveePresetLayer(scene);
  const hasPreset = !!(presetLayer && presetLayer.presets && g.sku in presetLayer.presets);
  const rgbLayer = goveeRgbLayerFor(scene, g.sku);
  const isActive = hasPreset || !!rgbLayer;
  const mode = hasPreset ? 'preset' : rgbLayer ? 'solid' : 'off';

  const box = el('div', 'group-card' + (isActive ? '' : ' inactive'));
  const head = el('div', 'group-head');
  const title = el('div', 'group-title');
  const h3 = el('h3'); h3.textContent = g.name; title.appendChild(h3);
  const sub = el('span', 'group-sub'); sub.textContent = g.subtitle + ' · ' + g.sku;
  title.appendChild(sub);
  head.appendChild(title);
  head.appendChild(el('div', 'spacer'));
  const toggle = el('label', 'toggle' + (isActive ? ' on' : ''));
  const cb = el('input'); cb.type = 'checkbox'; cb.checked = isActive;
  const tx = el('span'); tx.textContent = isActive ? 'on' : 'off';
  cb.onchange = () => {
    setGroupMode(scene, g.sku, cb.checked ? 'preset' : 'off');
    setDirty(true); renderEditor(); maybePushPreview(scene);
  };
  toggle.appendChild(cb); toggle.appendChild(tx);
  head.appendChild(toggle);
  box.appendChild(head);

  if (!isActive) {
    const note = el('div', 'group-inactive-note');
    note.textContent = 'Off — turn on to add ' + g.name.toLowerCase() + ' to this scene.';
    box.appendChild(note);
    return box;
  }

  // Mode row
  const modeRow = el('div', 'mode-row');
  const ml = el('span', 'row-label'); ml.textContent = 'mode'; modeRow.appendChild(ml);
  const modeSel = el('select');
  [['preset', 'Preset scene'], ['solid', 'Solid color']].forEach(([v, lbl]) => {
    const o = el('option'); o.value = v; o.textContent = lbl;
    if (v === mode) o.selected = true;
    modeSel.appendChild(o);
  });
  modeSel.onchange = () => {
    setGroupMode(scene, g.sku, modeSel.value);
    setDirty(true); renderEditor(); maybePushPreview(scene);
  };
  modeRow.appendChild(modeSel);
  box.appendChild(modeRow);

  const grid = el('div', 'param-grid');
  if (mode === 'preset') {
    const k = el('div', 'k'); k.textContent = 'preset'; grid.appendChild(k);
    const sel = el('select');
    const none = el('option'); none.value = ''; none.textContent = '(pick one)';
    sel.appendChild(none);
    const list = (PRESETS[g.sku] || []).slice().sort((a,b) => a.name.localeCompare(b.name));
    if (list.length === 0) {
      const warn = el('option'); warn.value = ''; warn.disabled = true;
      warn.textContent = 'no presets cached for ' + g.sku;
      sel.appendChild(warn);
    }
    list.forEach(pr => {
      const o = el('option'); o.value = pr.param_id;
      o.textContent = pr.name + ' (' + pr.param_id + ')';
      if (pr.param_id === presetLayer.presets[g.sku]) o.selected = true;
      sel.appendChild(o);
    });
    sel.onchange = () => {
      const pid = parseInt(sel.value, 10);
      const layer = ensurePresetLayer(scene);
      if (isNaN(pid)) {
        delete layer.presets[g.sku];
        if (Object.keys(layer.presets).length === 0) {
          scene.layers = scene.layers.filter(l => l !== layer);
        }
      } else {
        layer.presets[g.sku] = pid;
      }
      setDirty(true); maybePushPreview(scene);
    };
    const wrap = el('div'); wrap.style.gridColumn = '2 / span 2'; wrap.appendChild(sel);
    grid.appendChild(wrap);
  } else if (mode === 'solid') {
    // color
    const k1 = el('div', 'k'); k1.textContent = 'color'; grid.appendChild(k1);
    const ci = el('input'); ci.type = 'color'; ci.value = rgbToHex(rgbLayer.rgb || [255,120,20]);
    ci.oninput = () => { rgbLayer.rgb = hexToRgb(ci.value); setDirty(true); maybePushPreview(scene); };
    const cw = el('div'); cw.style.gridColumn = '2 / span 2'; cw.appendChild(ci);
    grid.appendChild(cw);
    // brightness
    const k2 = el('div', 'k'); k2.textContent = 'brightness'; grid.appendChild(k2);
    const br = rgbLayer.brightness != null ? rgbLayer.brightness : 80;
    const range = el('input'); range.type = 'range'; range.min = 1; range.max = 100; range.value = br;
    const valSpan = el('div', 'val'); valSpan.textContent = br + ' %';
    range.oninput = () => {
      const v = parseInt(range.value, 10);
      rgbLayer.brightness = v;
      valSpan.textContent = v + ' %';
      setDirty(true); maybePushPreview(scene);
    };
    grid.appendChild(range); grid.appendChild(valSpan);
  }
  box.appendChild(grid);
  return box;
}

// ---- DMX device group ----

function dmxEffectsOf(scene) {
  const set = new Set(FIXTURES.dmx_effects.map(s => s.type));
  return scene.layers.filter(l => set.has(l.type));
}

function renderDmxGroup(scene) {
  const effects = dmxEffectsOf(scene);
  const isActive = effects.length > 0;
  const gm = FIXTURES.dmx_group;
  const box = el('div', 'group-card' + (isActive ? '' : ' inactive'));
  const head = el('div', 'group-head');
  const title = el('div', 'group-title');
  const h3 = el('h3'); h3.textContent = gm.name; title.appendChild(h3);
  const sub = el('span', 'group-sub'); sub.textContent = gm.subtitle;
  title.appendChild(sub);
  head.appendChild(title);
  head.appendChild(el('div', 'spacer'));
  const toggle = el('label', 'toggle' + (isActive ? ' on' : ''));
  const cb = el('input'); cb.type = 'checkbox'; cb.checked = isActive;
  const tx = el('span'); tx.textContent = isActive ? 'on' : 'off';
  cb.onchange = () => {
    if (cb.checked) {
      scene.layers.push(defaultEffect('breathe'));
    } else {
      const set = new Set(FIXTURES.dmx_effects.map(s => s.type));
      scene.layers = scene.layers.filter(l => !set.has(l.type));
    }
    setDirty(true); renderEditor(); maybePushPreview(scene);
  };
  toggle.appendChild(cb); toggle.appendChild(tx);
  head.appendChild(toggle);
  box.appendChild(head);

  if (!isActive) {
    const note = el('div', 'group-inactive-note');
    note.textContent = 'Off — turn on to add a DMX effect to this scene.';
    box.appendChild(note);
    return box;
  }

  effects.forEach(effect => box.appendChild(renderDmxEffect(scene, effect, effects)));

  // Add another effect
  const addRow = el('div', 'add-effect');
  const lbl = el('span', 'row-label'); lbl.textContent = 'add effect';
  const sel = el('select');
  const def = el('option'); def.value = ''; def.textContent = '…'; sel.appendChild(def);
  FIXTURES.dmx_effects.forEach(ls => {
    const o = el('option'); o.value = ls.type; o.textContent = ls.label; sel.appendChild(o);
  });
  const addBtn = el('button'); addBtn.textContent = 'Add';
  addBtn.onclick = () => {
    if (!sel.value) return;
    scene.layers.push(defaultEffect(sel.value));
    setDirty(true); renderEditor(); maybePushPreview(scene);
  };
  addRow.appendChild(lbl); addRow.appendChild(sel); addRow.appendChild(addBtn);
  box.appendChild(addRow);
  return box;
}

function renderDmxEffect(scene, effect, allEffects) {
  const schema = FIXTURES.dmx_effects.find(s => s.type === effect.type);
  const box = el('div', 'effect-card');
  const head = el('div', 'effect-head');
  const typeSel = el('select');
  FIXTURES.dmx_effects.forEach(ls => {
    const o = el('option'); o.value = ls.type; o.textContent = ls.label;
    if (ls.type === effect.type) o.selected = true;
    typeSel.appendChild(o);
  });
  typeSel.onchange = () => {
    const idx = scene.layers.indexOf(effect);
    scene.layers[idx] = defaultEffect(typeSel.value);
    setDirty(true); renderEditor(); maybePushPreview(scene);
  };
  head.appendChild(typeSel);
  if (schema && schema.description) {
    const d = el('span', 'effect-desc'); d.textContent = schema.description;
    head.appendChild(d);
  }
  head.appendChild(el('div', 'spacer'));
  // reorder within the effects list (not across govee layers)
  const posInEffects = allEffects.indexOf(effect);
  const up = el('button'); up.textContent = '\u2191'; up.title = 'move up';
  up.disabled = posInEffects === 0;
  up.onclick = () => {
    const prev = allEffects[posInEffects - 1];
    const i1 = scene.layers.indexOf(prev), i2 = scene.layers.indexOf(effect);
    scene.layers[i1] = effect; scene.layers[i2] = prev;
    setDirty(true); renderEditor(); maybePushPreview(scene);
  };
  const down = el('button'); down.textContent = '\u2193'; down.title = 'move down';
  down.disabled = posInEffects === allEffects.length - 1;
  down.onclick = () => {
    const next = allEffects[posInEffects + 1];
    const i1 = scene.layers.indexOf(effect), i2 = scene.layers.indexOf(next);
    scene.layers[i1] = next; scene.layers[i2] = effect;
    setDirty(true); renderEditor(); maybePushPreview(scene);
  };
  const rm = el('button', 'danger'); rm.textContent = '\u00d7'; rm.title = 'remove effect';
  rm.onclick = () => {
    scene.layers = scene.layers.filter(l => l !== effect);
    setDirty(true); renderEditor(); maybePushPreview(scene);
  };
  head.appendChild(up); head.appendChild(down); head.appendChild(rm);
  box.appendChild(head);

  const grid = el('div', 'param-grid');
  if (schema) (schema.params || []).forEach(p => renderParam(grid, scene, effect, p));
  box.appendChild(grid);
  return box;
}

// ---- Effect param renderer ----

function formatVal(v, p) {
  const num = p.kind === 'float' ? (+v).toFixed(2) : String(v);
  return p.unit ? (num + ' ' + p.unit) : num;
}

function renderParam(grid, scene, effect, p) {
  if (p.kind === 'target') {
    const k = el('div','k'); k.textContent = p.label || 'target'; grid.appendChild(k);
    const sel = el('select');
    FIXTURES.dmx_targets.forEach(t => {
      const o = el('option'); o.value = t.id; o.textContent = t.name;
      if (t.id === effect.target) o.selected = true; sel.appendChild(o);
    });
    sel.onchange = () => { effect.target = sel.value; setDirty(true); maybePushPreview(scene); };
    const wrap = el('div'); wrap.style.gridColumn = '2 / span 2'; wrap.appendChild(sel);
    grid.appendChild(wrap); return;
  }
  if (p.kind === 'color') {
    const k = el('div','k'); k.textContent = p.label || 'color'; grid.appendChild(k);
    const key = p.key || 'rgb';
    const ci = el('input'); ci.type = 'color';
    ci.value = rgbToHex(effect[key] || [0,0,0]);
    ci.oninput = () => { effect[key] = hexToRgb(ci.value); setDirty(true); maybePushPreview(scene); };
    const wrap = el('div'); wrap.appendChild(ci);
    // Palette swatches: clicking one snaps the picker (and the layer value)
    // to that color. Cheaper than scrubbing the OS color wheel.
    wrap.appendChild(renderPaletteRow(c => {
      effect[key] = c;
      ci.value = rgbToHex(c);
      setDirty(true); maybePushPreview(scene);
    }));
    wrap.style.gridColumn = '2 / span 2'; grid.appendChild(wrap); return;
  }
  if (p.kind === 'color_list') {
    const k = el('div','k'); k.textContent = p.label || 'colors'; grid.appendChild(k);
    const wrap = el('div', 'color-list'); wrap.style.gridColumn = '2 / span 2';
    effect.colors = effect.colors || [[255,0,0]];
    const colorRow = el('div');
    colorRow.style.display = 'flex';
    colorRow.style.flexWrap = 'wrap';
    colorRow.style.gap = '4px';
    colorRow.style.alignItems = 'center';
    effect.colors.forEach((c, i) => {
      const ci = el('input'); ci.type = 'color'; ci.value = rgbToHex(c);
      ci.oninput = () => { effect.colors[i] = hexToRgb(ci.value); setDirty(true); maybePushPreview(scene); };
      colorRow.appendChild(ci);
      if (effect.colors.length > 1) {
        const rm = el('button'); rm.textContent = 'x';
        rm.onclick = () => { effect.colors.splice(i,1); setDirty(true); renderEditor(); maybePushPreview(scene); };
        colorRow.appendChild(rm);
      }
    });
    const add = el('button'); add.textContent = '+';
    add.onclick = () => { effect.colors.push([255,255,255]); setDirty(true); renderEditor(); maybePushPreview(scene); };
    colorRow.appendChild(add);
    wrap.appendChild(colorRow);

    // Palette swatches: click adds to the list. "use 12" replaces the list
    // with the full palette (random pick across all basics — for popcorn
    // and other multi-color modes). "clear" wipes back to a single white.
    wrap.appendChild(renderPaletteRow(
      c => {
        effect.colors.push(c);
        setDirty(true); renderEditor(); maybePushPreview(scene);
      },
      [
        {label: 'use 12', onClick: () => {
          effect.colors = BASIC_PALETTE.map(p => p.rgb.slice());
          setDirty(true); renderEditor(); maybePushPreview(scene);
        }},
        {label: 'clear', onClick: () => {
          effect.colors = [[255, 255, 255]];
          setDirty(true); renderEditor(); maybePushPreview(scene);
        }},
      ]
    ));
    // Tiny hint to make the random-vs-fixed mental model obvious.
    const hint = el('div');
    hint.style.fontSize = '10px';
    hint.style.color = '#888';
    hint.style.marginTop = '4px';
    hint.textContent = effect.colors.length > 1
      ? `${effect.colors.length} colors — random pick per pop / cycle`
      : 'single color — fixed';
    wrap.appendChild(hint);

    grid.appendChild(wrap); return;
  }
  if (p.kind === 'int' || p.kind === 'float') {
    const k = el('div','k'); k.textContent = p.label || p.key; grid.appendChild(k);
    const cur = (effect[p.key] != null) ? effect[p.key] : (p.default != null ? p.default : 0);
    const range = el('input'); range.type = 'range';
    range.min = p.min; range.max = p.max; range.step = p.step || (p.kind === 'float' ? 0.01 : 1);
    range.value = cur;
    const valSpan = el('div', 'val'); valSpan.textContent = formatVal(cur, p);
    range.oninput = () => {
      const v = p.kind === 'float' ? parseFloat(range.value) : parseInt(range.value, 10);
      effect[p.key] = v;
      valSpan.textContent = formatVal(v, p);
      setDirty(true); maybePushPreview(scene);
    };
    grid.appendChild(range); grid.appendChild(valSpan); return;
  }
  if (p.kind === 'select') {
    const k = el('div','k'); k.textContent = p.label || p.key; grid.appendChild(k);
    const sel = el('select');
    const cur = (effect[p.key] != null) ? effect[p.key] : (p.default != null ? p.default : (p.options[0] && p.options[0].value));
    (p.options || []).forEach(opt => {
      const o = el('option'); o.value = opt.value; o.textContent = opt.label;
      if (opt.value === cur) o.selected = true;
      sel.appendChild(o);
    });
    sel.onchange = () => { effect[p.key] = sel.value; setDirty(true); maybePushPreview(scene); };
    const wrap = el('div'); wrap.style.gridColumn = '2 / span 2'; wrap.appendChild(sel);
    grid.appendChild(wrap); return;
  }
  if (p.kind === 'rate') {
    // BPM-locked rate widget. Writes effect.rate_hz xor effect.rate_beats —
    // the engine treats rate_beats as winning when present, but we keep the
    // JSON honest by clearing the other field on toggle.
    renderRateWidget(grid, scene, effect, p);
    return;
  }
  if (p.kind === 'pulse_period') {
    renderPulsePeriodWidget(grid, scene, effect, p);
    return;
  }
}

// ---- Rate widget (Hz / Beats toggle) ----

const RATE_BPM_REF = 120;  // reference BPM used when pre-filling rate_beats from rate_hz.

function clearChildren(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function nearestChip(chips, value) {
  let best = chips[0], bestDist = Math.abs(value - chips[0]);
  for (const c of chips) {
    const d = Math.abs(value - c);
    if (d < bestDist) { best = c; bestDist = d; }
  }
  return best;
}

function rateMode(effect) {
  return (effect.rate_beats != null && effect.rate_beats > 0) ? 'beats' : 'hz';
}

function renderRateWidget(grid, scene, effect, p) {
  const k = el('div', 'k'); k.textContent = p.label || 'speed'; grid.appendChild(k);
  const wrap = el('div'); wrap.style.gridColumn = '2 / span 2';
  wrap.style.display = 'flex'; wrap.style.flexDirection = 'column'; wrap.style.gap = '6px';

  const toggle = el('select');
  ['hz', 'beats'].forEach(m => {
    const o = el('option'); o.value = m;
    o.textContent = m === 'hz' ? `Hz (${p.hz_unit || 'Hz'})` : 'Beats (BPM-locked)';
    toggle.appendChild(o);
  });
  toggle.value = rateMode(effect);
  wrap.appendChild(toggle);

  const body = el('div');
  wrap.appendChild(body);
  grid.appendChild(wrap);

  function paint() {
    clearChildren(body);
    if (rateMode(effect) === 'hz') {
      const cur = (effect.rate_hz != null) ? effect.rate_hz : p.hz_default;
      const range = el('input'); range.type = 'range';
      range.min = p.hz_min; range.max = p.hz_max; range.step = p.hz_step || 0.1;
      range.value = cur;
      const valSpan = el('div', 'val'); valSpan.textContent = (+cur).toFixed(2) + ' ' + (p.hz_unit || 'Hz');
      range.oninput = () => {
        const v = parseFloat(range.value);
        effect.rate_hz = v;
        delete effect.rate_beats;
        valSpan.textContent = v.toFixed(2) + ' ' + (p.hz_unit || 'Hz');
        setDirty(true); maybePushPreview(scene);
      };
      body.appendChild(range); body.appendChild(valSpan);
    } else {
      const chips = el('div'); chips.style.display = 'flex'; chips.style.gap = '4px'; chips.style.flexWrap = 'wrap';
      const cur = (effect.rate_beats != null) ? effect.rate_beats : p.beats_default;
      (p.beats_chips || [0.25, 0.5, 1, 2, 4, 8]).forEach(c => {
        const b = el('button');
        b.textContent = c < 1 ? `1/${Math.round(1/c)} beat` : `${c} beat${c === 1 ? '' : 's'}`;
        if (Math.abs(c - cur) < 1e-9) b.style.outline = '2px solid #f80';
        b.onclick = () => {
          effect.rate_beats = c; delete effect.rate_hz;
          setDirty(true); paint(); maybePushPreview(scene);
        };
        chips.appendChild(b);
      });
      const num = el('input'); num.type = 'number'; num.step = '0.05'; num.min = '0.05';
      num.value = cur; num.style.width = '80px';
      num.onchange = () => {
        const v = parseFloat(num.value);
        if (v > 0) {
          effect.rate_beats = v; delete effect.rate_hz;
          setDirty(true); paint(); maybePushPreview(scene);
        }
      };
      const valSpan = el('div', 'val');
      valSpan.textContent = `~${((RATE_BPM_REF / 60) / cur).toFixed(2)} ${p.hz_unit || 'Hz'} @ ${RATE_BPM_REF} BPM`;
      body.appendChild(chips); body.appendChild(num); body.appendChild(valSpan);
    }
  }

  toggle.onchange = () => {
    if (toggle.value === 'beats') {
      const curHz = (effect.rate_hz != null) ? effect.rate_hz : p.hz_default;
      const rawBeats = (RATE_BPM_REF / 60) / Math.max(0.01, curHz);
      effect.rate_beats = nearestChip(p.beats_chips || [0.25, 0.5, 1, 2, 4, 8], rawBeats);
      delete effect.rate_hz;
    } else {
      const curBeats = (effect.rate_beats != null) ? effect.rate_beats : p.beats_default;
      effect.rate_hz = +(((RATE_BPM_REF / 60) / Math.max(0.01, curBeats)).toFixed(2));
      delete effect.rate_beats;
    }
    setDirty(true); paint(); maybePushPreview(scene);
  };

  paint();
}

function pulsePeriodMode(effect) {
  return (effect.period_beats != null && effect.period_beats > 0) ? 'beats' : 'hz';
}

function renderPulsePeriodWidget(grid, scene, effect, p) {
  const k = el('div', 'k'); k.textContent = p.label || 'off / period'; grid.appendChild(k);
  const wrap = el('div'); wrap.style.gridColumn = '2 / span 2';
  wrap.style.display = 'flex'; wrap.style.flexDirection = 'column'; wrap.style.gap = '6px';

  const toggle = el('select');
  ['hz', 'beats'].forEach(m => {
    const o = el('option'); o.value = m;
    o.textContent = m === 'hz' ? 'off time (ms)' : 'period (BPM-locked)';
    toggle.appendChild(o);
  });
  toggle.value = pulsePeriodMode(effect);
  wrap.appendChild(toggle);

  const body = el('div');
  wrap.appendChild(body);
  grid.appendChild(wrap);

  function paint() {
    clearChildren(body);
    if (pulsePeriodMode(effect) === 'hz') {
      const cur = (effect.off_ms != null) ? effect.off_ms : p.off_default;
      const range = el('input'); range.type = 'range';
      range.min = p.off_min; range.max = p.off_max; range.step = 1;
      range.value = cur;
      const valSpan = el('div', 'val'); valSpan.textContent = cur + ' ' + (p.off_unit || 'ms');
      range.oninput = () => {
        const v = parseInt(range.value, 10);
        effect.off_ms = v; delete effect.period_beats;
        valSpan.textContent = v + ' ' + (p.off_unit || 'ms');
        setDirty(true); maybePushPreview(scene);
      };
      body.appendChild(range); body.appendChild(valSpan);
    } else {
      const chips = el('div'); chips.style.display = 'flex'; chips.style.gap = '4px'; chips.style.flexWrap = 'wrap';
      const cur = (effect.period_beats != null) ? effect.period_beats : p.beats_default;
      (p.beats_chips || [0.25, 0.5, 1, 2, 4, 8]).forEach(c => {
        const b = el('button');
        b.textContent = c < 1 ? `1/${Math.round(1/c)} beat` : `${c} beat${c === 1 ? '' : 's'}`;
        if (Math.abs(c - cur) < 1e-9) b.style.outline = '2px solid #f80';
        b.onclick = () => {
          effect.period_beats = c; delete effect.off_ms;
          setDirty(true); paint(); maybePushPreview(scene);
        };
        chips.appendChild(b);
      });
      const num = el('input'); num.type = 'number'; num.step = '0.05'; num.min = '0.05';
      num.value = cur; num.style.width = '80px';
      num.onchange = () => {
        const v = parseFloat(num.value);
        if (v > 0) {
          effect.period_beats = v; delete effect.off_ms;
          setDirty(true); paint(); maybePushPreview(scene);
        }
      };
      const valSpan = el('div', 'val');
      const periodMs = Math.round((60 / RATE_BPM_REF) * cur * 1000);
      valSpan.textContent = `~${periodMs} ms period @ ${RATE_BPM_REF} BPM`;
      body.appendChild(chips); body.appendChild(num); body.appendChild(valSpan);
    }
  }

  toggle.onchange = () => {
    if (toggle.value === 'beats') {
      const onMs = (effect.on_ms != null) ? effect.on_ms : 80;
      const curOff = (effect.off_ms != null) ? effect.off_ms : p.off_default;
      const periodMs = Math.max(1, onMs + curOff);
      const rawBeats = (RATE_BPM_REF / 60) * (periodMs / 1000);
      effect.period_beats = nearestChip(p.beats_chips || [0.25, 0.5, 1, 2, 4, 8], rawBeats);
      delete effect.off_ms;
    } else {
      const curBeats = (effect.period_beats != null) ? effect.period_beats : p.beats_default;
      const periodMs = Math.round((60 / RATE_BPM_REF) * curBeats * 1000);
      const onMs = (effect.on_ms != null) ? effect.on_ms : 80;
      effect.off_ms = Math.max(0, periodMs - onMs);
      delete effect.period_beats;
    }
    setDirty(true); paint(); maybePushPreview(scene);
  };

  paint();
}

function defaultEffect(type) {
  const base = { type };
  const schema = FIXTURES.dmx_effects.find(s => s.type === type);
  if (!schema) return base;
  (schema.params || []).forEach(p => {
    if (p.optional) return;
    if (p.kind === 'target') base.target = 'all';
    else if (p.kind === 'color') base[p.key || 'rgb'] = Array.isArray(p.default) ? p.default.slice() : [255, 120, 20];
    else if (p.kind === 'color_list') base.colors = Array.isArray(p.default) ? p.default.map(c => c.slice()) : [[255,0,0],[0,0,255]];
    else if (p.kind === 'int' || p.kind === 'float') base[p.key] = p.default != null ? p.default : 0;
    else if (p.kind === 'select') base[p.key] = p.default != null ? p.default : (p.options && p.options[0] && p.options[0].value);
    else if (p.kind === 'rate') base.rate_hz = p.hz_default != null ? p.hz_default : 1.0;
    else if (p.kind === 'pulse_period') base.off_ms = p.off_default != null ? p.off_default : 40;
  });
  return base;
}

// ---- CRUD ----
function createScene(category) {
  const s = {
    id: newId(),
    name: 'New ' + category + ' scene',
    category,
    layers: [defaultEffect('breathe')],
  };
  CATALOG.scenes.push(s);
  selectedId = s.id;
  setDirty(true);
  renderAll();
}
function duplicate(scene) {
  const copy = JSON.parse(JSON.stringify(scene));
  copy.id = newId();
  copy.name = scene.name + ' (copy)';
  CATALOG.scenes.push(copy);
  selectedId = copy.id;
  setDirty(true);
  renderAll();
}
function deleteScene(scene) {
  if (!confirm('Delete "' + scene.name + '"?')) return;
  CATALOG.scenes = CATALOG.scenes.filter(s => s.id !== scene.id);
  if (selectedId === scene.id) selectedId = null;
  setDirty(true);
  renderAll();
}

// ---- Save ----
async function save() {
  try {
    const r = await fetch('/api/scenes', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(CATALOG),
    });
    if (!r.ok) throw new Error((await r.json()).error || r.statusText);
    const j = await r.json();
    CATALOG = j;
    setDirty(false);
    toast('Saved ' + j.scenes.length + ' scenes');
    renderAll();
  } catch (e) { toast('Save failed: ' + e.message, true); }
}
$('btn-save').onclick = save;
window.addEventListener('beforeunload', (e) => {
  if (dirty) { e.preventDefault(); e.returnValue = ''; }
});
window.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 's') { e.preventDefault(); save(); }
});

// ---- Preview ----
async function startPreview(scene) {
  try {
    const r = await fetch('/api/preview/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({scene}),
    });
    if (!r.ok) throw new Error((await r.json()).error || r.statusText);
    previewing = true;
    renderEditor();
    toast('Previewing "' + scene.name + '"');
  } catch (e) { toast('Preview failed: ' + e.message, true); }
}
async function stopPreview() {
  try {
    await fetch('/api/preview/stop', {method: 'POST'});
    previewing = false;
    renderEditor();
  } catch (e) {}
}
function maybePushPreview(scene) {
  if (!previewing) return;
  clearTimeout(debouncePreview);
  debouncePreview = setTimeout(() => {
    fetch('/api/preview/update', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({scene}),
    }).catch(() => {});
  }, 120);
}

// ---- Two-step build preview ----
// Plays the CURRENT edited scene (unsaved is fine) through the live
// breakdown→buildup intensity ramp, so staged `enter_at` layers visibly
// switch on as the build climbs. Author Govee/base at enter_at 0, DMX
// add-ons higher (0.4/0.6/0.8), hit Two-step, and watch them accrete.
let twoStepRunning = false;
let twoStepTimer = null;
const TWO_STEP_STAGE_SECS = 15;
async function startTwoStep(scene) {
  try {
    const r = await fetch('/api/build/start', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({scene, stage_secs: TWO_STEP_STAGE_SECS}),
    });
    const j = await r.json();
    if (j.error) throw new Error(j.error);
    twoStepRunning = true; previewing = false; renderEditor();
    toast('Two-step: ' + scene.name + ' — breakdown → buildup (' + (TWO_STEP_STAGE_SECS*2) + 's)');
    clearTimeout(twoStepTimer);
    twoStepTimer = setTimeout(() => { twoStepRunning = false; renderEditor(); }, TWO_STEP_STAGE_SECS*2000 + 500);
  } catch (e) { toast('Two-step failed: ' + e.message, true); }
}
async function stopTwoStep() {
  clearTimeout(twoStepTimer);
  twoStepRunning = false;
  try { await fetch('/api/build/stop', {method: 'POST'}); } catch (e) {}
  renderEditor();
}
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    # ---- GET ----

    def do_GET(self):
        if self.path == "/api/state":
            with _state_lock:
                # Project the active deck's track entry into `track` so the
                # client doesn't have to know about tracks_by_deck.
                snap = dict(_state)
                deck = snap.get("deck")
                tbd = snap.get("tracks_by_deck") or {}
                snap["track"] = tbd.get(deck) if deck is not None else None
                self._send_json(snap)
            return
        if self.path == "/api/scenes":
            store = _lazy_store()
            self._send_json(store.snapshot())
            return
        if self.path == "/api/fixtures":
            self._send_json(FIXTURES)
            return
        if self.path == "/api/govee/presets":
            self._send_json({
                "presets": _govee_presets_for_ui(),
                "skus": _govee_skus_known(),
            })
            return
        if self.path == "/api/lighting/status":
            dl = _lazy_direct_lights()
            if dl is None:
                self._send_json({"mode": None, "preview": False, "engine_running": False, "available": False})
                return
            st = dl.status()
            st["available"] = True
            self._send_json(st)
            return
        if self.path == "/api/intensity":
            dl = _lazy_direct_lights()
            if dl is None:
                self._send_json({"available": False, "value": 1.0, "manual": None})
                return
            st = dl.intensity_status()
            st["available"] = True
            self._send_json(st)
            return
        if self.path in ("/", "/index.html"):
            self._send_html(INDEX_HTML)
            return
        if self.path in ("/editor", "/editor/"):
            self._send_html(EDITOR_HTML)
            return
        self.send_response(404); self.end_headers()

    # ---- PUT / POST ----

    def do_PUT(self):
        if self.path == "/api/scenes":
            try:
                body = self._read_body()
                store = _lazy_store()
                store.save(body)
                self._send_json(store.snapshot())
            except Exception as e:
                self._send_json({"error": str(e)}, status=400)
            return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == "/api/preview/start":
            dl = _lazy_direct_lights()
            if dl is None:
                self._send_json({"error": "direct_lights not available (is main.py running?)"}, status=503)
                return
            try:
                body = self._read_body()
                scene = body.get("scene")
                if not isinstance(scene, dict):
                    raise ValueError("missing scene")
                dl.apply_scene_preview(scene)
                self._send_json({"ok": True, "scene_id": scene.get("id")})
            except Exception as e:
                self._send_json({"error": str(e)}, status=400)
            return
        if self.path == "/api/preview/update":
            dl = _lazy_direct_lights()
            if dl is None:
                self._send_json({"error": "direct_lights not available"}, status=503)
                return
            try:
                body = self._read_body()
                scene = body.get("scene")
                dl.apply_scene_preview(scene)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, status=400)
            return
        if self.path == "/api/preview/stop":
            dl = _lazy_direct_lights()
            if dl is None:
                self._send_json({"ok": True, "noop": True})
                return
            try:
                dl.stop_preview(resume_mode=True)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, status=400)
            return
        if self.path == "/api/blackout":
            dl = _lazy_direct_lights()
            if dl is None:
                self._send_json({"error": "direct_lights not available (is main.py running?)"}, status=503)
                return
            try:
                dl.blackout()
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, status=400)
            return
        if self.path == "/api/scene/refresh":
            dl = _lazy_direct_lights()
            if dl is None:
                self._send_json({"error": "direct_lights not available"}, status=503)
                return
            try:
                refreshed = dl.refresh_scene()
                self._send_json({"ok": True, "refreshed": bool(refreshed)})
            except Exception as e:
                self._send_json({"error": str(e)}, status=400)
            return
        if self.path == "/api/intensity":
            dl = _lazy_direct_lights()
            if dl is None:
                self._send_json({"error": "direct_lights not available"}, status=503)
                return
            try:
                body = self._read_body()
                # null clears the manual override and resumes the auto curve.
                value = body.get("value", None)
                dl.set_intensity_manual(value)
                self._send_json(dl.intensity_status())
            except Exception as e:
                self._send_json({"error": str(e)}, status=400)
            return
        if self.path == "/api/build/start":
            dl = _lazy_direct_lights()
            if dl is None:
                self._send_json({"error": "direct_lights not available (is main.py running?)"}, status=503)
                return
            try:
                body = self._read_body()
                stage_secs = float(body.get("stage_secs", 15.0))
                stage_secs = max(2.0, min(120.0, stage_secs))
                # Priority: an inline scene dict (editor's live, possibly-unsaved
                # scene) > a saved scene_id > a random build-category scene.
                raw = body.get("scene")
                if isinstance(raw, dict) and raw.get("layers") is not None:
                    scene = raw
                else:
                    store = _lazy_store()
                    scene_id = body.get("scene_id")
                    if scene_id:
                        scene = next((s for s in store.scenes if s.get("id") == scene_id), None)
                        if scene is None:
                            raise ValueError(f"no scene with id {scene_id!r}")
                    else:
                        scene = store.pick_scene("build")
                        if scene is None:
                            raise ValueError("no scenes in category 'build'")
                _start_build(scene, stage_secs)
                self._send_json({"ok": True, "scene": scene.get("name"),
                                 "scene_id": scene.get("id"), "stage_secs": stage_secs})
            except Exception as e:
                self._send_json({"error": str(e)}, status=400)
            return
        if self.path == "/api/build/stop":
            _build_stop.set()
            dl = _lazy_direct_lights()
            if dl is not None:
                try:
                    dl.set_intensity_manual(None)
                    dl.stop_preview(resume_mode=True)
                except Exception:
                    pass
            self._send_json({"ok": True})
            return
        self.send_response(404); self.end_headers()


def start_background(port: int = PORT) -> None:
    """Run dashboard in a background thread (called from main.py)."""
    threading.Thread(target=tail_log, daemon=True).start()
    # Threaded server: one slow handler (e.g. a preview that blocks on offline
    # Govee) must never freeze the whole UI. Each request gets its own thread;
    # shared state is already guarded by locks in direct_lights / _state.
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="dashboard-http").start()
    print(f"[dashboard] serving http://localhost:{port} (monitor + /editor)", flush=True)


def main() -> None:
    threading.Thread(target=tail_log, daemon=True).start()
    print(f"[dashboard] serving http://localhost:{PORT} (monitor + /editor)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
