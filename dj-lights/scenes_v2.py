#!/usr/bin/env python3
"""
DMX Scene Library v2 — Simplified for house music.

4 categories that match real track structure:
  BREAKDOWN  — Kick drops out, minimal, atmospheric
  BUILDUP    — Energy rising, tension building toward drop
  GROOVE     — Kick + bass running, the main body of the track
  DROP       — Maximum impact moment after breakdown/buildup

Plus AMBIENT for idle/transitions.

Each scene function also receives a govee_cmd callback for Govee integration.
Govee commands are fire-and-forget; scenes call govee_cmd() to set Govee state.

DMX Channel 5 (Dimmer/Strobe):
  0-127   = Master dimmer (0=off, 127=full)
  128-227 = Strobe (128=slow, 227=fast/23Hz)
  228-255 = Full on (no strobe)
"""

import time
import math
import random

# Brightness caps per category — scaled to 50% max
DIM_AMBIENT = 35
DIM_BREAKDOWN = 30
DIM_BUILDUP = 50
DIM_GROOVE = 55          # merged groove/drive/peak
DIM_DROP = 64            # 50% of 127
HW_STROBE_SLOW = 128
HW_STROBE_MED = 170
HW_STROBE_FAST = 210
HW_STROBE_MAX = 227

# Govee scene indices (from govee dj command)
# 0-9: both (strips + bulbs), 10-14: bulbs only, 15-19: music-reactive strips
GOVEE_AMBIENT = list(range(0, 5))       # calm both-device scenes
GOVEE_BREAKDOWN = [10, 11, 12]          # bulbs only, subtle
GOVEE_BUILDUP = [0, 1, 2, 3]           # both, building
GOVEE_GROOVE = list(range(0, 10)) + list(range(15, 20))  # full range + music reactive
GOVEE_DROP = [3, 4, 7, 9]              # intense scenes (Dance Party, Disco, Flash, Crazy)


def _beat(bpm):
    return 60.0 / bpm


def _lerp(a, b, t):
    return int(a + (b - a) * t)


def _lerp_color(c1, c2, t):
    return tuple(_lerp(c1[i], c2[i], t) for i in range(len(c1)))


def _smooth_frame(dmx):
    dmx.send_frame()
    time.sleep(0.025)


def _energy_progress(get_energy):
    """Convert current audio energy (0-100) to a 0-1 buildup progress curve.
    Low energy = near 0, high energy = near 1. Uses quadratic curve for drama."""
    if not get_energy:
        return 0.5
    energy = get_energy()
    # Map 5-70 energy range to 0-1
    ratio = max(0, min(1, (energy - 5) / 65))
    return ratio ** 1.5  # slightly exponential for dramatic ramp


def _hsv_to_rgb(h, s, v):
    h = h % 360
    c = v * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = v - c
    if h < 60:
        r, g, b = c, x, 0
    elif h < 120:
        r, g, b = x, c, 0
    elif h < 180:
        r, g, b = 0, c, x
    elif h < 240:
        r, g, b = 0, x, c
    elif h < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x
    return int((r + m) * 255), int((g + m) * 255), int((b + m) * 255)


# ============================================================
#  AMBIENT — Idle, pre-track, transitions
# ============================================================

def ambient_ocean_drift(dmx, bpm, govee_cmd=None, get_energy=None):
    """Deep blue slowly shifts to teal. Bar zones offset for depth."""
    if govee_cmd:
        govee_cmd(["scene", str(random.choice(GOVEE_AMBIENT))])
    t = 0
    while True:
        phase = math.sin(t * 0.15) * 0.5 + 0.5
        r, g, b = 0, _lerp(20, 80, phase), _lerp(120, 220, 1 - phase)
        dmx.set_12s(r, g, b, 0, DIM_AMBIENT)
        for z in range(1, 5):
            zp = math.sin(t * 0.15 + z * 0.7) * 0.5 + 0.5
            dmx.set_bar_zone(z, 0, _lerp(20, 80, zp), _lerp(120, 220, 1 - zp), 0, DIM_AMBIENT)
        _smooth_frame(dmx)
        t += 0.025

