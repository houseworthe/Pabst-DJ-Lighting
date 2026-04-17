#!/usr/bin/env python3
"""
Pro DJ Link Raw Bridge — Direct UDP packet parser

Bypasses python-prodj-link entirely. Listens on the Pro DJ Link ports,
decodes XDJ-XZ status/beat packets from raw bytes, and pushes events
to lightd via Unix socket.

Designed for all-in-one units (XDJ-XZ, XDJ-RX3) that confuse the
library's keepalive negotiation with multi-device announcements.

Packet format reverse-engineered from tcpdump captures of XDJ-XZ fw 1.24.

Usage:
    python3 prodjlink_raw.py [--interface en0]
"""

import os
import sys
import json
import time
import struct
import socket
import signal
import logging
import threading
from collections import defaultdict

SOCKET_PATH = '/tmp/lightd.sock'

# Pro DJ Link ports
KEEPALIVE_PORT = 50000 # Keepalive packets (broadcast)
BEAT_PORT = 50001      # Beat packets (broadcast)
STATUS_PORT = 50002    # Status packets (unicast)

VCDJ_PLAYER_NUMBER = 5  # Virtual CDJ player number (avoid 1-4)

# Packet magic header (10 bytes)
MAGIC = b'Qspt1WmJOL'

# Status packet sub-types (byte at offset 0x34)
STATUS_TYPE_CDJ = 0x0a  # Full CDJ status (292 bytes)

# Play state flags in status packets
PLAY_STATE_MAP = {
    0x00: 'stopped',
    0x04: 'playing',
    0x06: 'playing',  # playing + looping
    0x0e: 'playing',  # playing + looping + master
    0x05: 'paused',   # cued
}

# State tracking per deck
deck_state = defaultdict(lambda: {
    'bpm': 0.0,
    'beat': 0,        # beat within bar (1-4)
    'beat_count': 0,   # total beat count from track start
    'play_state': 'stopped',
    'pitch': 1.0,
    'loop_active': False,
    'on_air': False,
    'is_master': False,
    'title': None,
    'artist': None,
    'key': None,
    'last_update': 0,
})

# Persistent lightd socket
_lightd_sock = None
_lightd_lock = threading.Lock()
_running = True


_lightd_connected_logged = False

def get_lightd_sock():
    """Get or create persistent connection to lightd."""
    global _lightd_sock, _lightd_connected_logged
    if _lightd_sock is not None:
        return _lightd_sock
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(SOCKET_PATH)
        _lightd_sock = sock
        if not _lightd_connected_logged:
            logging.info("Connected to lightd")
            _lightd_connected_logged = True
        return sock
    except Exception as e:
        logging.debug(f"lightd connect failed: {e}")
        return None


def close_lightd_sock():
    global _lightd_sock, _lightd_connected_logged
    if _lightd_sock:
        try:
            _lightd_sock.close()
        except:
            pass
        _lightd_sock = None
        _lightd_connected_logged = False


def send_to_lightd(event_type, data):
    """Send JSON event to lightd via a fresh Unix socket connection."""
    event = {
        'source': 'prodjlink',
        'type': event_type,
        'timestamp': time.time(),
        **data
    }
    msg = json.dumps(event) + '\n'

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect(SOCKET_PATH)
        sock.sendall(msg.encode())
        try:
            sock.recv(4096)
        except:
            pass
        sock.close()
        return True
    except Exception:
        return False


def parse_beat_packet(data):
    """
    Parse a Pro DJ Link beat packet (port 50001, 96 bytes).

    Offset map (verified from XDJ-XZ fw 1.24 tcpdump):
    0x00-0x09: Magic "Qspt1WmJOL" (10 bytes)
    0x0a:      Packet type (0x28 = beat)
    0x0b-0x1f: Device name (null-padded)
    0x20-0x21: Unknown
    0x21:      Player number (1-indexed)
    0x22-0x23: Payload length (0x003c = 60)
    0x24-0x53: Beat grid + padding
    0x54-0x57: Pitch (uint32, /0x100000 = ratio)
    0x58-0x5b: BPM * 100 (uint32 BE)
    0x5c:      Beat within bar (1-4, 1-indexed)
    0x5d-0x5e: Padding
    0x5f:      Player number / device type
    """
    if len(data) < 0x5f:
        return None
    if data[:10] != MAGIC:
        return None

    pkt_type = data[0x0a]
    if pkt_type != 0x28:
        return None  # not a beat packet

    player_number = data[0x21]

    # BPM at offset 0x58-0x5b, big-endian uint32, divided by 100
    bpm_raw = struct.unpack('>I', data[0x58:0x5c])[0]
    bpm = bpm_raw / 100.0

    # Beat within bar at offset 0x5c (1-indexed)
    beat_in_bar = data[0x5c]
    if beat_in_bar < 1 or beat_in_bar > 4:
        beat_in_bar = 1

    # Pitch at 0x54-0x57
    pitch_raw = struct.unpack('>I', data[0x54:0x58])[0]
    pitch = pitch_raw / 0x100000 if pitch_raw > 0 else 1.0

    return {
        'player': player_number,
        'bpm': bpm,
        'beat_in_bar': beat_in_bar,
        'pitch': pitch,
    }


