#!/usr/bin/env python3
"""
DMX Scene Library — categorized effects for intelligent selection.

Categories:
  AMBIENT   — Static/slow washes for low-energy moments, intros, outros
  GROOVE    — Gentle movement, good for steady grooves
  DRIVE     — More active patterns for driving sections
  PEAK      — High energy, fast movement for peak moments
  DROP      — Maximum impact for drops
  BREAKDOWN — Minimal, moody, stripped back

Each scene is a generator function that yields DMX frames continuously.
The engine picks scenes based on audio energy + frequency analysis.
"""

import time
import math
import random


# ===== SCENE DEFINITIONS =====
# Each scene is a dict with:
#   name, category, energy (0-100), fn(dmx, bpm) -> runs continuously

DEFAULT_DIM = 25  # ~10% brightness (0-255 scale)
PEAK_DIM = 80     # ~30% for peak/drop moments
MAX_DIM = 150     # cap even for drops — these lights are BRIGHT

def _beat_sleep(bpm):
    return 60.0 / bpm


# ---------- AMBIENT (energy 0-20) ----------

def ambient_deep_ocean(dmx, bpm):
    """Slow deep blue pulse across all fixtures."""
    t = 0
    while True:
        brightness = int(30 + 20 * math.sin(t * 0.3))
        dmx.set_all(0, 20, 150, 0, brightness)
        dmx.send_for(0.05)
        t += 0.05

def ambient_warm_glow(dmx, bpm):
    """Static warm amber wash, very low."""
    dmx.set_all(200, 100, 30, 220, 40)
    dmx.send_hold()

def ambient_midnight(dmx, bpm):
    """Deep purple, barely visible."""
    dmx.set_all(30, 0, 60, 0, 25)
    dmx.send_hold()

