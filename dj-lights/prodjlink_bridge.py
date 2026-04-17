#!/usr/bin/env python3
"""
Pro DJ Link Bridge — Python implementation using python-prodj-link

Connects to Pioneer Pro DJ Link devices (XDJ-XZ) and pushes events
to lightd via Unix socket at /tmp/lightd.sock.

Events pushed:
- Track metadata (title, artist, key, BPM)
- Beat position (1-4 within bar)
- Phrase position (beat count from track start)
- Play/pause state per deck
- Loop status
- Master deck ID
- Pitch/tempo adjustments

Usage:
    python3 prodjlink_bridge.py
"""

import os
import sys
import json
import time
import socket
import struct
import random
import logging
import threading
from collections import defaultdict

# Add python-prodj-link to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'python-prodj-link'))

from prodj.core.prodj import ProDj

SOCKET_PATH = '/tmp/lightd.sock'
VCDJ_PLAYER_NUMBER = 5  # Virtual CDJ player number (avoid conflicts with real decks)

# State tracking for each deck
deck_state = defaultdict(lambda: {
    'title': None,
    'artist': None,
    'album': None,
    'key': None,
    'bpm': 0.0,
    'beat': 0,
    'beat_count': 0,
    'play_state': 'stopped',
    'pitch': 1.0,
    'actual_pitch': 1.0,
    'loop_active': False,
    'loop_start': None,
    'loop_end': None,
    'on_air': False,
    'is_master': False,
    'last_update': 0,
})

last_master = None
prodj = None

# Waveform and beatgrid storage per deck
deck_waveform = {}  # deck_number: raw entries list
deck_beatgrid = {}  # deck_number: parsed beats list
deck_analysis_pending = {}  # deck_number: set of pending requests
deck_last_track_id = {}  # deck_number: last analyzed/requested track id
deck_analysis_cache = {}  # (deck_number, track_id) -> analysis dict
deck_last_sent_analysis = {}  # deck_number -> track_id last sent to lightd
COLOR_THEMES = [
    {"primary": [255, 0, 80], "secondary": [180, 0, 255]},   # magenta/purple
    {"primary": [0, 200, 255], "secondary": [255, 128, 0]},   # cyan/orange
    {"primary": [255, 50, 50], "secondary": [255, 200, 50]},  # red/gold
    {"primary": [50, 100, 255], "secondary": [255, 255, 255]},# blue/white
    {"primary": [0, 255, 128], "secondary": [255, 180, 0]},   # green/amber
    {"primary": [200, 0, 255], "secondary": [0, 255, 200]},   # violet/teal
    {"primary": [255, 100, 0], "secondary": [0, 150, 255]},   # orange/blue
    {"primary": [255, 0, 200], "secondary": [50, 255, 50]},   # pink/green
    {"primary": [0, 255, 255], "secondary": [255, 0, 100]},   # cyan/rose
    {"primary": [255, 200, 0], "secondary": [100, 0, 255]},   # yellow/indigo
]
deck_color_theme = {}  # deck_number: theme dict

# Persistent socket connection to lightd
_lightd_sock = None
_lightd_lock = threading.Lock()


def _get_lightd_sock():
    """Get or create a persistent socket connection to lightd."""
    global _lightd_sock
    if _lightd_sock is not None:
        return _lightd_sock
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(SOCKET_PATH)
        _lightd_sock = sock
        logging.debug("Connected to lightd socket")
        return sock
    except Exception as e:
        logging.debug(f"Failed to connect to lightd: {e}")
        return None


def _close_lightd_sock():
    """Close the persistent socket."""
    global _lightd_sock
    if _lightd_sock is not None:
        try:
            _lightd_sock.close()
        except:
            pass
        _lightd_sock = None


def send_to_lightd(event_type, data):
    """Send a JSON event to lightd via persistent Unix socket."""
    global _lightd_sock
    
    event = {
        'source': 'prodjlink',
        'type': event_type,
        'timestamp': time.time(),
        **data
    }
    msg = json.dumps(event) + '\n'
    
    with _lightd_lock:
        # Try sending on existing connection, reconnect once on failure
        for attempt in range(2):
            sock = _get_lightd_sock()
            if sock is None:
                return False
            try:
                sock.sendall(msg.encode())
                # Read response
                try:
                    response = sock.recv(4096).decode().strip()
                    logging.debug(f"lightd response: {response}")
                except:
                    pass
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                logging.debug(f"Socket broken, reconnecting (attempt {attempt + 1})")
                _close_lightd_sock()
                if attempt == 0:
                    continue
                return False


