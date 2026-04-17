#!/usr/bin/env python3
"""
Custom strobe/chase patterns across DMX fixtures.

Chain: Tetra 12 #1 → Tetra 12 #2 → Bar Zone 1 → Zone 2 → Zone 3 → Zone 4

Modes:
  chase [bpm]     — Sequential flash down the chain (default 130 BPM)
  bounce [bpm]    — Chase that bounces back and forth
  random [bpm]    — Random fixture strobes
  split [bpm]     — 12s vs Bar alternating
  wave [bpm]      — Color wave rolling through chain
  buildup [bpm]   — Starts slow, builds to all-flash (good for drops)
  all [bpm]       — Sync flash all fixtures together
"""

import sys
import os
import time
import signal
import random

sys.path.insert(0, os.path.dirname(__file__))
from dmx_controller import DMX

# 6 addressable segments in our chain
SEGMENTS = [
    ('12_1', 'tetra12', 1),     # Tetra 12 #1 at addr 1
    ('12_2', 'tetra12', None),  # Tetra 12 #2 (also addr 1, same as #1)
    ('bar_1', 'bar_zone', 1),
    ('bar_2', 'bar_zone', 2),
    ('bar_3', 'bar_zone', 3),
    ('bar_4', 'bar_zone', 4),
]

# Since both 12s are on d.001, we actually have 5 independent segments:
# 12s (both), bar z1, bar z2, bar z3, bar z4
CHASE_SEGMENTS = [
    'wash',    # both Tetra 12s
    'bar_1',
    'bar_2',
    'bar_3',
    'bar_4',
]

STROBE_COLORS = [
    (255, 255, 255, 0),   # white
    (255, 0, 0, 0),       # red
    (0, 0, 255, 0),       # blue
    (255, 0, 128, 0),     # pink
    (128, 0, 255, 0),     # purple
    (0, 255, 255, 0),     # cyan
]


def set_segment(dmx, seg_name, r, g, b, a=0, dimmer=255):
    if seg_name == 'wash':
        dmx.set_12s(r, g, b, a, dimmer)
    elif seg_name.startswith('bar_'):
        zone = int(seg_name[-1])
        dmx.set_bar_zone(zone, r, g, b, a, dimmer)


def clear_all(dmx):
    dmx.set_12s(0, 0, 0, 0, 0)
    for z in range(1, 5):
        dmx.set_bar_zone(z, 0, 0, 0, 0, 0)


def flash_segment(dmx, seg_name, r, g, b, a, on_time, off_time):
    """Flash a single segment on then off."""
    set_segment(dmx, seg_name, r, g, b, a, 255)
    dmx.send_for(on_time)
    set_segment(dmx, seg_name, 0, 0, 0, 0, 0)
    dmx.send_for(off_time)


def mode_chase(dmx, bpm=130, color=None):
    """Sequential flash down the chain on each beat."""
    beat = 60.0 / bpm
    on_time = beat * 0.3   # 30% duty cycle
    off_time = beat * 0.7
    r, g, b, a = color or (255, 255, 255, 0)
    
    print(f"⚡ CHASE STROBE @ {bpm} BPM ({beat*1000:.0f}ms per beat)")
    i = 0
    while True:
        seg = CHASE_SEGMENTS[i % len(CHASE_SEGMENTS)]
        clear_all(dmx)
        set_segment(dmx, seg, r, g, b, a, 255)
        dmx.send_for(on_time)
        clear_all(dmx)
        dmx.send_for(off_time)
        i += 1


def mode_bounce(dmx, bpm=130, color=None):
    """Chase that bounces: wash → bar1 → bar2 → bar3 → bar4 → bar3 → bar2 → bar1 → ..."""
    beat = 60.0 / bpm
    on_time = beat * 0.3
    off_time = beat * 0.7
    r, g, b, a = color or (255, 255, 255, 0)
    
    bounce_order = ['wash', 'bar_1', 'bar_2', 'bar_3', 'bar_4', 'bar_3', 'bar_2', 'bar_1']
    
    print(f"⚡ BOUNCE STROBE @ {bpm} BPM")
    i = 0
    while True:
        seg = bounce_order[i % len(bounce_order)]
        clear_all(dmx)
        set_segment(dmx, seg, r, g, b, a, 255)
        dmx.send_for(on_time)
        clear_all(dmx)
        dmx.send_for(off_time)
        i += 1


