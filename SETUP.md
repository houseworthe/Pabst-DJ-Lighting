# dj-lighting-ultron — local setup

Set up on a fresh Mac (Apple Silicon).

## System dependencies

```bash
brew install libftdi portaudio
```

- `libftdi` → `/opt/homebrew/lib/libftdi1.dylib` (hard-coded in `dj-lights/dmx_controller.py`)
- `portaudio` → required by `pyaudio` and `sounddevice` (Scarlett 2i2 input)

## Python environment

Uses a project-local venv at `.venv/` on Python 3.13 (Python 3.14 lacks wheels for
`pyaudio` / `netifaces`).

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Verifying

```bash
.venv/bin/python -c "
import sys, os
sys.path.insert(0, os.path.abspath('dj-lights/python-prodj-link'))
sys.path.insert(0, os.path.abspath('dj-lights'))
sys.path.insert(0, os.path.abspath('dj-lights-mvp2'))
import numpy, sounddevice, pyaudio, netifaces, construct
import direct_lights, runtime, bridge, analysis, scenes
from prodj.core.prodj import ProDj
print('ok')
"
```

## Hardware not yet present on this machine

- Enttec Open DMX USB (`/dev/cu.usbserial-*`) — plug in before starting `lightd` or MVP2
- Scarlett 2i2 — needed for the audio-reactive daemon (`dj-lights/lightd.py`)
- XDJ-XZ on the Pro DJ Link ethernet subnet — needed for `live_bridge.py`

## Govee

Credentials live at `~/.config/govee/credentials.json`:

```json
{"api_key": "..."}
```

MVP2 talks to Govee in-process via `dj-lights-mvp2/govee_lan.py` — LAN UDP
(~10ms) whenever a device has a cached LAN IP, cloud API (~200ms) as fallback.

The `~/.local/bin/govee` CLI is a thin wrapper over the same module for manual
use:

```bash
govee refresh      # re-scan LAN, refresh device cache
govee devices      # show devices and route (LAN/cloud)
govee color #ff00aa --brightness 60
govee warm
govee off
```

Enable LAN Control per device in the Govee app, then run `govee refresh` while
on the same WiFi as the devices.

## Running

Always activate the venv (or use `.venv/bin/python` directly):

```bash
source .venv/bin/activate
```

MVP2 stack (single process — ProDJ Link + scene driver):

```bash
python dj-lights-mvp2/main.py
```

Full `lightd` stack (original):

```bash
python dj-lights/lightd.py           # audio-reactive daemon (needs Scarlett + DMX)
python dj-lights/dashboard.py        # :8420 — UI
python dj-lights/prodjlink_bridge.py # ProDJ Link ingest
```