def ambient_embers(dmx, bpm):
    """Slow red/orange flicker."""
    t = 0
    while True:
        flicker = random.randint(-15, 15)
        dmx.set_12s(180 + flicker, 40 + flicker//2, 0, 80, 35)
        dmx.set_bar(200 + flicker, 30, 0, 100, 30)
        dmx.send_for(0.15)
        t += 0.15

def ambient_northern_lights(dmx, bpm):
    """Slow color drift between green and purple."""
    t = 0
    while True:
        phase = math.sin(t * 0.15) * 0.5 + 0.5  # 0-1
        r = int(80 * phase)
        g = int(200 * (1 - phase))
        b = int(100 + 100 * phase)
        dmx.set_12s(r, g, b, 0, 40)
        dmx.set_bar(b, r, g, 0, 35)
        dmx.send_for(0.05)
        t += 0.05


# ---------- GROOVE (energy 20-40) ----------

def groove_pulse(dmx, bpm):
    """Gentle brightness pulse on the beat."""
    beat = _beat_sleep(bpm)
    while True:
        dmx.set_all(0, 100, 220, 0, DEFAULT_DIM)
        dmx.send_for(beat * 0.2)
        dmx.set_all(0, 100, 220, 0, 50)
        dmx.send_for(beat * 0.8)

def groove_two_tone(dmx, bpm):
    """12s and bar swap colors every 2 beats."""
    beat = _beat_sleep(bpm)
    colors = [(0, 80, 200, 0), (200, 0, 100, 0)]
    i = 0
    while True:
        c1 = colors[i % 2]
        c2 = colors[(i + 1) % 2]
        dmx.set_12s(*c1, 80)
        dmx.set_bar(*c2, 80)
        dmx.send_for(beat * 2)
        i += 1

def groove_bar_breathe(dmx, bpm):
    """Bar zones breathe in sequence, 12s static."""
    beat = _beat_sleep(bpm)
    dmx.set_12s(100, 0, 180, 0, 60)
    t = 0
    while True:
        for z in range(1, 5):
            phase = math.sin(t + z * 1.5) * 0.5 + 0.5
            dim = int(20 + 60 * phase)
            dmx.set_bar_zone(z, 0, 150, 200, 0, dim)
        dmx.send_for(0.05)
        t += 0.05

def groove_warm_sway(dmx, bpm):
    """Warm colors gently swaying between amber and orange."""
    t = 0
    while True:
        phase = math.sin(t * 0.4) * 0.5 + 0.5
        amber = int(100 + 155 * phase)
        dmx.set_12s(255, 120, 30, amber, 70)
        dmx.set_bar(255, 80 + int(40 * phase), 0, 200 - int(100 * phase), 60)
        dmx.send_for(0.05)
        t += 0.05

def groove_subtle_chase(dmx, bpm):
    """Very slow chase across bar zones, low intensity."""
    beat = _beat_sleep(bpm)
    while True:
        for z in range(1, 5):
            for zz in range(1, 5):
                dim = 80 if zz == z else 20
                dmx.set_bar_zone(zz, 0, 180, 255, 0, dim)
            dmx.set_12s(0, 100, 200, 0, 50)
            dmx.send_for(beat)


# ---------- DRIVE (energy 40-65) ----------

def drive_chase_white(dmx, bpm):
    """White chase down the chain on beat."""
    beat = _beat_sleep(bpm)
    segs = ['wash', 'bar_1', 'bar_2', 'bar_3', 'bar_4']
    i = 0
    while True:
        _clear_all(dmx)
        _set_seg(dmx, segs[i % len(segs)], 255, 255, 255, 0, DEFAULT_DIM * 2)
        dmx.send_for(beat * 0.3)
        _clear_all(dmx)
        dmx.send_for(beat * 0.7)
        i += 1

def drive_chase_color(dmx, bpm):
    """Color chase — each segment gets next color in palette."""
    beat = _beat_sleep(bpm)
    colors = [(255,0,0,0),(255,80,0,0),(255,255,0,0),(0,255,0,0),(0,255,255,0),(0,0,255,0),(128,0,255,0)]
    segs = ['wash', 'bar_1', 'bar_2', 'bar_3', 'bar_4']
    offset = 0
    while True:
        for i, seg in enumerate(segs):
            c = colors[(i + offset) % len(colors)]
            _set_seg(dmx, seg, *c, PEAK_DIM)
        dmx.send_for(beat)
        offset += 1

def drive_split_beat(dmx, bpm):
    """12s and bar alternate every beat."""
    beat = _beat_sleep(bpm)
    while True:
        _clear_all(dmx)
        dmx.set_12s(255, 0, 128, 0, DEFAULT_DIM * 2)
        dmx.send_for(beat * 0.4)
        _clear_all(dmx)
        dmx.send_for(beat * 0.1)
        dmx.set_bar(0, 200, 255, 0, DEFAULT_DIM * 2)
        dmx.send_for(beat * 0.4)
        _clear_all(dmx)
        dmx.send_for(beat * 0.1)

def drive_bounce(dmx, bpm):
    """Bounce pattern across bar, 12s accent on downbeat."""
    beat = _beat_sleep(bpm)
    bounce = ['bar_1', 'bar_2', 'bar_3', 'bar_4', 'bar_3', 'bar_2']
    i = 0
    while True:
        _clear_all(dmx)
        if i % 4 == 0:
            dmx.set_12s(255, 255, 255, 0, DEFAULT_DIM * 2)
        _set_seg(dmx, bounce[i % len(bounce)], 128, 0, 255, 0, DEFAULT_DIM * 2)
        dmx.send_for(beat * 0.35)
        _clear_all(dmx)
        dmx.send_for(beat * 0.65)
        i += 1

def drive_strobe_bar(dmx, bpm):
    """Bar strobes while 12s hold steady color."""
    beat = _beat_sleep(bpm)
    dmx.set_12s(0, 50, 180, 0, 100)
    while True:
        dmx.set_bar(255, 255, 255, 0, PEAK_DIM)
        dmx.send_for(beat * 0.15)
        dmx.set_bar(0, 50, 180, 0, 40)
        dmx.send_for(beat * 0.85)

def drive_color_wave(dmx, bpm):
    """Rolling color wave through bar zones."""
    beat = _beat_sleep(bpm)
    colors = [(255,0,80,0),(80,0,255,0),(0,200,255,0),(0,255,100,0)]
    offset = 0
    while True:
        dmx.set_12s(*colors[offset % len(colors)], DEFAULT_DIM * 2)
        for z in range(4):
            c = colors[(z + offset) % len(colors)]
            dmx.set_bar_zone(z + 1, *c, PEAK_DIM)
        dmx.send_for(beat)
        offset += 1

def drive_heartbeat(dmx, bpm):
    """Double-pulse heartbeat pattern (thump-thump... thump-thump...)."""
    beat = _beat_sleep(bpm)
    while True:
        # First thump
        dmx.set_all(255, 0, 0, 80, 220)
        dmx.send_for(beat * 0.15)
        dmx.set_all(255, 0, 0, 80, 40)
        dmx.send_for(beat * 0.2)
        # Second thump
        dmx.set_all(255, 0, 0, 80, DEFAULT_DIM * 2)
        dmx.send_for(beat * 0.12)
        dmx.set_all(255, 0, 0, 80, 30)
        dmx.send_for(beat * 0.53)


# ---------- PEAK (energy 65-85) ----------

def peak_fast_chase(dmx, bpm):
    """Double-time chase (flash on every half beat)."""
    half = _beat_sleep(bpm) / 2
    segs = ['wash', 'bar_1', 'bar_2', 'bar_3', 'bar_4']
    i = 0
    while True:
        _clear_all(dmx)
        _set_seg(dmx, segs[i % len(segs)], 255, 255, 255, 0, PEAK_DIM)
        dmx.send_for(half * 0.35)
        _clear_all(dmx)
        dmx.send_for(half * 0.65)
        i += 1

def peak_strobe_all(dmx, bpm):
    """Full white strobe on beat."""
    beat = _beat_sleep(bpm)
    while True:
        dmx.set_all(255, 255, 255, 128, PEAK_DIM)
        dmx.send_for(beat * 0.25)
        _clear_all(dmx)
        dmx.send_for(beat * 0.75)

def peak_random_flash(dmx, bpm):
    """Random segment + random color every beat."""
    beat = _beat_sleep(bpm)
    segs = ['wash', 'bar_1', 'bar_2', 'bar_3', 'bar_4']
    colors = [(255,0,0,0),(0,0,255,0),(255,0,200,0),(0,255,255,0),(255,255,255,0),(128,0,255,0)]
    while True:
        _clear_all(dmx)
        seg = random.choice(segs)
        r, g, b, a = random.choice(colors)
        _set_seg(dmx, seg, r, g, b, a, PEAK_DIM)
        dmx.send_for(beat * 0.3)
        _clear_all(dmx)
        dmx.send_for(beat * 0.7)

def peak_split_strobe(dmx, bpm):
    """Alternating 12s/bar strobe, double time."""
    half = _beat_sleep(bpm) / 2
    flip = True
    while True:
        _clear_all(dmx)
        if flip:
            dmx.set_12s(255, 255, 255, 0, PEAK_DIM)
        else:
            dmx.set_bar(255, 255, 255, 0, PEAK_DIM)
        dmx.send_for(half * 0.3)
        _clear_all(dmx)
        dmx.send_for(half * 0.7)
        flip = not flip

def peak_color_blast(dmx, bpm):
    """Full color blast cycling through palette on every beat."""
    beat = _beat_sleep(bpm)
    colors = [(255,0,0,0),(255,128,0,0),(255,0,200,0),(0,0,255,0),(128,0,255,0),(0,255,128,0)]
    i = 0
    while True:
        r, g, b, a = colors[i % len(colors)]
        dmx.set_all(r, g, b, a, PEAK_DIM)
        dmx.send_for(beat * 0.5)
        _clear_all(dmx)
        dmx.send_for(beat * 0.5)
        i += 1


# ---------- DROP (energy 85-100) ----------

def drop_full_strobe(dmx, bpm):
    """Maximum intensity full strobe, half-beat."""
    half = _beat_sleep(bpm) / 2
    while True:
        dmx.set_all(255, 255, 255, 255, MAX_DIM)
        dmx.send_for(half * 0.4)
        _clear_all(dmx)
        dmx.send_for(half * 0.6)

def drop_machine_gun(dmx, bpm):
    """Rapid-fire sequential hits, quarter beat."""
    quarter = _beat_sleep(bpm) / 4
    segs = ['wash', 'bar_1', 'bar_2', 'bar_3', 'bar_4']
    i = 0
    while True:
        _clear_all(dmx)
        _set_seg(dmx, segs[i % len(segs)], 255, 255, 255, 128, MAX_DIM)
        dmx.send_for(quarter * 0.5)
        _clear_all(dmx)
        dmx.send_for(quarter * 0.5)
        i += 1

def drop_explosion(dmx, bpm):
    """All white blast, then fast color chase."""
    beat = _beat_sleep(bpm)
    # 2 beats of full white
    dmx.set_all(255, 255, 255, 255, MAX_DIM)
    dmx.send_for(beat * 2)
    # Then fast color chase
    segs = ['wash', 'bar_1', 'bar_2', 'bar_3', 'bar_4']
    colors = [(255,0,0,0),(255,0,200,0),(0,0,255,0),(0,255,255,0),(128,0,255,0)]
    i = 0
    half = beat / 2
    while True:
        _clear_all(dmx)
        c = colors[i % len(colors)]
        _set_seg(dmx, segs[i % len(segs)], *c, MAX_DIM)
        dmx.send_for(half * 0.4)
        _clear_all(dmx)
        dmx.send_for(half * 0.6)
        i += 1

def drop_alternating_blast(dmx, bpm):
    """Red/blue alternating full blast, double time."""
    half = _beat_sleep(bpm) / 2
    flip = True
    while True:
        if flip:
            dmx.set_all(255, 0, 0, 0, MAX_DIM)
        else:
            dmx.set_all(0, 0, 255, 0, MAX_DIM)
        dmx.send_for(half * 0.4)
        _clear_all(dmx)
        dmx.send_for(half * 0.6)
        flip = not flip


# ---------- BREAKDOWN (energy 0-15) ----------

def breakdown_minimal(dmx, bpm):
    """Single bar zone barely visible, slow drift."""
    t = 0
    while True:
        zone = int(t / 4) % 4 + 1
        for z in range(1, 5):
            if z == zone:
                dmx.set_bar_zone(z, 40, 0, 80, 0, 20)
            else:
                dmx.set_bar_zone(z, 0, 0, 0, 0, 0)
        dmx.set_12s(0, 0, 0, 0, 0)
        dmx.send_for(0.1)
        t += 0.1

def breakdown_slow_fade(dmx, bpm):
    """Everything fades to near-black slowly."""
    t = 0
    while True:
        dim = int(max(5, 30 * math.sin(t * 0.1) * 0.5 + 15))
        dmx.set_all(30, 0, 60, 0, dim)
        dmx.send_for(0.05)
        t += 0.05

def breakdown_single_wash(dmx, bpm):
    """Just the 12s with a deep color, bar off."""
    dmx.set_12s(0, 20, 100, 0, 30)
    for z in range(1, 5):
        dmx.set_bar_zone(z, 0, 0, 0, 0, 0)
    dmx.send_hold()


# ===== HELPERS =====

def _clear_all(dmx):
    dmx.set_12s(0, 0, 0, 0, 0)
    for z in range(1, 5):
        dmx.set_bar_zone(z, 0, 0, 0, 0, 0)

def _set_seg(dmx, name, r, g, b, a=0, dim=255):
    if name == 'wash':
        dmx.set_12s(r, g, b, a, dim)
    elif name.startswith('bar_'):
        z = int(name[-1])
        dmx.set_bar_zone(z, r, g, b, a, dim)


# ===== SCENE REGISTRY =====

SCENES = [
    # AMBIENT (0-20)
    {'name': 'Deep Ocean',       'category': 'ambient',    'energy': 5,   'fn': ambient_deep_ocean},
    {'name': 'Warm Glow',        'category': 'ambient',    'energy': 8,   'fn': ambient_warm_glow},
    {'name': 'Midnight',         'category': 'ambient',    'energy': 3,   'fn': ambient_midnight},
    {'name': 'Embers',           'category': 'ambient',    'energy': 12,  'fn': ambient_embers},
    {'name': 'Northern Lights',  'category': 'ambient',    'energy': 15,  'fn': ambient_northern_lights},

    # GROOVE (20-40)
    {'name': 'Pulse',            'category': 'groove',     'energy': 25,  'fn': groove_pulse},
    {'name': 'Two Tone',         'category': 'groove',     'energy': 30,  'fn': groove_two_tone},
    {'name': 'Bar Breathe',      'category': 'groove',     'energy': 22,  'fn': groove_bar_breathe},
    {'name': 'Warm Sway',        'category': 'groove',     'energy': 28,  'fn': groove_warm_sway},
    {'name': 'Subtle Chase',     'category': 'groove',     'energy': 35,  'fn': groove_subtle_chase},

    # DRIVE (40-65)
    {'name': 'Chase White',      'category': 'drive',      'energy': 50,  'fn': drive_chase_white},
    {'name': 'Chase Color',      'category': 'drive',      'energy': 45,  'fn': drive_chase_color},
    {'name': 'Split Beat',       'category': 'drive',      'energy': 55,  'fn': drive_split_beat},
    {'name': 'Bounce',           'category': 'drive',      'energy': 52,  'fn': drive_bounce},
    {'name': 'Strobe Bar',       'category': 'drive',      'energy': 60,  'fn': drive_strobe_bar},
    {'name': 'Color Wave',       'category': 'drive',      'energy': 48,  'fn': drive_color_wave},
    {'name': 'Heartbeat',        'category': 'drive',      'energy': 58,  'fn': drive_heartbeat},

    # PEAK (65-85)
    {'name': 'Fast Chase',       'category': 'peak',       'energy': 75,  'fn': peak_fast_chase},
    {'name': 'Strobe All',       'category': 'peak',       'energy': 80,  'fn': peak_strobe_all},
    {'name': 'Random Flash',     'category': 'peak',       'energy': 70,  'fn': peak_random_flash},
    {'name': 'Split Strobe',     'category': 'peak',       'energy': 78,  'fn': peak_split_strobe},
    {'name': 'Color Blast',      'category': 'peak',       'energy': 72,  'fn': peak_color_blast},

    # DROP (85-100)
    {'name': 'Full Strobe',      'category': 'drop',       'energy': 95,  'fn': drop_full_strobe},
    {'name': 'Machine Gun',      'category': 'drop',       'energy': 100, 'fn': drop_machine_gun},
    {'name': 'Explosion',        'category': 'drop',       'energy': 90,  'fn': drop_explosion},
    {'name': 'Alt Blast',        'category': 'drop',       'energy': 92,  'fn': drop_alternating_blast},

    # BREAKDOWN (0-15)
    {'name': 'Minimal',          'category': 'breakdown',  'energy': 5,   'fn': breakdown_minimal},
    {'name': 'Slow Fade',        'category': 'breakdown',  'energy': 8,   'fn': breakdown_slow_fade},
    {'name': 'Single Wash',      'category': 'breakdown',  'energy': 10,  'fn': breakdown_single_wash},
]


def get_scenes_by_category(category):
    return [s for s in SCENES if s['category'] == category]


def get_scene_for_energy(energy_level):
    """Pick the best scene for a given energy level (0-100).
    Returns a random scene from the closest energy bracket."""
    if energy_level < 10:
        pool = get_scenes_by_category('breakdown') + get_scenes_by_category('ambient')
    elif energy_level < 25:
        pool = get_scenes_by_category('ambient') + get_scenes_by_category('groove')
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
    """Print all scenes grouped by category."""
    categories = ['ambient', 'groove', 'drive', 'peak', 'drop', 'breakdown']
    for cat in categories:
        scenes = get_scenes_by_category(cat)
        print(f"\n{'='*40}")
        print(f"  {cat.upper()} ({len(scenes)} scenes)")
        print(f"{'='*40}")
        for s in sorted(scenes, key=lambda x: x['energy']):
            print(f"  [{s['energy']:3d}] {s['name']}")
    print(f"\n  TOTAL: {len(SCENES)} scenes")


if __name__ == '__main__':
    list_scenes()