def mode_random(dmx, bpm=130):
    """Random segment, random color from palette on each beat."""
    beat = 60.0 / bpm
    on_time = beat * 0.4
    off_time = beat * 0.6
    
    print(f"⚡ RANDOM STROBE @ {bpm} BPM")
    while True:
        seg = random.choice(CHASE_SEGMENTS)
        r, g, b, a = random.choice(STROBE_COLORS)
        clear_all(dmx)
        set_segment(dmx, seg, r, g, b, a, 255)
        dmx.send_for(on_time)
        clear_all(dmx)
        dmx.send_for(off_time)


def mode_split(dmx, bpm=130, color=None):
    """12s and Bar alternate on each beat."""
    beat = 60.0 / bpm
    on_time = beat * 0.4
    off_time = beat * 0.1
    r, g, b, a = color or (255, 255, 255, 0)
    
    print(f"⚡ SPLIT STROBE @ {bpm} BPM (12s vs Bar)")
    while True:
        # Beat 1: 12s flash
        clear_all(dmx)
        dmx.set_12s(r, g, b, a, 255)
        dmx.send_for(on_time)
        clear_all(dmx)
        dmx.send_for(off_time)
        
        # Beat 2: Bar flash
        clear_all(dmx)
        dmx.set_bar(r, g, b, a, 255)
        dmx.send_for(on_time)
        clear_all(dmx)
        dmx.send_for(off_time)


def mode_wave(dmx, bpm=130):
    """Color wave rolling through — each segment gets the next color in palette."""
    beat = 60.0 / bpm
    colors = STROBE_COLORS
    
    print(f"🌊 COLOR WAVE @ {bpm} BPM")
    offset = 0
    while True:
        for i, seg in enumerate(CHASE_SEGMENTS):
            c = colors[(i + offset) % len(colors)]
            set_segment(dmx, seg, *c, 200)
        dmx.send_for(beat)
        offset += 1


def mode_buildup(dmx, bpm=130, bars=8):
    """Builds from slow single flashes to full-chain strobe over N bars.
    Perfect for leading into a drop."""
    beat = 60.0 / bpm
    total_beats = bars * 4
    r, g, b, a = 255, 255, 255, 0
    
    print(f"📈 BUILDUP @ {bpm} BPM ({bars} bars → {total_beats} beats)")
    
    for beat_num in range(total_beats):
        progress = beat_num / total_beats  # 0.0 → 1.0
        
        # Increase number of active segments as we build
        num_active = max(1, int(progress * len(CHASE_SEGMENTS)) + 1)
        
        # Decrease on-time gap (faster flashing)
        on_time = beat * (0.5 - progress * 0.35)  # 50% → 15% duty
        off_time = beat - on_time
        
        # Increase brightness
        dim = int(80 + progress * 175)  # 80 → 255
        
        clear_all(dmx)
        for i in range(num_active):
            seg = CHASE_SEGMENTS[i % len(CHASE_SEGMENTS)]
            set_segment(dmx, seg, r, g, b, a, dim)
        dmx.send_for(on_time)
        clear_all(dmx)
        dmx.send_for(off_time)
    
    # DROP: all segments full blast
    print("💥 DROP!")
    dmx.set_all(255, 255, 255, 128, 255)
    dmx.send_for(beat * 2)
    
    # Then into chase mode
    mode_chase(dmx, bpm, (255, 255, 255, 0))


def mode_all(dmx, bpm=130, color=None):
    """All fixtures flash in sync on every beat."""
    beat = 60.0 / bpm
    on_time = beat * 0.3
    off_time = beat * 0.7
    r, g, b, a = color or (255, 255, 255, 0)
    
    print(f"⚡ ALL FLASH @ {bpm} BPM")
    while True:
        dmx.set_all(r, g, b, a, 255)
        dmx.send_for(on_time)
        dmx.blackout()
        dmx.send_for(off_time)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    mode = sys.argv[1].lower()
    args = sys.argv[2:]
    bpm = int(args[0]) if args else 130

    dmx = DMX()

    def cleanup(sig, frame):
        print("\n🛑 Stopping...")
        dmx.blackout()
        dmx.send_for(0.5)
        dmx.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    try:
        dmx.open()

        modes = {
            'chase': lambda: mode_chase(dmx, bpm),
            'bounce': lambda: mode_bounce(dmx, bpm),
            'random': lambda: mode_random(dmx, bpm),
            'split': lambda: mode_split(dmx, bpm),
            'wave': lambda: mode_wave(dmx, bpm),
            'buildup': lambda: mode_buildup(dmx, bpm),
            'all': lambda: mode_all(dmx, bpm),
        }

        if mode in modes:
            modes[mode]()
        else:
            print(f"Unknown mode: {mode}")
            print("Modes: chase, bounce, random, split, wave, buildup, all")
    finally:
        dmx.blackout()
        dmx.send_for(0.5)
        dmx.close()


if __name__ == '__main__':
    main()
