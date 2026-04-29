"""
scene_engine — render a scene dict to DMX + Govee.

A scene is a declarative JSON doc with a list of layers. Each layer maps to a
primitive: solid, breathe, chase, pulse, govee_rgb, govee_preset.

The engine owns ONE render thread per running scene. Each tick (30ms) it
clears the DMX buffer, paints each DMX layer in order, flushes to the dongle.
Govee layers fire once on scene start (named presets play autonomously on
device; LAN RGB is one-shot until the scene changes).

Layer schema reference:

    solid         {type, target, rgb[r,g,b], amber, dim, strobe}
    breathe       {type, target, rgb, amber, hz, dim_min, dim_max, strobe}
    chase         {type, rgb, amber, rate_hz, dim_active, dim_rest, strobe}
                  (target is implicitly wash↔bar; not a per-zone chase)
    bar_chase     {type, colors, rate_hz, direction, tail, dim_active,
                   dim_rest, amber, strobe, wash}
                  (per-zone sweep across the 4 bar zones)
    pulse         {type, target, colors[[r,g,b],...], amber, on_ms, off_ms,
                   dim, strobe}
    strobe        {type, target, rgb, amber, dim, rate}
    random_flash  {type, target, rgb, amber, dim, min_gap_s, max_gap_s,
                   double_chance, flash_ms}
    govee_rgb     {type, skus[sku,...], rgb, brightness?}
    govee_preset  {type, skus[sku,...], param_id}

Targets for DMX layers:
    all, wash, bar_all, bar_z1, bar_z2, bar_z3, bar_z4

The engine never crashes on bad layers — it logs and skips. A scene is valid
if at least one layer renders cleanly.
"""
from __future__ import annotations

import math
import random
import threading
import time
from typing import Any, Callable, Optional


TICK_S = 0.03  # 30ms render tick; pulse timing uses monotonic clock, so jitter is invisible.

DMX_TARGETS = {"all", "wash", "wash_1", "wash_2", "bar_all", "bar_z1", "bar_z2", "bar_z3", "bar_z4"}
LAYER_TYPES = {"solid", "breathe", "chase", "bar_chase", "pulse", "strobe", "random_flash", "govee_rgb", "govee_preset"}


def _clamp(v: float, lo: float = 0, hi: float = 255) -> int:
    return int(max(lo, min(hi, v)))


def _scale(r: int, g: int, b: int, a: int, dim: int) -> tuple[int, int, int, int]:
    """Scale color channels by dim/255. We dim via RGB instead of the fixture's
    master-dim channel because our Tetras (in their current mode) don't honor
    that channel reliably — scaling RGB makes brightness behavior identical
    whether DIM is wired up or not."""
    f = max(0, min(255, int(dim))) / 255.0
    return _clamp(r * f), _clamp(g * f), _clamp(b * f), _clamp(a * f)


def _paint_target(dmx, target: str, r: int, g: int, b: int, a: int, dim: int, strobe: int) -> None:
    """Write one color+strobe to a named DMX target. Dim is baked into RGB;
    we pass 255 to the fixture's dim channel so it stays full if the channel
    is honored, and it's a no-op if it isn't."""
    rr, gg, bb, aa = _scale(r, g, b, a, dim)
    if target == "all":
        dmx.set_all(rr, gg, bb, aa, 255, strobe)
    elif target == "wash":
        dmx.set_12s(rr, gg, bb, aa, 255, strobe)
    elif target == "wash_1":
        dmx.set_12s_one(0, rr, gg, bb, aa, 255, strobe)
    elif target == "wash_2":
        dmx.set_12s_one(1, rr, gg, bb, aa, 255, strobe)
    elif target == "bar_all":
        for z in range(1, 5):
            dmx.set_bar_zone(z, rr, gg, bb, aa, 255, strobe)
    elif target in {"bar_z1", "bar_z2", "bar_z3", "bar_z4"}:
        zone = int(target[-1])
        dmx.set_bar_zone(zone, rr, gg, bb, aa, 255, strobe)