def ambient_ember_flicker(dmx, bpm, govee_cmd=None, get_energy=None):
    """Warm embers with organic random flicker. Amber heavy."""
    if govee_cmd:
        govee_cmd(["warm"])
    t = 0
    while True:
        for z in range(1, 5):
            flick = random.gauss(0, 8)
            dmx.set_bar_zone(z, _lerp(150, 200, random.random()), 30, 0,
                             _lerp(150, 255, random.random()), max(8, int(DIM_AMBIENT + flick)))
        dmx.set_12s(180, 40, 0, 200, DIM_AMBIENT)
        _smooth_frame(dmx)
        t += 0.025

def ambient_warm_blanket(dmx, bpm, govee_cmd=None, get_energy=None):
    """Static warm amber/orange. Cozy. Barely moves."""
    if govee_cmd:
        govee_cmd(["warm"])
    dmx.set_12s(200, 100, 20, 255, DIM_AMBIENT)
    for z in range(1, 5):
        dmx.set_bar_zone(z, 220, 80, 10, 230, DIM_AMBIENT)
    while True:
        _smooth_frame(dmx)

def ambient_moonlight(dmx, bpm, govee_cmd=None, get_energy=None):
    """Cool blue-white with very slow brightness breathing."""
    if govee_cmd:
        govee_cmd(["scene", str(random.choice(GOVEE_AMBIENT))])
    t = 0
    while True:
        breath = math.sin(t * 0.2) * 0.3 + 0.7
        dim = int(DIM_AMBIENT * breath)
        dmx.set_all(100, 120, 200, 0, dim)
        _smooth_frame(dmx)
        t += 0.025


# ============================================================
#  BREAKDOWN — Kick gone, minimal, atmospheric
# ============================================================

def breakdown_breathing_dark(dmx, bpm, govee_cmd=None, get_energy=None):
    """Everything very dim, slow breath."""
    if govee_cmd:
        govee_cmd(["scene", str(random.choice(GOVEE_BREAKDOWN))])
    t = 0
    while True:
        breath = math.sin(t * 0.15) * 0.5 + 0.5
        dim = _lerp(2, DIM_BREAKDOWN, breath)
        dmx.set_all(20, 0, 50, 0, dim)
        _smooth_frame(dmx)
        t += 0.025

def breakdown_wash_only(dmx, bpm, govee_cmd=None, get_energy=None):
    """Just the wash lights, deep color, bar off."""
    if govee_cmd:
        govee_cmd(["scene", str(random.choice(GOVEE_BREAKDOWN))])
    t = 0
    while True:
        breath = math.sin(t * 0.2) * 0.3 + 0.7
        dmx.set_12s(0, 15, 80, 0, int(DIM_BREAKDOWN * breath))
        for z in range(1, 5):
            dmx.set_bar_zone(z, 0, 0, 0, 0, 0)
        _smooth_frame(dmx)
        t += 0.025

def breakdown_tide(dmx, bpm, govee_cmd=None, get_energy=None):
    """Single color washes in and out across bar like a tide. Very slow."""
    if govee_cmd:
        govee_cmd(["scene", str(random.choice(GOVEE_BREAKDOWN))])
    t = 0
    while True:
        position = math.sin(t * 0.12) * 2 + 2
        for z in range(4):
            dist = abs(position - z)
            dim = max(0, int(DIM_BREAKDOWN * max(0, 1.0 - dist * 0.4)))
            dmx.set_bar_zone(z + 1, 60, 0, 120, 0, dim)
        dmx.set_12s(20, 0, 40, 0, max(2, int(8 * (math.sin(t * 0.1) * 0.5 + 0.5))))
        _smooth_frame(dmx)
        t += 0.025

def breakdown_deep_crossfade(dmx, bpm, govee_cmd=None, get_energy=None):
    """Bar zones crossfade between deep blue and deep purple. Glacial pace."""
    if govee_cmd:
        govee_cmd(["scene", str(random.choice(GOVEE_BREAKDOWN))])
    t = 0
    while True:
        for z in range(4):
            phase = math.sin(t * 0.08 + z * 1.5) * 0.5 + 0.5
            r = _lerp(10, 60, phase)
            b = _lerp(80, 30, phase)
            dmx.set_bar_zone(z + 1, r, 0, b, 0, 12)
        phase_w = math.sin(t * 0.06) * 0.5 + 0.5
        dmx.set_12s(_lerp(5, 30, phase_w), 0, _lerp(40, 60, phase_w), 0, 10)
        _smooth_frame(dmx)
        t += 0.025

