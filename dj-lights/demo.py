#!/usr/bin/env python3
"""demo.py — soundcheck & demo any light mode, scene, or fixture.

This is the agent-facing "show me the lights" tool. It drives the *same*
renderer the live set uses (`direct_lights` → `SceneEngine`), so what you see
here is exactly what plays during a track — no separate demo code path to rot.

What it can spotlight (run `demo.py list` to see them all):
  * fixtures   — each Tetra 12 wash, each Tetra Bar zone, and Govee, one at a
                 time (raw hardware / wiring / addressing check).
  * layers     — each of the 11 DMX layer-type primitives (chase, bar_chase,
                 wash_chase, pulse, strobe, random_flash, popcorn, ...) shown
                 in isolation with a vivid canned example.
  * scenes     — any of the named scenes in scenes.json, by id or name.
  * categories — all scenes in a PSSI category (intro/groove/buildup/...),
                 played through that category's live intensity curve.

Default (no args) = `soundcheck`: a ~1 min sweep of every fixture + every
primitive. Everything is configurable (`--secs`, `--color`, `--bpm`, ...).

How it reaches the lights (auto-detected, override with --driver):
  * If the dashboard is up on :8787, scene previews route through its HTTP API.
    No fight for the DMX device — safe to run *during a live set* (each item
    hot-swaps as a preview; the set's live mode resumes when the demo ends).
  * Otherwise it drives `direct_lights` in-process and owns DMX + Govee
    directly. main.py must NOT be running in this case (it holds the FTDI
    device); if DMX is unavailable the demo prints a warning and runs
    Govee-only.

Examples (run from the repo root, using the venv):
    ./.venv/bin/python dj-lights/demo.py                  # full soundcheck
    ./.venv/bin/python dj-lights/demo.py list
    ./.venv/bin/python dj-lights/demo.py scene Meteor
    ./.venv/bin/python dj-lights/demo.py category drop
    ./.venv/bin/python dj-lights/demo.py layer bar_chase
    ./.venv/bin/python dj-lights/demo.py fixture wash_1
    ./.venv/bin/python dj-lights/demo.py soundcheck --secs 4 --color 00ffff
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
SCENES_PATH = os.path.join(BASE, "scenes.json")
DASHBOARD_URL = "http://127.0.0.1:8787"

# PSSI categories, in set order. `build` is the combined breakdown→buildup
# climb (see analysis._coalesce_builds); live, its intensity follows the
# sub-phrase, but for the demo we ramp the whole span from the breakdown floor
# to full so add-on (enter_at) layers visibly switch on as it climbs.
CATEGORIES = ["intro", "groove", "build", "breakdown", "drop", "outro"]

# Per-category auto intensity curve (low, high) over phrase progress. The shared
# categories read the live curve from direct_lights so this never drifts; `build`
# is demo-only (the live engine drives it per sub-phrase, not as one (lo, hi)).
from direct_lights import _INTENSITY_CURVE_BY_MODE as _LIVE_CURVE  # noqa: E402

INTENSITY_CURVE_BY_CATEGORY: dict[str, tuple[float, float]] = {
    "intro":     _LIVE_CURVE["intro"],
    "groove":    _LIVE_CURVE["groove"],
    "build":     (_LIVE_CURVE["breakdown"][0], _LIVE_CURVE["buildup"][1]),
    "breakdown": _LIVE_CURVE["breakdown"],
    "drop":      _LIVE_CURVE["drop"],
    "outro":     _LIVE_CURVE["outro"],
}

# Vivid, deliberately-obvious example of every DMX layer primitive. Field
# names match the renderers in scene_engine.py — solid/dual_wash aren't used
# by any scene in scenes.json, the rest are tuned brighter/faster than a
# typical authored scene so the primitive reads clearly on its own.
DMX_PRIMITIVE_EXAMPLES: dict[str, dict] = {
    "solid":         {"type": "solid", "target": "all", "rgb": [255, 255, 255], "dim": 220},
    "breathe":       {"type": "breathe", "target": "all", "colors": [[0, 80, 255], [255, 0, 180]],
                      "hz": 0.5, "dim_min": 10, "dim_max": 220},
    "chase":         {"type": "chase", "colors": [[255, 0, 0], [0, 0, 255], [0, 255, 80]],
                      "rate_hz": 4.0, "dim_active": 220, "dim_rest": 0},
    "bar_chase":     {"type": "bar_chase", "colors": [[255, 0, 0], [255, 120, 0], [255, 255, 0], [0, 200, 255]],
                      "direction": "wrap", "tail": 1, "rate_hz": 3.0, "dim_active": 255, "dim_rest": 0},
    "wash_pingpong": {"type": "wash_pingpong", "colors": [[255, 80, 0], [0, 80, 255]],
                      "rate_hz": 2.0, "dim_active": 220, "dim_rest": 0},
    "wash_chase":    {"type": "wash_chase", "colors": [[255, 80, 0], [0, 120, 255]],
                      "hz": 0.5, "dim_min": 0, "dim_max": 220},
    "dual_wash":     {"type": "dual_wash", "rgb_left": [255, 0, 128], "rgb_right": [0, 200, 255], "dim": 210},
    "pulse":         {"type": "pulse", "target": "all",
                      "colors": [[255, 0, 0], [0, 0, 255], [255, 0, 180], [0, 255, 180]],
                      "on_ms": 90, "off_ms": 60, "dim": 220},
    "strobe":        {"type": "strobe", "target": "all", "rgb": [255, 255, 255], "dim": 255, "rate": 120},
    "random_flash":  {"type": "random_flash", "target": "all", "rgb": [255, 255, 255], "dim": 200,
                      "min_gap_s": 0.4, "max_gap_s": 1.0, "double_chance": 40, "flash_ms": 70},
    "popcorn":       {"type": "popcorn", "target": "all",
                      "colors": [[255, 0, 0], [0, 120, 255], [0, 255, 120], [255, 0, 180]],
                      "max_brightness": 255, "min_brightness": 0, "flash_rate_hz": 6.0, "decay_ms": 250},
}

PRIMITIVE_BLURB: dict[str, str] = {
    "solid":         "static color on every fixture",
    "breathe":       "slow color-cycling brightness pulse",
    "chase":         "color steps across wash 1 → wash 2 → bar",
    "bar_chase":     "color runs across the Tetra Bar's 4 zones",
    "wash_pingpong": "hard alternation between the two washes",
    "wash_chase":    "smooth crossfade between the two washes",
    "dual_wash":     "static split — wash 1 vs wash 2 different colors",
    "pulse":         "on/off flashing, color advances each cycle",
    "strobe":        "software strobe (bright flashes on a dark baseline)",
    "random_flash":  "sporadic camera-flash hits",
    "popcorn":       "scattered per-zone color pops",
    "bar_shoot":     "bursts shooting across the Tetra Bar",
    "acid_kaleidoscope": "kaleidoscopic color churn across all segments",
    "acid_cathedral":    "strobing cathedral-light acid look",
    "acid_bloom":        "blooming fractal color wash",
}


def dmx_primitives() -> list[str]:
    """The authoritative list of DMX layer primitives, in renderer-declaration
    order. Sourced from scene_engine.DMX_LAYER_RENDERERS so new primitives show
    up here automatically. Falls back to the curated blurb keys if the import
    fails (e.g. scene_engine moved)."""
    try:
        sys.path.insert(0, BASE)
        import scene_engine  # type: ignore  (stdlib-only import, no hardware)
        return list(scene_engine.DMX_LAYER_RENDERERS.keys())
    except Exception:
        return list(PRIMITIVE_BLURB.keys())


def blurb(ltype: str) -> str:
    return PRIMITIVE_BLURB.get(ltype, "(layer primitive)")

# Each fixture target → human description. DMX targets map to a `solid` layer;
# "govee" maps to a govee_rgb layer hitting every device.
FIXTURE_TARGETS: dict[str, str] = {
    "wash_1":  "Tetra 12 wash #1 (DMX addr 1)",
    "wash_2":  "Tetra 12 wash #2 (DMX addr 7)",
    "wash":    "both Tetra 12 washes",
    "bar_z1":  "Tetra Bar zone 1 (DMX addr 13)",
    "bar_z2":  "Tetra Bar zone 2 (DMX addr 19)",
    "bar_z3":  "Tetra Bar zone 3 (DMX addr 25)",
    "bar_z4":  "Tetra Bar zone 4 (DMX addr 31)",
    "bar_all": "Tetra Bar (all 4 zones)",
    "all":     "every DMX fixture (both washes + 4 bar zones)",
    "govee":   "all Govee LAN/cloud devices",
}

# Fixtures hit by the default soundcheck, in order. One wash, one wash, each
# bar zone, then Govee — enough to confirm wiring without redundant "all" hits.
SOUNDCHECK_FIXTURES = ["wash_1", "wash_2", "bar_z1", "bar_z2", "bar_z3", "bar_z4", "govee"]

BLACKOUT_SCENE = {"id": "demo-blackout", "blackout": True}


# ---------------------------------------------------------------------------
# Catalog helpers
# ---------------------------------------------------------------------------

def load_scenes() -> list[dict]:
    with open(SCENES_PATH) as f:
        return json.load(f)["scenes"]


def find_scene(scenes: list[dict], query: str) -> dict | None:
    """Match a scene by id (exact) or name (case-insensitive, exact then
    substring)."""
    q = query.strip().lower()
    for s in scenes:
        if s.get("id", "").lower() == q:
            return s
    for s in scenes:
        if s.get("name", "").lower() == q:
            return s
    matches = [s for s in scenes if q in s.get("name", "").lower()]
    return matches[0] if len(matches) == 1 else (matches[0] if matches else None)


def hex_to_rgb(h: str) -> list[int]:
    h = h.lstrip("#")
    return [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)]


def fixture_scene(target: str, rgb: list[int]) -> dict:
    """Build a one-layer scene that lights a single fixture target."""
    if target == "govee":
        return {"id": "demo-fixture-govee", "name": "fixture:govee",
                "layers": [{"type": "govee_rgb", "rgb": rgb, "brightness": 100}]}
    return {"id": f"demo-fixture-{target}", "name": f"fixture:{target}",
            "layers": [{"type": "solid", "target": target, "rgb": rgb, "dim": 220}]}


def example_layer(ltype: str, scenes: list[dict]) -> dict:
    """A renderable example of one primitive. Prefer a curated vivid example;
    else the first real occurrence in scenes.json; else a bare layer that
    leans on the renderer's own defaults."""
    if ltype in DMX_PRIMITIVE_EXAMPLES:
        return dict(DMX_PRIMITIVE_EXAMPLES[ltype])
    for s in scenes:
        for L in s.get("layers", []):
            if L.get("type") == ltype:
                return dict(L)
    return {"type": ltype}


