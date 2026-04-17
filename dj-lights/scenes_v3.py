#!/usr/bin/env python3
"""
DMX Scene Library v3 — Actually dramatic.

Design principles:
  - CONTRAST over subtlety. Dark ↔ bright, not dim ↔ slightly-less-dim.
  - MOVEMENT you can see. If it's not visible, it doesn't exist.
  - BLACKOUT is a color. Use it. The gap between flashes IS the effect.
  - KICK-SYNC over smooth fades for anything above groove.
  - Less is more. 4 great scenes per category > 8 invisible ones.
"""

import time
import math
import random

# Dimmer ranges — these are the PEAK values, scenes go from 0 to these
DIM_LOW = 40       # ambient peak
DIM_MED = 80       # groove/drive peak
DIM_HIGH = 127     # peak/drop (max before strobe)
HW_STROBE_SLOW = 140
HW_STROBE_MED = 180
HW_STROBE_FAST = 210
HW_STROBE_MAX = 227


def _beat(bpm):
    return 60.0 / bpm

def _lerp(a, b, t):
    return int(a + (b - a) * t)

def _hsv_to_rgb(h, s, v):
    h = h % 360
    c = v * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = v - c
    if h < 60: r, g, b = c, x, 0
    elif h < 120: r, g, b = x, c, 0
    elif h < 180: r, g, b = 0, c, x
    elif h < 240: r, g, b = 0, x, c
    elif h < 300: r, g, b = x, 0, c
    else: r, g, b = c, 0, x
    return int((r+m)*255), int((g+m)*255), int((b+m)*255)

def _frame(dmx):
    dmx.send_frame()
    time.sleep(0.025)


# ============================================================
#  AMBIENT — Slow but VISIBLE movement
# ============================================================

def ambient_color_wash(dmx, bpm):
    """Slow hue rotation across all fixtures. Full saturation, low brightness."""
    t = 0
    while True:
        hue = (t * 8) % 360  # full rotation every 45s
        r, g, b = _hsv_to_rgb(hue, 1.0, 1.0)
        # Bar zones offset by 90 degrees each
        dmx.set_12s(r, g, b, 0, DIM_LOW)
        for z in range(4):
            zh = (hue + z * 90) % 360
            zr, zg, zb = _hsv_to_rgb(zh, 1.0, 1.0)
            dmx.set_bar_zone(z+1, zr, zg, zb, 0, DIM_LOW)
        _frame(dmx)
        t += 0.025

def ambient_campfire(dmx, bpm):
    """Warm flicker with random intensity. Feels alive."""
    t = 0
    while True:
        for z in range(4):
            flick = random.uniform(0.3, 1.0)
            dim = int(DIM_LOW * flick)
            dmx.set_bar_zone(z+1, 255, _lerp(40, 120, flick), 0, 
                            int(200 * flick), dim)
        flick_w = random.uniform(0.4, 1.0)
        dmx.set_12s(200, 60, 0, int(180 * flick_w), int(DIM_LOW * flick_w))
        _frame(dmx)
        t += 0.025

def ambient_breathing(dmx, bpm):
    """Deep breath — lights go from near-black to dim and back. VISIBLE."""
    t = 0
    while True:
        # 6 second breath cycle
        breath = (math.sin(t * math.pi / 3) + 1) / 2  # 0-1
        dim = _lerp(2, DIM_LOW, breath)
        dmx.set_all(80, 0, 180, 0, dim)
        _frame(dmx)
        t += 0.025


# ============================================================
#  GROOVE — Beat-synced, visible movement
# ============================================================

def groove_pulse(dmx, bpm):
    """ON-OFF pulse on the beat. Not subtle."""
    beat = _beat(bpm)
    t = 0
    hue = random.randint(0, 360)
    while True:
        pos = (t % beat) / beat
        # Sharp attack, medium decay
        if pos < 0.1:
            intensity = pos / 0.1
        elif pos < 0.5:
            intensity = 1.0 - ((pos - 0.1) / 0.4)
        else:
            intensity = 0.0
        
        r, g, b = _hsv_to_rgb(hue, 1.0, 1.0)
        dim = int(DIM_MED * intensity)
        dmx.set_all(r, g, b, 0, dim)
        _frame(dmx)
        t += 0.025
        # Shift hue every 8 beats
        if int(t / beat) % 8 == 0 and pos < 0.03:
            hue = (hue + 60) % 360