def breakdown_zone_drift(dmx, bpm, govee_cmd=None, get_energy=None):
    """One bar zone barely visible, slowly drifts to next."""
    if govee_cmd:
        govee_cmd(["scene", str(random.choice(GOVEE_BREAKDOWN))])
    t = 0
    while True:
        position = (t * 0.1) % 4
        for z in range(4):
            dist = abs(position - z)
            dim = max(0, int(DIM_BREAKDOWN * max(0, 1.0 - dist * 0.8)))
            dmx.set_bar_zone(z + 1, 40, 0, 80, 0, dim)
        dmx.set_12s(0, 0, 0, 0, 0)
        _smooth_frame(dmx)
        t += 0.025


# ============================================================
#  BUILDUP — Energy rising, tension toward drop
# ============================================================

def buildup_accelerating_chase(dmx, bpm, govee_cmd=None, get_energy=None):
    """Chase pattern driven by audio energy. Faster and brighter as energy rises."""
    if govee_cmd:
        govee_cmd(["scene", str(random.choice(GOVEE_BUILDUP))])
    beat = _beat(bpm)
    t = 0
    while True:
        curve = _energy_progress(get_energy)

        # Chase speed: from 1/bar to 1/quarter-beat based on energy
        period = max(0.08, beat * _lerp(4, 0.25, curve))
        pos = (t % period) / period * 5

        dim = _lerp(5, DIM_BUILDUP, curve)
        r = _lerp(0, 255, curve)
        g = _lerp(0, 255, curve)
        b = 255
        a = _lerp(0, 128, curve)

        for z in range(5):
            dist = abs(pos - z)
            zone_dim = max(0, int(dim * max(0, 1.0 - dist * (1.5 - curve))))
            if z == 0:
                dmx.set_12s(r, g, b, a, zone_dim)
            else:
                dmx.set_bar_zone(z, r, g, b, a, zone_dim)
        _smooth_frame(dmx)
        t += 0.025

def buildup_rising_pulse(dmx, bpm, govee_cmd=None, get_energy=None):
    """Pulse driven by audio energy. Faster and brighter as volume builds."""
    if govee_cmd:
        govee_cmd(["scene", str(random.choice(GOVEE_BUILDUP))])
    beat = _beat(bpm)
    t = 0
    while True:
        curve = _energy_progress(get_energy)

        # Pulse rate: from half-beat to quarter-beat
        period = max(0.08, beat * _lerp(2, 0.25, curve))
        pos = (t % period) / period

        # Sharp pulse
        if pos < 0.15:
            intensity = 1.0
        elif pos < 0.4:
            intensity = 1.0 - (pos - 0.15) / 0.25
        else:
            intensity = 0

        dim = int(_lerp(5, DIM_BUILDUP, curve) * intensity)
        r = _lerp(200, 255, curve)
        g = _lerp(60, 255, curve)
        b = _lerp(0, 255, curve)

        dmx.set_all(r, g, b, _lerp(100, 200, curve), dim)
        _smooth_frame(dmx)
        t += 0.025

def buildup_snare_roll(dmx, bpm, govee_cmd=None, get_energy=None):
    """Rapid alternating zones — speed and brightness follow audio energy."""
    if govee_cmd:
        govee_cmd(["scene", str(random.choice(GOVEE_BUILDUP))])
    beat = _beat(bpm)
    t = 0
    toggle = False
    while True:
        curve = _energy_progress(get_energy)

        # Alternate rate: from beat to 16th note
        period = max(0.05, beat * _lerp(1, 0.125, curve))

        if (t % period) / period < 0.03:
            toggle = not toggle

        dim = _lerp(5, DIM_BUILDUP, curve)
        c = (255, 255, 255, _lerp(0, 200, curve))

        if toggle:
            dmx.set_12s(*c, dim)
            for z in range(1, 5):
                dmx.set_bar_zone(z, 0, 0, 0, 0, 0)
        else:
            dmx.set_12s(0, 0, 0, 0, 0)
            for z in range(1, 5):
                dmx.set_bar_zone(z, *c, dim)

        _smooth_frame(dmx)
        t += 0.025

