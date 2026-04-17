#!/usr/bin/env python3
"""
Pro DJ Link Bridge with Waveform Analysis

Connects to Pioneer Pro DJ Link devices (XDJ-XZ), requests waveform data,
analyzes track structure, and generates complete scene maps for lightd.

Phase 2+3 implementation: on-load track analysis with full sequence generation.
"""

import os
import sys
import json
import time
import socket
import logging
import threading
import random
from collections import defaultdict, deque

# Add python-prodj-link to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'python-prodj-link'))

from prodj.core.prodj import ProDj

# Import scenes to match categories to scene pools
sys.path.insert(0, os.path.dirname(__file__))
from scenes_v2 import get_scenes_by_category, SCENES

SOCKET_PATH = '/tmp/lightd.sock'
VCDJ_PLAYER_NUMBER = 5

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
    'loaded_player_number': None,
    'loaded_slot': None,
    'track_id': None,
})

# Color theme pools (avoid repeating between decks)
COLOR_THEMES = [
    {"primary": [0, 100, 255], "secondary": [180, 0, 255]},      # blue / violet
    {"primary": [255, 0, 100], "secondary": [0, 200, 180]},      # magenta / teal
    {"primary": [255, 100, 0], "secondary": [0, 80, 255]},       # orange / blue
    {"primary": [0, 200, 180], "secondary": [255, 0, 80]},       # teal / pink
    {"primary": [180, 0, 255], "secondary": [0, 255, 120]},      # violet / emerald
    {"primary": [255, 0, 80], "secondary": [180, 0, 255]},       # pink / purple
    {"primary": [0, 255, 120], "secondary": [255, 100, 0]},      # emerald / orange
    {"primary": [0, 80, 255], "secondary": [255, 0, 100]},       # blue / magenta
]

used_color_themes = {}  # deck -> theme_index

last_master = None
prodj = None

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


def assign_color_theme(deck):
    """Assign a unique color theme to this deck, avoiding the other deck's theme."""
    global used_color_themes
    other_deck = 2 if deck == 1 else 1
    other_theme_idx = used_color_themes.get(other_deck)
    
    # Pool of available themes (exclude other deck's theme)
    available = [i for i in range(len(COLOR_THEMES)) if i != other_theme_idx]
    
    # Pick randomly from available
    theme_idx = random.choice(available)
    used_color_themes[deck] = theme_idx
    
    return COLOR_THEMES[theme_idx]


def parse_color_waveform(waveform_data):
    """Parse PWV4/PWV5 color waveform data.
    
    Color waveform format (PWV4):
    - 6 bytes per entry (R, G, B, R, G, B) — low/mid/high frequency energy encoded as color
    - Entries are per half-beat (so a 4-minute track at 128 BPM = ~2048 entries)
    
    Returns: list of energy values (0.0-1.0) per half-beat, derived from RGB magnitude.
    """
    if not waveform_data or 'entries' not in waveform_data:
        return []
    
    entries = waveform_data['entries']
    energy_profile = []
    
    # PWV4: 6 bytes per entry (payload_word_size=6)
    # Each entry: [R_low, G_low, B_low, R_mid, G_mid, B_mid] (frequency bands)
    # We'll use total magnitude as energy
    if isinstance(entries, list) and len(entries) > 0:
        for i in range(0, len(entries), 6):
            if i + 5 < len(entries):
                # Get RGB values (signed bytes, but we'll treat as magnitude)
                r_low, g_low, b_low = abs(entries[i]), abs(entries[i+1]), abs(entries[i+2])
                r_mid, g_mid, b_mid = abs(entries[i+3]), abs(entries[i+4]), abs(entries[i+5])
                
                # Total energy for this half-beat
                total = r_low + g_low + b_low + r_mid + g_mid + b_mid
                normalized = min(1.0, total / 255.0 / 6.0 * 2.0)  # scale to 0-1
                energy_profile.append(normalized)
    
    return energy_profile