def groove_bar_chase(dmx, bpm):
    """Single bright zone chases across bar on beat. Everything else dark."""
    beat = _beat(bpm)
    t = 0
    colors = [(255, 0, 100), (0, 100, 255), (100, 255, 0), (255, 100, 0)]
    ci = 0
    while True:
        beat_num = int(t / beat) % 4
        r, g, b = colors[ci % len(colors)]
        
        dmx.set_12s(0, 0, 0, 0, 0)
        for z in range(4):
            if z == beat_num:
                dmx.set_bar_zone(z+1, r, g, b, 0, DIM_MED)
            else:
                dmx.set_bar_zone(z+1, 0, 0, 0, 0, 0)
        _frame(dmx)
        t += 0.025
        if int(t / beat) % 4 == 0 and (t % beat) / beat < 0.03:
            ci += 1

def groove_ping_pong(dmx, bpm):
    """Light bounces between 12s and bar on each beat."""
    beat = _beat(bpm)
    t = 0
    while True:
        beat_num = int(t / beat) % 2
        pos = (t % beat) / beat
        # Fade trail
        intensity = max(0, 1.0 - pos * 2.5)
        dim = int(DIM_MED * intensity)
        
        if beat_num == 0:
            dmx.set_12s(0, 150, 255, 0, dim)
            dmx.set_bar(0, 0, 0, 0, 0)
        else:
            dmx.set_12s(0, 0, 0, 0, 0)
            for z in range(1, 5):
                dmx.set_bar_zone(z, 255, 0, 150, 0, dim)
        _frame(dmx)
        t += 0.025

def groove_two_color_swap(dmx, bpm):
    """12s one color, bar another. Swap every 4 beats with blackout gap."""
    beat = _beat(bpm)
    t = 0
    c1 = (0, 100, 255, 0)
    c2 = (255, 0, 100, 0)
    while True:
        phrase = int(t / (beat * 4))
        pos_in_phrase = (t % (beat * 4)) / (beat * 4)
        
        # Brief blackout on swap (first 5% of phrase)
        if pos_in_phrase < 0.05:
            dmx.set_all(0, 0, 0, 0, 0)
        elif phrase % 2 == 0:
            dmx.set_12s(*c1, DIM_MED)
            for z in range(1, 5):
                dmx.set_bar_zone(z, *c2, DIM_MED)
        else:
            dmx.set_12s(*c2, DIM_MED)
            for z in range(1, 5):
                dmx.set_bar_zone(z, *c1, DIM_MED)
        _frame(dmx)
        t += 0.025


# ============================================================
#  DRIVE — Fast, aggressive movement
# ============================================================

def drive_strobe_chase(dmx, bpm):
    """Fast brightness wipe with blackout trail. Not subtle."""
    beat = _beat(bpm)
    t = 0
    while True:
        pos = (t % beat) / beat
        position = pos * 5  # moves across 5 zones per beat
        
        for z in range(5):
            dist = abs(position - z)
            if dist < 0.8:
                dim = int(DIM_HIGH * (1.0 - dist / 0.8))
            else:
                dim = 0  # BLACKOUT, not dim
            if z == 0:
                dmx.set_12s(255, 255, 255, 0, dim)
            else:
                dmx.set_bar_zone(z, 255, 255, 255, 0, dim)
        _frame(dmx)
        t += 0.025

def drive_kick_punch(dmx, bpm):
    """ALL lights slam on each beat, blackout between. Simple. Effective."""
    beat = _beat(bpm)
    t = 0
    colors = [(255,0,0,0), (0,0,255,0), (255,0,200,0), (0,200,255,0)]
    ci = 0
    while True:
        pos = (t % beat) / beat
        # ON for 15% of beat, OFF for rest
        if pos < 0.15:
            c = colors[ci % len(colors)]
            dmx.set_all(*c, DIM_HIGH)
        else:
            dmx.set_all(0, 0, 0, 0, 0)
        _frame(dmx)
        t += 0.025
        if pos < 0.03 and t > 0.1:
            ci += 1