def buildup_color_ramp(dmx, bpm, govee_cmd=None, get_energy=None):
    """Color shifts from cool to hot based on audio energy. Breathing gets faster."""
    if govee_cmd:
        govee_cmd(["scene", str(random.choice(GOVEE_BUILDUP))])
    beat = _beat(bpm)
    t = 0
    while True:
        progress = _energy_progress(get_energy)

        # Hue: blue (240) → red (0) as energy builds
        hue = _lerp(240, 0, progress)
        r, g, b = _hsv_to_rgb(hue, 1.0, 1.0)
        dim = _lerp(5, DIM_BUILDUP, progress)

        # Breathing gets faster with energy
        breath_period = max(0.3, beat * _lerp(4, 0.5, progress))
        breath = math.sin(t * math.pi * 2 / breath_period) * 0.3 + 0.7
        final_dim = int(dim * breath)

        dmx.set_all(r, g, b, 0, final_dim)
        _smooth_frame(dmx)
        t += 0.025


# ============================================================
#  GROOVE — Kick + bass running, main body of the track
#
#  Design rules:
#  - SUSTAINED color washes, NOT beat-synced strobing
#  - Hold each look for 8-32 bars before transitioning
#  - Transitions are slow crossfades, not hard cuts
#  - Subtle movement (breathing, drifting) is OK, rapid flashing is NOT
#  - Think club wash lights, not concert strobes
# ============================================================

def groove_sustained_wash(dmx, bpm, govee_cmd=None, get_energy=None):
    """Sustained color wash — one color across all fixtures, holds for 16 bars,
    then crossfades to next color over 4 bars. Club-style sustained looks.
    Govee: matching color."""
    beat = _beat(bpm)
    bar = beat * 4
    hold_bars = 16
    fade_bars = 4
    cycle = bar * (hold_bars + fade_bars)

    palettes = [
        (0, 80, 255, 0),    # deep blue
        (255, 0, 100, 0),   # magenta
        (0, 200, 180, 0),   # teal
        (180, 0, 255, 0),   # violet
        (255, 100, 0, 80),  # amber
        (0, 255, 120, 0),   # emerald
    ]
    random.shuffle(palettes)
    ci = 0
    if govee_cmd:
        r, g, b, _ = palettes[ci]
        govee_cmd(["color", f"#{r:02x}{g:02x}{b:02x}"])
    t = 0
    while True:
        pos = t % cycle
        hold_end = bar * hold_bars
        if pos < hold_end:
            c = palettes[ci % len(palettes)]
            dmx.set_all(*c, DIM_GROOVE)
        else:
            fade_pos = (pos - hold_end) / (bar * fade_bars)
            c1 = palettes[ci % len(palettes)]
            c2 = palettes[(ci + 1) % len(palettes)]
            c = _lerp_color(c1, c2, fade_pos)
            dmx.set_all(*c, DIM_GROOVE)
        _smooth_frame(dmx)
        t += 0.025
        if t % cycle < 0.03 and t > 1.0:
            ci += 1
            if govee_cmd:
                r, g, b, _ = palettes[ci % len(palettes)]
                govee_cmd(["color", f"#{r:02x}{g:02x}{b:02x}"])