def layer_scene(ltype: str, scenes: list[dict]) -> dict:
    return {"id": f"demo-layer-{ltype}", "name": f"layer:{ltype}",
            "layers": [example_layer(ltype, scenes)]}


# ---------------------------------------------------------------------------
# Drivers — same surface, two transports
# ---------------------------------------------------------------------------

class LocalDriver:
    """Drives direct_lights in-process. Owns DMX + Govee directly."""

    name = "in-process (direct_lights)"

    def __init__(self, bpm: float):
        sys.path.insert(0, BASE)
        import direct_lights  # type: ignore
        self.dl = direct_lights
        self.dl.set_bpm_provider(lambda: bpm)
        self.dl.warm_up()
        dmx = self.dl._ensure_dmx()
        if type(dmx).__name__ == "_NullDMX":
            print("  ⚠ DMX unavailable (no /dev/cu.usbserial, or held by a running "
                  "main.py). Running Govee-only.\n", flush=True)

    def play(self, scene: dict) -> None:
        self.dl.apply_scene_preview(scene)

    def set_intensity(self, value: float | None) -> None:
        self.dl.set_intensity_manual(value)

    def finish(self, blackout: bool) -> None:
        self.dl.set_intensity_manual(None)
        if blackout:
            self.dl.blackout()