def parse_status_packet(data):
    """
    Parse a Pro DJ Link status packet (port 50002, 292 bytes).

    Offset map (verified from XDJ-XZ fw 1.24 tcpdump captures):
    0x00-0x09: Magic "Qspt1WmJOL" (10 bytes)
    0x0a:      Packet type (0x0a = CDJ status)
    0x0b-0x1f: Device name (null-padded "XDJ-XZ")
    0x20:      Sub-type (0x05 = from deck)
    0x21:      Deck ID (0x01 = deck 1, 0x02 = deck 2)
    0x6b:      Play state (0x00=stopped, 0x04=playing, 0x06=playing+loop)
    0x8c-0x8f: Pitch (uint32 BE, /0x100000)
    0x92-0x93: BPM * 100 (uint16 BE)
    0xa6:      Beat within bar (1-4, 1-indexed)
    """
    if len(data) < 0xa7:
        return None
    if data[:10] != MAGIC:
        return None

    pkt_type = data[0x0a]
    if pkt_type != 0x0a:
        return None

    # Device name
    name_end = data.find(b'\x00', 0x0b)
    if name_end < 0 or name_end > 0x20:
        name_end = 0x20
    device_name = data[0x0b:name_end].decode('ascii', errors='replace')

    sub_byte = data[0x20]   # 0x05 = deck status
    deck_id = data[0x21]    # 0x01 = deck 1, 0x02 = deck 2

    if len(data) < 292:
        return None

    # Play state at offset 0x6b
    play_state_byte = data[0x6b]
    play_state = PLAY_STATE_MAP.get(play_state_byte, f'unknown_{play_state_byte:#x}')

    # BPM at 0x92-0x93 (big-endian uint16, /100)
    bpm_raw = struct.unpack('>H', data[0x92:0x94])[0]
    if bpm_raw == 0xffff:
        bpm = 0.0  # no track loaded
    else:
        bpm = bpm_raw / 100.0

    # Pitch at 0x8c-0x8f
    pitch_raw = struct.unpack('>I', data[0x8c:0x90])[0]
    pitch = pitch_raw / 0x100000 if pitch_raw > 0 else 1.0

    # Beat within bar at 0xa6 (1-indexed, cycles 1-4)
    beat_in_bar = data[0xa6]
    if beat_in_bar < 1 or beat_in_bar > 4:
        beat_in_bar = 0  # unknown/stopped

    # Track beat count at 0xa2-0xa3 (uint16 BE) — actual position in track
    track_beat_count = struct.unpack('>H', data[0xa2:0xa4])[0]

    # Track ID at 0x2c-0x2f (uint32 BE)
    track_id = struct.unpack('>I', data[0x2c:0x30])[0]

    # Loaded player at 0x28, loaded slot at 0x29 (0x02 = sd, 0x03 = usb)
    loaded_player = data[0x28]
    loaded_slot_byte = data[0x29]
    loaded_slot = 'sd' if loaded_slot_byte == 0x02 else 'usb' if loaded_slot_byte == 0x03 else 'unknown'

    # Is this deck loaded? Check if BPM > 0
    is_loaded = bpm > 0 and sub_byte == 0x05

    return {
        'device_name': device_name,
        'deck': deck_id,
        'play_state': play_state,
        'play_state_byte': play_state_byte,
        'bpm': bpm,
        'pitch': pitch,
        'beat_in_bar': beat_in_bar,
        'is_loaded': is_loaded,
        'track_id': track_id,
        'loaded_player': loaded_player,
        'loaded_slot': loaded_slot,
        'track_beat_count': track_beat_count,
    }


# Global beat counter (used by the monkey-patched handlers)
_beat_counter = defaultdict(int)
_status_beat_counter = defaultdict(int)
_last_status_log = defaultdict(float)
_loop_pending = {}  # deck -> (is_looping, first_seen_time)  for debounce
LOOP_DEBOUNCE_MS = 500  # ignore loop toggles shorter than this

