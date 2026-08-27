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

## Build-up behavior

Surfaced 2026-05-31 after a set — build-ups are the focus before next time.

- [ ] **(top)** Stop mode-thrash in the run-up to a drop.
  Live observation: modes flip a lot in the bars before the drop, and each
  flip re-picks a scene (visible reset on the floor). `normalize_phrases`
  in `analysis.py` already coalesces *adjacent same-mode* phrases (it merges
  on `merged[-1]["mode"] == phrase["mode"]`), so what's left is alternation
  between *different* families just before the drop — e.g.
  buildup→groove→buildup→drop, or short breakdown stabs mid-climb. Two angles
  to try (measure first with real PSSI from a few tracks): (a) collapse short
  (< N-beat) cross-mode sections that sit immediately before a `drop` into
  the surrounding build family; (b) add a min-dwell / hysteresis on the
  scene re-pick in `main.py::on_client_change` so a micro-section doesn't
  trigger a fresh `apply_mode`. Goal: one coherent build look that holds into
  the drop, not a strobe of scene changes. Don't flatten *all* variety —
  only the pre-drop thrash.

- [ ] Compounding build-ups — get crazier as the build goes on.
  We already escalate *amplitude*: `direct_lights.py` ramps buildup intensity
  `(0.30, 1.00)` across the phrase via `set_intensity_phrase`, and
  `scene_engine.py` gates strobe off below `i=0.6` then ramps it in. The ask
  is *structural* escalation, not just brighter: as `i` climbs through the
  buildup, progressively add/intensify the look — e.g. faster chase rates,
  more active zones, additional layers switching on at thresholds, or
  stepping to a wilder scene at the top third. Cleanest hook is the existing
  per-phrase intensity `i` (already 0→1 over the buildup) used as a driver
  for layer gating/rates in the renderer, so it stays BPM- and
  position-aware. Decide: escalate *within* one scene (layer/rate thresholds)
  vs. *sequence* of buildup scenes by intensity band. Prototype the
  within-scene version first — less to wire, and it composes with the
  thrash fix above.

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