def on_client_change(player_number):
    """Called when a client's status changes."""
    if prodj is None:
        return
    
    client = prodj.cl.getClient(player_number)
    if client is None:
        return
    
    state = deck_state[player_number]
    now = time.time()
    
    # Check what changed
    changed = {}
    
    if client.bpm != state['bpm']:
        changed['bpm'] = client.bpm
        state['bpm'] = client.bpm
    
    if client.beat != state['beat']:
        changed['beat'] = client.beat
        state['beat'] = client.beat
    
    if client.beat_count != state['beat_count']:
        changed['beat_count'] = client.beat_count
        state['beat_count'] = client.beat_count
    
    # Track absolute position (seconds) from beatgrid mapping
    if hasattr(client, 'position') and client.position is not None:
        state['position'] = client.position
        changed['position'] = client.position
    
    if hasattr(client, 'play_state') and client.play_state != state['play_state']:
        changed['play_state'] = client.play_state
        state['play_state'] = client.play_state
    
    if client.pitch != state['pitch']:
        changed['pitch'] = client.pitch
        state['pitch'] = client.pitch
    
    if client.actual_pitch != state['actual_pitch']:
        changed['actual_pitch'] = client.actual_pitch
        state['actual_pitch'] = client.actual_pitch
    
    if hasattr(client, 'key') and client.key != state['key']:
        changed['key'] = client.key
        state['key'] = client.key
    
    # Check loop status
    loop_active = (hasattr(client, 'loop_start') and client.loop_start is not None and 
                   hasattr(client, 'loop_end') and client.loop_end is not None and
                   client.loop_start > 0 and client.loop_end > 0)
    if loop_active != state['loop_active']:
        changed['loop_active'] = loop_active
        state['loop_active'] = loop_active
        if loop_active:
            changed['loop_start'] = client.loop_start if hasattr(client, 'loop_start') else None
            changed['loop_end'] = client.loop_end if hasattr(client, 'loop_end') else None
    
    # Check on-air status
    if hasattr(client, 'on_air') and client.on_air != state['on_air']:
        changed['on_air'] = client.on_air
        state['on_air'] = client.on_air
    
    # Check master status
    is_master = 'master' in getattr(client, 'state', [])
    if is_master != state['is_master']:
        changed['is_master'] = is_master
        state['is_master'] = is_master
        
        # Announce master deck change globally
        global last_master
        if is_master and last_master != player_number:
            last_master = player_number
            send_to_lightd('master_change', {
                'master_deck': player_number,
                'bpm': client.bpm,
            })
    
    state['last_update'] = now
    
    # Send update if anything changed
    if changed:
        logging.info(f"Deck {player_number} update: {changed}")
        
        # Send to lightd
        send_to_lightd('deck_update', {
            'deck': player_number,
            'changes': changed,
            'state': {
                'bpm': state['bpm'],
                'beat': state['beat'],
                'beat_count': state['beat_count'],
                'play_state': state['play_state'],
                'pitch': state['pitch'],
                'actual_pitch': state['actual_pitch'],
                'key': state['key'],
                'loop_active': state['loop_active'],
                'on_air': state['on_air'],
                'is_master': state['is_master'],
                'title': state['title'],
                'artist': state['artist'],
                'position': state.get('position'),
            }
        })


# PSSI phrase kind mapping for mood=1 (high)
# See pyrekordbox docs: https://pyrekordbox.readthedocs.io/en/stable/formats/anlz.html
PSSI_HIGH_MOOD = {
    1: "intro",
    2: "buildup",    # "Up" in rekordbox
    3: "breakdown",  # "Down" in rekordbox  
    5: "drop",       # "Chorus" in rekordbox
    6: "outro",
}
# Mood 2 (mid) and 3 (low) use different kind values
PSSI_MID_MOOD = {
    1: "intro",
    2: "groove",     # Verse 1-6
    3: "groove",
    5: "drop",       # Chorus
    6: "breakdown",  # Bridge
    7: "outro",
}
PSSI_LOW_MOOD = {
    1: "intro",
    2: "groove",     # Verse 1-2
    3: "groove",
    5: "drop",       # Chorus
    6: "breakdown",  # Bridge
    7: "outro",
}