class HttpDriver:
    """Drives the running dashboard's preview API. No DMX contention."""

    name = "dashboard HTTP (:8787)"

    def __init__(self, base: str):
        self.base = base

    def _post(self, path: str, body: dict) -> None:
        data = json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            urllib.request.urlopen(req, timeout=2.0).read()
        except urllib.error.URLError as e:
            print(f"  ⚠ {path} failed: {e}", flush=True)

    def play(self, scene: dict) -> None:
        self._post("/api/preview/start", {"scene": scene})

    def set_intensity(self, value: float | None) -> None:
        self._post("/api/intensity", {"value": value})

    def finish(self, blackout: bool) -> None:
        self._post("/api/intensity", {"value": None})
        # preview/stop resumes the live mode if a set is playing; blacks out
        # otherwise. /api/blackout forces dark regardless.
        self._post("/api/blackout" if blackout else "/api/preview/stop", {})


def make_driver(mode: str, bpm: float):
    if mode == "local":
        return LocalDriver(bpm)
    if mode == "http":
        return HttpDriver(DASHBOARD_URL)
    # auto: prefer the dashboard if it's up and direct_lights is reachable.
    try:
        with urllib.request.urlopen(DASHBOARD_URL + "/api/lighting/status", timeout=0.7) as r:
            if json.load(r).get("available"):
                return HttpDriver(DASHBOARD_URL)
    except Exception:
        pass
    return LocalDriver(bpm)


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