def _layer_solid(dmx, layer: dict, t: float, state: dict) -> None:
    target = layer.get("target", "all")
    if target not in DMX_TARGETS:
        return
    r, g, b = (layer.get("rgb") or [0, 0, 0])[:3]
    a = layer.get("amber", 0)
    dim = layer.get("dim", 255)
    strobe = layer.get("strobe", 0)
    _paint_target(dmx, target, _clamp(r), _clamp(g), _clamp(b), _clamp(a), _clamp(dim), _clamp(strobe))


BREATHE_WASH_DIM_CAP = 76  # ~30% — wash (Tetra 12) lights up the room fast on breathe, so cap here.


def _layer_breathe(dmx, layer: dict, t: float, state: dict) -> None:
    """Dimmer pulses between dim_min and dim_max. If `colors` has more than
    one entry, the color advances one step per full breath, swapping at the
    bottom of the cycle so the transition is invisible.

    Wash fixtures are capped at BREATHE_WASH_DIM_CAP regardless of dim_max —
    full-bright wash breathe bleaches the whole room."""
    target = layer.get("target", "all")
    if target not in DMX_TARGETS:
        return
    a = layer.get("amber", 0)
    hz = float(layer.get("hz", 0.25))
    dim_min = float(layer.get("dim_min", 0))
    dim_max = float(layer.get("dim_max", 255))
    # 1 - cos keeps the wave at 0 on cycle boundaries so color swaps are dark.
    wave = 0.5 - 0.5 * math.cos(2 * math.pi * hz * t)
    dim = dim_min + (dim_max - dim_min) * wave

    colors = layer.get("colors")
    if isinstance(colors, list) and colors:
        idx = int(t * hz) % len(colors)
        r, g, b = colors[idx][:3]
    else:
        r, g, b = (layer.get("rgb") or [0, 0, 0])[:3]

    dim_full = _clamp(dim)
    dim_wash = min(dim_full, BREATHE_WASH_DIM_CAP)
    wr, wg, wb, wa = _scale(r, g, b, a, dim_wash)
    br, bg, bb_, ba = _scale(r, g, b, a, dim_full)
    if target == "all":
        dmx.set_12s(wr, wg, wb, wa, 255, 0)
        for z in range(1, 5):
            dmx.set_bar_zone(z, br, bg, bb_, ba, 255, 0)
    elif target == "wash":
        dmx.set_12s(wr, wg, wb, wa, 255, 0)
    elif target == "wash_1":
        dmx.set_12s_one(0, wr, wg, wb, wa, 255, 0)
    elif target == "wash_2":
        dmx.set_12s_one(1, wr, wg, wb, wa, 255, 0)
    elif target == "bar_all":
        for z in range(1, 5):
            dmx.set_bar_zone(z, br, bg, bb_, ba, 255, 0)
    elif target in {"bar_z1", "bar_z2", "bar_z3", "bar_z4"}:
        dmx.set_bar_zone(int(target[-1]), br, bg, bb_, ba, 255, 0)