def groove_split_hold(dmx, bpm, govee_cmd=None, get_energy=None):
    """12s and bar hold different complementary colors. Swaps every 8 bars.
    No flashing — just a slow swap with a brief dim-down transition.
    Govee: warm ambient."""
    if govee_cmd:
        govee_cmd(["warm"])
    beat = _beat(bpm)
    bar = beat * 4
    swap_bars = 8
    cycle = bar * swap_bars
    pairs = [
        ((0, 100, 255, 0), (255, 60, 0, 80)),    # blue / amber
        ((255, 0, 100, 0), (0, 200, 180, 0)),     # magenta / teal
        ((180, 0, 255, 0), (0, 255, 120, 0)),     # violet / emerald
        ((255, 100, 0, 0), (0, 80, 255, 0)),      # orange / blue
    ]
    pi = 0
    t = 0
    while True:
        pos = t % cycle
        transition_start = cycle - bar  # last bar is transition
        if pos < transition_start:
            c1, c2 = pairs[pi % len(pairs)]
            dmx.set_12s(*c1, DIM_GROOVE)
            for z in range(1, 5):
                dmx.set_bar_zone(z, *c2, int(DIM_GROOVE * 0.8))
        elif pos < transition_start + bar * 0.5:
            fade = 1.0 - (pos - transition_start) / (bar * 0.5)
            c1, c2 = pairs[pi % len(pairs)]
            dim = int(DIM_GROOVE * fade)
            dmx.set_12s(*c1, dim)
            for z in range(1, 5):
                dmx.set_bar_zone(z, *c2, int(dim * 0.8))
        else:
            fade = (pos - transition_start - bar * 0.5) / (bar * 0.5)
            c2_new, c1_new = pairs[(pi + 1) % len(pairs)]
            dim = int(DIM_GROOVE * fade)
            dmx.set_12s(*c1_new, dim)
            for z in range(1, 5):
                dmx.set_bar_zone(z, *c2_new, int(dim * 0.8))
        _smooth_frame(dmx)
        t += 0.025
        if t % cycle < 0.03 and t > 1.0:
            pi += 1

def groove_slow_chase(dmx, bpm, govee_cmd=None, get_energy=None):
    """Color slowly migrates across bar zones over 8 bars. One zone bright,
    the transition between zones takes a full bar (smooth handoff).
    Govee: bulbs-only scene."""
    if govee_cmd:
        govee_cmd(["scene", str(random.choice(range(10, 15)))])
    beat = _beat(bpm)
    bar = beat * 4
    cycle = bar * 8
    colors = [(0, 120, 255, 0), (255, 0, 120, 0), (120, 0, 255, 0), (0, 255, 160, 0)]
    ci = 0
    t = 0
    while True:
        pos = (t % cycle) / cycle
        zone_f = pos * 4
        active_zone = int(zone_f)
        blend = zone_f - active_zone

        c = colors[ci % len(colors)]
        dmx.set_12s(*c, int(DIM_GROOVE * 0.3))

        for z in range(4):
            if z == active_zone:
                dim = int(DIM_GROOVE * (1.0 - blend * 0.7))
                dmx.set_bar_zone(z + 1, *c, dim)
            elif z == (active_zone + 1) % 4:
                dim = int(DIM_GROOVE * blend * 0.7)
                dmx.set_bar_zone(z + 1, *c, dim)
            else:
                dmx.set_bar_zone(z + 1, 0, 0, 0, 0, 0)

        _smooth_frame(dmx)
        t += 0.025
        if t % cycle < 0.03 and t > 1.0:
            ci += 1

def groove_warm_pulse(dmx, bpm, govee_cmd=None, get_energy=None):
    """Gentle sine-wave brightness pulse synced to every 2 bars. All fixtures same color.
    Not a strobe — smooth breathing. Govee: warm."""
    if govee_cmd:
        govee_cmd(["warm"])
    beat = _beat(bpm)
    pulse_period = beat * 8
    color = (255, 80, 0, 100)  # warm amber
    t = 0
    while True:
        phase = (t % pulse_period) / pulse_period
        brightness = 0.4 + 0.6 * (0.5 + 0.5 * math.sin(phase * 2 * math.pi - math.pi / 2))
        dim = int(DIM_GROOVE * brightness)
        dmx.set_all(*color, dim)
        _smooth_frame(dmx)
        t += 0.025