def play_item(driver, scene: dict, secs: float, *, intensity=None, curve=None) -> None:
    """Run one scene for `secs`. `intensity` pins a fixed value; `curve` ramps
    (lo, hi) across the dwell to mimic a category's live auto-curve."""
    driver.play(scene)
    if curve is None:
        driver.set_intensity(intensity)  # None → auto (plays as authored, i=1.0)
        time.sleep(secs)
        return
    lo, hi = curve
    steps = max(1, int(secs / 0.4))
    for i in range(steps):
        progress = i / max(1, steps - 1)
        driver.set_intensity(lo + (hi - lo) * progress)
        time.sleep(secs / steps)


def _ramp_intensity(driver, lo: float, hi: float, secs: float) -> None:
    """Drive intensity from lo→hi over `secs`, mimicking phrase progress."""
    steps = max(1, int(secs / 0.3))
    for i in range(steps):
        progress = i / max(1, steps - 1)
        driver.set_intensity(lo + (hi - lo) * progress)
        time.sleep(secs / steps)


def run_build_sequence(driver, scene: dict, stage_secs: float, hold: bool) -> None:
    """Play ONE scene through the live two-step build: stage 1 follows the
    breakdown curve (calm base, DMX gated off by enter_at), then stage 2
    follows the buildup curve (DMX add-on layers accrete and ramp to the drop).

    This is exactly what the live engine does across a breakdown→buildup
    section, just time-driven instead of beat-driven — no DJ gear needed.
    """
    bd = _LIVE_CURVE["breakdown"]   # (0.12, 0.32) — DMX add-ons stay gated
    bu = _LIVE_CURVE["buildup"]     # (0.30, 1.00) — add-ons enter + ramp
    print(f"▶ driver: {driver.name}   build two-step: "
          f"{stage_secs:g}s breakdown → {stage_secs:g}s buildup  "
          f"({scene['name']})\n", flush=True)
    try:
        driver.play(scene)
        print(f"  stage 1/2  BREAKDOWN  i {bd[0]:.2f}→{bd[1]:.2f}   "
              f"Govee/base only — DMX gated", flush=True)
        _ramp_intensity(driver, bd[0], bd[1], stage_secs)
        print(f"  stage 2/2  BUILDUP    i {bu[0]:.2f}→{bu[1]:.2f}   "
              f"DMX accretes + ramps to the drop", flush=True)
        _ramp_intensity(driver, bu[0], bu[1], stage_secs)
    except KeyboardInterrupt:
        print("\n  interrupted — cleaning up", flush=True)
    finally:
        driver.finish(blackout=not hold)
        print("\n✓ done" + ("  (held last look)" if hold else "  (blackout)"), flush=True)


