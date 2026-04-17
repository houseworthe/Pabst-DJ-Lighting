#!/usr/bin/env python3
"""Preview just breakdown scenes."""
import sys, os, time, multiprocessing
sys.path.insert(0, os.path.dirname(__file__))
from dmx_controller import DMX
from scenes_v2 import SCENES

PREVIEW_TIME = 8

breakdown_scenes = sorted(
    [s for s in SCENES if s['category'] == 'breakdown'],
    key=lambda s: s['energy']
)

def run_scene(scene_idx):
    scene = breakdown_scenes[scene_idx]
    dmx = DMX()
    dmx.open()
    deadline = time.time() + PREVIEW_TIME
    orig_send_for = dmx.send_for
    orig_send_hold = dmx.send_hold
    class TimeUp(Exception): pass
    def timed_send_for(s):
        r = deadline - time.time()
        if r <= 0: raise TimeUp()
        orig_send_for(min(s, r))
        if time.time() >= deadline: raise TimeUp()
    def timed_send_hold():
        while time.time() < deadline:
            dmx.send_frame(); time.sleep(0.023)
        raise TimeUp()
    dmx.send_for = timed_send_for
    dmx.send_hold = timed_send_hold
    try:
        scene['fn'](dmx, 130)
    except TimeUp: pass
    except Exception as e: print(f'  err: {e}')
    dmx.blackout()
    dmx.send_for = orig_send_for
    dmx.send_for(0.3)
    dmx.close()

if __name__ == '__main__':
    print(f'Previewing {len(breakdown_scenes)} BREAKDOWN scenes ({PREVIEW_TIME}s each)...\n')
    for i, s in enumerate(breakdown_scenes):
        print(f'  [{i+1}/{len(breakdown_scenes)}] {s["name"]} (energy {s["energy"]})')
        sys.stdout.flush()
        p = multiprocessing.Process(target=run_scene, args=(i,))
        p.start()
        p.join(timeout=PREVIEW_TIME + 3)
        if p.is_alive(): p.terminate(); p.join(1)
    dmx = DMX(); dmx.open(); dmx.blackout(); dmx.send_for(0.5); dmx.close()
    print('\n✅ Done')