def detect_sections(energy_profile, bpm):
    """Detect track sections from energy profile.
    
    Logic:
    - Intro: first section before energy builds (energy < 0.3 for 8+ half-beats)
    - Breakdown: sustained energy drop (>40% decrease for 16+ half-beats)
    - Buildup: energy ramp after breakdown (continuous increase over 8+ half-beats)
    - Drop: sudden energy spike after buildup (>60% increase)
    - Outro: last section where energy fades (energy < 0.3 for final 16+ half-beats)
    - Groove: everything else (steady mid-high energy)
    
    Returns: list of section dicts with type, start_beat, end_beat
    """
    if not energy_profile or len(energy_profile) < 16:
        # Fallback: whole track is groove
        return [{"type": "groove", "start_beat": 0, "end_beat": len(energy_profile) * 2}]
    
    sections = []
    window_size = 16  # 8 beats (16 half-beats)
    i = 0
    half_beats = len(energy_profile)
    
    # Helper: moving average
    def avg(start, end):
        if start < 0: start = 0
        if end > len(energy_profile): end = len(energy_profile)
        if end <= start: return 0
        return sum(energy_profile[start:end]) / (end - start)
    
    # Detect intro (low energy at start)
    intro_end = 0
    for i in range(0, min(64, half_beats), window_size):
        if avg(i, i + window_size) > 0.3:
            intro_end = i
            break
    if intro_end > 0:
        sections.append({"type": "intro", "start_beat": 0, "end_beat": intro_end * 2})
    
    # Detect outro (low energy at end)
    outro_start = half_beats
    for i in range(half_beats - window_size, max(half_beats - 128, intro_end), -window_size):
        if avg(i, i + window_size) > 0.3:
            outro_start = i + window_size
            break
    
    # Scan middle for breakdowns, buildups, drops
    i = intro_end
    current_section = None
    current_start = i
    baseline = avg(i, i + window_size) if i < half_beats - window_size else 0.5
    
    while i < outro_start:
        # Current window energy
        curr_energy = avg(i, min(i + window_size, half_beats))
        
        # Check for breakdown (40% drop sustained for 16+ half-beats)
        if curr_energy < baseline * 0.6 and current_section != 'breakdown':
            if current_section and current_start < i:
                sections.append({"type": current_section, "start_beat": current_start * 2, "end_beat": i * 2})
            current_section = 'breakdown'
            current_start = i
            baseline = curr_energy
        
        # Check for buildup (energy ramping up from breakdown)
        elif current_section == 'breakdown' and curr_energy > baseline * 1.3:
            sections.append({"type": 'breakdown', "start_beat": current_start * 2, "end_beat": i * 2})
            current_section = 'buildup'
            current_start = i
            baseline = curr_energy
        
        # Check for drop (sudden spike after buildup)
        elif current_section == 'buildup' and curr_energy > baseline * 1.4:
            sections.append({"type": 'buildup', "start_beat": current_start * 2, "end_beat": i * 2})
            current_section = 'drop'
            current_start = i
            baseline = curr_energy
        
        # Groove (steady energy)
        elif curr_energy > 0.4 and current_section not in ('groove', 'drop'):
            if current_section and current_start < i:
                sections.append({"type": current_section, "start_beat": current_start * 2, "end_beat": i * 2})
            current_section = 'groove'
            current_start = i
            baseline = curr_energy
        
        # Update baseline gradually
        baseline = baseline * 0.9 + curr_energy * 0.1
        i += window_size // 2
    
    # Close final section before outro
    if current_section and current_start < outro_start:
        sections.append({"type": current_section, "start_beat": current_start * 2, "end_beat": outro_start * 2})
    
    # Add outro
    if outro_start < half_beats:
        sections.append({"type": "outro", "start_beat": outro_start * 2, "end_beat": half_beats * 2})
    
    # If no sections detected, default to groove
    if not sections:
        sections.append({"type": "groove", "start_beat": 0, "end_beat": half_beats * 2})
    
    return sections


def assign_scenes_to_sections(sections):
    """Assign scenes from scenes_v2.py to each section.
    
    Uses the existing category pools. For each section, pick a random scene
    from the appropriate category pool, avoiding repetition.
    
    Returns: sections list with 'scene' and 'category' fields added.
    """
    used_scenes = set()
    
    # Map section types to scene categories
    type_to_category = {
        'intro': 'ambient',
        'breakdown': 'breakdown',
        'buildup': 'buildup',
        'drop': 'drop',
        'outro': 'ambient',
        'groove': 'groove',
    }
    
    for section in sections:
        section_type = section['type']
        category = type_to_category.get(section_type, 'groove')
        
        # Get scene pool for this category
        pool = get_scenes_by_category(category)
        if not pool:
            pool = SCENES
        
        # Avoid recent scenes
        available = [s for s in pool if s['name'] not in used_scenes]
        if not available:
            available = pool
        
        # Pick random scene
        scene = random.choice(available)
        section['scene'] = scene['name']
        section['category'] = category
        used_scenes.add(scene['name'])
        
        # Keep used_scenes size limited
        if len(used_scenes) > 10:
            used_scenes.pop()
    
    return sections


