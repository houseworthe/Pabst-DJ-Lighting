# TODO

Open feature ideas surfaced 2026-04-29 between sets. Top picks marked **(top)** —
worth doing before the next live set; the rest can wait.

## Sync / grouped-fire

- [ ] **(top)** Measure actual sync gap before optimizing.
  We already fire ~1s ahead via `SCENE_LOOKAHEAD_MS` in `main.py`, so DMX and
  Govee should both land before the beat. Instrument `_fire_govee_layers` in
  `scene_engine.py` — log per-SKU arrival time and eyeball the rig — to confirm
  where the perceived lag actually is. Could be sequential cloud calls, could
  be perception (DMX is sharper-edged so reads as "first"), could be a
  too-tight lookahead for the slowest bulb. Don't optimize blind.

- [ ] Parallel Govee fan-out.
  `_fire_govee_layers` iterates SKUs sequentially; each cloud-API bulb adds
  ~200ms. Replace with a thread-pool scatter so all bulbs hit within one
  network roundtrip. Only worth doing once the measurement above confirms
  this is the real cause.

## Music-aware automation

- [ ] **(top)** BPM-locked rates.
  `chase` / `bar_chase` / `pulse` use absolute `rate_hz` — a scene that looks
  great at 120 BPM is wrong at 90. Add a `beats` knob (e.g. "1 zone per beat",
  "every half-bar") and compute Hz live from the active deck's BPM. Touches
  the renderers in `scene_engine.py` plus the editor schemas in `dashboard.py`.
  Live BPM is already on the prodj client (`getattr(client, "bpm", None)`),
  needs to be plumbed into the render thread (per-tick read or set on scene
  start).

## Live-set ergonomics

- [ ] Manual blackout button + force-refresh in the dashboard.
  Replace the empty-layer-preview hack we used 2026-04-17 with a real
  `/api/blackout` endpoint and a button in the monitor. Also: a "re-pick
  scene for this mode" button for when the random pick lands on something
  that doesn't fit the moment.

- [ ] Strobe master fader.
  Global 0–100% multiplier applied to every `strobe` layer (and to the
  `strobe` field on other layers) at render time. For venues that hate
  strobing or early-set warmup. UI: a single slider in the dashboard
  monitor, persisted per-process.

- [ ] Bar-position overlay in the monitor.
  Show current phrase, current mode, beats-until-next-mode, "drop in 8 bars"
  countdown. Helps ride the flow visually instead of guessing.

## Skipped — not worth the complexity for this rig

- MIDI / OSC bindings, scheduled time-of-day scenes, per-artist color themes,
  custom waveform analysis. Reconsider if the rig grows or the workflow shifts.