def analyze_from_pssi(deck_number):
    """Build sections from rekordbox PSSI song structure data."""
    ss = deck_song_structure.get(deck_number)
    if not ss or not ss.get('entries'):
        return None
    
    mood = ss.get('mood', 1)
    end_beat = ss.get('end_beat', 0)
    entries = ss['entries']
    
    if mood == 2:
        kind_map = PSSI_MID_MOOD
    elif mood == 3:
        kind_map = PSSI_LOW_MOOD
    else:
        kind_map = PSSI_HIGH_MOOD
    
    sections = []
    for i, entry in enumerate(entries):
        beat_start = entry['beat']
        beat_end = entries[i + 1]['beat'] if i + 1 < len(entries) else end_beat
        kind = entry['kind']
        section_type = kind_map.get(kind, "groove")
        
        sections.append({
            "type": section_type,
            "start_beat": beat_start,
            "end_beat": beat_end,
        })
    
    # Ensure last section goes to end_beat
    if sections:
        sections[-1]["end_beat"] = max(sections[-1]["end_beat"], end_beat)
    
    # Merge adjacent sections of the same type
    merged = []
    for s in sections:
        if merged and merged[-1]["type"] == s["type"]:
            merged[-1]["end_beat"] = s["end_beat"]
        else:
            merged.append(dict(s))
    sections = merged
    
    logging.info(f"Deck {deck_number}: PSSI sections ({len(sections)} phrases, mood={mood}):")
    for s in sections:
        bars = (s['end_beat'] - s['start_beat']) // 4
        logging.info(f"  {s['type']:12} beats {s['start_beat']:4}-{s['end_beat']:4} ({bars} bars)")
    
    return sections, end_beat


