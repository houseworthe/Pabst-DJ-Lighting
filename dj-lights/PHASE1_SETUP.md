# Phase 1: Pro DJ Link Integration — Setup Guide

## What Was Built

### 1. Pro DJ Link Bridge (`prodjlink_bridge.py`)
Python bridge using `python-prodj-link` library that:
- Connects to Pro DJ Link devices on the network (XDJ-XZ)
- Creates a virtual CDJ (Player 5) to participate in the Pro DJ Link network
- Captures real-time data:
  - Track metadata (title, artist, key, BPM)
  - Beat position (1-4 within bar)
  - Phrase position (beat count from track start)
  - Play/pause state per deck
  - Loop status (engaged/disengaged, start/end times)
  - Master deck identification
  - Pitch/tempo adjustments (actual vs. physical pitch)
  - On-air status per deck
- Pushes events to lightd via Unix socket at `/tmp/lightd.sock` (newline-terminated JSON)

### 2. Extended lightd (`lightd.py`)
Modified to accept Pro DJ Link events alongside existing FFT pipeline:
- **Hybrid mode**: Pro DJ Link as primary clock source, FFT as secondary (energy/frequency analysis only)
- **Graceful fallback**: If Pro DJ Link disconnects, automatically falls back to FFT-only mode
- **New state tracking**:
  - `prodjlink_connected` - connection status
  - `prodjlink_master_deck` - which deck is master (tempo source)
  - `prodjlink_decks` - per-deck state (title, artist, BPM, key, play state, loop status, etc.)
  - `prodjlink_clock_source` - True = using Pro DJ Link for BPM/beats, False = FFT fallback
- **Event handlers**:
  - `connection` - Pro DJ Link bridge connected/disconnected
  - `track_load` - New track loaded on a deck
  - `deck_update` - Deck state changed (beat, play state, loop, etc.)
  - `master_change` - Master deck changed
- **Clock source priority**: Pro DJ Link > Manual BPM > FFT auto-detect
- **Phrase tracking**: Uses Pro DJ Link beat count from master deck to trigger scene changes

### 3. Updated Dashboard (`dashboard.py`)
New UI elements to display Pro DJ Link data:
- **Connection status bar**: Shows Pro DJ Link connected/disconnected + clock source (Pro DJ Link vs FFT Fallback)
- **Per-deck cards** (Deck 1 & 2):
  - Track title & artist
  - BPM & musical key
  - Play state (visual indicator: green border when playing, orange when master)
  - Beat position indicator (4 dots showing 1-4 within bar, current beat highlighted)
  - Loop status indicator (🔄 LOOP when engaged)
- **Clock source badge**: "Clock: Pro DJ Link" (green) or "Clock: FFT Fallback" (orange)
- **Deck state classes**:
  - `.active` - deck is playing
  - `.master` - deck is master (tempo source)
  - `.playing` - deck number turns green

## Architecture

```
XDJ-XZ (Ethernet)
        ↓ Pro DJ Link protocol
python-prodj-link (Virtual CDJ #5)
        ↓ Event JSON over Unix socket
lightd.py (lighting daemon)
├── Pro DJ Link events → BPM/beat/phrase tracking
├── FFT pipeline → energy/frequency analysis (no beat detection when Link active)
└── Scene engine → DMX + Govee output
        ↓
dashboard.py (web UI, port 8420)
```

## Dependencies

### Python Packages (in venv)
- `construct` (2.10.70+) - binary protocol parsing
- `netifaces` (0.11.0+) - network interface enumeration

Already installed in `/Users/ultron/.openclaw/workspace-main/projects/dj-lights/venv/`

### External Libraries
- **python-prodj-link**: Cloned to `python-prodj-link/` directory
  - GitHub: https://github.com/flesniak/python-prodj-link
  - Pure Python implementation of Pioneer's Pro DJ Link protocol

### Java (for beat-link fallback, optional)
- OpenJDK 25.0.2 installed via Homebrew (`/opt/homebrew/opt/openjdk/`)
- beat-link JAR download failed, but not needed for Python bridge

## Files Created/Modified

### New Files
- `prodjlink_bridge.py` - Pro DJ Link bridge (main integration point)
- `test_prodjlink.py` - Test script to verify socket protocol
- `start_prodjlink.sh` - Startup script for the bridge
- `PHASE1_SETUP.md` - This file

### Modified Files
- `lightd.py` - Added Pro DJ Link state tracking and event handlers
- `dashboard.py` - Added Pro DJ Link UI elements (deck cards, connection status)

## How to Test (Without Ethernet)

Since the Ethernet cable hasn't arrived yet, you can:

### 1. Test the Socket Protocol
```bash
cd /Users/ultron/.openclaw/workspace-main/projects/dj-lights

# Terminal 1: Start lightd
python3 lightd.py

# Terminal 2: Run the test script
python3 test_prodjlink.py
```

This will:
- Send simulated Pro DJ Link events to lightd
- Verify the event handling works
- Check that lightd correctly updates its state

### 2. Test the Dashboard Rendering
```bash
# Start lightd + dashboard
python3 lightd.py &
python3 dashboard.py &

# Open dashboard in browser
open http://localhost:8420

# Run test events
python3 test_prodjlink.py
```

Watch the dashboard update with:
- Pro DJ Link connection status
- Deck cards showing track info
- Beat position indicators
- Clock source badge

## Tomorrow (When Ethernet Cable Arrives)

### Setup Steps:
1. **Connect XDJ-XZ to Mac mini**:
   - Plug Ethernet cable into XDJ-XZ "Extension" port
   - Plug other end into Mac mini Ethernet port
   - XDJ-XZ and Mac should be on the same network (Pro DJ Link network is auto-discovered)

2. **Start the Stack**:
   ```bash
   cd /Users/ultron/.openclaw/workspace-main/projects/dj-lights
   
   # Start lightd (in background or separate terminal)
   python3 lightd.py &
   
   # Start dashboard (in background or separate terminal)
   python3 dashboard.py &
   
   # Start Pro DJ Link bridge
   ./start_prodjlink.sh
   # or manually:
   # source venv/bin/activate
   # python3 prodjlink_bridge.py
   ```

3. **Open Dashboard**:
   ```bash
   open http://localhost:8420
   ```

4. **Load a Track on XDJ-XZ**:
   - Load any track on Deck 1 or Deck 2
   - Press play
   - Dashboard should immediately show:
     - "Pro DJ Link: Connected"
     - Track title, artist, BPM, key
     - Beat position updating in real-time
     - Master deck indicator (orange border)

5. **Test Features**:
   - **Beat sync**: Dashboard beat pips should match XDJ-XZ display perfectly (no drift)
   - **Loop detection**: Engage a loop on the XDJ-XZ → dashboard shows "🔄 LOOP"
   - **Hot cue jumps**: Jump to a hot cue → lightd logs the position change
   - **Master deck switching**: Change master deck → dashboard updates master indicator
   - **Disconnection fallback**: Unplug Ethernet → dashboard shows "Clock: FFT Fallback"

