"""Live dashboard + scene editor for dj-lights-mvp2.

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
from http.server import BaseHTTPRequestHandler, HTTPServer
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
    "categories": ["intro", "groove", "buildup", "breakdown", "drop", "outro"],
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
                {"key": "rate_hz", "kind": "float", "min": 0.1, "max": 20.0, "step": 0.1,
                 "default": 1.0, "label": "speed", "unit": "toggles/s"},
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
                {"key": "rate_hz", "kind": "float", "min": 0.5, "max": 20.0, "step": 0.1,
                 "default": 4.0, "label": "speed", "unit": "zones/s"},
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
                {"key": "off_ms", "kind": "int", "min": 0, "max": 5000, "default": 40,
                 "label": "off time", "unit": "ms"},
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
    ],
}


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
  .mode-buildup   { background: #8a5500; }
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
  intro: '#1a2b4a', groove: '#1a4a2b', buildup: '#8a5500',
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
.cat-dot-buildup   { background: #8a5500; }
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
    wrap.style.gridColumn = '2 / span 2'; grid.appendChild(wrap); return;
  }
  if (p.kind === 'color_list') {
    const k = el('div','k'); k.textContent = p.label || 'colors'; grid.appendChild(k);
    const wrap = el('div', 'color-list'); wrap.style.gridColumn = '2 / span 2';
    effect.colors = effect.colors || [[255,0,0]];
    effect.colors.forEach((c, i) => {
      const ci = el('input'); ci.type = 'color'; ci.value = rgbToHex(c);
      ci.oninput = () => { effect.colors[i] = hexToRgb(ci.value); setDirty(true); maybePushPreview(scene); };
      wrap.appendChild(ci);
      if (effect.colors.length > 1) {
        const rm = el('button'); rm.textContent = 'x';
        rm.onclick = () => { effect.colors.splice(i,1); setDirty(true); renderEditor(); maybePushPreview(scene); };
        wrap.appendChild(rm);
      }
    });
    const add = el('button'); add.textContent = '+';
    add.onclick = () => { effect.colors.push([255,255,255]); setDirty(true); renderEditor(); maybePushPreview(scene); };
    wrap.appendChild(add); grid.appendChild(wrap); return;
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
        self.send_response(404); self.end_headers()


def start_background(port: int = PORT) -> None:
    """Run dashboard in a background thread (called from main.py)."""
    threading.Thread(target=tail_log, daemon=True).start()
    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="dashboard-http").start()
    print(f"[dashboard] serving http://localhost:{port} (monitor + /editor)", flush=True)


def main() -> None:
    threading.Thread(target=tail_log, daemon=True).start()
    print(f"[dashboard] serving http://localhost:{PORT} (monitor + /editor)", flush=True)
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