def groove_two_tone_hold(dmx, bpm, govee_cmd=None, get_energy=None):
    """Two colors split between 12s and bar. Crossfade swap every 16 bars.
    Very slow, very sustained. Govee: matches bar color."""
    beat = _beat(bpm)
    bar = beat * 4
    hold_bars = 16
    fade_bars = 2
    cycle = bar * (hold_bars + fade_bars)

    pairs = [
        ((0, 100, 255, 0), (255, 0, 80, 0)),     # blue / pink
        ((255, 0, 80, 0), (0, 200, 255, 0)),      # pink / cyan
        ((0, 200, 255, 0), (180, 0, 255, 0)),     # cyan / purple
        ((180, 0, 255, 0), (0, 100, 255, 0)),     # purple / blue
    ]
    pi = 0
    if govee_cmd:
        r, g, b, _ = pairs[0][1]
        govee_cmd(["color", f"#{r:02x}{g:02x}{b:02x}"])
    t = 0
    while True:
        pos = t % cycle
        hold_end = bar * hold_bars
        c1_a, c2_a = pairs[pi % len(pairs)]
        c1_b, c2_b = pairs[(pi + 1) % len(pairs)]
        if pos < hold_end:
            dmx.set_12s(*c1_a, DIM_GROOVE)
            for z in range(1, 5):
                dmx.set_bar_zone(z, *c2_a, int(DIM_GROOVE * 0.8))
        else:
            fade = (pos - hold_end) / (bar * fade_bars)
            c1 = _lerp_color(c1_a, c1_b, fade)
            c2 = _lerp_color(c2_a, c2_b, fade)
            dmx.set_12s(*c1, DIM_GROOVE)
            for z in range(1, 5):
                dmx.set_bar_zone(z, *c2, int(DIM_GROOVE * 0.8))
        _smooth_frame(dmx)
        t += 0.025
        if t % cycle < 0.03 and t > 1.0:
            pi += 1
            if govee_cmd:
                r, g, b, _ = pairs[pi % len(pairs)][1]
                govee_cmd(["color", f"#{r:02x}{g:02x}{b:02x}"])

def groove_zone_drift(dmx, bpm, govee_cmd=None, get_energy=None):
    """Each zone slowly cycles through hues independently but at different offsets.
    Creates a slow rainbow drift across the bar. Govee: music-reactive strips."""
    if govee_cmd:
        govee_cmd(["scene", str(random.choice(range(15, 20)))])
    t = 0
    while True:
        for z in range(5):
            hue = ((t * 6) + z * 72) % 360  # 72 degrees apart, ~60s full rotation
            r, g, b = _hsv_to_rgb(hue, 0.9, 1.0)
            if z == 0:
                dmx.set_12s(r, g, b, 0, DIM_GROOVE)
            else:
                dmx.set_bar_zone(z, r, g, b, 0, int(DIM_GROOVE * 0.8))
        _smooth_frame(dmx)
        t += 0.025

def groove_deep_blue(dmx, bpm, govee_cmd=None, get_energy=None):
    """Deep blue wash with very subtle brightness undulation synced to 4-bar phrases.
    The quintessential house music look. Govee: cool blue."""
    if govee_cmd:
        govee_cmd(["color", "#0040FF"])
    beat = _beat(bpm)
    phrase = beat * 16  # 4 bars
    t = 0
    while True:
        phase = (t % phrase) / phrase
        brightness = 0.75 + 0.25 * (0.5 + 0.5 * math.sin(phase * 2 * math.pi))
        dim = int(DIM_GROOVE * brightness)
        dmx.set_all(0, 40, 255, 0, dim)
        _smooth_frame(dmx)
        t += 0.025

def groove_magenta_teal(dmx, bpm, govee_cmd=None, get_energy=None):
    """Magenta on 12s, teal on bar. Static hold — no movement, no flash.
    Clean two-color split. Govee: matching teal."""
    if govee_cmd:
        govee_cmd(["color", "#00C8B4"])
    t = 0
    while True:
        dmx.set_12s(255, 0, 120, 0, DIM_GROOVE)
        for z in range(1, 5):
            dmx.set_bar_zone(z, 0, 200, 180, 0, int(DIM_GROOVE * 0.8))
        _smooth_frame(dmx)
        t += 0.025

def groove_kick_accent(dmx, bpm, govee_cmd=None, get_energy=None):
    """Sustained color wash with a subtle brightness bump on each beat.
    Not a strobe — just a 10% brightness nudge that decays over the beat.
    The wash stays constant, the kick adds a gentle pulse. Govee: both-device scene."""
    if govee_cmd:
        govee_cmd(["scene", str(random.choice(range(0, 10)))])
    beat = _beat(bpm)
    colors = [(0, 80, 255, 0), (255, 0, 100, 0), (120, 0, 255, 0), (0, 200, 160, 0)]
    ci = random.randint(0, len(colors) - 1)
    color_hold = beat * 32  # swap color every 32 beats (8 bars)
    t = 0
    while True:
        pos = (t % beat) / beat
        accent = max(0, 1.0 - pos * 3) * 0.2  # 20% bump, decays over first third of beat
        brightness = 0.8 + accent
        dim = int(DIM_GROOVE * brightness)
        c = colors[ci % len(colors)]
        dmx.set_all(*c, dim)
        _smooth_frame(dmx)
        t += 0.025
        if t % color_hold < 0.03 and t > 1.0:
            ci += 1

