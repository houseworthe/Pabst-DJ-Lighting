"""
direct_lights — drives DMX (Enttec) + Govee for one active scene at a time.

Scenes are declarative dicts loaded from scenes.json. On mode change we pick
a random scene from the matching category and hand it to a SceneEngine. The
engine owns one render thread that paints DMX every 30ms and fires Govee
layers once on entry.

Two entry points:
    apply_mode(mode)          -> picks random scene for category, runs it
    apply_scene_preview(scene) -> runs an arbitrary scene dict (for the editor)

Both route through the same _run_scene() so a preview transparently replaces
a live scene (and vice versa).
"""
from __future__ import annotations

import glob
import os
import sys
import threading
import time
from typing import Callable, Optional

BASE = os.path.dirname(__file__)

from dmx_controller import DMX  # type: ignore
from govee_lan import GoveeClient
from scene_engine import SceneEngine
from scenes_store import get_store

_dmx: Optional[DMX] = None
_dmx_last_try: float = 0.0
_dmx_retry_interval: float = 5.0
_govee = GoveeClient()

# Live BPM source. main.py registers a callable that returns the active
# deck's bpm (or None when no deck is active / metadata is missing).
# _bpm_cache holds the last value we accepted in [60, 200] — the realistic
# DJ range. Reads outside that window are ignored, so a glitched 0 or 9999
# packet doesn't snap the chase. Init 120 keeps preview / cold-start sensible.
_BPM_FILTER_LO = 60.0
_BPM_FILTER_HI = 200.0
_bpm_cache: float = 120.0
_bpm_source: Optional[Callable[[], Optional[float]]] = None


def set_bpm_provider(fn: Callable[[], Optional[float]]) -> None:
    """Register the live-BPM callable. Called once at startup from main.py."""
    global _bpm_source
    _bpm_source = fn


def _bpm_for_engine() -> float:
    """Return live BPM for the SceneEngine. Updates and returns the cache."""
    global _bpm_cache
    if _bpm_source is not None:
        try:
            raw = _bpm_source()
        except Exception:
            raw = None
        if raw is not None:
            try:
                bpm = float(raw)
            except (TypeError, ValueError):
                bpm = 0.0
            if _BPM_FILTER_LO <= bpm <= _BPM_FILTER_HI:
                _bpm_cache = bpm
    return _bpm_cache


# Intensity automation. Each scene runs at a scalar 0..1 — at 1.0 every layer
# plays as authored, below 1 the engine attenuates dim/rate/strobe/density
# fields per layer type (see scene_engine._apply_intensity).
#
# Two drivers feed this scalar:
#   * Manual override — dashboard slider sets a fixed value until cleared.
#   * Auto curve      — driven by PSSI phrase progress, varies by mode:
#                       buildup ramps up, drop starts hot and decays, etc.
# Manual wins. Cleared with set_intensity_manual(None) → auto resumes.

_intensity_lock = threading.Lock()
_intensity_phrase: dict = {"start_beat": 0, "end_beat": 0, "mode": None}
_intensity_manual: Optional[float] = None  # None = auto curve drives
_beat_source: Optional[Callable[[], Optional[int]]] = None


# Per-mode auto curve. (low, high) maps progress-through-phrase to intensity.
# Buildup ramps; drop starts hot and decays (the "explode then ease" feel);
# steady categories are flat. Tuned to taste — tweak here, no engine changes.
# Keyed by a phrase's `intensity_mode` (set in analysis.normalize_phrases). For
# a combined `build` section the sub-phrases carry intensity_mode "breakdown"
# (the calm base — low, so add-on layers stay gated off) then "buildup" (the
# ramp to full at the drop, which brings the add-ons in). The lone "build" entry
# is a defensive fallback if a phrase ever reaches here without a sub-mode.
_INTENSITY_CURVE_BY_MODE: dict[str, tuple[float, float]] = {
    "intro":     (0.45, 0.45),
    "groove":    (0.75, 0.75),
    "build":     (0.30, 1.00),
    "buildup":   (0.30, 1.00),
    "breakdown": (0.12, 0.32),
    "drop":      (1.00, 0.55),
    "outro":     (0.50, 0.50),
}


def set_beat_provider(fn: Callable[[], Optional[int]]) -> None:
    """Register the live beat-count callable. Called once at startup from main.py."""
    global _beat_source
    _beat_source = fn


def set_intensity_phrase(start_beat: int, end_beat: int, mode: Optional[str]) -> None:
    """Snapshot the active phrase boundaries — auto curve uses these to compute progress."""
    with _intensity_lock:
        _intensity_phrase["start_beat"] = int(start_beat)
        _intensity_phrase["end_beat"] = int(end_beat)
        _intensity_phrase["mode"] = mode


