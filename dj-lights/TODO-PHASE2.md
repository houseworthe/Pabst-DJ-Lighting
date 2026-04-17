# DJ Lights Phase 2 — TODO

## What's Working (as of 2026-04-03)
- XDJ-XZ Pro DJ Link connection (rekordbox announcement + mixer status trick)
- Both decks reporting status, beats, metadata, play states
- Color waveform download working (had to fix recv buffer to handle 134KB blobs)
- Phrase-aware section detection using color waveform frequency band heights
- Dashboard showing waveform + sections + moving playhead
- Section changes triggering scene switches in lightd
- Scene pools mapped to real scene names from scenes_v2.py

## Priority Fixes Needed

### 1. Section timing offset
Sections don't hit exactly on the musical beat. The 32-beat chunks start from beat 0 
but the track's actual phrase structure might start at a different offset. 
Need to detect the first downbeat and offset all section boundaries accordingly.

**Possible fix:** Use the beatgrid data — rekordbox beatgrids include bar markers 
that tell you where phrases start. Align chunk boundaries to bar 1.

### 2. Color waveform byte format verification  
Current assumption: bytes [0,2,4] = low/mid/high heights, [1,3,5] = colors
Need to verify against pyrekordbox or Deep Symmetry docs. Might be:
- [red, green, blue, height_low, height_mid, height_high]
- Or some other layout
Getting this right improves section detection accuracy.

### 3. Master deck detection
XDJ-XZ master flag not always propagating. Dashboard should follow 
whichever deck is actively playing (has beat updates), not just the CDJ master flag.

### 4. Track switch smoothness
When loading a new track, the dashboard should immediately show the new waveform.
Currently there can be a delay while analysis runs.

## Files Changed Today
- `python-prodj-link/prodj/core/clientlist.py` — all-in-one XDJ-XZ dual-deck support
- `python-prodj-link/prodj/core/vcdj.py` — rekordbox announcement + mixer status packets
- `python-prodj-link/prodj/core/prodj.py` — SO_BROADCAST on status socket, debug logging
- `python-prodj-link/prodj/data/dbclient.py` — color waveform recv buffer fix (524KB, 65K chunks)
- `prodjlink_bridge.py` — phrase-aware section detection, color waveform height extraction, fixed scene pools
- `lightd.py` — socket recv buffer fix, master deck section tracking
- `dashboard.py` — socket recv buffer fix, playhead from master deck beat_count, track change detection