def drive_color_split_chase(dmx, bpm):
    """Two colors chase each other across the bar. 12s holds complement."""
    beat = _beat(bpm)
    t = 0
    while True:
        pos = (t % (beat * 2)) / (beat * 2)
        # Color A chases forward, B chases backward
        pos_a = (pos * 4) % 4
        pos_b = (3 - pos * 4) % 4
        
        dmx.set_12s(100, 0, 255, 0, DIM_MED)
        for z in range(4):
            dist_a = abs(z - pos_a)
            dist_b = abs(z - pos_b)
            if dist_a < dist_b and dist_a < 1.2:
                dim = int(DIM_HIGH * max(0, 1 - dist_a / 1.2))
                dmx.set_bar_zone(z+1, 255, 0, 80, 0, dim)
            elif dist_b < 1.2:
                dim = int(DIM_HIGH * max(0, 1 - dist_b / 1.2))
                dmx.set_bar_zone(z+1, 0, 80, 255, 0, dim)
            else:
                dmx.set_bar_zone(z+1, 0, 0, 0, 0, 0)
        _frame(dmx)
        t += 0.025

def drive_heartbeat(dmx, bpm):
    """Double-pump heartbeat. Thump-thump, pause. Dramatic."""
    beat = _beat(bpm)
    t = 0
    while True:
        pos = (t % beat) / beat
        # First hit at 0%, second at 25%
        p1 = max(0, 1.0 - abs(pos) * 12) if pos < 0.15 else 0
        p2 = max(0, 1.0 - abs(pos - 0.25) * 12) if 0.15 < pos < 0.4 else 0
        intensity = max(p1, p2)
        
        dim = int(DIM_HIGH * intensity)
        dmx.set_all(255, 0, 30, int(80 * intensity), dim)
        _frame(dmx)
        t += 0.025


# ============================================================
#  PEAK — High energy, almost overwhelming
# ============================================================

def peak_rapid_color_flash(dmx, bpm):
    """New color on every beat, full brightness, blackout between."""
    beat = _beat(bpm)
    t = 0
    colors = [(255,0,0), (0,0,255), (255,0,200), (0,255,200), 
              (255,255,0), (128,0,255), (0,255,100)]
    ci = 0
    while True:
        pos = (t % beat) / beat
        if pos < 0.2:
            r, g, b = colors[ci % len(colors)]
            dmx.set_all(r, g, b, 0, DIM_HIGH)
        else:
            dmx.set_all(0, 0, 0, 0, 0)
        _frame(dmx)
        t += 0.025
        if pos < 0.03 and t > 0.1:
            ci += 1

def peak_alternating_slam(dmx, bpm):
    """12s and bar alternate every beat. Hard cuts, no fades."""
    beat = _beat(bpm)
    t = 0
    while True:
        beat_num = int(t / beat) % 2
        pos = (t % beat) / beat
        
        if pos > 0.7:
            # Blackout gap before switch
            dmx.set_all(0, 0, 0, 0, 0)
        elif beat_num == 0:
            dmx.set_12s(255, 0, 128, 0, DIM_HIGH)
            dmx.set_bar(0, 0, 0, 0, 0)
        else:
            dmx.set_12s(0, 0, 0, 0, 0)
            dmx.set_bar(0, 128, 255, 0, DIM_HIGH)
        _frame(dmx)
        t += 0.025

def peak_scatter_blast(dmx, bpm):
    """Random zones blast on/off rapidly. Chaotic energy."""
    beat = _beat(bpm) / 2  # half-beat timing
    t = 0
    colors = [(255,0,0), (0,0,255), (255,0,200), (0,255,200), (255,255,255)]
    while True:
        if (t % beat) / beat < 0.03:
            # New random pattern each half beat
            for z in range(5):
                if random.random() > 0.4:
                    r, g, b = random.choice(colors)
                    dim = DIM_HIGH
                else:
                    r, g, b = 0, 0, 0
                    dim = 0
                if z == 0:
                    dmx.set_12s(r, g, b, 0, dim)
                else:
                    dmx.set_bar_zone(z, r, g, b, 0, dim)
        _frame(dmx)
        t += 0.025

