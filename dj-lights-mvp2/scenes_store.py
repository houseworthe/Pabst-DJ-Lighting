"""
scenes_store — load, watch, save the scenes.json catalog.

One instance per process. direct_lights uses pick_scene(category) to get a
random scene for the incoming mode. The dashboard uses save() to overwrite
the catalog atomically; a background mtime watcher picks up the change and
swaps the in-memory catalog within ~0.5s.

Hot-reload contract: a running scene is NOT retroactively restarted when its
definition is edited. The new definition applies the next time pick_scene()
is called (next mode change during a live set; next preview hit in the
editor). This keeps hot-reload predictable: you never see a scene jump mid-
render because someone saved the editor.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from pathlib import Path
from typing import Any, Optional


SCENES_PATH = Path(__file__).resolve().parent / "scenes.json"
WATCH_INTERVAL_S = 0.5

CATEGORIES = ["intro", "groove", "buildup", "breakdown", "drop", "outro"]


def _migrate_scene(scene: dict) -> dict:
    """Upgrade legacy layer shapes to the current schema.

    - Legacy: multiple `{type: govee_preset, skus: [sku], param_id}` layers.
      Current: one `{type: govee_preset, presets: {sku: param_id}}` layer.
      The merged layer takes the position of the first legacy entry so
      ordering relative to DMX layers is preserved.
    - Legacy breathe: `{rgb: [r,g,b]}`.
      Current breathe: `{colors: [[r,g,b], ...]}`.
    """
    layers = scene.get("layers")
    if not isinstance(layers, list):
        return scene
    merged_presets: dict = {}
    first_legacy_idx: Optional[int] = None
    legacy_indices: list[int] = []
    for i, l in enumerate(layers):
        if not isinstance(l, dict):
            continue
        if l.get("type") == "govee_preset" and "presets" not in l:
            legacy_indices.append(i)
            for sku in (l.get("skus") or []):
                pid = l.get("param_id")
                if pid is not None:
                    merged_presets[sku] = int(pid)
            if first_legacy_idx is None:
                first_legacy_idx = i

    new_layers: list = []
    for i, l in enumerate(layers):
        if i == first_legacy_idx:
            new_layers.append({"type": "govee_preset", "presets": merged_presets})
            continue
        if i in legacy_indices:
            continue
        if isinstance(l, dict) and l.get("type") == "breathe" and "colors" not in l and "rgb" in l:
            l = {**l, "colors": [list(l["rgb"])[:3]]}
            l.pop("rgb", None)
        new_layers.append(l)
    return {**scene, "layers": new_layers}


class ScenesStore:
    def __init__(self, path: Path = SCENES_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"version": 1, "scenes": []}
        self._mtime: float = 0.0
        self._watcher_thread: Optional[threading.Thread] = None
        self._watcher_stop = threading.Event()
        self.load()

    # -- read --

    def load(self) -> None:
        """Read scenes.json. Silent on missing; clear-error on corrupt."""
        try:
            raw = self.path.read_text()
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("scenes"), list):
                raise ValueError("scenes.json must be {version, scenes: [...]}")
            data["scenes"] = [
                _migrate_scene(s) if isinstance(s, dict) else s
                for s in data["scenes"]
            ]
            with self._lock:
                self._data = data
                try:
                    self._mtime = self.path.stat().st_mtime
                except FileNotFoundError:
                    self._mtime = 0.0
            print(f"[scenes_store] loaded {len(data['scenes'])} scenes from {self.path.name}", flush=True)
        except FileNotFoundError:
            with self._lock:
                self._data = {"version": 1, "scenes": []}
                self._mtime = 0.0
            print(f"[scenes_store] {self.path.name} not found — starting empty", flush=True)
        except Exception as e:
            print(f"[scenes_store] load failed ({e}) — keeping prior catalog", flush=True)

    @property
    def scenes(self) -> list[dict]:
        with self._lock:
            return list(self._data.get("scenes", []))

    def scenes_by_category(self) -> dict[str, list[dict]]:
        by_cat: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
        for scene in self.scenes:
            cat = scene.get("category")
            if cat in by_cat:
                by_cat[cat].append(scene)
            else:
                # unknown category — still surface it so the UI can show/move it
                by_cat.setdefault(cat or "uncategorized", []).append(scene)
        return by_cat

    def get_scene(self, scene_id: str) -> Optional[dict]:
        for s in self.scenes:
            if s.get("id") == scene_id:
                return s
        return None

    def pick_scene(self, category: str) -> Optional[dict]:
        """Return a random scene from the category, or None if empty."""
        pool = [s for s in self.scenes if s.get("category") == category]
        if not pool:
            return None
        return random.choice(pool)

    def snapshot(self) -> dict:
        """Return the raw catalog (deep copy) for API consumers."""
        with self._lock:
            return json.loads(json.dumps(self._data))

    # -- write --

    def save(self, data: dict) -> None:
        """Overwrite scenes.json atomically. Caller must pass a full catalog."""
        if not isinstance(data, dict) or not isinstance(data.get("scenes"), list):
            raise ValueError("save payload must be {version, scenes: [...]}")
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.path)
        with self._lock:
            self._data = data
            self._mtime = self.path.stat().st_mtime
        print(f"[scenes_store] saved {len(data['scenes'])} scenes", flush=True)

    # -- watcher --

    def start_watcher(self) -> None:
        """Poll mtime in a background thread; reload on change."""
        if self._watcher_thread and self._watcher_thread.is_alive():
            return
        self._watcher_stop.clear()
        self._watcher_thread = threading.Thread(
            target=self._watch, name="scenes-watcher", daemon=True
        )
        self._watcher_thread.start()

    def stop_watcher(self) -> None:
        self._watcher_stop.set()

    def _watch(self) -> None:
        while not self._watcher_stop.wait(WATCH_INTERVAL_S):
            try:
                mtime = self.path.stat().st_mtime
            except FileNotFoundError:
                continue
            if mtime > self._mtime:
                self.load()


_default_store: Optional[ScenesStore] = None
_store_lock = threading.Lock()


def get_store() -> ScenesStore:
    """Singleton — scene engine, dashboard, and direct_lights all share one."""
    global _default_store
    with _store_lock:
        if _default_store is None:
            _default_store = ScenesStore()
            _default_store.start_watcher()
        return _default_store
