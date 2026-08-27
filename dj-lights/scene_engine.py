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
    chase         {type, rgb, amber, rate_hz | rate_beats, dim_active,
                   dim_rest, strobe}
                  (target is implicitly wash↔bar; not a per-zone chase.
                   rate_beats = beats per toggle; tempo-locked when set.)
    bar_chase     {type, colors, rate_hz | rate_beats, direction, tail,
                   dim_active, dim_rest, amber, strobe, wash}
                  (per-zone sweep across the 4 bar zones.
                   rate_beats = beats per zone-step; tempo-locked when set.)
    bar_shoot     {type, colors, mode, retract, shoot_ms, hold_ms,
                   retract_ms, gap_ms, dim_active, dim_rest, amber, strobe,
                   wash}
                  (launch across the 4 bar zones then recoil. mode ∈
                   {out,in,center,split}; retract ∈ {recede,fade}.)
    pulse         {type, target, colors[[r,g,b],...], amber, on_ms,
                   off_ms | period_beats, dim, strobe}
                  (period_beats = beats per cycle; tempo-locked when set.
                   on_ms stays absolute even in beats mode — flashes don't
                   stretch with BPM.)
    strobe        {type, target, rgb, amber, dim, rate}
    random_flash  {type, target, rgb, amber, dim, min_gap_s, max_gap_s,
                   double_chance, flash_ms}
    popcorn       {type, target, mode, colors[[r,g,b],...], amber,
                   max_brightness, min_brightness, flash_rate_hz, decay_ms,
                   strobe}
                  (each addressable unit pops to max and decays linearly to
                   min over decay_ms. flash_rate_hz is the *combined* rate
                   across all units in scope. mode='solo' enforces one pop
                   at a time (no overlap); 'overlap' allows simultaneous
                   pops. target ∈ {all, wash, bar_all}.)
    govee_rgb     {type, skus[sku,...], rgb, brightness?}
    govee_preset  {type, skus[sku,...], param_id}

Targets for DMX layers:
    all, wash, bar_all, bar_z1, bar_z2, bar_z3, bar_z4

