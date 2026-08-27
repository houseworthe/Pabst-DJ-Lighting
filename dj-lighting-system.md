# DJ Lighting System

## Equipment
- **Controller:** Pioneer XDJ-XZ
- **DMX Interface:** Enttec Open DMX USB 70303 (serial: `/dev/cu.usbserial-BG01THVM`)
- **DMX Fixtures:** 2x Venue Tetra 12 (RGBA wash), 1x Venue Tetra Bar (RGBA 4-zone strip)
- **Govee:** 4x COB strips (H61E5), 4x bulbs (H6010)
- **Audio Input:** Scarlett 2i2 Input 2 (channel index 1)

## DMX Chain
```
Laptop USB → Enttec Open DMX (5-pin) → 5-to-3 adapter → Tetra 12 #1 (d.001)
Tetra 12 #1 DMX OUT → Tetra 12 #2 (d.007)
Tetra 12 #2 DMX OUT → Tetra Bar (d.013, last in chain)
```

**Each fixture now has its own DMX address** (was previously both 12s mirrored on d.001).
The two Tetra 12s can be controlled independently. Addresses are spaced 6 apart so the
6-ch fixtures don't overlap; the 24-ch Bar is last so its channels 13-36 don't collide.

Manuals: `~/Documents/Personal-Projects/dj-lighting-ultron/Hardware Manuals/`
(Venue Tetra 12, Tetra Bar, Venue Dongle).

## DMX Addressing
| Fixture | Address | Mode | Channels |
|---------|---------|------|----------|
| Tetra 12 #1 | d.001 | 6-ch | 1-6 (R,G,B,A,Dimmer,Strobe) |
| Tetra 12 #2 | d.007 | 6-ch | 7-12 (R,G,B,A,Dimmer,Strobe) |
| Tetra Bar | d.013 | 24-ch | 13-36 (4 zones × 6ch) |

Set in `dmx_controller.py`: `TETRA12_ADDRS = [1, 7]`, `TETRA_BAR_ADDR = 13`.

### Tetra 12 Channel Map (6-ch)
| Offset | Function | Notes |
|--------|----------|-------|
| 0 | Red | 0-255 |
| 1 | Green | 0-255 |
| 2 | Blue | 0-255 |
| 3 | Amber | 0-255 |
| 4 | Dimmer | 0-127 = dimmer, 128-227 = hardware strobe (slow→fast), 228-255 = full on |
| 5 | Strobe | 0-255 |

### Tetra Bar Channel Map (24-ch)
4 zones × 6 channels each (R, G, B, Amber, Dimmer, Strobe). Zone 1 = ch 1-6, Zone 2 = ch 7-12, etc.

## Software Stack

### Core Files (all in `projects/dj-lights/`)
| File | Purpose |
|------|---------|
| `lightd.py` | **Main daemon** — audio-reactive DMX + Govee, Unix socket control |
| `lightctl` | **CLI wrapper** (bash) — linked to `~/.local/bin/lightctl` |
| `dashboard.py` | **Live web dashboard** — port 8420, polls daemon at 20fps |
| `dmx_controller.py` | **DMX driver** — libftdi (`/opt/homebrew/lib/libftdi1.dylib`), FTDI break via baud-rate switching |
| `scenes_v2.py` | **31 scenes** across 7 categories with smooth fades, rolling waves, breathing, hardware strobe |
| `strobe_modes.py` | Strobe patterns (chase/bounce/random/split/wave/buildup/all) |

### CLI Commands
```bash
lightctl start          # start daemon (or use launchd)
lightctl stop           # stop daemon
lightctl status         # JSON status dump
lightctl scene <name>   # force a specific scene
lightctl scenes         # list all scenes
lightctl strobe         # trigger strobe
lightctl blackout       # all lights off
lightctl warm           # warm amber preset
lightctl brightness <n> # set brightness
lightctl govee on/off   # toggle Govee integration
lightctl quit           # shutdown daemon
```

### Starting/Stopping
```bash
# Manual
python3 projects/dj-lights/lightd.py &
python3 projects/dj-lights/dashboard.py &

# launchd (RunAtLoad=false)
launchctl load ~/Library/LaunchAgents/com.ultron.lightd.plist
launchctl start com.ultron.lightd

# Dashboard
open http://localhost:8420
```

### Socket
Unix socket at `/tmp/lightd.sock`. Send newline-terminated commands, get JSON responses.

## How lightd Works

### Audio Analysis
- Captures from Scarlett Input 2 via sounddevice
- FFT → energy bands (sub, kick, mid, high)
- Beat detection via kick energy peaks
- BPM calculated from beat intervals (15% median filter for outlier rejection)
- Adaptive energy normalization: 98th percentile of 2-min rolling window