# Waveform energy tracking per deck
_deck_waveform = {}       # deck -> list of energy values (0-31)
_deck_beatgrid = {}       # deck -> list of beat entries with timestamps
_deck_track_id = {}       # deck -> current track_id
_deck_metadata = {}       # deck -> metadata dict
_deck_duration = {}       # deck -> track duration in seconds
_prodj_ref = None         # reference to ProDj instance for data queries
_last_energy_send = defaultdict(float)
_fetch_lock = threading.Lock()  # serialize track data requests


def fetch_track_data(deck, player_number, slot, track_id):
    """Fetch waveform, beatgrid, and metadata for a newly loaded track."""
    global _prodj_ref
    if _prodj_ref is None:
        return

    with _fetch_lock:  # serialize all data requests
        logging.info(f"Fetching track data: deck={deck} player={player_number} slot={slot} track={track_id}")
        # Wait for library's PDB download to finish
        time.sleep(5)

        try:
            _prodj_ref.data.dbc.receive_timeout_count = 10

            # Get metadata
            md = _prodj_ref.data.dbc.handle_request("metadata", (player_number, slot, track_id))
            if md:
                _deck_metadata[deck] = md
                _deck_duration[deck] = md.get('duration', 0)
                logging.info(f"Deck {deck}: {md.get('artist', '?')} - {md.get('title', '?')} ({md.get('bpm', 0)} BPM, {md.get('key', '?')})")
                send_to_lightd('track_load', {
                    'deck': deck,
                    'title': md.get('title', ''),
                    'artist': md.get('artist', ''),
                    'album': md.get('album', ''),
                    'bpm': md.get('bpm', 0),
                    'key': md.get('key', ''),
                    'duration': md.get('duration', 0),
                    'genre': md.get('genre', ''),
                })
        except Exception as e:
            logging.warning(f"Metadata fetch failed: {e}")

        try:
            # Get preview waveform (900 bytes = energy across track)
            wf = _prodj_ref.data.dbc.handle_request("preview_waveform", (player_number, slot, track_id))
            if wf:
                _deck_waveform[deck] = list(wf)
                logging.info(f"Deck {deck}: preview waveform loaded ({len(wf)} segments, max={max(wf)})")
        except Exception as e:
            logging.warning(f"Waveform fetch failed: {e}")

        try:
            # Get beatgrid
            bg = _prodj_ref.data.dbc.handle_request("beatgrid", (player_number, slot, track_id))
            if bg:
                _deck_beatgrid[deck] = bg
                logging.info(f"Deck {deck}: beatgrid loaded ({len(bg)} beats)")
        except Exception as e:
            logging.warning(f"Beatgrid fetch failed: {e}")


def get_energy_at_position(deck, beat_count):
    """Get the energy level (0-100) at the current beat position in the track."""
    wf = _deck_waveform.get(deck)
    bg = _deck_beatgrid.get(deck)
    if not wf:
        return None

    total_beats = len(bg) if bg else 0
    if total_beats == 0:
        return None

    # beat_count is the actual track position from the status packet
    # Clamp to valid range
    track_beat = min(beat_count, total_beats)

    # Map beat position to waveform index
    fraction = track_beat / max(total_beats, 1)
    wf_index = int(fraction * len(wf))
    wf_index = max(0, min(wf_index, len(wf) - 1))

    # Average a small window for smoothness
    window = 5
    start = max(0, wf_index - window)
    end = min(len(wf), wf_index + window)
    avg = sum(wf[start:end]) / max(1, end - start)

    # Normalize to 0-100
    max_val = max(wf) if max(wf) > 0 else 1
    energy = (avg / max_val) * 100.0
    return energy


