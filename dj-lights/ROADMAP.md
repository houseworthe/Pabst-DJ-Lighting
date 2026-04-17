# DJ Lighting System Roadmap

## Current State (v1.0, Live)

**5,400 lines across 12 files in `projects/dj-lights/`**

### What's Built
* **lightd.py** (1,127 lines): Main daemon. Audio reactive DMX + Govee control via Unix socket
* **scenes_v2.py** (812 lines): 31 scenes across 7 categories with smooth fades, rolling waves, breathing, hardware strobe
* **dashboard.py** (599 lines): Live web UI on port 8420
* **dmx_controller.py**: libftdi driver with FTDI break via baud rate switching
* **lightctl**: CLI for start/stop/scene/strobe/blackout commands

### How It Works Today
* FFT audio analysis from Scarlett 2i2 Input 2 → beat detection → BPM tracking
* 4+1 category model: Ambient, Breakdown, Buildup, Groove, Drop
* Structure detection: kick energy tracking, breakdown detection (kick drops for 3s), buildup detection, drop detection (kick returns)
* Scene selection is phrase based (every 32 beats / ~8 bars), with variety swaps every 4 phrases
* Drop sequences: dedicated 8 bar takeovers with 6 inline modes (machine_gun, color_cannon, ping_pong, etc.)
* Govee integration: 4x COB strips + 4x bulbs via HTTP API, serialized per scene

### Hardware
| Device | Type | Address | Connection |
|--------|------|---------|------------|
| Enttec Open DMX USB | DMX Interface | N/A | USB to Mac mini |
| 2x Venue Tetra 12 | RGBA Wash | d.001 | DMX daisy chain |
| 1x Venue Tetra Bar | 4 zone RGBA Strip | d.008 | DMX daisy chain |
| 4x Govee COB Strips | RGB LED Strips | N/A | WiFi / HTTP API |
| 4x Govee Bulbs | RGB Bulbs | N/A | WiFi / HTTP API |
| Scarlett 2i2 | Audio Interface | Input 2 | USB to Mac mini |

---

## How It's Used

The dashboard (port 8420) is the control surface. During a set, the workflow is:

1. DJ behind the XDJ-XZ, dashboard open on a screen or phone nearby
2. Play tracks, mix, loop, do your thing
3. Watch the dashboard to monitor what the lights are doing, what section lightd thinks it's in, what scenes are active
4. Yell at Ultron when something needs to change ("go dark," "kill the strobe," "more red")
5. Ultron adjusts via voice command or dashboard override

Every phase of this roadmap includes dashboard updates because the dashboard is how you stay in the loop without taking your hands off the decks. It's the feedback mechanism for the entire system.

---

**Target: Week of March 31, 2026**

### Goal
Connect the XDJ-XZ to the Mac mini via Ethernet and get real time track metadata, BPM, beat position, and playback state flowing into lightd.

### Hardware Needed
* ✅ Jadaol CAT6 50ft flat Ethernet cable (XDJ-XZ "Extension" port → Mac mini Ethernet) — PURCHASED
* Optional: 5 port gigabit switch if Mac mini needs wired internet simultaneously (otherwise use WiFi for internet, dedicate Ethernet to DJ Link)

### Software Tasks
1. **Install beat-link-trigger** on the Mac mini (Java, confirmed working with XDJ-XZ since v0.6.0)
2. **Build a bridge process** that consumes Pro DJ Link data and pushes it to lightd via the existing Unix socket protocol
   * Track metadata (title, artist, key, BPM)
   * Beat position (beat within bar: 1, 2, 3, 4)
   * Phrase position (beat count from track start)
   * Play/pause state per deck
   * Loop engaged/disengaged
   * Master deck identification
   * Pitch/tempo adjustments
3. **Extend lightd.py** to accept Pro DJ Link events alongside existing FFT pipeline
   * Pro DJ Link becomes the primary clock source (exact BPM, no drift)
   * FFT pipeline becomes secondary: energy/frequency analysis only, no longer responsible for beat detection or BPM tracking
   * Hybrid mode: if Ethernet disconnects, gracefully fall back to FFT only