def on_waveform_received(request, player_number, slot, track_id, waveform_data):
    """Called when color waveform data is received from Pro DJ Link."""
    logging.info(f"📊 Waveform received for deck {player_number}, track {track_id}")
    
    # Parse waveform into energy profile
    energy_profile = parse_color_waveform(waveform_data)
    
    if not energy_profile:
        logging.warning(f"Failed to parse waveform for deck {player_number}")
        send_to_lightd('track_analysis', {
            'deck': player_number,
            'status': 'failed',
            'reason': 'waveform_parse_error',
        })
        return
    
    state = deck_state[player_number]
    bpm = state.get('bpm', 128.0)
    
    logging.info(f"  Energy profile: {len(energy_profile)} half-beats, BPM {bpm}")
    
    # Detect sections
    sections = detect_sections(energy_profile, bpm)
    logging.info(f"  Detected {len(sections)} sections: {[s['type'] for s in sections]}")
    
    # Assign scenes
    sections = assign_scenes_to_sections(sections)
    
    # Assign color theme
    color_theme = assign_color_theme(player_number)
    
    # Build complete track analysis event
    analysis = {
        'deck': player_number,
        'title': state.get('title', 'Unknown'),
        'artist': state.get('artist', 'Unknown'),
        'bpm': bpm,
        'color_theme': color_theme,
        'waveform': energy_profile,
        'sections': sections,
        'status': 'ready',
    }
    
    # Log summary
    logging.info(f"  🎨 Color theme: {color_theme}")
    logging.info(f"  Sections:")
    for s in sections:
        logging.info(f"    [{s['type']:10}] beats {s['start_beat']:4}-{s['end_beat']:4} → {s['scene']}")
    
    # Send to lightd
    send_to_lightd('track_analysis', analysis)


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
    
    # Check for track load (new track_id)
    if hasattr(client, 'track_id') and client.track_id and client.track_id > 0:
        if client.track_id != state.get('track_id'):
            state['track_id'] = client.track_id
            state['loaded_player_number'] = getattr(client, 'loaded_player_number', player_number)
            state['loaded_slot'] = getattr(client, 'loaded_slot', 2)  # default USB slot
            changed['track_loaded'] = True
    
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
            }
        })


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
    
    # Request waveform for analysis
    if prodj and state.get('loaded_player_number') and state.get('loaded_slot') and state.get('track_id'):
        logging.info(f"  Requesting waveform for analysis...")
        
        def waveform_callback(req, pn, sl, tid, data):
            on_waveform_received(req, player_number, sl, tid, data)
        
        try:
            # Request color waveform (PWV4/PWV5)
            prodj.data.get_color_waveform(
                state['loaded_player_number'],
                state['loaded_slot'],
                state['track_id'],
                waveform_callback
            )
        except Exception as e:
            logging.error(f"  Failed to request waveform: {e}")
            send_to_lightd('track_analysis', {
                'deck': player_number,
                'status': 'failed',
                'reason': 'waveform_request_error',
            })


def main():
    global prodj
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S'
    )
    
    logging.info("=" * 60)
    logging.info("🎛️  Pro DJ Link Bridge + Waveform Analysis (Phase 2+3)")
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
        prodj.cl.auto_request_beatgrid = False  # We don't need beatgrids
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
            requested = set()
            while True:
                time.sleep(2)
                
                for client in prodj.cl.clients:
                    pn = client.player_number
                    
                    # Request metadata if we have a track loaded but no metadata yet
                    if (hasattr(client, 'track_id') and client.track_id and 
                        client.track_id > 0 and
                        hasattr(client, 'loaded_slot') and
                        (pn, client.track_id) not in requested):
                        
                        logging.debug(f"Requesting metadata for deck {pn}, track {client.track_id}")
                        
                        def _make_cb(player_num):
                            return lambda req, pn, slot, tid, md: on_track_metadata(req, player_num, slot, tid, md)
                        
                        prodj.data.get_metadata(
                            client.loaded_player_number,
                            client.loaded_slot,
                            client.track_id,
                            _make_cb(pn)
                        )
                        requested.add((pn, client.track_id))
        
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