def groove_violet_emerald(dmx, bpm, govee_cmd=None, get_energy=None):
    """Violet wash slowly breathes, emerald bar zones hold steady.
    Opposite-spectrum contrast. Govee: purple."""
    if govee_cmd:
        govee_cmd(["color", "#8000FF"])
    beat = _beat(bpm)
    breath_period = beat * 16  # very slow 4-bar breath
    t = 0
    while True:
        phase = (t % breath_period) / breath_period
        wash_brightness = 0.6 + 0.4 * (0.5 + 0.5 * math.sin(phase * 2 * math.pi))
        dmx.set_12s(140, 0, 255, 0, int(DIM_GROOVE * wash_brightness))
        for z in range(1, 5):
            dmx.set_bar_zone(z, 0, 200, 100, 0, int(DIM_GROOVE * 0.75))
        _smooth_frame(dmx)
        t += 0.025


# ============================================================
#  DROP — Maximum impact (8-bar sequences fired by daemon)
# ============================================================

def drop_hw_strobe_max(dmx, bpm, govee_cmd=None, get_energy=None):
    """Hardware strobe at max speed on everything."""
    if govee_cmd:
        govee_cmd(["scene", str(random.choice(GOVEE_DROP))])
    dmx.set_all(255, 255, 255, 255, HW_STROBE_FAST)
    while True:
        _smooth_frame(dmx)

def drop_color_blast_strobe(dmx, bpm, govee_cmd=None, get_energy=None):
    """Alternate red/blue with hardware strobe."""
    if govee_cmd:
        govee_cmd(["scene", str(random.choice(GOVEE_DROP))])
    beat = _beat(bpm)
    t = 0
    while True:
        beat_num = int(t / beat)
        if beat_num % 2 == 0:
            dmx.set_all(255, 0, 0, 0, HW_STROBE_MED)
        else:
            dmx.set_all(0, 0, 255, 0, HW_STROBE_MED)
        dmx.send_for(beat)
        t += beat

def drop_machine_gun(dmx, bpm, govee_cmd=None, get_energy=None):
    """Rapid wipe at quarter-beat speed, with fade trail."""
    if govee_cmd:
        govee_cmd(["scene", str(random.choice(GOVEE_DROP))])
    quarter = _beat(bpm) / 4
    t = 0
    while True:
        pos = (t % quarter) / quarter
        position = pos * 5
        for z in range(5):
            dist = abs(position - z)
            dim = max(0, int(DIM_DROP * max(0, 1.0 - dist * 1.0)))
            if z == 0:
                dmx.set_12s(255, 255, 255, 128, dim)
            else:
                dmx.set_bar_zone(z, 255, 255, 255, 128, dim)
        _smooth_frame(dmx)
        t += 0.025

def drop_explosion_fade(dmx, bpm, govee_cmd=None, get_energy=None):
    """Full white blast that fades into fast color chase."""
    if govee_cmd:
        govee_cmd(["party"])
    beat = _beat(bpm)
    start = time.time()
    while time.time() - start < beat * 2:
        elapsed = time.time() - start
        decay = max(0, 1.0 - elapsed / (beat * 2))
        dim = int(DIM_DROP * decay)
        dmx.set_all(255, 255, 255, 255, dim)
        _smooth_frame(dmx)
    t = 0
    colors = [(255,0,80,0),(80,0,255,0),(0,200,255,0),(255,200,0,0)]
    while True:
        half = _beat(bpm) / 2
        pos = (t % half) / half
        position = pos * 5
        ci = int(t / half) % len(colors)
        c = colors[ci]
        for z in range(5):
            dist = abs(position - z)
            dim = max(0, int(DIM_DROP * max(0, 1.0 - dist * 1.0)))
            if z == 0:
                dmx.set_12s(*c, dim)
            else:
                dmx.set_bar_zone(z, *c, dim)
        _smooth_frame(dmx)
        t += 0.025