### 4+1 Category Model (House Music)
Simplified from 6 categories to match real track structure:

| Category | Detection | DMX Scenes | Govee |
|----------|-----------|------------|-------|
| **Ambient** | Energy < 30, no kick | 4 scenes (slow, atmospheric) | Calm both-device (0-4), brightness 3% |
| **Breakdown** | Kick drops out (ratio < 50% for 3s) | 5 scenes (minimal, dark) | Bulbs only (10-12), brightness 1% |
| **Buildup** | After breakdown, before drop | 4 scenes (accelerating, ramping) | Both-device (0-3), brightness 5-8% |
| **Groove** | Energy ≥ 30, kick present | 12 scenes (beat-synced, active) | Full range + music reactive, brightness 5% |
| **Drop** | Kick returns after breakdown | 4 scenes + 6 inline modes | Intense scenes (3,4,7,9), brightness 15% |

- **Groove** is the default state — kick + bass running, the body of the track
- **Buildup** and **Drop** are structure-detected (kick disappears → reappears), not energy-based
- **Ambient** is for idle/transitions/low energy
- Each scene handles BOTH DMX and Govee via `govee_cmd` callback

### Scene Selection
- Phrase-based evaluation: every 32 detected beats (~8 bars)
- Variety swap every 4 phrases within same category
- Scenes run in `while True` loops, interrupted by `StoppableDMX` proxy

### Structure Detection (Breakdown → Buildup → Drop)
- Tracks kick band energy vs 75th percentile baseline
- **Breakdown**: kick ratio < 50% for 3 consecutive seconds → breakdown scene + Govee dims
- **Buildup**: automatically triggered after breakdown detected — accelerating DMX patterns
- **Drop**: kick ratio returns > 80% OR energy > 35 (after confirmed breakdown + 2s min duration)
- 15-second warmup guard prevents false triggers on startup
- Drop check runs on **every audio frame** for instant response

### Drop Sequences
- `_fire_drop()` stops current scene, spawns dedicated thread owning DMX for 8 bars
- 6 inline modes (randomly picked): machine_gun, color_cannon, ping_pong, scatter_blast, split_strobe, blackout_kicks
- All use movement + blackout gaps (no static strobe)
- Govee picks from GOVEE_DROP scenes at 15% brightness
- After 32 beats, `_end_drop_sequence()` transitions to groove

### Brightness Scaling (50% max)
| Category | Dimmer Cap |
|----------|-----------|
| Ambient | 20 |
| Breakdown | 15 |
| Buildup | 50 |
| Groove | 55 |
| Drop | 64 |

### Govee Integration
- Each scene sets its own Govee state via `govee_cmd` callback (no centralized rotation)
- `_govee_cmd()` — serialized command runner with busy lock (prevents overlapping API calls)
- Scene categories have curated Govee scene pools (GOVEE_AMBIENT, GOVEE_BREAKDOWN, etc.)
- Uses `govee scene <N>` (single-shot), NOT `govee dj` (blocking loop)

## Key Technical Constraints
- **libftdi required** — Enttec Open DMX needs proper FTDI break timing; pyserial unreliable on macOS
- **Skip `ftdi_usb_close()` on shutdown** — causes SIGSEGV on macOS; let OS clean up
- **Never call `govee dj` from lightd** — it's a blocking infinite loop
- **Govee API calls must be serialized** — overlapping calls reset lights to white
- **No breakdown → no drop** — drop can only fire after confirmed breakdown

## DMX Abstraction (dmx_controller.py)
```python
dmx.set_12s(r, g, b, a, dim)           # both Tetra 12s
dmx.set_bar_zone(zone, r, g, b, a, dim) # single bar zone (0-3)
dmx.set_all(r, g, b, a, dim)           # all fixtures
dmx.send_frame()                        # transmit 512-byte universe
```

## Future
- Pro DJ Link integration (Phase 2) — read BPM/phrase/track from XDJ-XZ via ethernet
- DJ Sam agent handoff (Phase 5) — agent controls lighting decisions
- Custom wake word for voice control

## Known Issue: Fixtures Hold State
DMX fixtures (Tetras + Bar) latch their last received values. When lightd stops without sending a blackout, or if the USB connection drops, lights stay on. Sending blackout frames after the fact may not work if the FTDI device lost its connection. **Fix: power cycle the fixtures** (switch on back of each unit).

`lightd` sends a blackout frame on shutdown then skips `ftdi_usb_close()` (segfaults/glitches on macOS). The standalone `dmx_controller.py` now does the same — its `close()` is a no-op that lets the OS reclaim the USB handle. This fixed the fixtures "tweaking" (flickering erratically) at the end of `dmx_controller.py test`.