def run_sequence(driver, items: list[tuple[str, str, dict]], args) -> None:
    """items: list of (kind, label, scene). Plays each for args.secs with a
    short blackout gap between, then finishes."""
    total = len(items)
    gap_s = args.gap / 1000.0
    print(f"▶ driver: {driver.name}   {total} item(s) × {args.secs:g}s\n", flush=True)
    try:
        for i, (kind, label, scene) in enumerate(items, 1):
            print(f"  [{i:2d}/{total}] {kind:9s} {label}", flush=True)
            curve = None
            intensity = args.intensity
            if kind == "category" and args.intensity is None:
                curve = INTENSITY_CURVE_BY_CATEGORY.get(scene.get("_category"))
            play_item(driver, scene, args.secs, intensity=intensity, curve=curve)
            if gap_s > 0 and i < total:
                driver.play(BLACKOUT_SCENE)
                time.sleep(gap_s)
    except KeyboardInterrupt:
        print("\n  interrupted — cleaning up", flush=True)
    finally:
        driver.finish(blackout=not args.hold)
        print("\n✓ done" + ("  (held last look)" if args.hold else "  (blackout)"), flush=True)


# ---------------------------------------------------------------------------
# Item builders per command
# ---------------------------------------------------------------------------

def items_soundcheck(scenes, rgb) -> list[tuple[str, str, dict]]:
    items: list[tuple[str, str, dict]] = []
    for t in SOUNDCHECK_FIXTURES:
        items.append(("fixture", f"{t:8s} — {FIXTURE_TARGETS[t]}", fixture_scene(t, rgb)))
    for ltype in dmx_primitives():
        items.append(("layer", f"{ltype:17s} — {blurb(ltype)}", layer_scene(ltype, scenes)))
    return items


def cmd_list(scenes) -> None:
    print("FIXTURES (demo.py fixture <name>):")
    for t, desc in FIXTURE_TARGETS.items():
        print(f"  {t:9s} {desc}")
    print("\nLAYER PRIMITIVES (demo.py layer <type>):")
    for t in dmx_primitives():
        print(f"  {t:17s} {blurb(t)}")
    print("\nCATEGORIES (demo.py category <name>):")
    for c in CATEGORIES:
        n = sum(1 for s in scenes if s.get("category") == c)
        lo, hi = INTENSITY_CURVE_BY_CATEGORY[c]
        print(f"  {c:10s} {n} scene(s)   intensity {lo:.2f}→{hi:.2f}")
    print("\nSCENES (demo.py scene <id|name>):")
    for c in CATEGORIES:
        for s in scenes:
            if s.get("category") != c:
                continue
            layers = ", ".join(L["type"] for L in s.get("layers", [])) or "(empty)"
            print(f"  {s['id']:24s} {s['name']:22s} [{c}]  {layers}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="demo.py",
        description="Soundcheck & demo any light mode, scene, or fixture.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run `demo.py list` to see every fixture, primitive, category, and scene.",
    )
    p.add_argument("command", nargs="?", default="soundcheck",
                   choices=["soundcheck", "list", "scene", "category", "layer", "fixture", "build"],
                   help="what to demo (default: soundcheck — sweep all fixtures + primitives)")
    p.add_argument("target", nargs="?", help="scene id/name, category, layer type, or fixture name")
    p.add_argument("--secs", type=float, default=2.5, help="seconds per item (default 2.5)")
    p.add_argument("--stage-secs", type=float, default=15.0,
                   help="seconds per stage for `build` (default 15 ≈ 8 bars @128bpm)")
    p.add_argument("--gap", type=float, default=250, help="blackout gap between items, ms (default 250)")
    p.add_argument("--bpm", type=float, default=128.0, help="BPM for BPM-locked rates (local driver only)")
    p.add_argument("--intensity", type=float, default=None,
                   help="pin intensity 0..1 (overrides a category's auto-curve)")
    p.add_argument("--color", default="ffffff", help="hex color for fixture checks (default ffffff)")
    p.add_argument("--hold", action="store_true", help="leave the last look on instead of blacking out")
    p.add_argument("--driver", choices=["auto", "local", "http"], default="auto",
                   help="how to reach the lights (default: auto-detect dashboard)")
    return p