def _layer_chase(dmx, layer: dict, t: float, state: dict) -> None:
    """Wash/bar ping-pong. Tetra 12 wash and Tetra Bar alternate at rate_hz
    toggles per second.

    One color: wash lit + bar dark, then the opposite.
    Two+ colors: wash shows colors[k % N], bar shows colors[(k+1) % N], so each
    group keeps swapping colors (and neither group goes fully dark unless
    dim_rest < dim_active — the 'off' group still renders at dim_rest)."""
    colors = layer.get("colors")
    if not (isinstance(colors, list) and colors):
        # Legacy: single rgb. Fall back so hand-edited / migrated scenes work.
        rgb = layer.get("rgb") or [255, 255, 255]
        colors = [rgb]
    a = layer.get("amber", 0)
    rate_hz = float(layer.get("rate_hz", 1.0))
    dim_on = int(layer.get("dim_active", 128))
    dim_off = int(layer.get("dim_rest", 0))
    step = int(t * rate_hz)

    if len(colors) >= 2:
        wash_rgb = colors[step % len(colors)][:3]
        bar_rgb = colors[(step + 1) % len(colors)][:3]
        wash_dim = dim_on
        bar_dim = dim_on
    else:
        wash_rgb = bar_rgb = colors[0][:3]
        wash_dim = dim_on if step % 2 == 0 else dim_off
        bar_dim = dim_off if step % 2 == 0 else dim_on

    wr, wg, wb, wa = _scale(wash_rgb[0], wash_rgb[1], wash_rgb[2], a, wash_dim)
    br, bg, bb_, ba = _scale(bar_rgb[0], bar_rgb[1], bar_rgb[2], a, bar_dim)
    dmx.set_12s(wr, wg, wb, wa, 255, 0)
    for z in range(1, 5):
        dmx.set_bar_zone(z, br, bg, bb_, ba, 255, 0)


def _layer_bar_chase(dmx, layer: dict, t: float, state: dict) -> None:
    """Per-zone chase across the 4 bar zones — the classic L→R sweep.

    Direction:
      "wrap"     — head loops 1→2→3→4→1; tail wraps around the ends.
      "pingpong" — head bounces 1→2→3→4→3→2→1; tail follows the direction
                   of motion, so it always trails behind the head.

    `tail` (0..3) adds dimmer trailing zones between the head (`dim_active`)
    and the inactive level (`dim_rest`). tail=0 = sharp single-zone hit.
    Color advances one slot per zone-step, so a 4-color palette gives each
    zone a different color as the head walks across.

    `wash`:
      "off"     — Tetra 12s stay dark (frame-clear blackout).
      "match"   — wash mirrors the head color at dim_active. Reads as a pulse
                  that walks alongside the chase. Useful as the only wash
                  driver in a scene; pair with a separate solid/breathe layer
                  if you want a steady wash baseline underneath.
      "include" — wash 1 and wash 2 become positions 5 and 6 in the rotation.
                  Head walks bar_z1 → z2 → z3 → z4 → wash_2 → wash_1 → repeat,
                  reading as a clockwise sweep when wash_1 sits left of the bar
                  and wash_2 sits right. (Pingpong bounces the same path.)
    """
    colors = layer.get("colors") or [[255, 255, 255]]
    if not colors:
        colors = [[255, 255, 255]]
    rate_hz = float(layer.get("rate_hz", 4.0))
    direction = layer.get("direction", "wrap")
    tail = max(0, min(3, int(layer.get("tail", 0))))
    dim_active = int(layer.get("dim_active", 255))
    dim_rest = int(layer.get("dim_rest", 0))
    amber = layer.get("amber", 0)
    strobe = int(layer.get("strobe", 0))
    wash_mode = layer.get("wash", "off")
    include_wash = wash_mode == "include"

    n = 6 if include_wash else 4
    step = int(t * rate_hz)

    def head_for(s: int) -> int:
        if direction == "pingpong":
            cycle = (n - 1) * 2  # 6 for 4 zones
            sm = s % cycle
            return sm if sm < n else cycle - sm
        return s % n

    head = head_for(step)
    color_idx = step % len(colors)
    r, g, b = colors[color_idx][:3]

    if direction == "pingpong":
        prev = head_for(step - 1) if step > 0 else head
        sign = 1 if head >= prev else -1
    else:
        sign = 1  # wrap mode advances right; tail wraps via modulo below

    span = max(1, dim_active - dim_rest)
    for zi in range(n):
        if direction == "wrap":
            offset = (head - zi) % n  # always 0..n-1, tail wraps around
        else:
            offset = (head - zi) * sign  # negative = ahead of head, ignored

        if offset == 0:
            dim = dim_active
        elif 1 <= offset <= tail:
            strength = (tail + 1 - offset) / (tail + 1)
            dim = dim_rest + int(span * strength)
        else:
            dim = dim_rest

        rr, gg, bb_, aa = _scale(r, g, b, amber, dim)
        if zi < 4:
            dmx.set_bar_zone(zi + 1, rr, gg, bb_, aa, 255, _clamp(strobe))
        elif zi == 4:
            dmx.set_12s_one(1, rr, gg, bb_, aa, 255, _clamp(strobe))  # wash_2 (right)
        else:
            dmx.set_12s_one(0, rr, gg, bb_, aa, 255, _clamp(strobe))  # wash_1 (left)

    if not include_wash and wash_mode == "match":
        wr, wg, wb, wa = _scale(r, g, b, amber, dim_active)
        dmx.set_12s(wr, wg, wb, wa, 255, _clamp(strobe))