def _raw_handle_beat(data, addr):
    """Our raw beat packet handler — called before the library's parser."""
    parsed = parse_beat_packet(data)
    if parsed is None:
        return

    player = parsed['player']
    bpm = parsed['bpm']
    beat_in_bar = parsed['beat_in_bar']

    if bpm <= 0:
        return

    state = deck_state[player]
    now = time.time()
    changed = {}

    if abs(state['bpm'] - bpm) > 0.01:
        changed['bpm'] = bpm
        state['bpm'] = bpm

    if state['beat'] != beat_in_bar:
        changed['beat'] = beat_in_bar
        state['beat'] = beat_in_bar
        _beat_counter[player] += 1
        changed['beat_count'] = _beat_counter[player]
        state['beat_count'] = _beat_counter[player]

    state['last_update'] = now

    if changed:
        logging.debug(f"Beat deck {player}: BPM={bpm:.2f} beat={beat_in_bar}/4 total={_beat_counter[player]}")
        send_to_lightd('deck_update', {
            'deck': player,
            'changes': changed,
            'state': {
                'bpm': state['bpm'],
                'beat': state['beat'],
                'beat_count': state['beat_count'],
                'play_state': state['play_state'],
                'pitch': state.get('pitch', 1.0),
                'loop_active': state.get('loop_active', False),
                'on_air': state.get('on_air', False),
                'is_master': state.get('is_master', True),
                'title': state.get('title'),
                'artist': state.get('artist'),
                'key': state.get('key'),
            }
        })


def _raw_handle_status(data, addr):
    """Our raw status packet handler — called before the library's parser."""
    parsed = parse_status_packet(data)
    if parsed is None:
        return

    deck = parsed['deck']
    if deck not in (1, 2):
        return

    # Skip decks that aren't actively playing
    if parsed['play_state'] == 'stopped' and parsed['bpm'] <= 0:
        return

    # Detect track change — fetch waveform data for new tracks
    new_track_id = parsed.get('track_id', 0)
    if new_track_id > 0 and new_track_id != _deck_track_id.get(deck, 0):
        _deck_track_id[deck] = new_track_id
        # Fetch in background thread to not block packet processing
        t = threading.Thread(
            target=fetch_track_data,
            args=(deck, parsed['loaded_player'], parsed['loaded_slot'], new_track_id),
            daemon=True
        )
        t.start()

    state = deck_state[deck]
    now = time.time()
    changed = {}

    # Play state
    new_play_state = parsed['play_state']
    if new_play_state != state['play_state']:
        changed['play_state'] = new_play_state
        state['play_state'] = new_play_state

    # BPM
    if parsed['bpm'] > 0 and abs(state['bpm'] - parsed['bpm']) > 0.01:
        changed['bpm'] = parsed['bpm']
        state['bpm'] = parsed['bpm']

    # Beat from status
    if parsed['beat_in_bar'] > 0 and parsed['beat_in_bar'] != state['beat']:
        changed['beat'] = parsed['beat_in_bar']
        state['beat'] = parsed['beat_in_bar']
        _status_beat_counter[deck] += 1
        # Only update beat_count from status if beat listener hasn't
        if _beat_counter[deck] == 0:
            changed['beat_count'] = _status_beat_counter[deck]
            state['beat_count'] = _status_beat_counter[deck]

    # Loop detection with debounce (play_state_byte alternates rapidly)
    is_looping = parsed['play_state_byte'] in (0x06, 0x0e)
    current_loop = state.get('loop_active', False)
    if is_looping != current_loop:
        # Check if this is a new pending change or continuation
        pending = _loop_pending.get(deck)
        if pending is None or pending[0] != is_looping:
            # New pending state
            _loop_pending[deck] = (is_looping, now)
        elif now - pending[1] >= LOOP_DEBOUNCE_MS / 1000.0:
            # Held long enough, commit
            changed['loop_active'] = is_looping
            state['loop_active'] = is_looping
            _loop_pending.pop(deck, None)
    else:
        # State matches, clear any pending
        _loop_pending.pop(deck, None)

    state['last_update'] = now

    # Send energy based on waveform position using REAL track beat count
    track_bc = parsed.get('track_beat_count', 0)
    if state.get('play_state') == 'playing' and deck in _deck_waveform and track_bc > 0:
        energy = get_energy_at_position(deck, track_bc)
        if energy is not None and now - _last_energy_send[deck] > 0.4:
            send_to_lightd('energy_update', {
                'deck': deck,
                'energy': round(energy, 1),
                'beat_count': track_bc,
            })
            _last_energy_send[deck] = now

    if changed:
        if now - _last_status_log[deck] > 1.0:
            logging.info(f"Status deck {deck}: {changed}")
            _last_status_log[deck] = now

        send_to_lightd('deck_update', {
            'deck': deck,
            'changes': changed,
            'state': {
                'bpm': state['bpm'],
                'beat': state['beat'],
                'beat_count': state['beat_count'],
                'play_state': state['play_state'],
                'pitch': parsed.get('pitch', 1.0),
                'loop_active': state.get('loop_active', False),
                'on_air': True,
                'is_master': (deck == 1),
                'title': state.get('title'),
                'artist': state.get('artist'),
                'key': state.get('key'),
            }
        })


