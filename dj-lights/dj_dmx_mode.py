#!/usr/bin/env python3
"""
DJ DMX Modes — runs alongside or independent of Govee DJ mode.

Modes:
  sync     — Color rotation synced to Govee scene changes (default)
  solo     — DMX-only color rotation (Govees untouched)
  strobe   — DMX strobe mode
  reactive — Audio-reactive (reads from Scarlett, controls DMX + Govee)
  warm     — Static warm wash (good for chill sets)

Usage:
  python3 dj_dmx_mode.py sync [interval_sec]
  python3 dj_dmx_mode.py solo [interval_sec]
  python3 dj_dmx_mode.py strobe [speed]
  python3 dj_dmx_mode.py reactive
  python3 dj_dmx_mode.py warm
"""

import sys
import time
import signal
import random
import os

sys.path.insert(0, os.path.dirname(__file__))
from dmx_controller import DMX, PALETTE

# DJ color schemes — each is (12s_color, bar_zone_colors)
# Designed for deep/tech house vibes
DJ_SCENES = [
    # name, 12s (r,g,b,a), bar zones [(r,g,b,a), ...]
    ("Deep Sea",
     (0, 40, 200, 0),
     [(0, 20, 150, 0), (0, 60, 255, 0), (0, 20, 150, 0), (0, 60, 255, 0)]),
    ("Lava",
     (255, 30, 0, 100),
     [(255, 0, 0, 50), (255, 60, 0, 100), (255, 0, 0, 50), (255, 60, 0, 100)]),
    ("Ultraviolet",
     (100, 0, 255, 0),
     [(60, 0, 180, 0), (140, 0, 255, 0), (60, 0, 180, 0), (140, 0, 255, 0)]),
    ("Forest",
     (0, 180, 50, 60),
     [(0, 120, 30, 40), (0, 220, 80, 80), (0, 120, 30, 40), (0, 220, 80, 80)]),
    ("Sunset",
     (255, 80, 0, 180),
     [(255, 40, 0, 100), (255, 120, 0, 200), (255, 40, 0, 100), (255, 120, 0, 200)]),
    ("Ice",
     (80, 200, 255, 0),
     [(40, 150, 255, 0), (120, 230, 255, 0), (40, 150, 255, 0), (120, 230, 255, 0)]),
    ("Neon Pink",
     (255, 0, 128, 0),
     [(200, 0, 100, 0), (255, 0, 160, 0), (200, 0, 100, 0), (255, 0, 160, 0)]),
    ("Cosmic",
     (80, 0, 180, 0),
     [(120, 0, 255, 0), (40, 0, 120, 0), (120, 0, 255, 0), (40, 0, 120, 0)]),
    ("Golden Hour",
     (255, 160, 40, 255),
     [(255, 140, 20, 200), (255, 180, 60, 255), (255, 140, 20, 200), (255, 180, 60, 255)]),
    ("Hypnotic",
     (0, 255, 180, 0),
     [(0, 180, 120, 0), (0, 255, 220, 0), (0, 180, 120, 0), (0, 255, 220, 0)]),
    ("Blood Moon",
     (200, 0, 0, 80),
     [(150, 0, 0, 40), (255, 0, 30, 100), (150, 0, 0, 40), (255, 0, 30, 100)]),
    ("Electric",
     (0, 100, 255, 0),
     [(255, 0, 200, 0), (0, 150, 255, 0), (255, 0, 200, 0), (0, 150, 255, 0)]),
    ("Midnight",
     (20, 0, 80, 0),
     [(10, 0, 40, 0), (40, 0, 120, 0), (10, 0, 40, 0), (40, 0, 120, 0)]),
    ("Inferno",
     (255, 0, 0, 150),
     [(255, 50, 0, 200), (255, 0, 0, 100), (255, 50, 0, 200), (255, 0, 0, 100)]),
    ("Aurora",
     (0, 255, 100, 0),
     [(0, 200, 255, 0), (0, 255, 60, 0), (0, 200, 255, 0), (0, 255, 60, 0)]),
]

DIMMER_DJ = 25  # ~10% brightness for DJ mode


def mode_sync(dmx, interval=59):
    """Color rotation — matches Govee DJ timing."""
    print(f"🎛️  DMX DJ Mode — SYNC ({interval}s rotation, {len(DJ_SCENES)} scenes)")
    scenes = list(range(len(DJ_SCENES)))
    random.shuffle(scenes)
    i = 0
    while True:
        scene = DJ_SCENES[scenes[i % len(scenes)]]
        name, wash_color, bar_zones = scene
        r, g, b, a = wash_color
        dmx.set_12s(r, g, b, a, DIMMER_DJ)
        for z in range(4):
            zr, zg, zb, za = bar_zones[z]
            dmx.set_bar_zone(z + 1, zr, zg, zb, za, DIMMER_DJ)
        print(f"  🎨 {name}")
        dmx.send_for(interval)
        i += 1


def mode_solo(dmx, interval=59):
    """DMX-only color rotation (same as sync but labeled differently)."""
    print(f"🎛️  DMX DJ Mode — SOLO ({interval}s rotation)")
    mode_sync(dmx, interval)


def mode_strobe(dmx, speed=180):
    """Full white strobe on all DMX fixtures."""
    print(f"⚡ DMX STROBE (speed={speed})")
    dmx.set_all(255, 255, 255, 0, 255, speed)
    dmx.send_hold()


def mode_warm(dmx):
    """Static warm wash."""
    print("🔥 Warm wash")
    dmx.set_all(255, 180, 80, 200, 80)
    dmx.send_hold()


def mode_blackout(dmx):
    """All DMX off."""
    dmx.blackout()
    dmx.send_for(1)
    print("⬛ DMX blackout")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    mode = sys.argv[1].lower()
    args = sys.argv[2:]

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

        if mode == 'sync':
            interval = int(args[0]) if args else 59
            mode_sync(dmx, interval)
        elif mode == 'solo':
            interval = int(args[0]) if args else 59
            mode_solo(dmx, interval)
        elif mode == 'strobe':
            speed = int(args[0]) if args else 180
            mode_strobe(dmx, speed)
        elif mode == 'warm':
            mode_warm(dmx)
        elif mode == 'blackout':
            mode_blackout(dmx)
        else:
            print(f"Unknown mode: {mode}")
            print("Modes: sync, solo, strobe, warm, blackout")
    finally:
        dmx.blackout()
        dmx.send_for(0.5)
        dmx.close()


if __name__ == '__main__':
    main()