### Troubleshooting:
- **Bridge can't find Pro DJ Link devices**:
  - Make sure XDJ-XZ is on and Ethernet cable is connected
  - Check Mac network settings (Ethernet should be active)
  - Pro DJ Link uses UDP multicast for discovery (firewall shouldn't block it)
  
- **Events not reaching lightd**:
  - Check that lightd socket exists: `ls -la /tmp/lightd.sock`
  - Test with: `echo "status" | nc -U /tmp/lightd.sock`
  
- **Dashboard not updating**:
  - Check browser console for errors
  - Verify dashboard is polling: Network tab should show `/api/status` requests every 50ms

## Event Protocol (JSON over Unix Socket)

### Connection Event
```json
{
  "source": "prodjlink",
  "type": "connection",
  "timestamp": 1234567890.123,
  "status": "connected",
  "vcdj_number": 5
}
```

### Track Load Event
```json
{
  "source": "prodjlink",
  "type": "track_load",
  "timestamp": 1234567890.123,
  "deck": 1,
  "title": "Losing It",
  "artist": "Fisher",
  "album": "",
  "bpm": 128.0,
  "key": "Am",
  "duration": 210
}
```

### Deck Update Event
```json
{
  "source": "prodjlink",
  "type": "deck_update",
  "timestamp": 1234567890.123,
  "deck": 1,
  "changes": {
    "play_state": "playing",
    "beat": 1,
    "beat_count": 64,
    "loop_active": true
  },
  "state": {
    "bpm": 128.0,
    "beat": 1,
    "beat_count": 64,
    "play_state": "playing",
    "pitch": 1.0,
    "actual_pitch": 1.0,
    "key": "Am",
    "loop_active": true,
    "on_air": true,
    "is_master": true,
    "title": "Losing It",
    "artist": "Fisher"
  }
}
```

### Master Change Event
```json
{
  "source": "prodjlink",
  "type": "master_change",
  "timestamp": 1234567890.123,
  "master_deck": 1,
  "bpm": 128.0
}
```

## Status Query Response (Extended)

```bash
echo "status" | nc -U /tmp/lightd.sock
```

Response includes new `prodjlink` object:
```json
{
  "mode": "auto",
  "scene": "Ocean Drift",
  "category": "groove",
  "energy": 65.3,
  "bpm": 128.0,
  "bpm_source": "prodjlink",
  "prodjlink": {
    "connected": true,
    "clock_source": true,
    "master_deck": 1,
    "decks": {
      "1": {
        "title": "Losing It",
        "artist": "Fisher",
        "bpm": 128.0,
        "key": "Am",
        "beat": 1,
        "beat_count": 256,
        "play_state": "playing",
        "loop_active": false,
        "is_master": true,
        "on_air": true
      },
      "2": {
        "title": "Another Track",
        "artist": "Artist Name",
        ...
      }
    }
  }
}
```

## Next Steps (Phase 2+)

### Phase 2: On-Load Track Analysis
- When a track loads, trigger background analysis (librosa, madmom)
- Detect sections (intro, breakdown, buildup, drop, outro)
- Cache analysis in SQLite
- Vibe vector generation (energy, fullness, darkness, drive, etc.)

### Phase 3: Full Track Sequencing
- Pre-generate entire lighting sequence on track load
- Track color identity (theme per track, changes on track transition)
- Arc planning (escalation across multiple drops)
- Audio-reactive intensity layer (energy envelope modulation)

### Phase 4: Live DJ Override Detection
- Loop escalation (intensity ramps on each loop cycle)
- Hot cue jump handling (instant scene change to new section)
- EQ kill detection (FFT sees bass dropout)
- Pitch bend handling

### Phase 5: DJ Sam Integration
- Agent-controlled track selection
- Autonomous lighting with zero human input
- Live style switching (voice command: "go dark," "festival mode," etc.)

## Known Limitations

### Current Implementation
- **No XDJ-XZ testing yet**: Can't verify actual Pro DJ Link connection until Ethernet cable arrives tomorrow
- **beat-link JAR not downloaded**: Java fallback not available, but Python bridge is primary anyway
- **No track analysis yet**: Phase 2 feature (on-load background analysis)
- **No pre-sequencing**: Phase 3 feature (full track light sequences)

### python-prodj-link Library
- **Not officially tested with XDJ-XZ**: Library documentation doesn't explicitly mention XDJ-XZ, but Pro DJ Link is Pioneer's standard protocol (should work)
- **Metadata requests**: May need to poll for track metadata (handled in bridge's monitor_decks thread)

## Success Criteria (Phase 1)

✅ **Code Complete**:
- [x] Pro DJ Link bridge implemented (Python)
- [x] lightd extended to accept Pro DJ Link events
- [x] Dashboard updated to display Pro DJ Link data
- [x] Socket protocol tested locally
- [x] All Python code syntax-checked

⏳ **Testing Required** (Tomorrow):
- [ ] Actual Pro DJ Link connection to XDJ-XZ
- [ ] Beat sync verification (zero drift over 30+ min set)
- [ ] Track metadata retrieval
- [ ] Loop detection
- [ ] Master deck switching
- [ ] Disconnect/reconnect graceful fallback

## Appendix: python-prodj-link Client Data Structure

From `python-prodj-link/prodj/core/clientlist.py`, each client provides:

```python
client.player_number      # 1-4 (real decks) or 5 (virtual CDJ)
client.model             # "XDJ-XZ", "CDJ-2000nexus", etc.
client.ip_addr           # IP address
client.mac_addr          # MAC address

# Track info
client.track_id          # Rekordbox track ID
client.loaded_player_number  # Which player the track came from
client.loaded_slot       # "usb" or "sd"
client.metadata          # {title, artist, album, bpm, duration, artwork_id}

# Playback state
client.bpm               # Current BPM (float)
client.pitch             # Physical pitch fader position (1.0 = no adjustment)
client.actual_pitch      # Actual pitch including master tempo (1.0 = no adjustment)
client.beat              # Current beat (1-4, or other values depending on device)
client.beat_count        # Total beats from track start
client.play_state        # "playing", "paused", "cued", "cueing"
client.position          # Playhead position in seconds (if beatgrid available)

# Loop state
client.loop_start        # Loop start time in seconds (None if no loop)
client.loop_end          # Loop end time in seconds (None if no loop)
client.whole_loop_length # Loop length in beats

# Musical key
client.key               # Musical key (e.g., "Am", "C#", etc.)
client.key_shift         # Key shift amount

# Network state
client.on_air            # True if channel is on-air (from DJM mixer)
client.state             # ['on_air', 'sync', 'master', 'play'] - list of active flags
```

## Support

If issues arise tomorrow during testing:
1. Check logs: `tail -f` on lightd output
2. Verify socket: `echo "status" | nc -U /tmp/lightd.sock | jq`
3. Check network: `ifconfig` (Ethernet should have an IP)
4. Pro DJ Link discovery: Bridge logs should show "New Player X" messages
5. Dashboard console: Open browser DevTools, check for JS errors

Good luck! 🎛️✨