def peak_hw_strobe_color(dmx, bpm):
    """Hardware strobe with color. Not just white."""
    dmx.set_12s(255, 0, 100, 0, HW_STROBE_MED)
    for z in range(1, 5):
        dmx.set_bar_zone(z, 0, 100, 255, 0, HW_STROBE_MED)
    while True:
        _frame(dmx)


# ============================================================
#  DROP — Maximum impact. 8 bars of absolute chaos.
# ============================================================

def drop_white_strobe_max(dmx, bpm):
    """Full white hardware strobe at max speed. The classic."""
    dmx.set_all(255, 255, 255, 255, HW_STROBE_MAX)
    while True:
        _frame(dmx)

def drop_kick_slam(dmx, bpm):
    """EVERYTHING blasts white on each beat. Total blackout between.
    The simplest, most effective drop pattern."""
    beat = _beat(bpm)
    t = 0
    while True:
        pos = (t % beat) / beat
        if pos < 0.12:
            dmx.set_all(255, 255, 255, 255, 127)
        else:
            dmx.set_all(0, 0, 0, 0, 0)
        _frame(dmx)
        t += 0.025

def drop_color_cannon(dmx, bpm):
    """Alternating red/blue full blast with hardware strobe."""
    beat = _beat(bpm)
    t = 0
    while True:
        beat_num = int(t / beat) % 2
        if beat_num == 0:
            dmx.set_all(255, 0, 0, 0, HW_STROBE_FAST)
        else:
            dmx.set_all(0, 0, 255, 0, HW_STROBE_FAST)
        dmx.send_frame()
        time.sleep(beat / 4)
        t += beat / 4

def drop_machine_gun(dmx, bpm):
    """Quarter-beat wipe across all fixtures. Relentless."""
    quarter = _beat(bpm) / 4
    t = 0
    while True:
        pos = (t % quarter) / quarter
        position = pos * 5
        for z in range(5):
            dist = abs(position - z)
            if dist < 0.6:
                dim = 127
            else:
                dim = 0
            if z == 0:
                dmx.set_12s(255, 255, 255, 200, dim)
            else:
                dmx.set_bar_zone(z, 255, 255, 255, 200, dim)
        _frame(dmx)
        t += 0.025

def drop_explosion(dmx, bpm):
    """Full white blast → rapid color chase. Two-phase drop."""
    beat = _beat(bpm)
    # Phase 1: 2 beats of full white
    start = time.time()
    while time.time() - start < beat * 2:
        dmx.set_all(255, 255, 255, 255, 127)
        _frame(dmx)
    # Phase 2: rapid color kicks
    colors = [(255,0,0,0),(0,0,255,0),(255,0,200,0),(0,255,200,0)]
    t = 0
    while True:
        pos = (t % beat) / beat
        ci = int(t / beat) % len(colors)
        if pos < 0.15:
            dmx.set_all(*colors[ci], DIM_HIGH)
        else:
            dmx.set_all(0, 0, 0, 0, 0)
        _frame(dmx)
        t += 0.025


# ============================================================
#  BREAKDOWN — Minimal. The silence IS the effect.
# ============================================================

def breakdown_single_ember(dmx, bpm):
    """One zone barely glowing. Everything else dead black."""
    t = 0
    while True:
        active = int(t * 0.08) % 4
        for z in range(4):
            if z == active:
                flick = random.uniform(0.5, 1.0)
                dmx.set_bar_zone(z+1, 180, 30, 0, int(150*flick), int(12*flick))
            else:
                dmx.set_bar_zone(z+1, 0, 0, 0, 0, 0)
        dmx.set_12s(0, 0, 0, 0, 0)
        _frame(dmx)
        t += 0.025

def breakdown_deep_breath(dmx, bpm):
    """Near-blackout breathing. You should barely be able to tell."""
    t = 0
    while True:
        breath = (math.sin(t * 0.15) + 1) / 2
        dim = _lerp(0, 8, breath)
        dmx.set_all(20, 0, 60, 0, dim)
        _frame(dmx)
        t += 0.025