### Validation
* lightd correctly receives and logs track changes on both decks
* Beat counter in lightd is locked to Pro DJ Link (zero drift over a 30 minute set)
* Dashboard displays current track info, BPM, and beat position
* Loop detection works: engaging a loop on XDJ-XZ holds the current lighting state

### Bridge Technology: beat-link (Java) ✅ CONFIRMED
Using **beat-link** (Java) as a standalone bridge process. It is the only stack explicitly confirmed working with the XDJ-XZ (since v0.6.0, built from Teo Tormo's packet captures and testing). Outputs events over WebSocket or local Unix socket to lightd. If it works well, consider porting the protocol handling to Python later.

### Dashboard Updates (Phase 1)
* Display Pro DJ Link connection status (connected/disconnected, device name)
* Show per deck info: track title, artist, BPM, key, play state
* Beat position indicator (1, 2, 3, 4 within bar) synced to Pro DJ Link clock
* Phrase counter (beat count from track start)
* Loop status indicator per deck
* Clock source indicator: "Pro DJ Link" vs "FFT Fallback"

---

## Phase 2: On-Load Track Analysis

**Target: Week 2**

### Goal
Every time a track loads onto a deck, automatically run a full analysis in the background. By the time the track comes in, lightd has a complete structure map, section boundaries, and vibe profile ready to drive the lighting. Cache results so repeat loads are instant.

### Why On-Load (Not Pre-Analysis)
* You always have 1 to 3 minutes between loading a track and mixing it in. Analysis takes 3 to 5 seconds (without stems) or 15 to 30 seconds (with stems). Plenty of time.
* Works with any track from any source: DJ Sam library, new Beatport downloads, a random USB someone hands you at a party. No prep required.
* No maintenance: no batch jobs to run, no sync issues, no "forgot to analyze that one."
* Cached in SQLite: first load analyzes, every subsequent load is instant lookup.

### Analysis Stack (v1: No Stem Separation)
* **librosa**: Spectral centroid (brightness), RMS energy contours, onset strength, chroma features, spectral bandwidth/rolloff, segment recurrence matrix for structure detection
* **madmom**: Beat and downbeat tracking, tempo estimation
* **essentia**: Mood/genre classification, danceability, energy descriptors (stretch goal)

Stem separation via demucs is deferred to a later phase. Spectral analysis alone provides ~80% of the vibe information needed for intelligent scene selection.

### Pipeline Steps (triggered on Pro DJ Link track load event)
1. Pro DJ Link reports track loaded on deck N (title, artist, BPM, key)
2. Check SQLite cache: if track exists, load analysis instantly and skip to step 11
3. Locate audio file (from USB mount or rekordbox export path)
4. Run librosa spectral analysis: centroid, RMS, onset strength, bandwidth, chroma
5. Run madmom beat/downbeat detection
6. Compute per-beat energy envelope (RMS value for every beat of the track)
7. Detect silence/near-silence regions (energy < 0.05) and blackout_slam events (silence → immediate energy spike)
8. Detect isolated element regions (only 1 to 2 frequency bands active: risers, vocal hits, snare rolls)
9. Build recurrence matrix → identify section boundaries via spectral clustering
10. Classify each section: intro, breakdown, buildup, drop, outro
11. Compute vibe vector per section
12. Store in SQLite, keyed by track title + artist
13. Trigger full sequence generation (Phase 3): assign color theme, plan scenes for every section, map energy envelope
14. Push complete sequence to lightd → lightd is ready before the track comes in

### Storage Schema
```
tracks: id, title, artist, key, bpm, duration, analyzed_at
sections: id, track_id, type (intro/breakdown/buildup/drop/etc), start_beat, end_beat, start_time, end_time
section_vibes: section_id, energy, fullness, darkness, drive, texture, space, vocal_presence, harmonic_complexity
```

### Timing Budget
| Step | Duration | Notes |
|------|----------|-------|
| SQLite cache hit | <10ms | Instant for repeat loads (includes cached sequence) |
| librosa spectral analysis | 2 to 3 seconds | Full track, all features |
| madmom beat detection | 1 to 2 seconds | Downbeat + tempo |
| Structure segmentation | <1 second | Clustering on recurrence matrix |
| Vibe vector computation | <100ms | Math on existing features |
| Full sequence generation | <200ms | Scene selection for all sections (Phase 3) |
| **Total (cache miss)** | **3 to 6 seconds** | Well within the DJ's prep window |

### Dashboard Updates (Phase 2)
* Display analysis status per deck: "Analyzing..." → "Ready" with progress indicator
* Show detected structure map as a horizontal timeline bar (color coded sections: blue=intro, purple=breakdown, orange=buildup, red=drop, gray=outro)
* Display vibe vector as a radar/spider chart or simple bar graph per section
* Show cache hit/miss status ("Cached" vs "Fresh analysis")

---

## Phase 3: Full Track Light Sequencing

**Target: Week 3**

### Goal
When a track loads and analysis completes, generate the entire lighting sequence for the full track upfront. Each track gets its own color identity. The sequence is planned as one cohesive arc, not decided section by section. The vibe vector filters the scene pool for each section, but the final pick within that pool is random with variety enforcement.

### Track Color Identity
Every track gets assigned a color theme on load. This is the visual signature of the track.

* **Theme assignment**: Pick a primary and secondary color from a palette pool. The palette pool contains ~10 to 15 distinct themes (magenta/purple, cyan/orange, red/gold, blue/white, green/amber, etc.)
* **Conflict avoidance**: Whatever theme the other deck's current track is using gets excluded. This means when you transition between tracks, the audience sees a clear color shift.
* **Theme persistence**: If the same track loads again later in the set, it gets the same theme (cached). This creates visual consistency ("oh that's the magenta track again").
* **Accent color**: Each theme includes a neutral accent (white, warm white, or pale version of primary) for strobes and highlights.

### Vibe Vector (per section)
| Dimension | Source | Range | What It Means |
|-----------|--------|-------|---------------|
| Energy | RMS of full mix | 0.0 to 1.0 | Overall loudness/intensity |
| Fullness | Count of active frequency bands | 0.0 to 1.0 | How many layers are playing |
| Darkness | Inverse spectral centroid | 0.0 to 1.0 | Low = bright/airy, high = dark/heavy |
| Drive | Kick + bass stem energy ratio | 0.0 to 1.0 | Rhythmic propulsion |
| Texture | Synth stem spectral complexity | 0.0 to 1.0 | Simple pad vs layered synths |
| Space | Stereo width + reverb estimation | 0.0 to 1.0 | Intimate vs expansive |
| Vocal Presence | Vocal stem energy | 0.0 to 1.0 | Instrumental vs vocal heavy |
| Harmonic Complexity | Chroma feature variance | 0.0 to 1.0 | Simple chord vs complex harmony |

### Filtered Random Pool (Scene Selection)
The vibe vector is a **filter**, not a selector. It eliminates scenes that would be wrong, then picks randomly from what's left.

1. Start with the full scene pool (31 built-in scenes + any generated scenes)
2. **Vibe filter**: Remove scenes that don't fit the section character
   * Low energy section → eliminate strobe heavy scenes, fast chases
   * High energy drop → eliminate slow breathing, ambient fades
   * Breakdown → eliminate anything with strobes
3. **Color constraint**: Remaining scenes get re-colored to match the track's assigned theme
4. **Recency filter**: Remove any scene that was used in the last 3 sections of this track
5. **Adjacent variety**: If the previous section used a movement pattern (e.g. rolling wave), prefer a different pattern
6. Pick randomly from the surviving pool

### Full Track Sequence Generation
After analysis completes, walk the entire structure map and generate the full sequence:

```python
{
    "track": "Losing It",
    "artist": "Fisher",
    "color_theme": {
        "primary": [255, 0, 80],    # hot magenta
        "secondary": [180, 0, 255], # purple
        "accent": [255, 255, 255]   # white
    },
    "sequence": [
        {
            "section": "intro",
            "start_beat": 0, "end_beat": 64,
            "scene": "slow_breathe",
            "movement": "breathing",
            "intensity": 0.3
        },
        {
            "section": "buildup_1",
            "start_beat": 64, "end_beat": 128,
            "scene": "rising_sweep",
            "movement": "upward_chase",
            "intensity_curve": "linear_ramp"
        },
        {
            "section": "drop_1",
            "start_beat": 128, "end_beat": 256,
            "scene": "color_cannon",
            "movement": "ping_pong",
            "intensity": 1.0,
            "strobe_on_downbeat": true
        },
        {
            "section": "breakdown_1",
            "start_beat": 256, "end_beat": 320,
            "scene": "slow_drift",
            "movement": "color_fade",
            "intensity": 0.4
        },
        {
            "section": "drop_2",
            "start_beat": 384, "end_beat": 512,
            "scene": "machine_gun",
            "movement": "rapid_alternate",
            "intensity": 1.0,
            "strobe_on_downbeat": true,
            "escalation": "more_intense_than_drop_1"
        }
    ]
}
```

### Arc Planning
Because the full track is visible, the sequence generator can plan an arc:

* **Escalation**: If a track has multiple drops, each successive drop gets a more intense scene (faster movement, more zones active, heavier strobe)
* **Contrast**: Breakdowns are designed to contrast with the surrounding drops. If the drop is fast and bright, the breakdown should be slow and dark.
* **Drift**: Long breakdowns (16+ bars) get a slow color drift within the track's theme rather than holding static
* **Intro/Outro**: Low intensity, single fixture focus, gradual fade in/out

### Audio-Reactive Intensity Layer
The pre-analyzed waveform gives us an **energy envelope** for the entire track. This is a per-beat (or sub-beat) intensity value that modulates the overall brightness and activity of whatever scene is active. The scene defines what the lights do. The energy envelope defines how much.

#### How It Works
During analysis, compute an RMS energy value for every beat of the track. Store this as a float array alongside the section map. This becomes the "energy envelope" that lightd follows during playback.

```python
"energy_envelope": {
    "resolution": "per_beat",
    "values": [0.0, 0.0, 0.1, 0.15, 0.2, ..., 0.0, 0.0, 1.0, 1.0, ...]
    #          ^^^^ silence before drop ^^^^    ^^^^^^^^^^^^ drop hits
}
```

#### Silence and Near-Silence
* If the energy at the current beat is below a threshold (e.g. < 0.05), lights go to blackout or deep dim
* This happens automatically. No special scene needed. The energy envelope just drives brightness to zero.
* The classic "audio cuts out for 2 beats before the drop" creates an automatic blackout → slam on. The lights react to the absence of sound, not just the presence of it.

#### Isolated Elements (Risers, Vocal Hits, FX)
Pre-analysis identifies sections where only 1 to 2 frequency bands are active (e.g. just a high frequency riser, or just a snare roll). These sections get tagged with a special mode:

* **"follow_audio" mode**: Instead of running a scene at a fixed intensity, the lights directly track the real time FFT amplitude. As a riser sweeps up, brightness ramps up with it. A single clap hit creates a single light flash. The lights become a visual representation of the isolated sound.
* **Frequency-to-color mapping**: When in follow_audio mode, the dominant frequency band maps to a color. Low rumble = deep red/amber. Mid synth = track theme primary color. High riser = white/bright accent.

#### Energy Envelope Integration
The energy envelope sits between the scene engine and the DMX output:

```
Scene (color, movement, pattern)
        ↓
Energy Envelope (per-beat intensity multiplier from waveform)
        ↓
Real-time FFT (sub-beat energy for follow_audio sections)
        ↓
Final DMX values
```

In normal sections (drops, grooves), the energy envelope keeps things at ~0.7 to 1.0 and the scene runs as designed. In breakdowns and transitions, the energy envelope naturally pulls intensity down. In silence moments, it hits zero. In follow_audio sections, the real-time FFT takes over completely.

#### Pre-Drop Blackout Detection
During analysis, specifically detect the pattern: "energy drops below threshold for 1 to 4 beats, then immediately spikes above 0.8." Tag these as **"blackout_slam"** events. The sequence generator plans for these:

* 4 beats before the silence: begin dimming
* Silence beats: full blackout, all fixtures off
* First beat of energy return: all fixtures slam to 100%, strobe optional, scene hard-cuts to the drop scene

This is one of the most dramatic moments in a DJ set and it happens automatically because the waveform told us it was coming.

### Transition Logic
* Section boundaries are known from the pre-generated sequence
* Pre-load next scene 4 beats before the boundary
* Crossfade timing depends on transition type:
  * Breakdown → Buildup: gradual ramp over 8 beats
  * Buildup → Drop: hard cut on the downbeat
  * Drop → Breakdown: quick fade over 4 beats
  * Intro → first section: slow fade over 16 beats
* During track transitions (deck to deck), crossfade between the two tracks' color themes

### Dashboard Updates (Phase 3)
* **Full sequence timeline**: Horizontal bar showing the entire planned light show for each deck, color coded by scene type, with a playhead showing current position
* **Energy envelope waveform**: Overlay the energy envelope on the timeline so you can see exactly where silences, risers, and blackout_slam events are pre-mapped
* **Track color theme swatch**: Display the assigned primary/secondary/accent colors per deck
* **Next section preview**: Show what scene is coming and when (countdown in beats)
* **Active scene info**: Current scene name, movement pattern, intensity level, and current mode (normal / follow_audio / blackout)
* **Real-time intensity meter**: Show the actual output intensity being sent to fixtures, so you can see the energy envelope modulating the scene in real time
* **Sequence regenerate button**: Don't like the plan? Hit regenerate and it re-rolls all the random selections while keeping the same track color theme
* **Color palette history**: Rolling view of the last 5 to 10 color themes used across tracks for variety awareness

---

## Phase 4: Live DJ Override Detection

**Target: Week 4**

### Goal
Make the lighting system respond intelligently to live DJ decisions that deviate from the pre-analyzed track map.

### Override Events (from Pro DJ Link)
| DJ Action | Detection Method | Lighting Response |
|-----------|-----------------|-------------------|
| Loop engaged | Pro DJ Link loop status | Hold current scene, escalate intensity each loop cycle |
| Loop released | Pro DJ Link loop status | Snap to correct section scene based on new position |
| Hot cue jump | Position jumps discontinuously | Instantly load scene for the section at new position |
| Track scratch/rewind | Position moves backward | Flash/strobe effect, then resume |
| Pitch bend (jog nudge) | Tempo fluctuation in Pro DJ Link | No change (temporary) |
| Tempo change (slider) | BPM update from Pro DJ Link | Adjust all timing math globally |
| Track swap (quick cut) | New track loaded + immediately playing | Fast transition to new track's scene |
| EQ kill (bass cut) | FFT detects bass dropout while Pro DJ Link says still playing | Dim warm colors, emphasize cool tones |

### Hybrid Detection
Some DJ moves aren't visible in Pro DJ Link but are audible in the FFT:

* **EQ kills**: Cutting the bass on the mixer doesn't change Pro DJ Link data, but FFT sees the frequency gap. Use this to modulate color temperature in real time.
* **Filter sweeps**: FFT detects the spectral rolloff changing. Map to brightness sweep on fixtures.
* **Effects (echo, reverb, flanger)**: FFT sees the spectral smearing. Add subtle movement variation.

This is where the dual input system (Pro DJ Link + FFT) really shines. Structure and timing from Pro DJ Link. Nuance and mixer interaction from FFT.

### Dashboard Updates (Phase 4)
* Override event log: scrolling feed showing "Loop engaged on Deck 1," "Hot cue jump to 2:34," etc.
* Visual indicator when lightd is in override mode vs following the pre-analyzed section map
* FFT vs Pro DJ Link data comparison: show when FFT detects something Pro DJ Link doesn't (EQ kills, filter sweeps)
* Escalation meter: during loops, show the intensity ramp so you can see how the lights are building

---

## Phase 5: DJ Sam Integration + Live Style Switching

**Target: Week 5+**

### Goal
Connect the lighting system to the DJ Sam agent so that when DJ Sam is autonomously selecting and mixing tracks, the lighting follows automatically with zero human input. Also add voice/dashboard style switching for live sets.

### Integration Points
* DJ Sam selects a track → triggers on-load analysis if not cached → lighting system pre-loads scenes for the incoming track
* DJ Sam's mix decisions (when to transition, what section to mix into) feed directly into the lighting transition engine
* DJ Sam can request specific lighting moods via the agent protocol ("play something dark and build toward a peak")

### Architecture
```
DJ Sam (OpenClaw agent)
        ↓ track selection + mix timing
XDJ-XZ (standalone playback)
        ↓ Pro DJ Link (Ethernet)
Mac mini
├── beat-link bridge → lightd (lighting control)
├── analysis DB (on-load cache in SQLite)
└── DMX + Govee output → fixtures
```

The DJ Sam → lighting pipeline is fully autonomous. Load a playlist, walk away, and the room runs itself with intelligent, track aware lighting that adapts to every section of every song.

### Live Style Switching
Voice command or dashboard toggle: "go dark," "festival mode," "chill." This overrides the vibe mapping with a style bias that shifts all color palettes and movement patterns.

### Dashboard Updates (Phase 5)
* DJ Sam status: show whether DJ Sam is active and controlling the decks
* Upcoming track preview: what DJ Sam plans to play next, with pre-computed scene preview
* Autonomous mode indicator: clear visual distinction between "Ethan is DJing" and "DJ Sam is running the show"
* Set history log: scrolling list of tracks played, sections hit, and scenes used
* Style mode selector: buttons for "Dark," "Festival," "Chill," "Auto" directly in the dashboard

---

## Hardware Summary

### Currently Owned
* Enttec Open DMX USB
* 2x Venue Tetra 12
* 1x Venue Tetra Bar
* 4x Govee COB Strips
* 4x Govee Bulbs
* Scarlett 2i2
* Mac mini (Ultron)
* XDJ-XZ

### Needed (Phase 1)
* ✅ 1x Jadaol CAT6 50ft flat Ethernet cable, white ($9.99) — PURCHASED 3/31
* ✅ 1x Amazon Basics 2 pack XLR cables, 15ft ($16.19) — PURCHASED 3/31

### Nice to Have (Future)
* Additional DMX fixtures (moving heads would unlock pan/tilt scene dimensions)
* Fog machine with DMX control (beam visibility)
* Dedicated gigabit switch for DJ Link network isolation

---

## Tech Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| Pro DJ Link | beat-link (Java) | XDJ-XZ communication |
| Audio Analysis | librosa, madmom, essentia | Spectral features, beat tracking |
| Stem Separation | demucs (Meta) | Isolate kicks, bass, vocals, synths |
| Structure Detection | librosa segmentation + clustering | Section boundary identification |
| Scene Engine | Python (lightd) | Scene selection, generation, transitions |
| DMX Output | libftdi via dmx_controller.py | Fixture control |
| Govee Output | HTTP API | Smart light control |
| Dashboard | Python web server (port 8420) | Live monitoring and control |
| Database | SQLite | Track analysis cache |
| Agent Integration | OpenClaw (DJ Sam) | Autonomous operation |

---

## Weekly Plan

| Week | Phase | Deliverable |
|------|-------|-------------|
| Week 1 (Mar 31) | Phase 1 | Pro DJ Link connected, beat-link bridge running, lightd receiving track + beat data. Dashboard: deck info, BPM, beat position, connection status |
| Week 2 | Phase 2 | On-load analysis pipeline, SQLite cache, structure detection on track load. Dashboard: analysis status, section timeline, vibe vector display |
| Week 3 | Phase 3 | Full track light sequencing on load, track color identity, filtered random scene pool, arc planning. Dashboard: sequence timeline, color theme swatches, regenerate button |
| Week 4 | Phase 4 | Loop/hot cue/override detection working, hybrid FFT+Link mode. Dashboard: override event log, escalation meter |
| Week 5+ | Phase 5 | DJ Sam integration, live style switching, fully autonomous operation. Dashboard: autonomous mode, style selector, set history |