def set_intensity_manual(value: Optional[float]) -> None:
    """Pin intensity to a fixed value, or pass None to clear and resume auto."""
    global _intensity_manual
    if value is None:
        _intensity_manual = None
        return
    try:
        v = float(value)
    except (TypeError, ValueError):
        return
    if v != v:  # NaN guard
        return
    _intensity_manual = max(0.0, min(1.0, v))


def _intensity_auto_value() -> float:
    """Auto curve: phrase progress → intensity, shaped by current mode."""
    with _intensity_lock:
        phrase = dict(_intensity_phrase)
    mode = phrase["mode"]
    lo_hi = _INTENSITY_CURVE_BY_MODE.get(mode)
    if lo_hi is None:
        # Unknown / no phrase yet (preview, cold start). Play scenes as authored.
        return 1.0
    lo, hi = lo_hi
    if _beat_source is None:
        return lo
    try:
        beat = _beat_source()
    except Exception:
        beat = None
    if beat is None:
        return lo
    span = phrase["end_beat"] - phrase["start_beat"]
    if span <= 0:
        return lo
    progress = (float(beat) - phrase["start_beat"]) / float(span)
    if progress < 0.0:
        progress = 0.0
    elif progress > 1.0:
        progress = 1.0
    return lo + (hi - lo) * progress


def _intensity_for_engine() -> float:
    """Called by SceneEngine on every tick. Manual wins; otherwise auto curve."""
    manual = _intensity_manual
    if manual is not None:
        return manual
    return _intensity_auto_value()


def intensity_status() -> dict:
    """Dashboard snapshot: current effective value, manual override, phrase."""
    with _intensity_lock:
        phrase = dict(_intensity_phrase)
    return {
        "value": _intensity_for_engine(),
        "manual": _intensity_manual,
        "phrase": phrase,
    }


class _NullDMX:
    """No-op DMX stand-in when the Enttec dongle is unplugged.

    Keeps the render threads alive so Govee scenes still fire. Real DMX takes
    over on the next _ensure_dmx() call after the USB is plugged back in.
    """

    def set_all(self, *a, **k): pass
    def send_frame(self, *a, **k): pass
    def blackout(self, *a, **k): pass
    def set_12s(self, *a, **k): pass
    def set_12s_one(self, *a, **k): pass
    def set_bar_zone(self, *a, **k): pass
    def set_fixture(self, *a, **k): pass


_lock = threading.Lock()
_current_mode: Optional[str] = None         # the PSSI category currently running (None during preview-only)
_current_scene_id: Optional[str] = None
_current_engine: Optional[SceneEngine] = None
_preview_active: bool = False


def _ensure_dmx():
    """Return real DMX if the Enttec is present, else a null stub.

    Only calls into libftdi when /dev/cu.usbserial* exists — attempting
    d.open() without a device leaks a pthread TLS key inside libusb on
    macOS, which exhausts PTHREAD_KEYS_MAX after a few hundred retries
    and kills the process with a pthread_key_create assertion.
    """
    global _dmx, _dmx_last_try
    if isinstance(_dmx, DMX):
        return _dmx
    now = time.monotonic()
    if _dmx is not None and (now - _dmx_last_try) < _dmx_retry_interval:
        return _dmx
    _dmx_last_try = now
    nodes = glob.glob("/dev/cu.usbserial*")
    if not nodes:
        if not isinstance(_dmx, _NullDMX):
            print("[dmx] no /dev/cu.usbserial* — using null stub, will retry", flush=True)
        _dmx = _NullDMX()
        return _dmx
    try:
        d = DMX()
        d.open()
        _dmx = d
        print(f"[dmx] opened ({nodes[0]})", flush=True)
    except Exception as e:
        if not isinstance(_dmx, _NullDMX):
            print(f"[dmx] open failed ({e}) — using null stub, will retry", flush=True)
        _dmx = _NullDMX()
    return _dmx


def _stop_engine_locked() -> None:
    """Caller must hold _lock. Stops the current render thread (if any)."""
    global _current_engine
    if _current_engine is not None:
        try:
            _current_engine.stop(timeout=1.0)
        except Exception:
            pass
        _current_engine = None