def start_prodj_with_raw_hooks():
    """
    Start python-prodj-link for keepalive handshake, but monkey-patch
    the beat/status handlers to use our raw byte parser instead of
    the library's construct-based parser (which works fine, we just
    want to also run our own logic).
    """
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'python-prodj-link'))
        from prodj.core.prodj import ProDj

        prodj = ProDj()

        # Save original handlers
        orig_beat = prodj.handle_beat_packet
        orig_status = prodj.handle_status_packet

        # Monkey-patch: run our raw parser first, then optionally the original
        def hooked_beat(data, addr):
            _raw_handle_beat(data, addr)
            # Still let the library parse it (for its internal state)
            try:
                orig_beat(data, addr)
            except Exception:
                pass  # Library parse failures are fine — we got our data

        def hooked_status(data, addr):
            _raw_handle_status(data, addr)
            try:
                orig_status(data, addr)
            except Exception:
                pass

        prodj.handle_beat_packet = hooked_beat
        prodj.handle_status_packet = hooked_status

        global _prodj_ref
        _prodj_ref = prodj

        # Suppress noisy library logs (player number cycling spam)
        # The library uses root logger, so we filter by message
        class PlayerNumberFilter(logging.Filter):
            def filter(self, record):
                msg = record.getMessage()
                if 'changed player number' in msg:
                    return False
                if 'dropped due to timeout' in msg:
                    return False
                if 'pdb failed' in msg:
                    return False
                return True
        logging.getLogger().addFilter(PlayerNumberFilter())

        prodj.start()
        prodj.cl.auto_request_beatgrid = False
        prodj.data.pdb_enabled = False  # we use dbclient directly, skip PDB parsing
        prodj.vcdj_set_player_number(VCDJ_PLAYER_NUMBER)
        prodj.vcdj_enable()
        logging.info(f"ProDj started with raw hooks (vCDJ player {VCDJ_PLAYER_NUMBER})")
        return prodj
    except Exception as e:
        logging.error(f"Failed to start ProDj: {e}")
        import traceback
        traceback.print_exc()
        return None


def print_summary():
    """Print periodic status summary."""
    while _running:
        time.sleep(5)
        active = {d: s for d, s in deck_state.items() if s['last_update'] > time.time() - 10}
        if active:
            for d, s in sorted(active.items()):
                logging.info(
                    f"  Deck {d}: {s['bpm']:.1f} BPM | beat {s['beat']}/4 | "
                    f"{s['play_state']} | beats={s['beat_count']} | "
                    f"loop={'ON' if s.get('loop_active') else 'off'}"
                )
        else:
            logging.info("  No active decks")


def main():
    global _running

    import argparse
    parser = argparse.ArgumentParser(description='Pro DJ Link Raw Bridge')
    parser.add_argument('--interface', '-i', default='en0', help='Network interface')
    parser.add_argument('--debug', '-d', action='store_true', help='Debug logging')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S'
    )

    logging.info("=" * 60)
    logging.info("🎛️  Pro DJ Link Raw Bridge")
    logging.info("=" * 60)
    logging.info(f"Interface: {args.interface}")
    logging.info(f"Pushing events to: {SOCKET_PATH}")
    logging.info("No keepalive negotiation — pure packet sniffing")
    logging.info("")

    def shutdown(sig, frame):
        global _running
        logging.info("\n🛑 Shutting down...")
        _running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Send connection event
    send_to_lightd('connection', {
        'status': 'connected',
        'vcdj_number': 0,  # no vCDJ needed
        'mode': 'raw_listener',
    })

    # Start ProDj with monkey-patched handlers
    prodj = start_prodj_with_raw_hooks()
    if prodj is None:
        logging.error("Cannot start. Exiting.")
        return

    # Send connection event to lightd
    send_to_lightd('connection', {
        'status': 'connected',
        'vcdj_number': VCDJ_PLAYER_NUMBER,
        'mode': 'raw_hooks',
    })

    # Start summary printer
    summary_thread = threading.Thread(target=print_summary, daemon=True)
    summary_thread.start()

    logging.info("✅ Running. Library handles keepalive, we parse the data.")

    try:
        while _running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass

    _running = False

    send_to_lightd('connection', {'status': 'disconnected'})
    close_lightd_sock()

    if prodj:
        try:
            prodj.stop()
        except:
            pass

    logging.info("✅ Shutdown complete")


if __name__ == '__main__':
    main()