The engine never crashes on bad layers — it logs and skips. A scene is valid
if at least one layer renders cleanly.
"""
from __future__ import annotations

import json
import math
import random
import threading
import time
from typing import Any, Callable, Optional


TICK_S = 0.03  # 30ms render tick; pulse timing uses monotonic clock, so jitter is invisible.

DMX_TARGETS = {"all", "wash", "wash_1", "wash_2", "bar_all", "bar_z1", "bar_z2", "bar_z3", "bar_z4"}
LAYER_TYPES = {"solid", "breathe", "chase", "bar_chase", "wash_pingpong", "wash_chase", "dual_wash", "pulse", "strobe", "random_flash", "popcorn", "bar_shoot", "bar_flow", "acid_kaleidoscope", "acid_bloom", "govee_rgb", "govee_preset"}

# Per-unit target lists for popcorn — the layer addresses each unit
# independently so each "light" pops on its own schedule.
POPCORN_UNIT_MAP = {
    "all": ("wash_1", "wash_2", "bar_z1", "bar_z2", "bar_z3", "bar_z4"),
    "wash": ("wash_1", "wash_2"),
    "bar_all": ("bar_z1", "bar_z2", "bar_z3", "bar_z4"),
}


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


# ---------------------------------------------------------------------------
# Acid primitives — continuous-hue procedural effects
# ---------------------------------------------------------------------------
#
# The chase/pulse family all cycle through a *fixed* palette one slot at a
# time. The "acid" layers instead compute hue continuously (rotating rainbows,
# per-segment random walks) so the rig never lands on a flat repeating frame.
# They address the rig as 6 spatial segments, left → right across the room:
#
#     seg 0     = wash_1 (Tetra 12 left, d.001)
#     seg 1..4  = bar zones 1..4 (Tetra Bar, center)
#     seg 5     = wash_2 (Tetra 12 right, d.007)
#
# so a hue that sweeps with the segment index reads as a physical L→R wipe.

ACID_N = 6  # number of spatial segments


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    """HSV → 0..255 RGB. h in degrees (wrapped), s and v in [0, 1].

    This is the workhorse for every acid layer — continuous hue is the whole
    point, so we never quantize to a palette."""
    h = h % 360.0
    s = max(0.0, min(1.0, s))
    v = max(0.0, min(1.0, v))
    c = v * s
    x = c * (1 - abs((h / 60.0) % 2 - 1))
    m = v - c
    if h < 60:
        rp, gp, bp = c, x, 0.0
    elif h < 120:
        rp, gp, bp = x, c, 0.0
    elif h < 180:
        rp, gp, bp = 0.0, c, x
    elif h < 240:
        rp, gp, bp = 0.0, x, c
    elif h < 300:
        rp, gp, bp = x, 0.0, c
    else:
        rp, gp, bp = c, 0.0, x
    return _clamp((rp + m) * 255), _clamp((gp + m) * 255), _clamp((bp + m) * 255)


def _acid_paint_segment(dmx, idx: int, r: int, g: int, b: int, a: int = 0, strobe: int = 0) -> None:
    """Paint one of the 6 spatial segments (see ACID_N comment). Brightness is
    baked into RGB by the caller, so we always pass dim=255 to the fixture."""
    if idx <= 0:
        dmx.set_12s_one(0, r, g, b, a, 255, strobe)   # wash_1 (left)
    elif idx >= ACID_N - 1:
        dmx.set_12s_one(1, r, g, b, a, 255, strobe)   # wash_2 (right)
    else:
        dmx.set_bar_zone(idx, r, g, b, a, 255, strobe)  # seg 1..4 -> bar zone 1..4


RATE_HZ_MIN = 0.1   # floor matches dashboard slider min; below this a chase looks frozen.
RATE_HZ_MAX = 40.0  # ceiling absorbs absurd rate_beats at high BPM (e.g. 0.0625 @ 200).
FALLBACK_BPM = 120.0  # used only if state["_bpm"] is missing (engine never sets it).
DT_MAX_S = 0.1      # phase integration clamp; absorbs tick stalls without leaping.


def _resolve_rate_hz(layer: dict, bpm: float, default_hz: float) -> float:
    """Effective Hz for chase / bar_chase. rate_beats wins if > 0; else
    rate_hz; else default. Result clamped to [RATE_HZ_MIN, RATE_HZ_MAX]."""
    rb = layer.get("rate_beats")
    if isinstance(rb, (int, float)) and rb > 0:
        hz = (float(bpm) / 60.0) / float(rb)
    else:
        hz = float(layer.get("rate_hz", default_hz))
    return max(RATE_HZ_MIN, min(RATE_HZ_MAX, hz))


def _advance_phase(state: dict, t: float, rate_hz: float) -> float:
    """Integrate state['_phase'] += rate_hz * dt. Monotonic across BPM
    changes — without this, a moving rate_hz makes step jump backward."""
    last_t = state.get("_last_t")
    if last_t is None:
        state["_last_t"] = t
        state.setdefault("_phase", 0.0)
        return state["_phase"]
    dt = t - last_t
    if dt < 0:
        dt = 0.0
    elif dt > DT_MAX_S:
        dt = DT_MAX_S
    phase = state.get("_phase", 0.0) + rate_hz * dt
    state["_phase"] = phase
    state["_last_t"] = t
    return phase


def _resolve_pulse_period_ms(layer: dict, bpm: float) -> tuple[int, int]:
    """Returns (on_ms, period_ms). period_beats wins if > 0; on_ms stays
    absolute either way. If on_ms ≥ period_ms (extreme tempo / tiny period),
    on_ms is clamped so the off phase is at least 20% of the period."""
    on_ms = max(1, int(layer.get("on_ms", 80)))
    pb = layer.get("period_beats")
    if isinstance(pb, (int, float)) and pb > 0:
        period_ms = max(1, int((60.0 / float(bpm)) * float(pb) * 1000.0))
    else:
        off_ms = max(0, int(layer.get("off_ms", 40)))
        period_ms = max(1, on_ms + off_ms)
    if on_ms >= period_ms:
        on_ms = max(1, int(period_ms * 0.8))
    return on_ms, period_ms


def _advance_pulse_phase(state: dict, t: float, period_ms: int) -> tuple[int, float]:
    """Integrate cycle phase in ms. Returns (cycle_idx, phase_ms in [0, period_ms))."""
    last_t = state.get("_last_t")
    if last_t is None:
        state["_last_t"] = t
        state.setdefault("_cycle_idx", 0)
        state.setdefault("_phase_ms", 0.0)
        return state["_cycle_idx"], state["_phase_ms"]
    dt = t - last_t
    if dt < 0:
        dt = 0.0
    elif dt > DT_MAX_S:
        dt = DT_MAX_S
    phase_ms = state.get("_phase_ms", 0.0) + dt * 1000.0
    cycle_idx = state.get("_cycle_idx", 0)
    while period_ms > 0 and phase_ms >= period_ms:
        phase_ms -= period_ms
        cycle_idx += 1
    state["_phase_ms"] = phase_ms
    state["_cycle_idx"] = cycle_idx
    state["_last_t"] = t
    return cycle_idx, phase_ms


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
    bpm = state.get("_bpm", FALLBACK_BPM)
    rate_hz = _resolve_rate_hz(layer, bpm, default_hz=1.0)
    dim_on = int(layer.get("dim_active", 128))
    dim_off = int(layer.get("dim_rest", 0))
    step = int(_advance_phase(state, t, rate_hz))

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
    bpm = state.get("_bpm", FALLBACK_BPM)
    rate_hz = _resolve_rate_hz(layer, bpm, default_hz=4.0)
    direction = layer.get("direction", "wrap")
    tail = max(0, min(3, int(layer.get("tail", 0))))
    dim_active = int(layer.get("dim_active", 255))
    dim_rest = int(layer.get("dim_rest", 0))
    amber = layer.get("amber", 0)
    strobe = int(layer.get("strobe", 0))
    wash_mode = layer.get("wash", "off")
    include_wash = wash_mode == "include"

    n = 6 if include_wash else 4
    step = int(_advance_phase(state, t, rate_hz))

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


def _layer_wash_pingpong(dmx, layer: dict, t: float, state: dict) -> None:
    """Hard ping-pong between wash_1 and wash_2 at rate_hz toggles/sec.

    Color advances one slot per toggle, so a 2-color palette gives each
    side its own color. `dim_rest` keeps the "off" wash glowing — set to 0
    for a single-wash-on-at-a-time strobe feel."""
    colors = layer.get("colors") or [[255, 255, 255]]
    rate_hz = float(layer.get("rate_hz", 2.0))
    dim_on = int(layer.get("dim_active", 200))
    dim_off = int(layer.get("dim_rest", 0))
    amber = layer.get("amber", 0)
    strobe = _clamp(int(layer.get("strobe", 0)))
    step = int(t * rate_hz)
    r, g, b = colors[step % len(colors)][:3]
    on_r, on_g, on_b, on_a = _scale(r, g, b, amber, dim_on)
    off_r, off_g, off_b, off_a = _scale(r, g, b, amber, dim_off)
    if step % 2 == 0:
        dmx.set_12s_one(0, on_r, on_g, on_b, on_a, 255, strobe)
        dmx.set_12s_one(1, off_r, off_g, off_b, off_a, 255, strobe)
    else:
        dmx.set_12s_one(0, off_r, off_g, off_b, off_a, 255, strobe)
        dmx.set_12s_one(1, on_r, on_g, on_b, on_a, 255, strobe)


def _layer_wash_chase(dmx, layer: dict, t: float, state: dict) -> None:
    """Smooth crossfade between wash_1 and wash_2 — sinusoidal blend.

    As wash_1 brightens from dim_min toward dim_max, wash_2 darkens from
    dim_max toward dim_min, and vice versa. One full back-and-forth per
    1/hz seconds. Color advances one slot per half-cycle (per fade)."""
    colors = layer.get("colors") or [[255, 255, 255]]
    hz = float(layer.get("hz", 0.5))
    dim_min = int(layer.get("dim_min", 0))
    dim_max = int(layer.get("dim_max", 200))
    amber = layer.get("amber", 0)
    strobe = _clamp(int(layer.get("strobe", 0)))

    wave = 0.5 + 0.5 * math.sin(2 * math.pi * hz * t)
    dim_a = dim_min + (dim_max - dim_min) * wave
    dim_b = dim_min + (dim_max - dim_min) * (1.0 - wave)

    half_cycle_idx = int(t * 2 * hz) % len(colors)
    r, g, b = colors[half_cycle_idx][:3]

    ar, ag, ab, aa = _scale(r, g, b, amber, int(dim_a))
    br, bg, bb_, ba = _scale(r, g, b, amber, int(dim_b))
    dmx.set_12s_one(0, ar, ag, ab, aa, 255, strobe)
    dmx.set_12s_one(1, br, bg, bb_, ba, 255, strobe)


def _layer_dual_wash(dmx, layer: dict, t: float, state: dict) -> None:
    """Static asymmetric wash: wash_1 holds rgb_left, wash_2 holds rgb_right.

    Useful as a base layer under a busy bar chase, or as a standalone
    breakdown look (teal/magenta room split etc.)."""
    rgb_l = layer.get("rgb_left") or [255, 0, 255]
    rgb_r = layer.get("rgb_right") or [0, 255, 255]
    dim = _clamp(int(layer.get("dim", 200)))
    amber = layer.get("amber", 0)
    strobe = _clamp(int(layer.get("strobe", 0)))
    lr, lg, lb, la = _scale(rgb_l[0], rgb_l[1], rgb_l[2], amber, dim)
    rr, rg, rb, ra = _scale(rgb_r[0], rgb_r[1], rgb_r[2], amber, dim)
    dmx.set_12s_one(0, lr, lg, lb, la, 255, strobe)
    dmx.set_12s_one(1, rr, rg, rb, ra, 255, strobe)


def _layer_pulse(dmx, layer: dict, t: float, state: dict) -> None:
    """Alternate target between a color (from colors[], advancing per cycle)
    and blackout.

    Period: `period_beats` (BPM-locked) wins if > 0; otherwise on_ms+off_ms.
    on_ms stays absolute either way — flashes don't stretch across tempos.
    """
    target = layer.get("target", "all")
    if target not in DMX_TARGETS:
        return
    colors = layer.get("colors") or [[255, 255, 255]]
    a = layer.get("amber", 0)
    dim = layer.get("dim", 255)
    strobe = layer.get("strobe", 0)
    bpm = state.get("_bpm", FALLBACK_BPM)
    on_ms, period_ms = _resolve_pulse_period_ms(layer, bpm)
    cycle_idx, phase_ms = _advance_pulse_phase(state, t, period_ms)
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


_POPCORN_SOLO_THRESHOLD = 0.05  # in solo mode, treat a unit below 5% as "done" so the next pop can fire while the tail finishes — keeps cadence from being strictly bounded by decay_ms.


def _layer_popcorn(dmx, layer: dict, t: float, state: dict) -> None:
    """Random per-unit pops with linear decay back to a baseline.

    Each unit (per POPCORN_UNIT_MAP[target]) holds an independent brightness
    `level` in [0.0, 1.0]. On each tick:
      - level decays toward 0 at rate (1 / decay_ms).
      - In `solo` mode, at most one pop is active at a time: a single
        per-tick roll at probability (flash_rate_hz * dt) fires, and only
        if no in-scope unit is still meaningfully lit (level > 0.05). Pop
        target is uniformly random across in-scope units.
      - In `overlap` mode, every unit independently rolls
        (flash_rate_hz * dt / n) — multiple units can be lit simultaneously,
        which is what produces the "double-flash" feel.
      - Painted brightness is min_brightness + (max - min) * level, so a unit
        at rest sits at min_brightness and a fresh pop reaches max_brightness.

    Rate is *combined across all units* in scope — pick 5 Hz and you get ~5
    pops per second total regardless of whether scope is wash (2), bar (4),
    or everything (6). Keeps visual cadence stable when target changes.
    Note: in solo mode actual rate is capped near 1/decay_seconds because
    new pops wait for the previous one to finish.
    """
    target = layer.get("target", "all")
    units = POPCORN_UNIT_MAP.get(target)
    if not units:
        return

    colors_in = layer.get("colors") or [[255, 255, 255]]
    colors = [c[:3] for c in colors_in if isinstance(c, (list, tuple)) and len(c) >= 3]
    if not colors:
        colors = [[255, 255, 255]]
    max_b = _clamp(layer.get("max_brightness", 255))
    min_b = _clamp(layer.get("min_brightness", 0))
    if min_b > max_b:
        min_b = max_b
    span = max_b - min_b
    flash_rate = max(0.0, float(layer.get("flash_rate_hz", 5.0)))
    decay_ms = max(20.0, float(layer.get("decay_ms", 250.0)))
    decay_per_s = 1000.0 / decay_ms  # level units per second
    amber = _clamp(layer.get("amber", 0))
    strobe = _clamp(layer.get("strobe", 0))
    mode = layer.get("mode", "solo")
    if mode not in {"solo", "overlap"}:
        mode = "solo"

    last_t = state.get("_last_t")
    if last_t is None:
        dt = 0.0
    else:
        dt = t - last_t
        if dt < 0:
            dt = 0.0
        elif dt > DT_MAX_S:
            dt = DT_MAX_S
    state["_last_t"] = t

    # Per-unit state: {level: float, color: [r,g,b]}.
    units_state = state.setdefault("_units", {})
    decay_step = decay_per_s * dt
    n = len(units)

    # 1. Decay every in-scope unit. Out-of-scope state lingers harmlessly
    #    (we never render it; tick-start blackout in the engine handles
    #    the off pixels).
    for unit_name in units:
        u = units_state.get(unit_name)
        if u is None:
            u = {"level": 0.0, "color": colors[0]}
            units_state[unit_name] = u
        u["level"] = max(0.0, u["level"] - decay_step)

    # 2. Trigger phase. Gating depends on mode.
    if mode == "solo":
        any_active = any(
            units_state[u]["level"] > _POPCORN_SOLO_THRESHOLD for u in units
        )
        if not any_active:
            p = min(1.0, flash_rate * dt)
            if p > 0 and random.random() < p:
                chosen = random.choice(units)
                units_state[chosen]["level"] = 1.0
                units_state[chosen]["color"] = random.choice(colors)
    else:  # overlap — original per-unit rolling, multiple units can be active
        p_per_unit = min(1.0, flash_rate * dt / n) if n else 0.0
        if p_per_unit > 0:
            for unit_name in units:
                if random.random() < p_per_unit:
                    units_state[unit_name]["level"] = 1.0
                    units_state[unit_name]["color"] = random.choice(colors)

    # 3. Render every in-scope unit at its current level.
    for unit_name in units:
        u = units_state[unit_name]
        dim = min_b + int(round(span * u["level"]))
        cr, cg, cb = u["color"][:3]
        _paint_target(dmx, unit_name, _clamp(cr), _clamp(cg), _clamp(cb), amber, dim, strobe)


def _layer_acid_kaleidoscope(dmx, layer: dict, t: float, state: dict) -> None:
    """Kaleidoscope Meltdown — a full rainbow rotates across the rig at
    `spin_dps` degrees/sec. Each segment is offset by `spread_deg` (a
    non-harmonic value like 47° keeps the segments from ever lining up), and
    each segment breathes its brightness on its own slightly-detuned sine, so
    the whole rig shimmers and never lands on a flat frame.

      spin_dps    rotation speed of the base hue (°/s; negative spins the
                  other way).
      spread_deg  hue offset between adjacent segments.
      breathe_hz  base breathing frequency; segment i runs ~18% faster per
                  index so the breaths drift in and out of phase.
      sat         color saturation (0..255).
      dim_min/max breathing brightness floor/ceiling (0..255).
    """
    spin = float(layer.get("spin_dps", 40.0))
    spread = float(layer.get("spread_deg", 47.0))
    breathe_hz = float(layer.get("breathe_hz", 0.3))
    sat = _clamp(layer.get("sat", 255)) / 255.0
    dim_min = float(layer.get("dim_min", 20))
    dim_max = float(layer.get("dim_max", 220))
    base = spin * t
    for i in range(ACID_N):
        hue = base + i * spread
        f = breathe_hz * (1.0 + 0.18 * i)
        wave = 0.5 - 0.5 * math.cos(2 * math.pi * f * t + i * 1.3)
        v = (dim_min + (dim_max - dim_min) * wave) / 255.0
        r, g, b = _hsv_to_rgb(hue, sat, v)
        _acid_paint_segment(dmx, i, r, g, b)


def _layer_acid_bloom(dmx, layer: dict, t: float, state: dict) -> None:
    """Fractal Seizure Bloom — controlled chaos.

    Every segment owns a hue that random-walks independently. On top of that:
    random brightness spikes bloom a segment to full and decay back; white
    sparks flash a segment near-white; the whole rig periodically inverts to
    complementary hues; and random "color cannon" stabs slam a single segment
    to a fresh saturated hue at full brightness. No two frames repeat.

      walk_speed      hue random-walk magnitude (°/s, gaussian).
      spike_rate      brightness blooms per second (combined).
      spark_rate      white sparks per second (combined).
      cannon_rate     color-cannon stabs per second (combined).
      invert_period_s seconds between whole-rig complementary inversions.
      base_dim        resting brightness between blooms (0..255).
      sat             base saturation (0..255).
      decay_ms        bloom/spark decay time.
    """
    walk_speed = float(layer.get("walk_speed", 70.0))
    spike_rate = float(layer.get("spike_rate", 4.0))
    spark_rate = float(layer.get("spark_rate", 1.5))
    cannon_rate = float(layer.get("cannon_rate", 0.8))
    invert_period = max(0.5, float(layer.get("invert_period_s", 4.0)))
    base_dim = _clamp(layer.get("base_dim", 40))
    sat = _clamp(layer.get("sat", 255)) / 255.0
    decay_ms = max(20.0, float(layer.get("decay_ms", 300.0)))

    last_t = state.get("_last_t")
    if last_t is None:
        dt = 0.0
    else:
        dt = t - last_t
        if dt < 0:
            dt = 0.0
        elif dt > DT_MAX_S:
            dt = DT_MAX_S
    state["_last_t"] = t

    hues = state.get("_hues")
    if hues is None:
        # Seed by index so the very first frame is already a spread, not a
        # single color (random module is fine here — visual, not security).
        hues = [(i * 360.0 / ACID_N) for i in range(ACID_N)]
        state["_hues"] = hues
        state["_levels"] = [0.0] * ACID_N
        state["_sparks"] = [0.0] * ACID_N
        state["_next_invert"] = invert_period
    levels = state["_levels"]
    sparks = state["_sparks"]

    decay_step = (1000.0 / decay_ms) * dt

    state["_next_invert"] -= dt
    if state["_next_invert"] <= 0.0:
        state["_next_invert"] += invert_period
        for i in range(ACID_N):
            hues[i] = (hues[i] + 180.0) % 360.0

    for i in range(ACID_N):
        hues[i] = (hues[i] + random.gauss(0.0, walk_speed) * dt) % 360.0
        levels[i] = max(0.0, levels[i] - decay_step)
        sparks[i] = max(0.0, sparks[i] - decay_step * 1.6)  # sparks die faster

    if random.random() < min(1.0, spike_rate * dt):
        levels[random.randrange(ACID_N)] = 1.0
    if random.random() < min(1.0, spark_rate * dt):
        sparks[random.randrange(ACID_N)] = 1.0
    if random.random() < min(1.0, cannon_rate * dt):
        j = random.randrange(ACID_N)
        hues[j] = random.uniform(0.0, 360.0)
        levels[j] = 1.0

    for i in range(ACID_N):
        spark = sparks[i]
        v = (base_dim + (255 - base_dim) * levels[i]) / 255.0
        if spark > 0.02:
            # desaturate toward white as the spark peaks
            r, g, b = _hsv_to_rgb(hues[i], sat * (1.0 - spark), max(v, spark))
        else:
            r, g, b = _hsv_to_rgb(hues[i], sat, v)
        _acid_paint_segment(dmx, i, r, g, b)


# Per-zone "arrival slot" for each shoot mode. Lower slot = lit earlier as the
# shoot front advances; ties (center / split) light two zones at once.
_BAR_SHOOT_SLOTS = {
    "out":    {1: 0, 2: 1, 3: 2, 4: 3},   # left  → right
    "in":     {4: 0, 3: 1, 2: 2, 1: 3},   # right → left
    "center": {2: 0, 3: 0, 1: 1, 4: 1},   # middle → edges
    "split":  {1: 0, 4: 0, 2: 1, 3: 1},   # edges  → middle
}


def _layer_bar_shoot(dmx, layer: dict, t: float, state: dict) -> None:
    """Launch-and-recoil across the 4 bar zones — the "shootout".

    One cycle runs four acts back to back:
      shoot   (shoot_ms)   light fills the zones in `mode` order, fast.
      hold    (hold_ms)    every zone sits at dim_active.
      retract (retract_ms) brightness is pulled back. `retract`:
                             "recede" — zones go dark in reverse arrival
                                        order, so the light visibly recoils
                                        toward where it launched from.
                             "fade"   — every lit zone dims uniformly down to
                                        dim_rest (a flat brightness drain).
      gap     (gap_ms)      everything rests at dim_rest before the next launch.

    `mode` picks the shoot axis:
      "out"    bar fills left→right, recoils right→left.
      "in"     bar fills right→left, recoils left→right.
      "center" fills from the middle two zones outward, recoils inward.
      "split"  fills from the outer zones inward, recoils outward.

    `colors` advances one slot per launch, so a multi-color palette fires a
    different color each cycle. `wash="match"` mirrors the rig-wide average
    brightness onto both Tetra 12s in the launch color (otherwise the wash
    stays dark via the engine's per-tick blackout).
    """
    colors = layer.get("colors") or [[255, 255, 255]]
    if not colors:
        colors = [[255, 255, 255]]
    slots = _BAR_SHOOT_SLOTS.get(layer.get("mode", "out"), _BAR_SHOOT_SLOTS["out"])
    retract_mode = layer.get("retract", "recede")
    shoot_ms = max(1, int(layer.get("shoot_ms", 120)))
    hold_ms = max(0, int(layer.get("hold_ms", 40)))
    retract_ms = max(1, int(layer.get("retract_ms", 220)))
    gap_ms = max(0, int(layer.get("gap_ms", 180)))
    dim_active = int(layer.get("dim_active", 255))
    dim_rest = int(layer.get("dim_rest", 0))
    amber = layer.get("amber", 0)
    strobe = _clamp(int(layer.get("strobe", 0)))
    wash_mode = layer.get("wash", "off")

    period_ms = shoot_ms + hold_ms + retract_ms + gap_ms
    t_ms = t * 1000.0
    phase = t_ms % period_ms
    cycle = int(t_ms // period_ms)

    max_slot = max(slots.values())
    span_slots = max_slot + 1  # arrival steps the front walks across

    # Per-zone fill level in [0, 1]; 0 → dim_rest, 1 → dim_active.
    levels: dict[int, float] = {}
    if phase < shoot_ms:
        front = (phase / shoot_ms) * span_slots
        for z, slot in slots.items():
            levels[z] = max(0.0, min(1.0, front - slot))
    elif phase < shoot_ms + hold_ms:
        for z in slots:
            levels[z] = 1.0
    elif phase < shoot_ms + hold_ms + retract_ms:
        rp = (phase - shoot_ms - hold_ms) / retract_ms  # 0..1
        if retract_mode == "fade":
            for z in slots:
                levels[z] = max(0.0, 1.0 - rp)
        else:  # recede — last-arrived zones darken first, light recoils
            drain = rp * span_slots
            for z, slot in slots.items():
                rev = max_slot - slot
                levels[z] = max(0.0, min(1.0, 1.0 - (drain - rev)))
    else:
        for z in slots:
            levels[z] = 0.0

    r, g, b = colors[cycle % len(colors)][:3]
    bspan = dim_active - dim_rest
    for z in (1, 2, 3, 4):
        dim = dim_rest + int(round(bspan * levels.get(z, 0.0)))
        rr, gg, bb_, aa = _scale(r, g, b, amber, dim)
        dmx.set_bar_zone(z, rr, gg, bb_, aa, 255, strobe)

    if wash_mode == "match":
        avg = sum(levels.get(z, 0.0) for z in (1, 2, 3, 4)) / 4.0
        wdim = dim_rest + int(round(bspan * avg))
        wr, wg, wb, wa = _scale(r, g, b, amber, wdim)
        dmx.set_12s(wr, wg, wb, wa, 255, strobe)


def _layer_bar_flow(dmx: Any, layer: dict, t: float, state: dict) -> None:
    """Continuous smooth flow across the bar — a soft blob of light glides
    from zone to zone, crossfading instead of snapping.

    Where bar_chase steps a hard head zone-to-zone, bar_flow moves a
    *continuous* position and paints each zone by its distance from that
    position through a raised-cosine falloff, so adjacent zones hand off
    brightness smoothly (the "liquid" feel). `width` sets how many zones the
    glow spans — wider = softer, more zones lit at once.

    Speed: rate_hz (positions/sec) or rate_beats (BPM-locked) — same contract
    as chase / bar_chase. A slow rate (≈0.5–1.5 positions/sec) reads as a calm
    drifting glow; crank it for a fast sweep.

    direction:
      "wrap"     position loops 0→N continuously; the glow crosses the 1↔4
                 seam without a visible edge.
      "pingpong" position bounces end to end.
    wash:
      "off"      bar zones only (4 positions).
      "match"    wash mirrors the brightest bar zone's level.
      "include"  wash_1 / wash_2 become the end positions, so the glow flows
                 left→right across the whole room (6 positions).
    `colors` advances one slot per full pass.
    """
    colors = layer.get("colors") or [[255, 255, 255]]
    if not colors:
        colors = [[255, 255, 255]]
    bpm = state.get("_bpm", FALLBACK_BPM)
    rate_hz = _resolve_rate_hz(layer, bpm, default_hz=1.0)
    direction = layer.get("direction", "wrap")
    width = max(0.2, float(layer.get("width", 1.2)))
    dim_active = int(layer.get("dim_active", 255))
    dim_rest = int(layer.get("dim_rest", 0))
    amber = layer.get("amber", 0)
    strobe = _clamp(int(layer.get("strobe", 0)))
    wash_mode = layer.get("wash", "off")
    include_wash = wash_mode == "include"

    n = 6 if include_wash else 4
    phase = _advance_phase(state, t, rate_hz)

    if direction == "pingpong":
        period = 2 * (n - 1)
        x = phase % period
        head = x if x <= (n - 1) else period - x  # triangle 0..n-1..0
        lap = int(phase // (n - 1))
        wrap = False
    else:
        head = phase % n
        lap = int(phase // n)
        wrap = True

    r, g, b = colors[lap % len(colors)][:3]
    span = dim_active - dim_rest

    levels = []
    for zi in range(n):
        d = abs(zi - head)
        if wrap:
            d = min(d, n - d)  # wrap distance so the blob crosses the seam
        x = min(1.0, d / width)
        levels.append(0.5 * (1.0 + math.cos(math.pi * x)))  # 1 at center → 0 at width

    for zi in range(n):
        dim = dim_rest + int(round(span * levels[zi]))
        rr, gg, bb_, aa = _scale(r, g, b, amber, dim)
        if include_wash:
            _acid_paint_segment(dmx, zi, rr, gg, bb_, aa, strobe)
        else:
            dmx.set_bar_zone(zi + 1, rr, gg, bb_, aa, 255, strobe)

    if not include_wash and wash_mode == "match":
        peak = max(levels) if levels else 0.0
        wdim = dim_rest + int(round(span * peak))
        wr, wg, wb, wa = _scale(r, g, b, amber, wdim)
        dmx.set_12s(wr, wg, wb, wa, 255, strobe)


def _govee_signature(scene: dict) -> tuple:
    """Stable key for the Govee subset of a scene. update_scene() compares
    these to decide whether to re-fire Govee layers."""
    return tuple(
        json.dumps(l, sort_keys=True)
        for l in scene.get("layers", [])
        if l.get("type") in {"govee_preset", "govee_rgb"}
    )


DMX_LAYER_RENDERERS: dict[str, Callable[[Any, dict, float, dict], None]] = {
    "solid": _layer_solid,
    "breathe": _layer_breathe,
    "chase": _layer_chase,
    "bar_chase": _layer_bar_chase,
    "wash_pingpong": _layer_wash_pingpong,
    "wash_chase": _layer_wash_chase,
    "dual_wash": _layer_dual_wash,
    "pulse": _layer_pulse,
    "strobe": _layer_strobe,
    "random_flash": _layer_random_flash,
    "popcorn": _layer_popcorn,
    "bar_shoot": _layer_bar_shoot,
    "bar_flow": _layer_bar_flow,
    "acid_kaleidoscope": _layer_acid_kaleidoscope,
    "acid_bloom": _layer_acid_bloom,
}


# ---------------------------------------------------------------------------
# Intensity transform
# ---------------------------------------------------------------------------
#
# A single scalar `intensity ∈ [0, 1]` modulates how aggressive every DMX
# layer reads. Declared layer values are treated as the i=1.0 ceiling — at
# i=1 every scene plays exactly as authored, and i<1 attenuates a fixed set
# of fields per layer type (brightness scales down, rates slow, strobe gates
# off, event density drops).
#
# Govee layers (govee_rgb, govee_preset) are never touched: presets play
# autonomously on-device and can't be smoothly modulated, and their LAN
# rate-limit makes per-tick brightness changes impractical.
#
# A layer can opt out with `"intensify": false`, in which case the engine
# passes it through unchanged regardless of the active intensity value.

def _lerp(lo: float, hi: float, t: float) -> float:
    return lo + (hi - lo) * t


def _scale_value(value, lo_frac: float, i: float) -> float:
    """value * lerp(lo_frac, 1.0, i). At i=1 returns value untouched."""
    return value * _lerp(lo_frac, 1.0, i)


def _scale_dim(value, lo_frac: float, i: float) -> int:
    return int(round(_scale_value(value, lo_frac, i)))


def _gamma_dim(value, i: float, gamma: float = 2.0) -> int:
    """Brightness with perceptual curve: dim drops fast as i falls.
    At i=1 returns value, at i=0.5 returns ~25% (gamma=2), at i=0 returns 0.
    """
    if i <= 0.0:
        return 0
    if i >= 1.0:
        return int(round(value))
    return int(round(value * (i ** gamma)))


def _stretch(value, max_mult: float, i: float) -> float:
    """For rate_beats / gap_s where bigger = slower. At i=0 returns
    value*max_mult (much slower), at i=1 returns value untouched.
    """
    return value * _lerp(max_mult, 1.0, i)


def _gate(value, threshold: float, i: float) -> float:
    """Hold at 0 until i>=threshold, then ramp linearly to `value` at i=1."""
    if i < threshold:
        return 0.0
    span = max(1e-6, 1.0 - threshold)
    return value * ((i - threshold) / span)


def _t_solid(layer, i):
    out = dict(layer)
    if "dim" in layer:
        out["dim"] = _gamma_dim(layer["dim"], i, 2.0)
    return out


def _t_breathe(layer, i):
    out = dict(layer)
    if "dim_max" in layer:
        out["dim_max"] = _gamma_dim(layer["dim_max"], i, 1.8)
    if "hz" in layer:
        out["hz"] = _scale_value(layer["hz"], 0.25, i)
    return out


def _t_chase(layer, i):
    out = dict(layer)
    if "dim_active" in layer:
        out["dim_active"] = _gamma_dim(layer["dim_active"], i, 1.8)
    rb = layer.get("rate_beats")
    if isinstance(rb, (int, float)) and rb > 0:
        # rate_beats: smaller = faster, so at low intensity we *stretch* it.
        out["rate_beats"] = _stretch(rb, 4.0, i)
    elif "rate_hz" in layer:
        out["rate_hz"] = _scale_value(layer["rate_hz"], 0.20, i)
    return out


def _t_bar_chase(layer, i):
    out = dict(layer)
    if "dim_active" in layer:
        out["dim_active"] = _gamma_dim(layer["dim_active"], i, 1.8)
    rb = layer.get("rate_beats")
    if isinstance(rb, (int, float)) and rb > 0:
        out["rate_beats"] = _stretch(rb, 2.0, i)
    elif "rate_hz" in layer:
        out["rate_hz"] = _scale_value(layer["rate_hz"], 0.20, i)
    if "tail" in layer:
        out["tail"] = int(round(int(layer["tail"]) * (i ** 1.5)))
    if layer.get("strobe"):
        out["strobe"] = int(round(_gate(int(layer["strobe"]), 0.7, i)))
    return out


def _t_pulse(layer, i):
    out = dict(layer)
    # pulse dim is hard-gated below 0.15 — sparse pops should be silent at floor
    if "dim" in layer:
        if i < 0.15:
            out["dim"] = 0
        else:
            out["dim"] = _gamma_dim(layer["dim"], i, 1.8)
    pb = layer.get("period_beats")
    if isinstance(pb, (int, float)) and pb > 0:
        out["period_beats"] = _stretch(pb, 3.0, i)
    elif "off_ms" in layer:
        out["off_ms"] = int(round(int(layer["off_ms"]) * _lerp(3.0, 1.0, i)))
    return out


def _t_strobe(layer, i):
    out = dict(layer)
    rate = int(layer.get("rate", 0) or 0)
    if rate > 0:
        # strobe is the loudest knob — gate it off below i=0.6, then ramp.
        out["rate"] = int(round(_gate(rate, 0.6, i)))
    return out


def _t_random_flash(layer, i):
    out = dict(layer)
    # below i=0.10, push gaps so wide the layer is effectively silent
    if i < 0.10:
        out["min_gap_s"] = 600.0
        out["max_gap_s"] = 600.0
        out["dim"] = 0
        return out
    if "min_gap_s" in layer:
        out["min_gap_s"] = _stretch(float(layer["min_gap_s"]), 2.5, i)
    if "max_gap_s" in layer:
        out["max_gap_s"] = _stretch(float(layer["max_gap_s"]), 2.5, i)
    if "dim" in layer:
        out["dim"] = _gamma_dim(layer["dim"], i, 1.7)
    return out


def _t_popcorn(layer, i):
    out = dict(layer)
    if "flash_rate_hz" in layer:
        out["flash_rate_hz"] = float(layer["flash_rate_hz"]) * _lerp(0.15, 1.0, i)
    if "max_brightness" in layer:
        out["max_brightness"] = _gamma_dim(layer["max_brightness"], i, 1.7)
    if layer.get("strobe"):
        out["strobe"] = int(round(_gate(int(layer["strobe"]), 0.7, i)))
    return out


def _t_wash_pingpong(layer, i):
    out = dict(layer)
    if "dim_active" in layer:
        out["dim_active"] = _gamma_dim(layer["dim_active"], i, 1.8)
    if "rate_hz" in layer:
        out["rate_hz"] = _scale_value(layer["rate_hz"], 0.20, i)
    return out


def _t_wash_chase(layer, i):
    out = dict(layer)
    if "dim_max" in layer:
        out["dim_max"] = _gamma_dim(layer["dim_max"], i, 1.8)
    if "hz" in layer:
        out["hz"] = _scale_value(layer["hz"], 0.25, i)
    return out


def _t_dual_wash(layer, i):
    out = dict(layer)
    if "dim" in layer:
        out["dim"] = _gamma_dim(layer["dim"], i, 1.8)
    return out


def _t_bar_flow(layer, i):
    out = dict(layer)
    if "dim_active" in layer:
        out["dim_active"] = _gamma_dim(layer["dim_active"], i, 1.8)
    rb = layer.get("rate_beats")
    if isinstance(rb, (int, float)) and rb > 0:
        out["rate_beats"] = _stretch(rb, 4.0, i)
    elif "rate_hz" in layer:
        out["rate_hz"] = _scale_value(layer["rate_hz"], 0.20, i)
    if layer.get("strobe"):
        out["strobe"] = int(round(_gate(int(layer["strobe"]), 0.7, i)))
    return out


def _t_bar_shoot(layer, i):
    out = dict(layer)
    if "dim_active" in layer:
        out["dim_active"] = _gamma_dim(layer["dim_active"], i, 1.8)
    if "gap_ms" in layer:
        # lower energy → longer rests between launches, so the shootout fires
        # less often while the motion itself stays crisp.
        out["gap_ms"] = int(round(_stretch(int(layer["gap_ms"]), 4.0, i)))
    if layer.get("strobe"):
        out["strobe"] = int(round(_gate(int(layer["strobe"]), 0.7, i)))
    return out


def _t_acid_kaleidoscope(layer, i):
    out = dict(layer)
    if "dim_max" in layer:
        out["dim_max"] = _gamma_dim(layer["dim_max"], i, 1.8)
    if "spin_dps" in layer:
        out["spin_dps"] = _scale_value(layer["spin_dps"], 0.3, i)
    return out


def _t_acid_bloom(layer, i):
    out = dict(layer)
    if "base_dim" in layer:
        out["base_dim"] = _gamma_dim(layer["base_dim"], i, 1.7)
    if "spike_rate" in layer:
        out["spike_rate"] = _scale_value(layer["spike_rate"], 0.2, i)
    if "cannon_rate" in layer:
        out["cannon_rate"] = _scale_value(layer["cannon_rate"], 0.2, i)
    return out


INTENSITY_TRANSFORMS: dict[str, Callable[[dict, float], dict]] = {
    "solid": _t_solid,
    "breathe": _t_breathe,
    "chase": _t_chase,
    "bar_chase": _t_bar_chase,
    "pulse": _t_pulse,
    "strobe": _t_strobe,
    "random_flash": _t_random_flash,
    "popcorn": _t_popcorn,
    "wash_pingpong": _t_wash_pingpong,
    "wash_chase": _t_wash_chase,
    "dual_wash": _t_dual_wash,
    "bar_shoot": _t_bar_shoot,
    "bar_flow": _t_bar_flow,
    "acid_kaleidoscope": _t_acid_kaleidoscope,
    "acid_bloom": _t_acid_bloom,
}


def _enter_gate(layer: dict, intensity: float) -> Optional[float]:
    """Intensity-gated layer entry — the "add-on" mechanism.

    A layer with `enter_at: e` (0..1) renders nothing until the live intensity
    reaches `e`, then fades in: the returned value is intensity remapped from
    [e, 1] onto [0, 1], so the layer climbs from its own floor exactly as if the
    build were starting there. Pass this result to _apply_intensity.

    Returns None when the layer is below its entry threshold (skip rendering).
    A layer with no `enter_at` (or enter_at <= 0) is a base layer — it plays
    from the bottom and this returns the intensity unchanged.
    """
    e = layer.get("enter_at")
    if not e:  # None or 0 -> base layer, ungated
        return intensity
    if intensity < e:
        return None
    return (intensity - e) / max(1e-6, 1.0 - e)


def _apply_intensity(layer: dict, intensity: float) -> dict:
    """Return a copy of `layer` with intensity-driven fields scaled.

    Fast path: if intensity >= 1 or the layer opts out (`intensify: false`)
    or has no transform, return the original dict — no allocation.
    """
    if intensity >= 1.0:
        return layer
    if layer.get("intensify") is False:
        return layer
    transform = INTENSITY_TRANSFORMS.get(layer.get("type"))
    if transform is None:
        return layer
    if intensity < 0.0:
        intensity = 0.0
    return transform(layer, intensity)


class SceneEngine:
    """Runs a single scene dict against a DMX controller + Govee client.

    start() fires one-shot Govee layers and spawns the DMX render thread.
    stop() signals the thread to exit and joins with a timeout. The caller is
    responsible for any subsequent blackout — stop() leaves fixtures at their
    last painted state so a scene swap is glitch-free.
    """

    def __init__(
        self,
        scene: dict,
        dmx,
        govee,
        bpm_fn: Optional[Callable[[], float]] = None,
        intensity_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self.scene = scene
        self.dmx = dmx
        self.govee = govee
        # bpm_fn returns a positive float on every tick. Renderers that
        # use rate_beats / period_beats divide BPM by their beats value to
        # get live Hz. None = fall through to FALLBACK_BPM (preview/tests).
        self.bpm_fn: Callable[[], float] = bpm_fn or (lambda: FALLBACK_BPM)
        # intensity_fn returns 0..1. Mutates layer dicts pre-render via
        # _apply_intensity. None / >=1 = scenes play exactly as authored.
        self.intensity_fn: Callable[[], float] = intensity_fn or (lambda: 1.0)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Compiled DMX layers + per-layer state, swapped atomically by
        # update_scene() so the editor can drag sliders without tearing down
        # the render thread or re-firing Govee layers.
        self._lock = threading.Lock()
        self._dmx_layers: list = [
            l for l in scene.get("layers", []) if l.get("type") in DMX_LAYER_RENDERERS
        ]
        self._states: list[dict] = [{} for _ in self._dmx_layers]

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

    def update_scene(self, new_scene: dict) -> None:
        """Hot-swap layers without restarting the render thread.

        Called when the editor pushes a slider/color change for the same scene
        id — avoids the ~1s engine teardown and, more importantly, skips the
        Govee network fan-out when only DMX layers changed.

        Per-layer render state (random_flash schedules etc.) is preserved when
        the layer at the same index keeps its type. Govee layers are only
        re-fired when their JSON signature changes.
        """
        with self._lock:
            old_layers = self._dmx_layers
            old_states = self._states
            old_sig = _govee_signature(self.scene)
            new_dmx = [
                l for l in new_scene.get("layers", []) if l.get("type") in DMX_LAYER_RENDERERS
            ]
            new_states: list[dict] = []
            for i, l in enumerate(new_dmx):
                if i < len(old_layers) and old_layers[i].get("type") == l.get("type"):
                    new_states.append(old_states[i])
                else:
                    new_states.append({})
            self.scene = new_scene
            self._dmx_layers = new_dmx
            self._states = new_states
            new_sig = _govee_signature(new_scene)
        if old_sig != new_sig:
            self._fire_govee_layers()

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
                # verify=True so a dropped LAN UDP packet doesn't leave a
                # device (typically the COB strips) stuck lit on the previous
                # scene's color while a DMX-only scene is running.
                self.govee.turn_skus(list(to_off), False, verify=True)
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
        # Layers and per-layer state live on self and can be swapped under
        # _lock by update_scene(). We snapshot the references each tick so
        # renderers don't see a half-applied swap.
        # Always run the tick loop even with zero DMX layers, so the previous
        # scene's final frame gets overwritten by blackout instead of holding.
        # Scene contract: absent DMX layer = fixtures off.
        while not self._stop.is_set():
            t = time.monotonic() - start
            with self._lock:
                layers = self._dmx_layers
                states = self._states
            # One BPM read per tick — passed to every renderer via state.
            # Renderers that don't use it ignore the key.
            try:
                bpm = float(self.bpm_fn())
            except Exception:
                bpm = FALLBACK_BPM
            if not (bpm > 0):
                bpm = FALLBACK_BPM
            try:
                intensity = float(self.intensity_fn())
            except Exception:
                intensity = 1.0
            if intensity != intensity:  # NaN guard
                intensity = 1.0
            intensity = max(0.0, min(1.0, intensity))
            try:
                self.dmx.blackout()
                for layer, state in zip(layers, states):
                    renderer = DMX_LAYER_RENDERERS.get(layer["type"])
                    if renderer is None:
                        continue
                    state["_bpm"] = bpm
                    state["_intensity"] = intensity
                    local_i = _enter_gate(layer, intensity)
                    if local_i is None:
                        continue  # add-on layer below its enter_at threshold
                    try:
                        renderer(self.dmx, _apply_intensity(layer, local_i), t, state)
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
        if ltype == "popcorn":
            tgt = layer.get("target")
            if tgt not in POPCORN_UNIT_MAP:
                errors.append(
                    f"layer {i} (popcorn): invalid target {tgt!r}; "
                    f"must be one of {sorted(POPCORN_UNIT_MAP)}"
                )
        if ltype in {"chase", "bar_chase", "bar_flow"}:
            has_hz = "rate_hz" in layer
            has_beats = "rate_beats" in layer and (layer.get("rate_beats") or 0) > 0
            if has_hz and has_beats:
                errors.append(
                    f"layer {i} ({ltype}): both rate_hz and rate_beats set; pick one"
                )
        if ltype == "pulse":
            has_off = "off_ms" in layer
            has_period_beats = (
                "period_beats" in layer and (layer.get("period_beats") or 0) > 0
            )
            if has_off and has_period_beats:
                errors.append(
                    f"layer {i} (pulse): both off_ms and period_beats set; pick one"
                )
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