def analyze_waveform(deck_number):
    """Build track analysis. Uses PSSI (rekordbox phrases) when available,
    falls back to waveform energy analysis.
    """
    waveform = deck_waveform.get(deck_number)
    beatgrid = deck_beatgrid.get(deck_number)
    
    if not beatgrid:
        logging.warning(f"Deck {deck_number}: Missing beatgrid for analysis")
        return None
    
    total_beats = len(beatgrid)
    
    # --- Try PSSI first (rekordbox's own phrase analysis) ---
    pssi_result = analyze_from_pssi(deck_number)
    if pssi_result is not None:
        sections, end_beat = pssi_result
        # Use beatgrid length as total_beats (more accurate than PSSI end_beat)
        # PSSI end_beat can extend past actual track content
        total_beats = len(beatgrid) if beatgrid else end_beat
        logging.info(f"Deck {deck_number}: Using PSSI phrases ({len(sections)} sections)")
        
        # Waveform is optional in the critical path. If missing, create a flat placeholder
        # for UI consumers and focus on deterministic sections.
        if waveform:
            n_entries = len(waveform) // 6 if len(waveform) >= 6 else len(waveform)
            if n_entries > 0 and len(waveform) >= 6:
                entry_energies = []
                for i in range(n_entries):
                    base = i * 6
                    entry_energies.append(abs(waveform[base + 3]) + abs(waveform[base + 4]) + abs(waveform[base + 5]))
                entries_per_beat = n_entries / total_beats if total_beats > 0 else 1
                beat_energy = []
                for b in range(total_beats):
                    si = int(b * entries_per_beat)
                    ei = min(int((b + 1) * entries_per_beat), n_entries)
                    beat_energy.append(sum(entry_energies[si:ei]) / max(1, ei - si) if ei > si else 0)
                max_be = max(beat_energy) if beat_energy and max(beat_energy) > 0 else 1
                beat_energy = [e / max_be for e in beat_energy]
            else:
                beat_energy = [0.5] * total_beats
        else:
            beat_energy = [0.5] * total_beats
        
        # Assign scenes deterministically per track/section
        SCENE_POOLS = {
            "intro": ["Ocean Drift", "Ember Flicker", "Warm Blanket", "Moonlight"],
            "outro": ["Ocean Drift", "Ember Flicker", "Warm Blanket", "Moonlight"],
            "breakdown": ["Breathing Dark", "Wash Only", "Tide", "Deep Crossfade", "Zone Drift"],
            "buildup": ["Accelerating Chase", "Rising Pulse", "Snare Roll", "Color Ramp"],
            "drop": ["HW Strobe Max", "Color Blast Strobe", "Machine Gun", "Explosion Fade"],
            "groove": ["Sustained Wash", "Split Hold", "Slow Chase", "Warm Pulse", "Two Tone Hold",
                        "Zone Drift", "Deep Blue", "Magenta Teal", "Kick Accent", "Violet Emerald"],
        }
        state = deck_state[deck_number]
        track_key = f"{state.get('title','Unknown')}|{state.get('artist','Unknown')}|{state.get('bpm',128.0)}|{total_beats}"
        used_scenes = set()
        for idx, section in enumerate(sections):
            pool = SCENE_POOLS.get(section["type"], SCENE_POOLS["groove"])
            available = [s for s in pool if s not in used_scenes] or pool
            pick_idx = abs(hash(f"{track_key}|{section['type']}|{section['start_beat']}|{idx}")) % len(available)
            scene = available[pick_idx]
            used_scenes.add(scene)
            section["scene"] = scene
            section["category"] = section["type"] if section["type"] not in ("intro", "outro") else "ambient"
        
        other_deck = 2 if deck_number == 1 else 1
        other_theme = deck_color_theme.get(other_deck)
        available_themes = [t for t in COLOR_THEMES if t != other_theme] or COLOR_THEMES
        theme = available_themes[abs(hash(track_key + '|theme')) % len(available_themes)]
        deck_color_theme[deck_number] = theme
        return {
            "deck": deck_number,
            "title": state.get("title", "Unknown"),
            "artist": state.get("artist", "Unknown"),
            "bpm": state.get("bpm", 128.0),
            "track_id": deck_last_track_id.get(deck_number),
            "color_theme": theme,
            "waveform": [round(e, 3) for e in beat_energy],
            "sections": sections,
            "total_beats": total_beats,
            "analysis_status": "ready"
        }
    
    # --- Fallback: waveform energy analysis ---
    logging.info(f"Deck {deck_number}: No PSSI, falling back to waveform analysis")
    
    if total_beats < 32:
        logging.warning(f"Deck {deck_number}: Track too short ({total_beats} beats)")
        return None
    
    # --- Step 1: Compute per-beat energy from waveform ---
    # Waveform is flat array of signed int8, 6 values per position
    # Map waveform positions to beats using beatgrid timestamps
    
    # Color waveform: 6 signed int8 per position
    # Format per position (from python-prodj-link GUI code):
    #   d0, d1: steepness control (blue rendering)
    #   d2: blueness  
    #   d3: RED height (bass/low frequency)
    #   d4: GREEN height (mid frequency)
    #   d5: BLUE height (high frequency)
    # For energy detection, use d3+d4+d5 (the frequency band heights)
    n_entries = len(waveform) // 6 if len(waveform) >= 6 else 0
    if n_entries == 0:
        # Fallback: treat each value as an entry  
        n_entries = len(waveform)
        entry_energies = [abs(waveform[i]) for i in range(n_entries)]
    else:
        entry_energies = []
        for i in range(n_entries):
            base = i * 6
            red_h = abs(waveform[base + 3])   # bass/low
            green_h = abs(waveform[base + 4]) # mid
            blue_h = abs(waveform[base + 5])  # high
            entry_energies.append(red_h + green_h + blue_h)
    
    if not entry_energies:
        logging.warning(f"Deck {deck_number}: Empty energy array")
        return None
    
    # Map entries to beats using RMS energy (better for structure detection
    # since regular waveform gives peak amplitude, not perceived loudness)
    entries_per_beat = n_entries / total_beats if total_beats > 0 else 1
    beat_energy_raw = []
    for b in range(total_beats):
        start_idx = int(b * entries_per_beat)
        end_idx = int((b + 1) * entries_per_beat)
        end_idx = min(end_idx, n_entries)
        if start_idx >= end_idx:
            beat_energy_raw.append(0)
        else:
            # RMS instead of mean — squares emphasize louder parts, sqrt brings back to scale
            vals = entry_energies[start_idx:end_idx]
            rms = (sum(v * v for v in vals) / len(vals)) ** 0.5
            beat_energy_raw.append(rms)
    
    # Smooth with 4-beat (1-bar) moving average to reduce transient noise
    beat_energy = []
    smooth_window = 4
    for i in range(total_beats):
        start = max(0, i - smooth_window)
        end = min(total_beats, i + smooth_window + 1)
        beat_energy.append(sum(beat_energy_raw[start:end]) / (end - start))
    
    # Normalize beat energy to 0-1
    max_be = max(beat_energy) if beat_energy and max(beat_energy) > 0 else 1
    beat_energy = [e / max_be for e in beat_energy]
    
    # --- Step 2: Find phrase alignment from beatgrid ---
    # The beatgrid has a 'beat' field (1-4) showing position in bar.
    # Find the first downbeat (beat=1) to align our phrase chunks.
    phrase_offset = 0
    for i, bg in enumerate(beatgrid):
        if isinstance(bg, dict) and bg.get('beat', 0) == 1:
            phrase_offset = i
            break
    
    PHRASE_BEATS = 32  # 8 bars * 4 beats
    
    # Compute energy per 8-bar chunk, aligned to the first downbeat
    chunk_starts = []
    pos = phrase_offset
    while pos < total_beats:
        chunk_starts.append(pos)
        pos += PHRASE_BEATS
    n_chunks = len(chunk_starts)
    if n_chunks == 0:
        chunk_starts = [0]
        n_chunks = 1
    
    chunk_energy = []
    for i, start in enumerate(chunk_starts):
        end = chunk_starts[i + 1] if i + 1 < n_chunks else total_beats
        chunk_e = sum(beat_energy[start:end]) / (end - start) if end > start else 0
        chunk_energy.append(chunk_e)
    
    logging.info(f"Deck {deck_number}: Phrase offset={phrase_offset} beats (aligned to first downbeat)")
    
    # Track median energy (excluding very quiet intro/outro chunks)
    sorted_chunks = sorted(chunk_energy)
    track_median = sorted_chunks[len(sorted_chunks) // 2] if sorted_chunks else 0.5
    if track_median < 0.05:
        track_median = 0.05
    
    # --- Step 3: Classify each chunk ---
    INTRO_OUTRO_THRESH = 0.60   # below 60% of median = intro/outro
    BREAKDOWN_DROP_RATIO = 0.40 # 40% drop from recent avg = breakdown
    BUILDUP_RECOVERY = 0.80     # 80% of pre-breakdown = still building
    
    chunk_types = []
    pre_breakdown_energy = None  # energy level before the breakdown started
    recent_avg = track_median     # rolling average of recent high-energy chunks
    
    for c in range(n_chunks):
        e = chunk_energy[c]
        ratio_to_median = e / track_median
        
        # Intro: first chunks below threshold
        if c == 0 and ratio_to_median < INTRO_OUTRO_THRESH:
            chunk_types.append("intro")
            continue
        
        # Extend intro if still quiet at the start
        if len(chunk_types) > 0 and all(t == "intro" for t in chunk_types) and ratio_to_median < INTRO_OUTRO_THRESH:
            chunk_types.append("intro")
            continue
        
        # Outro: last chunks below threshold (assigned in post-pass)
        # For now, classify based on energy dynamics
        
        prev_type = chunk_types[-1] if chunk_types else None
        
        # Check for breakdown: significant energy drop from recent average
        # Only trigger from groove (not immediately after a drop)
        # AND the chunk must actually be below the track median (not just below a high peak)
        if prev_type == "groove" and recent_avg > 0:
            drop_ratio = e / recent_avg
            below_median = e < track_median * 0.85
            if drop_ratio < (1.0 - BREAKDOWN_DROP_RATIO) and below_median:
                pre_breakdown_energy = recent_avg
                chunk_types.append("breakdown")
                continue
        
        # Continue breakdown if still low relative to pre-breakdown level AND below median
        if prev_type == "breakdown" and pre_breakdown_energy and e < pre_breakdown_energy * 0.70 and e < track_median * 0.90:
            chunk_types.append("breakdown")
            continue
        
        # Buildup: after breakdown, rising but not yet at pre-breakdown level
        if prev_type in ("breakdown", "buildup") and pre_breakdown_energy:
            if e < pre_breakdown_energy * BUILDUP_RECOVERY:
                chunk_types.append("buildup")
                continue
            else:
                # Energy recovered — this is a drop
                chunk_types.append("drop")
                pre_breakdown_energy = None
                recent_avg = e
                continue
        
        # After a drop, reset to groove and rebuild the rolling average
        if prev_type == "drop":
            chunk_types.append("groove")
            recent_avg = e
            continue
        
        # Default: groove (normal energy)
        chunk_types.append("groove")
        # Update rolling average for high-energy sections
        if e > track_median * 0.5:
            recent_avg = recent_avg * 0.6 + e * 0.4
    
    # --- Step 4: Post-pass — detect outro ---
    # Walk backwards from end, mark low-energy chunks as outro
    for c in range(n_chunks - 1, -1, -1):
        if chunk_energy[c] / track_median < INTRO_OUTRO_THRESH:
            chunk_types[c] = "outro"
        else:
            break
    
    # --- Step 5: Convert chunks to sections, merging adjacent same-type ---
    raw_sections = []
    for c in range(n_chunks):
        start_beat = chunk_starts[c]
        end_beat = chunk_starts[c + 1] if c + 1 < n_chunks else total_beats
        ctype = chunk_types[c]
        
        if raw_sections and raw_sections[-1]["type"] == ctype:
            raw_sections[-1]["end_beat"] = end_beat
        else:
            raw_sections.append({
                "type": ctype,
                "start_beat": start_beat,
                "end_beat": end_beat,
            })
    
    # Handle any beats before the first downbeat as intro
    if phrase_offset > 0 and raw_sections:
        raw_sections.insert(0, {
            "type": "intro",
            "start_beat": 0,
            "end_beat": phrase_offset,
        })
    
    # --- Step 6: Merge small adjacent same-type sections < 64 beats ---
    sections = []
    for s in raw_sections:
        if sections and sections[-1]["type"] == s["type"]:
            combined = s["end_beat"] - sections[-1]["start_beat"]
            sections[-1]["end_beat"] = s["end_beat"]
        elif sections and (s["end_beat"] - s["start_beat"]) < 32:
            # Very short section (< 8 bars) — absorb into previous
            sections[-1]["end_beat"] = s["end_beat"]
        else:
            sections.append(dict(s))
    
    if not sections:
        sections = [{"type": "groove", "start_beat": 0, "end_beat": total_beats}]
    
    # Ensure last section extends to end
    sections[-1]["end_beat"] = total_beats
    
    # Log chunk energies for debugging
    chunk_str = ' '.join(f"{e:.2f}" for e in chunk_energy)
    logging.info(f"Deck {deck_number}: Chunk energies ({n_chunks}): [{chunk_str}]")
    logging.info(f"Deck {deck_number}: Track median={track_median:.3f}")
    logging.info(f"Deck {deck_number}: Chunk types: {chunk_types}")
    logging.info(f"Deck {deck_number}: Phrase analysis — {len(sections)} sections from {n_chunks} chunks")
    for s in sections:
        bars = (s['end_beat'] - s['start_beat']) // 4
        logging.info(f"  {s['type']:12} beats {s['start_beat']:4}-{s['end_beat']:4} ({bars} bars)")
    
    # --- Step 7: Assign scenes to sections ---
    SCENE_POOLS = {
        "intro": ["Ocean Drift", "Ember Flicker", "Warm Blanket", "Moonlight"],
        "outro": ["Ocean Drift", "Ember Flicker", "Warm Blanket", "Moonlight"],
        "breakdown": ["Breathing Dark", "Wash Only", "Tide", "Deep Crossfade", "Zone Drift"],
        "buildup": ["Accelerating Chase", "Rising Pulse", "Snare Roll", "Color Ramp"],
        "drop": ["HW Strobe Max", "Color Blast Strobe", "Machine Gun", "Explosion Fade"],
        "groove": ["Sustained Wash", "Split Hold", "Slow Chase", "Warm Pulse", "Two Tone Hold",
                    "Zone Drift", "Deep Blue", "Magenta Teal", "Kick Accent", "Violet Emerald"],
    }
    
    used_scenes = set()
    for section in sections:
        pool = SCENE_POOLS.get(section["type"], SCENE_POOLS["groove"])
        available = [s for s in pool if s not in used_scenes] or pool
        scene = random.choice(available)
        used_scenes.add(scene)
        section["scene"] = scene
        section["category"] = section["type"] if section["type"] not in ("intro", "outro") else "ambient"
    
    # --- Step 8: Build smoothed waveform for dashboard display ---
    # Resample beat_energy to a reasonable display resolution
    smoothed = beat_energy  # one value per beat, already normalized
    
    # Pick color theme (avoid other deck's theme)
    other_deck = 2 if deck_number == 1 else 1
    other_theme = deck_color_theme.get(other_deck)
    available_themes = [t for t in COLOR_THEMES if t != other_theme] or COLOR_THEMES
    theme = random.choice(available_themes)
    deck_color_theme[deck_number] = theme
    
    state = deck_state[deck_number]
    
    return {
        "deck": deck_number,
        "title": state.get("title", "Unknown"),
        "artist": state.get("artist", "Unknown"),
        "bpm": state.get("bpm", 128.0),
        "color_theme": theme,
        "waveform": [round(e, 3) for e in smoothed],
        "sections": sections,
        "total_beats": total_beats,
        "analysis_status": "ready"
    }


def on_waveform_received(request, player_number, slot, track_id, data):
    """Called when color waveform data is received."""
    if data is None:
        logging.warning(f"Deck {player_number}: No waveform data received")
        return
    
    logging.info(f"Deck {player_number}: Received waveform ({len(data)} entries)")
    deck_waveform[player_number] = list(data)
    
    if player_number in deck_analysis_pending:
        deck_analysis_pending[player_number].discard("waveform")
        _try_analyze(player_number)


def on_beatgrid_received(request, player_number, slot, track_id, data):
    """Called when beatgrid data is received."""
    if data is None:
        logging.warning(f"Deck {player_number}: No beatgrid data received")
        return
    
    logging.info(f"Deck {player_number}: Received beatgrid ({len(data)} beats)")
    deck_beatgrid[player_number] = list(data) if not isinstance(data, list) else data
    
    if player_number in deck_analysis_pending:
        deck_analysis_pending[player_number].discard("beatgrid")
        _try_analyze(player_number)


def _try_analyze(player_number):
    """Try to run analysis as soon as critical data is available.
    Critical path for tonight: PSSI + beatgrid. Waveform is optional.
    """
    pending = deck_analysis_pending.get(player_number, set())
    critical_pending = set(pending) - {'waveform'}
    if len(critical_pending) == 0:
        logging.info(f"Deck {player_number}: Critical data received, running analysis...")
        
        send_to_lightd('analysis_status', {
            'deck': player_number,
            'status': 'analyzing'
        })
        
        result = analyze_waveform(player_number)
        if result:
            track_id = deck_last_track_id.get(player_number)
            if track_id is not None:
                deck_analysis_cache[(player_number, track_id)] = result
                deck_last_sent_analysis[player_number] = track_id
            logging.info(f"Deck {player_number}: Analysis complete — {len(result['sections'])} sections detected")
            send_to_lightd('track_analysis', result)
            deck_analysis_pending[player_number] = set()
        else:
            logging.warning(f"Deck {player_number}: Analysis failed")
            send_to_lightd('analysis_status', {
                'deck': player_number,
                'status': 'failed'
            })


# Store song structure (PSSI) data per deck
deck_song_structure = {}

def _find_anlz_path_in_pdb(track_id):
    """Scan the raw PDB file for the ANLZ path of a given track ID."""
    import struct, re
    pdb_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'databases', 'player-1-sd.pdb')
    if not os.path.exists(pdb_path):
        return None
    
    with open(pdb_path, 'rb') as f:
        data = f.read()
    
    tid_bytes = struct.pack('<I', track_id)
    anlz_pattern = rb'/PIONEER/USBANLZ/P[0-9a-fA-F]{3}/[0-9a-fA-F]{8}/ANLZ0000\.DAT'
    
    pos = 0
    while True:
        pos = data.find(tid_bytes, pos)
        if pos == -1:
            break
        # Look within 800 bytes after this position for an ANLZ path
        region = data[pos:pos+800]
        m = re.search(anlz_pattern, region)
        if m:
            return m.group(0).decode('ascii')
        pos += 1
    return None


