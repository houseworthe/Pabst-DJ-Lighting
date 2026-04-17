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
from typing import Optional

BASE = os.path.dirname(__file__)
DJ_LIGHTS = os.path.abspath(os.path.join(BASE, "..", "dj-lights"))
# append (not insert) so our own mvp2 modules (dashboard, scene_engine, etc.)
# always shadow any older same-named files in dj-lights/.
if DJ_LIGHTS not in sys.path:
    sys.path.append(DJ_LIGHTS)

from dmx_controller import DMX  # type: ignore
from govee_lan import GoveeClient
from scene_engine import SceneEngine
from scenes_store import get_store

_dmx: Optional[DMX] = None
_dmx_last_try: float = 0.0
_dmx_retry_interval: float = 5.0
_govee = GoveeClient()


class _NullDMX:
    """No-op DMX stand-in when the Enttec dongle is unplugged.

    Keeps the render threads alive so Govee scenes still fire. Real DMX takes
    over on the next _ensure_dmx() call after the USB is plugged back in.
    """

    def set_all(self, *a, **k): pass
    def send_frame(self, *a, **k): pass
    def blackout(self, *a, **k): pass
    def set_12s(self, *a, **k): pass
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
    """Stop whatever's running and start a new SceneEngine for `scene`."""
    global _current_engine, _current_scene_id, _preview_active
    dmx = _ensure_dmx()
    with _lock:
        _stop_engine_locked()
        _current_scene_id = scene.get("id")
        _preview_active = is_preview
        engine = SceneEngine(scene, dmx, _govee)
        engine.start()
        _current_engine = engine
    print(f"DIRECT_LIGHTS scene -> {label} ({scene.get('id')})", flush=True)


def apply_mode(mode: str) -> None:
    """Pick a random scene for this PSSI category and run it.

    Called by main.py on every mode change. If the category has no scenes,
    blackout so lights don't freeze on the previous state.
    """
    global _current_mode, _preview_active
    store = get_store()
    # Don't clobber an active preview with a live-mode change — but in practice
    # the user said they won't play music while editing, so this is defensive.
    with _lock:
        if _preview_active:
            _current_mode = mode  # stash the mode; live resumes when preview stops
            return
        if mode == _current_mode and _current_engine is not None:
            return
        _current_mode = mode
    scene = store.pick_scene(mode)
    if scene is None:
        print(f"DIRECT_LIGHTS mode -> {mode} (no scenes in category)", flush=True)
        blackout()
        return
    _run_scene(scene, is_preview=False, label=f"mode:{mode}")


def apply_scene_preview(scene: dict) -> None:
    """Run an arbitrary scene dict (editor 'Play' button)."""
    _run_scene(scene, is_preview=True, label="preview")


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
        _govee.turn(False)
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
