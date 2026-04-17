#!/usr/bin/env python3
"""Preview all scenes — 5 seconds each, ordered by category."""

import sys, os, time, math, random, signal, multiprocessing
sys.path.insert(0, os.path.dirname(__file__))
from dmx_controller import DMX
from scenes_v2 import SCENES

PREVIEW_TIME = 5  # seconds per scene
BPM = 130

cat_order = {'ambient': 0, 'groove': 1, 'drive': 2, 'peak': 3, 'drop': 4, 'breakdown': 5}
sorted_scenes = sorted(SCENES, key=lambda s: (cat_order.get(s['category'], 9), s['energy']))


def run_one_scene(scene_idx):
    """Run a single scene in a subprocess for PREVIEW_TIME seconds."""
    scene = sorted_scenes[scene_idx]
    dmx = DMX()
    dmx.open()
    
    deadline = time.time() + PREVIEW_TIME
    
    # Monkey-patch send_for and send_hold to respect deadline
    orig_send_for = dmx.send_for
    orig_send_hold = dmx.send_hold
    
    class TimeUp(Exception):
        pass
    
    def timed_send_for(seconds):
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeUp()
        orig_send_for(min(seconds, remaining))
        if time.time() >= deadline:
            raise TimeUp()
    
    def timed_send_hold():
        while time.time() < deadline:
            dmx.send_frame()
            time.sleep(0.023)
        raise TimeUp()
    
    dmx.send_for = timed_send_for
    dmx.send_hold = timed_send_hold
    
    try:
        scene['fn'](dmx, BPM)
    except TimeUp:
        pass
    except Exception as e:
        print(f"    ⚠️  Error: {e}")
    
    dmx.blackout()
    dmx.send_for = orig_send_for
    dmx.send_for(0.3)
    dmx.close()


def main():
    current_cat = None
    
    for i, scene in enumerate(sorted_scenes):
        if scene['category'] != current_cat:
            current_cat = scene['category']
            print(f'\n===== {current_cat.upper()} =====')
        
        print(f'  [{i+1:2d}/{len(sorted_scenes)}] {scene["name"]} (energy {scene["energy"]})')
        sys.stdout.flush()
        
        p = multiprocessing.Process(target=run_one_scene, args=(i,))
        p.start()
        p.join(timeout=PREVIEW_TIME + 3)
        if p.is_alive():
            p.terminate()
            p.join(1)
    
    # Final blackout
    dmx = DMX()
    dmx.open()
    dmx.blackout()
    dmx.send_for(0.5)
    dmx.close()
    
    print(f'\n✅ Preview complete — {len(sorted_scenes)} scenes shown')
    print('Tell me which ones you like and which to cut!')


if __name__ == '__main__':
    main()