def main() -> int:
    args = build_parser().parse_args()
    scenes = load_scenes()

    if args.command == "list":
        cmd_list(scenes)
        return 0

    if args.command == "build":
        # Full two-step build: one scene through breakdown→buildup curves.
        if args.target:
            chosen = [find_scene(scenes, args.target)]
            if chosen[0] is None:
                print(f"no scene matching {args.target!r} — try `demo.py list`", file=sys.stderr)
                return 2
        else:
            chosen = [s for s in scenes if s.get("category") == "build"]
            if not chosen:
                print("no scenes in category 'build'", file=sys.stderr)
                return 2
        driver = make_driver(args.driver, args.bpm)
        gap_s = args.gap / 1000.0
        for n, s in enumerate(chosen, 1):
            if len(chosen) > 1:
                print(f"[{n}/{len(chosen)}]", flush=True)
            run_build_sequence(driver, s, args.stage_secs, args.hold)
            if gap_s > 0 and n < len(chosen):
                driver.play(BLACKOUT_SCENE)
                time.sleep(gap_s)
        return 0

    try:
        rgb = hex_to_rgb(args.color)
    except (ValueError, IndexError):
        print(f"bad --color {args.color!r} (expected 6-digit hex like 00ffff)", file=sys.stderr)
        return 2

    # Resolve the command into a list of (kind, label, scene) items.
    if args.command == "soundcheck":
        items = items_soundcheck(scenes, rgb)
    elif args.command == "scene":
        if not args.target:
            print("usage: demo.py scene <id|name>   (see `demo.py list`)", file=sys.stderr)
            return 2
        s = find_scene(scenes, args.target)
        if s is None:
            print(f"no scene matching {args.target!r} — try `demo.py list`", file=sys.stderr)
            return 2
        items = [("scene", f"{s['name']} ({s['id']})", s)]
    elif args.command == "category":
        if args.target not in INTENSITY_CURVE_BY_CATEGORY:
            print(f"unknown category {args.target!r} — one of {CATEGORIES}", file=sys.stderr)
            return 2
        cat_scenes = [s for s in scenes if s.get("category") == args.target]
        if not cat_scenes:
            print(f"no scenes in category {args.target!r}", file=sys.stderr)
            return 2
        items = []
        for s in cat_scenes:
            s = dict(s, _category=args.target)
            items.append(("category", f"{s['name']} ({args.target})", s))
    elif args.command == "layer":
        prims = dmx_primitives()
        if args.target not in prims:
            print(f"unknown layer {args.target!r} — one of {prims}", file=sys.stderr)
            return 2
        items = [("layer", f"{args.target} — {blurb(args.target)}", layer_scene(args.target, scenes))]
    elif args.command == "fixture":
        if args.target not in FIXTURE_TARGETS:
            print(f"unknown fixture {args.target!r} — one of {list(FIXTURE_TARGETS)}", file=sys.stderr)
            return 2
        items = [("fixture", f"{args.target} — {FIXTURE_TARGETS[args.target]}", fixture_scene(args.target, rgb))]
    else:  # unreachable (argparse choices)
        return 2

    driver = make_driver(args.driver, args.bpm)
    run_sequence(driver, items, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