def _download_song_structure(player_number, track_id):
    """Download .EXT analysis file and extract PSSI song structure."""
    from prodj.pdblib.usbanlzdatabase import UsbAnlzDatabase
    
    # Find the ANLZ path from the PDB
    dat_path = _find_anlz_path_in_pdb(track_id)
    if dat_path is None:
        logging.warning(f"Deck {player_number}: ANLZ path not found in PDB for track {track_id}")
        return
    
    ext_path = dat_path.replace('.DAT', '.EXT')
    logging.info(f"Deck {player_number}: Downloading {ext_path} for song structure...")
    
    # Get a real player client for NFS
    client = None
    for c in prodj.cl.clients:
        if c.player_number <= 4 and hasattr(c, 'ip_addr'):
            client = c
            break
    if client is None:
        logging.warning(f"Deck {player_number}: No client found for NFS download")
        return
    
    slot = getattr(client, 'loaded_slot', 'sd')
    ext_data = prodj.nfs.enqueue_buffer_download(client.ip_addr, slot, ext_path)
    
    if ext_data is None:
        logging.warning(f"Deck {player_number}: Failed to download {ext_path}")
        return
    
    logging.info(f"Deck {player_number}: Downloaded {len(ext_data)} bytes of EXT data")
    
    try:
        db = UsbAnlzDatabase()
        db.load_ext_buffer(ext_data)
        
        if 'song_structure' in db:
            ss = db['song_structure']
            deck_song_structure[player_number] = ss
            logging.info(f"Deck {player_number}: PSSI loaded - {len(ss['entries'])} phrases, mood={ss['mood']}, end_beat={ss['end_beat']}")
            for entry in ss['entries']:
                logging.info(f"  Phrase {entry['index']}: beat={entry['beat']}, kind={entry['kind']}")
        else:
            logging.warning(f"Deck {player_number}: No PSSI tag in EXT file")
    except Exception as e:
        logging.error(f"Deck {player_number}: Failed to parse EXT file: {e}")