def _run_scene(scene: dict, *, is_preview: bool, label: str) -> None:
    """Stop whatever's running and start a new SceneEngine for `scene`.

    A scene with `"blackout": true` is treated as a state, not a frame: no
    engine is started, DMX is zeroed and Govee turned off. `_current_mode`
    and `_preview_active` are preserved so the next mode change resumes
    normally — same contract as any other scene swap.
    """
    global _current_engine, _current_scene_id, _preview_active
    dmx = _ensure_dmx()
    is_blackout = bool(scene.get("blackout"))
    with _lock:
        # Hot-swap path: same scene id, same preview/live mode, engine still
        # running. Lets the editor drag sliders without tearing down the
        # render thread or re-firing Govee layers when only DMX changed.
        hot_swap_engine = None
        if (
            not is_blackout
            and _current_engine is not None
            and _current_scene_id == scene.get("id")
            and _preview_active == is_preview
        ):
            hot_swap_engine = _current_engine
        if hot_swap_engine is None:
            _stop_engine_locked()
            _current_scene_id = scene.get("id")
            _preview_active = is_preview
            if not is_blackout:
                engine = SceneEngine(
                    scene,
                    dmx,
                    _govee,
                    bpm_fn=_bpm_for_engine,
                    intensity_fn=_intensity_for_engine,
                )
                engine.start()
                _current_engine = engine
    if hot_swap_engine is not None:
        # Govee fan-out happens inside update_scene only if the govee subset
        # of layers actually changed — keep the call outside _lock either way.
        hot_swap_engine.update_scene(scene)
        print(f"DIRECT_LIGHTS update -> {label} ({scene.get('id')})", flush=True)
        return
    if is_blackout:
        # Network/serial calls outside the lock — match blackout()'s pattern.
        try:
            dmx.blackout()
            dmx.send_frame()
        except Exception:
            pass
        try:
            _govee.turn(False, verify=True)
        except Exception:
            pass
    suffix = " [blackout]" if is_blackout else ""
    print(f"DIRECT_LIGHTS scene -> {label} ({scene.get('id')}){suffix}", flush=True)


def apply_mode(mode: str) -> Optional[dict]:
    """Pick a random scene for this PSSI category and run it.

    Called by main.py on every mode change. If the category has no scenes,
    blackout so lights don't freeze on the previous state. Returns the chosen
    scene dict (or None) so the caller can log which scene actually fired.
    """
    global _current_mode, _preview_active
    store = get_store()
    # Don't clobber an active preview with a live-mode change — but in practice
    # the user said they won't play music while editing, so this is defensive.
    with _lock:
        if _preview_active:
            _current_mode = mode  # stash the mode; live resumes when preview stops
            return None
        if mode == _current_mode and _current_engine is not None:
            return None
        _current_mode = mode
    scene = store.pick_scene(mode)
    if scene is None and mode == "build":
        # Migration / safety net: a combined build section falls back to the
        # legacy buildup pool if no scenes are tagged `build` yet.
        scene = store.pick_scene("buildup")
    if scene is None:
        print(f"DIRECT_LIGHTS mode -> {mode} (no scenes in category)", flush=True)
        blackout()
        return None
    _run_scene(scene, is_preview=False, label=f"mode:{mode}")
    return scene


def apply_scene_preview(scene: dict) -> None:
    """Run an arbitrary scene dict (editor 'Play' button)."""
    _run_scene(scene, is_preview=True, label="preview")


def refresh_scene() -> bool:
    """Force a re-pick from the current category, excluding the running scene.

    Returns True if a new scene was started, False if no live mode is active
    or a preview is in flight (dashboard greys the button in those cases, but
    the check is here too so callers don't have to coordinate).
    """
    with _lock:
        if _preview_active or _current_mode is None:
            return False
        mode = _current_mode
        exclude = _current_scene_id
    store = get_store()
    scene = store.pick_scene(mode, exclude_id=exclude)
    if scene is None:
        return False
    _run_scene(scene, is_preview=False, label=f"refresh:{mode}")
    return True


def stop_preview(resume_mode: bool = True) -> None:
    """End preview. Resume the stashed live mode (if any), else blackout."""
    global _preview_active, _current_mode
    with _lock:
        was_preview = _preview_active
        _preview_active = False
        mode = _current_mode if resume_mode else None
    if not was_preview:
        return
    if mode:
        # Re-enter via apply_mode so we pick a fresh random scene from the
        # category — matches the "next mode change picks a new scene" contract.
        with _lock:
            _current_mode = None
        apply_mode(mode)
    else:
        blackout()


def blackout() -> None:
    """Stop render thread, zero the DMX universe, turn Govee off."""
    global _current_mode, _current_scene_id
    with _lock:
        _current_mode = None
        _current_scene_id = None
        _stop_engine_locked()
    try:
        dmx = _ensure_dmx()
        dmx.blackout()
        dmx.send_frame()
    except Exception:
        pass
    try:
        _govee.turn(False, verify=True)
    except Exception:
        pass


def warm_up() -> None:
    """Pre-scan LAN before the set starts so first scene change is instant."""
    _govee.ensure_ready()
    # Prime the scenes store singleton + watcher.
    get_store()


def status() -> dict:
    """Snapshot for the dashboard."""
    with _lock:
        return {
            "mode": _current_mode,
            "scene_id": _current_scene_id,
            "preview": _preview_active,
            "engine_running": _current_engine is not None,
        }


def govee_client() -> GoveeClient:
    """Shared Govee client (used by dashboard's /api/govee/presets)."""
    return _govee