# ============================================================
#  SCENE REGISTRY
# ============================================================

SCENES = [
    # AMBIENT (4 scenes)
    {'name': 'Ocean Drift',       'category': 'ambient',    'fn': ambient_ocean_drift},
    {'name': 'Ember Flicker',     'category': 'ambient',    'fn': ambient_ember_flicker},
    {'name': 'Warm Blanket',      'category': 'ambient',    'fn': ambient_warm_blanket},
    {'name': 'Moonlight',         'category': 'ambient',    'fn': ambient_moonlight},

    # BREAKDOWN (5 scenes)
    {'name': 'Breathing Dark',    'category': 'breakdown',  'fn': breakdown_breathing_dark},
    {'name': 'Wash Only',         'category': 'breakdown',  'fn': breakdown_wash_only},
    {'name': 'Tide',              'category': 'breakdown',  'fn': breakdown_tide},
    {'name': 'Deep Crossfade',    'category': 'breakdown',  'fn': breakdown_deep_crossfade},
    {'name': 'Zone Drift',        'category': 'breakdown',  'fn': breakdown_zone_drift},

    # BUILDUP (4 scenes)
    {'name': 'Accelerating Chase','category': 'buildup',    'fn': buildup_accelerating_chase},
    {'name': 'Rising Pulse',      'category': 'buildup',    'fn': buildup_rising_pulse},
    {'name': 'Snare Roll',        'category': 'buildup',    'fn': buildup_snare_roll},
    {'name': 'Color Ramp',        'category': 'buildup',    'fn': buildup_color_ramp},

    # GROOVE (10 scenes — sustained washes, slow transitions, no strobing)
    {'name': 'Sustained Wash',    'category': 'groove',     'fn': groove_sustained_wash},
    {'name': 'Split Hold',        'category': 'groove',     'fn': groove_split_hold},
    {'name': 'Slow Chase',        'category': 'groove',     'fn': groove_slow_chase},
    {'name': 'Warm Pulse',        'category': 'groove',     'fn': groove_warm_pulse},
    {'name': 'Two Tone Hold',     'category': 'groove',     'fn': groove_two_tone_hold},
    {'name': 'Zone Drift',        'category': 'groove',     'fn': groove_zone_drift},
    {'name': 'Deep Blue',         'category': 'groove',     'fn': groove_deep_blue},
    {'name': 'Magenta Teal',      'category': 'groove',     'fn': groove_magenta_teal},
    {'name': 'Kick Accent',       'category': 'groove',     'fn': groove_kick_accent},
    {'name': 'Violet Emerald',    'category': 'groove',     'fn': groove_violet_emerald},

    # DROP (4 scenes)
    {'name': 'HW Strobe Max',     'category': 'drop',       'fn': drop_hw_strobe_max},
    {'name': 'Color Blast Strobe','category': 'drop',       'fn': drop_color_blast_strobe},
    {'name': 'Machine Gun',       'category': 'drop',       'fn': drop_machine_gun},
    {'name': 'Explosion Fade',    'category': 'drop',       'fn': drop_explosion_fade},
]


def get_scenes_by_category(category):
    return [s for s in SCENES if s['category'] == category]


def get_scene_for_energy(energy_level):
    """Map energy level to category and pick a random scene."""
    if energy_level < 15:
        cat = 'breakdown'
    elif energy_level < 30:
        cat = 'ambient'
    else:
        cat = 'groove'
    # Note: buildup and drop are triggered by structure detection, not energy
    pool = get_scenes_by_category(cat)
    return random.choice(pool) if pool else random.choice(SCENES)


def list_scenes():
    categories = ['ambient', 'breakdown', 'buildup', 'groove', 'drop']
    for cat in categories:
        scenes = get_scenes_by_category(cat)
        if not scenes:
            continue
        print(f"\n{'='*45}")
        print(f"  {cat.upper()} ({len(scenes)} scenes)")
        print(f"{'='*45}")
        for s in scenes:
            print(f"  {s['name']}")
    print(f"\n  TOTAL: {len(SCENES)} scenes")


if __name__ == '__main__':
    list_scenes()