def on_track_metadata(request, player_number, slot, item_id, metadata):
    """Called when track metadata is received."""
    if request != "metadata" or metadata is None:
        return
    
    state = deck_state[player_number]
    
    # Update metadata
    state['title'] = metadata.get('title', 'Unknown')
    state['artist'] = metadata.get('artist', 'Unknown')
    state['album'] = metadata.get('album', '')
    
    # BPM might be in metadata too
    if 'bpm' in metadata and metadata['bpm']:
        state['bpm'] = metadata['bpm']
    
    logging.info(f"Deck {player_number} loaded: {state['artist']} - {state['title']} ({state['bpm']} BPM)")
    
    # Try to download the .EXT analysis file for PSSI song structure
    if prodj is not None and item_id:
        try:
            _download_song_structure(player_number, item_id)
        except Exception as e:
            logging.warning(f"Deck {player_number}: Failed to get song structure: {e}")
    
    # Send track load event
    send_to_lightd('track_load', {
        'deck': player_number,
        'title': state['title'],
        'artist': state['artist'],
        'album': state['album'],
        'bpm': state['bpm'],
        'key': state.get('key'),
        'duration': metadata.get('duration', 0),
    })


def main():
    global prodj
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S'
    )
    
    logging.info("=" * 60)
    logging.info("🎛️  Pro DJ Link Bridge (Python)")
    logging.info("=" * 60)
    logging.info(f"Connecting to Pro DJ Link network...")
    logging.info(f"Pushing events to: {SOCKET_PATH}")
    
    try:
        # Create ProDj instance
        prodj = ProDj()
        
        # Set callbacks
        prodj.set_client_keepalive_callback(on_client_change)
        prodj.set_client_change_callback(on_client_change)
        
        # Start ProDj
        prodj.start()
        
        # Configure virtual CDJ
        prodj.cl.auto_request_beatgrid = True  # Need beatgrids for position tracking
        prodj.vcdj_set_player_number(VCDJ_PLAYER_NUMBER)
        prodj.vcdj_enable()
        
        logging.info(f"✅ Virtual CDJ enabled (Player {VCDJ_PLAYER_NUMBER})")
        logging.info("Listening for Pro DJ Link devices...")
        
        # Send initial connection status
        send_to_lightd('connection', {
            'status': 'connected',
            'vcdj_number': VCDJ_PLAYER_NUMBER,
        })
        
        # Track metadata requests for active decks
        def monitor_decks():
            """Periodically request metadata for active decks."""
            while True:
                time.sleep(2)
                
                for client in prodj.cl.clients:
                    pn = client.player_number
                    tid = getattr(client, 'track_id', None)
                    lslot = getattr(client, 'loaded_slot', None)
                    lpn = getattr(client, 'loaded_player_number', None)
                    logging.info(f"Monitor: deck {pn} track_id={tid} slot={lslot} loaded_from={lpn}")

                    if not (hasattr(client, 'track_id') and client.track_id and client.track_id > 0 and hasattr(client, 'loaded_slot')):
                        continue

                    # New track_id on a deck = actual load event.
                    if deck_last_track_id.get(pn) != client.track_id:
                        logging.info(f"New track detected on deck {pn}: {deck_last_track_id.get(pn)} -> {client.track_id}")
                        deck_last_track_id[pn] = client.track_id
                        deck_last_sent_analysis.pop(pn, None)
                        deck_waveform.pop(pn, None)
                        deck_beatgrid.pop(pn, None)
                        deck_song_structure.pop(pn, None)
                        deck_analysis_pending[pn] = {"waveform", "beatgrid"}

                        send_to_lightd('analysis_status', {
                            'deck': pn,
                            'status': 'analyzing'
                        })

                        def _make_cb(player_num):
                            return lambda req, pn, slot, tid, md: on_track_metadata(req, player_num, slot, tid, md)
                        def _make_waveform_cb(player_num):
                            return lambda req, pn, slot, tid, data: on_waveform_received(req, player_num, slot, tid, data)
                        def _make_beatgrid_cb(player_num):
                            return lambda req, pn, slot, tid, data: on_beatgrid_received(req, player_num, slot, tid, data)

                        prodj.data.get_metadata(
                            client.loaded_player_number,
                            client.loaded_slot,
                            client.track_id,
                            _make_cb(pn)
                        )
                        try:
                            prodj.data.get_color_waveform(
                                client.loaded_player_number,
                                client.loaded_slot,
                                client.track_id,
                                _make_waveform_cb(pn)
                            )
                        except Exception as e:
                            logging.warning(f"Color waveform request failed, trying regular: {e}")
                            try:
                                prodj.data.get_waveform(
                                    client.loaded_player_number,
                                    client.loaded_slot,
                                    client.track_id,
                                    _make_waveform_cb(pn)
                                )
                            except Exception as e2:
                                logging.error(f"Waveform request failed: {e2}")
                        try:
                            prodj.data.get_beatgrid(
                                client.loaded_player_number,
                                client.loaded_slot,
                                client.track_id,
                                _make_beatgrid_cb(pn)
                            )
                        except Exception as e:
                            logging.error(f"Beatgrid request failed: {e}")
                        continue

                    # If lightd restarted and lost analysis, re-send cached analysis for same track.
                    if deck_last_sent_analysis.get(pn) != client.track_id:
                        cached = deck_analysis_cache.get((pn, client.track_id))
                        if cached:
                            logging.info(f"Re-sending cached analysis for deck {pn}, track {client.track_id}")
                            send_to_lightd('track_analysis', cached)
                            deck_last_sent_analysis[pn] = client.track_id
        
        # Start metadata monitor thread
        metadata_thread = threading.Thread(target=monitor_decks, daemon=True)
        metadata_thread.start()
        
        # Main loop - just keep alive
        prodj.join()
        
    except KeyboardInterrupt:
        logging.info("\n🛑 Shutting down...")
    except Exception as e:
        logging.error(f"❌ Error: {e}", exc_info=True)
    finally:
        if prodj:
            prodj.stop()
        
        # Send disconnection status
        send_to_lightd('connection', {
            'status': 'disconnected',
        })
        _close_lightd_sock()
        
        logging.info("✅ Shutdown complete")


if __name__ == '__main__':
    main()
