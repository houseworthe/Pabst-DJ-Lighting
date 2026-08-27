"""Demo the buildup auto-curve (0.30 -> 1.00) on an existing scene.

Picks `Ping Pong Bar` (buildup, id=scene-buk6ebkg) and ramps intensity over a
simulated 32-beat phrase at 128 BPM (~15s), matching what direct_lights would
do in a live track. Then holds at peak for 3s before blackout.
"""
import json, os, sys, time

from dmx_controller import DMX
from govee_lan import GoveeClient
from scene_engine import SceneEngine

SCENE_ID = "scene-buk6ebkg"  # Ping Pong Bar
BPM = 128.0
PHRASE_BEATS = 32
RAMP_SECS = PHRASE_BEATS * 60.0 / BPM  # ~15s
LO, HI = 0.30, 1.00  # production buildup curve

with open(os.path.join(os.path.dirname(__file__), "scenes.json")) as f:
    scenes = json.load(f)["scenes"]
scene = next(s for s in scenes if s["id"] == SCENE_ID)

dmx = DMX(); dmx.open()
govee = GoveeClient(); govee.ensure_ready()

start = [None]
def intensity_fn():
    if start[0] is None:
        return LO
    elapsed = time.monotonic() - start[0]
    progress = max(0.0, min(1.0, elapsed / RAMP_SECS))
    return LO + (HI - LO) * progress

eng = SceneEngine(scene, dmx, govee,
                  bpm_fn=lambda: BPM,
                  intensity_fn=intensity_fn)
eng.start()
start[0] = time.monotonic()

print(f"=== {scene['name']} (buildup) — auto curve {LO:.2f} -> {HI:.2f} over {RAMP_SECS:.1f}s ===")
print(f"    {len(scene['layers'])} layers: {', '.join(L['type'] for L in scene['layers'])}")
print()
ticks = int(RAMP_SECS) + 1
try:
    for _ in range(ticks):
        time.sleep(1)
        elapsed = time.monotonic() - start[0]
        intensity = intensity_fn()
        pct = int(round(intensity * 100))
        bar = "█" * int(intensity * 30) + "░" * (30 - int(intensity * 30))
        print(f"  {elapsed:5.1f}s  i={intensity:.2f}  {pct:3d}%  [{bar}]", flush=True)
    print()
    print("=== holding at peak for 3s ===")
    time.sleep(3)
finally:
    print("=== blackout ===")
    eng.stop()
    dmx.blackout(); dmx.send_frame()
    try: govee.turn(False)
    except: pass