def _layer_pulse(dmx, layer: dict, t: float, state: dict) -> None:
    """Alternate target between a color (from colors[], advancing per cycle) and blackout."""
    target = layer.get("target", "all")
    if target not in DMX_TARGETS:
        return
    colors = layer.get("colors") or [[255, 255, 255]]
    on_ms = max(1, int(layer.get("on_ms", 80)))
    off_ms = max(0, int(layer.get("off_ms", 40)))
    a = layer.get("amber", 0)
    dim = layer.get("dim", 255)
    strobe = layer.get("strobe", 0)
    period_ms = on_ms + off_ms
    t_ms = t * 1000.0
    cycle_idx = int(t_ms // period_ms)
    phase_ms = t_ms - cycle_idx * period_ms
    if phase_ms < on_ms:
        r, g, b = colors[cycle_idx % len(colors)][:3]
        _paint_target(dmx, target, _clamp(r), _clamp(g), _clamp(b), _clamp(a), _clamp(dim), _clamp(strobe))
    # else: leave zeros from the frame clear (off phase = blackout)


def _layer_strobe(dmx, layer: dict, t: float, state: dict) -> None:
    """Software strobe — dark baseline with brief bright flashes.

    We don't use the Chauvet strobe channel because its shutter is the inverse
    of what a DJ expects (on-with-brief-offs instead of off-with-brief-ons).
    Instead we drive the dim channel: paint one full-brightness frame for
    `on_ms`, then paint nothing (tick-start blackout leaves fixtures dark).

    `rate` maps 1..255 to period 2000ms..60ms (on_ms + off_ms). `rate == 0`
    means fully dark.
    """
    target = layer.get("target", "all")
    if target not in DMX_TARGETS:
        return
    rate = int(layer.get("rate", 0) or 0)
    if rate <= 0:
        return
    rate = max(1, min(255, rate))
    on_ms = 40
    off_ms = int(round(2000 - (rate - 1) * (2000 - 20) / 254))
    period_ms = on_ms + off_ms
    phase_ms = (t * 1000.0) % period_ms
    if phase_ms >= on_ms:
        return
    r, g, b = (layer.get("rgb") or [255, 255, 255])[:3]
    a = layer.get("amber", 0)
    dim = layer.get("dim", 255)
    _paint_target(dmx, target, _clamp(r), _clamp(g), _clamp(b), _clamp(a), _clamp(dim), 0)


def _layer_random_flash(dmx, layer: dict, t: float, state: dict) -> None:
    """Sporadic bright flashes at random gaps, occasionally doubled.

    Per-layer state holds an `events` queue of scheduled (start, end) windows.
    When the queue empties, the next event (and optionally a follow-up double)
    is drawn from `min_gap_s..max_gap_s`. Fixtures are dark between events.
    """
    min_gap = max(0.1, float(layer.get("min_gap_s", 4.0)))
    max_gap = max(min_gap, float(layer.get("max_gap_s", 8.0)))
    double_chance = max(0.0, min(1.0, float(layer.get("double_chance", 30)) / 100.0))
    flash_dur = max(0.02, float(layer.get("flash_ms", 60)) / 1000.0)

    events = state.setdefault("events", [])
    while events and events[0][1] <= t:
        state["last_end"] = events[0][1]
        events.pop(0)
    if not events:
        base = max(t, state.get("last_end", t))
        start = base + random.uniform(min_gap, max_gap)
        events.append((start, start + flash_dur))
        if random.random() < double_chance:
            dstart = start + flash_dur + random.uniform(0.08, 0.18)
            events.append((dstart, dstart + flash_dur))

    for start, end in events:
        if start <= t < end:
            target = layer.get("target", "all")
            if target not in DMX_TARGETS:
                return
            r, g, b = (layer.get("rgb") or [255, 255, 255])[:3]
            a = layer.get("amber", 0)
            dim = layer.get("dim", 80)
            _paint_target(dmx, target, _clamp(r), _clamp(g), _clamp(b), _clamp(a), _clamp(dim), 0)
            return


DMX_LAYER_RENDERERS: dict[str, Callable[[Any, dict, float, dict], None]] = {
    "solid": _layer_solid,
    "breathe": _layer_breathe,
    "chase": _layer_chase,
    "bar_chase": _layer_bar_chase,
    "pulse": _layer_pulse,
    "strobe": _layer_strobe,
    "random_flash": _layer_random_flash,
}


class SceneEngine:
    """Runs a single scene dict against a DMX controller + Govee client.

    start() fires one-shot Govee layers and spawns the DMX render thread.
    stop() signals the thread to exit and joins with a timeout. The caller is
    responsible for any subsequent blackout — stop() leaves fixtures at their
    last painted state so a scene swap is glitch-free.
    """

    def __init__(self, scene: dict, dmx, govee) -> None:
        self.scene = scene
        self.dmx = dmx
        self.govee = govee
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._fire_govee_layers()
        self._thread = threading.Thread(
            target=self._run,
            name=f"scene-{self.scene.get('id', 'anon')}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _fire_govee_layers(self) -> None:
        """Trigger Govee layers once at scene start. Presets play autonomously
        on-device; LAN RGB is held until we fire a new one.

        Any Govee SKU in the fleet that this scene does NOT reference is
        turned off first — otherwise the old scene's preset keeps running
        on devices the new scene ignores (e.g. a drop scene that only drives
        COB strips would leave the bulbs stuck on the previous color).
        """
        referenced_skus: set[str] = set()
        for layer in self.scene.get("layers", []):
            ltype = layer.get("type")
            if ltype == "govee_preset":
                presets = layer.get("presets")
                if isinstance(presets, dict):
                    referenced_skus.update(k for k, v in presets.items() if v is not None)
                else:
                    referenced_skus.update(layer.get("skus") or [])
            elif ltype == "govee_rgb":
                referenced_skus.update(layer.get("skus") or [])
        fleet_skus = {d.get("sku") for d in self.govee.devices if d.get("sku")}
        to_off = fleet_skus - referenced_skus
        if to_off:
            try:
                self.govee.turn_skus(list(to_off), False)
            except Exception as e:
                print(f"[scene_engine] govee turn off {sorted(to_off)} failed: {e}", flush=True)

        for layer in self.scene.get("layers", []):
            ltype = layer.get("type")
            try:
                if ltype == "govee_preset":
                    # Current format: {presets: {sku: param_id}}.
                    # Legacy format (still honored): {skus: [sku], param_id}.
                    presets = layer.get("presets")
                    if isinstance(presets, dict) and presets:
                        valid = {sku: int(pid) for sku, pid in presets.items() if pid is not None}
                        if valid:
                            self.govee.apply_mode_scenes(valid)
                    else:
                        skus = layer.get("skus") or []
                        param_id = layer.get("param_id")
                        if not skus or param_id is None:
                            continue
                        for sku in skus:
                            self.govee.set_scene_for_sku(sku, int(param_id))
                elif ltype == "govee_rgb":
                    skus = set(layer.get("skus") or [])
                    rgb = layer.get("rgb") or [0, 0, 0]
                    brightness = layer.get("brightness")
                    r, g, b = _clamp(rgb[0]), _clamp(rgb[1]), _clamp(rgb[2])
                    # set_color_and_brightness hits every device — filter to target SKUs.
                    if skus:
                        self._govee_rgb_for_skus(skus, r, g, b, brightness)
                    else:
                        self.govee.set_color_and_brightness(r, g, b, brightness)
            except Exception as e:
                print(f"[scene_engine] govee layer failed: {e}", flush=True)

    def _govee_rgb_for_skus(self, skus: set, r: int, g: int, b: int, brightness: Optional[int]) -> None:
        """Broadcast RGB+brightness only to devices matching the given SKU set."""
        color_cap = {
            "type": "devices.capabilities.color_setting",
            "instance": "colorRgb",
            "value": (r << 16) | (g << 8) | b,
        }
        color_lan = ("colorwc", {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0})

        def steps(dev):
            if dev.get("sku") not in skus:
                return []
            out = [{"lan": color_lan, "cap": color_cap}]
            if brightness is not None:
                pct = max(1, min(100, int(brightness)))
                out.append({
                    "lan": ("brightness", {"value": pct}),
                    "cap": {"type": "devices.capabilities.range", "instance": "brightness", "value": pct},
                })
            return out

        self.govee._broadcast_steps(steps)

    def _run(self) -> None:
        start = time.monotonic()
        layers = [l for l in self.scene.get("layers", []) if l.get("type") in DMX_LAYER_RENDERERS]
        # Per-layer mutable state — lets renderers like random_flash remember
        # schedules across ticks. Lives for the engine's lifetime only.
        states: list[dict] = [{} for _ in layers]
        # Always run the tick loop even with zero DMX layers, so the previous
        # scene's final frame gets overwritten by blackout instead of holding.
        # Scene contract: absent DMX layer = fixtures off.
        while not self._stop.is_set():
            t = time.monotonic() - start
            try:
                self.dmx.blackout()
                for layer, state in zip(layers, states):
                    renderer = DMX_LAYER_RENDERERS.get(layer["type"])
                    if renderer is None:
                        continue
                    try:
                        renderer(self.dmx, layer, t, state)
                    except Exception as e:
                        print(f"[scene_engine] layer {layer.get('type')} failed: {e}", flush=True)
                self.dmx.send_frame()
            except Exception as e:
                print(f"[scene_engine] render tick failed: {e}", flush=True)
            if self._stop.wait(TICK_S):
                break


def validate_scene(scene: dict) -> list[str]:
    """Return a list of human-readable validation errors. Empty = valid."""
    errors: list[str] = []
    if not isinstance(scene, dict):
        return ["scene must be a dict"]
    if not scene.get("id"):
        errors.append("missing id")
    if not scene.get("name"):
        errors.append("missing name")
    if not scene.get("category"):
        errors.append("missing category")
    layers = scene.get("layers")
    if not isinstance(layers, list):
        errors.append("layers must be a list")
        return errors
    for i, layer in enumerate(layers):
        if not isinstance(layer, dict):
            errors.append(f"layer {i}: not a dict")
            continue
        ltype = layer.get("type")
        if ltype not in LAYER_TYPES:
            errors.append(f"layer {i}: unknown type {ltype!r}")
            continue
        if ltype in {"solid", "breathe", "pulse", "strobe", "random_flash"}:
            tgt = layer.get("target")
            if tgt not in DMX_TARGETS:
                errors.append(f"layer {i} ({ltype}): invalid target {tgt!r}")
        if ltype == "govee_preset":
            presets = layer.get("presets")
            if isinstance(presets, dict):
                if not any(v is not None for v in presets.values()):
                    errors.append(f"layer {i} (govee_preset): no presets selected")
            else:
                if not layer.get("skus"):
                    errors.append(f"layer {i} (govee_preset): empty skus")
                if layer.get("param_id") is None:
                    errors.append(f"layer {i} (govee_preset): missing param_id")
    return errors