def breakdown_blackout(dmx, bpm):
    """Just... nothing. Let the music speak."""
    dmx.set_all(0, 0, 0, 0, 0)
    while True:
        _frame(dmx)


# ============================================================
#  SCENE REGISTRY
# ============================================================

SCENES = [
    # AMBIENT (3)
    {'name': 'Color Wash',     'category': 'ambient',   'energy': 10, 'fn': ambient_color_wash},
    {'name': 'Campfire',       'category': 'ambient',   'energy': 12, 'fn': ambient_campfire},
    {'name': 'Breathing',      'category': 'ambient',   'energy': 8,  'fn': ambient_breathing},

    # GROOVE (4)
    {'name': 'Pulse',          'category': 'groove',    'energy': 30, 'fn': groove_pulse},
    {'name': 'Bar Chase',      'category': 'groove',    'energy': 35, 'fn': groove_bar_chase},
    {'name': 'Ping Pong',      'category': 'groove',    'energy': 28, 'fn': groove_ping_pong},
    {'name': 'Color Swap',     'category': 'groove',    'energy': 32, 'fn': groove_two_color_swap},

    # DRIVE (4)
    {'name': 'Strobe Chase',   'category': 'drive',     'energy': 55, 'fn': drive_strobe_chase},
    {'name': 'Kick Punch',     'category': 'drive',     'energy': 58, 'fn': drive_kick_punch},
    {'name': 'Split Chase',    'category': 'drive',     'energy': 50, 'fn': drive_color_split_chase},
    {'name': 'Heartbeat',      'category': 'drive',     'energy': 52, 'fn': drive_heartbeat},

    # PEAK (4)
    {'name': 'Rapid Flash',    'category': 'peak',      'energy': 75, 'fn': peak_rapid_color_flash},
    {'name': 'Alt Slam',       'category': 'peak',      'energy': 72, 'fn': peak_alternating_slam},
    {'name': 'Scatter Blast',  'category': 'peak',      'energy': 78, 'fn': peak_scatter_blast},
    {'name': 'Color Strobe',   'category': 'peak',      'energy': 80, 'fn': peak_hw_strobe_color},

    # DROP (5)
    {'name': 'White Strobe',   'category': 'drop',      'energy': 95, 'fn': drop_white_strobe_max},
    {'name': 'Kick Slam',      'category': 'drop',      'energy': 90, 'fn': drop_kick_slam},
    {'name': 'Color Cannon',   'category': 'drop',      'energy': 92, 'fn': drop_color_cannon},
    {'name': 'Machine Gun',    'category': 'drop',      'energy': 100,'fn': drop_machine_gun},
    {'name': 'Explosion',      'category': 'drop',      'energy': 88, 'fn': drop_explosion},

    # BREAKDOWN (3)
    {'name': 'Single Ember',   'category': 'breakdown', 'energy': 5,  'fn': breakdown_single_ember},
    {'name': 'Deep Breath',    'category': 'breakdown', 'energy': 3,  'fn': breakdown_deep_breath},
    {'name': 'Blackout',       'category': 'breakdown', 'energy': 1,  'fn': breakdown_blackout},
]


def get_scenes_by_category(category):
    return [s for s in SCENES if s['category'] == category]

def get_scene_for_energy(energy_level):
    if energy_level < 10:
        pool = get_scenes_by_category('breakdown')
    elif energy_level < 25:
        pool = get_scenes_by_category('ambient')
    elif energy_level < 45:
        pool = get_scenes_by_category('groove')
    elif energy_level < 65:
        pool = get_scenes_by_category('drive')
    elif energy_level < 85:
        pool = get_scenes_by_category('peak')
    else:
        pool = get_scenes_by_category('drop')
    return random.choice(pool) if pool else random.choice(SCENES)

def list_scenes():
    for cat in ['ambient', 'groove', 'drive', 'peak', 'drop', 'breakdown']:
        scenes = get_scenes_by_category(cat)
        print(f"\n  {cat.upper()} ({len(scenes)})")
        for s in scenes:
            print(f"    [{s['energy']:3d}] {s['name']}")
    print(f"\n  TOTAL: {len(SCENES)} scenes")


if __name__ == '__main__':
    list_scenes()
