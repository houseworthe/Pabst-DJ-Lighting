#!/usr/bin/env python3
"""
Audio-Reactive Govee Lighting Engine v2
- Govee LAN API (UDP, ~10ms latency) instead of cloud
- BPM detection + change tracking
- Frequency-reactive color mapping
- Beat-synced color shifts with predictive timing
"""

import pyaudio
import numpy as np
import subprocess
import threading
import socket
import json
import time
import sys
import os
import signal
from collections import deque

# ===== CONFIG =====
RATE = 44100
CHUNK = 2048  # ~46ms per frame
CHANNELS = 4  # Scarlett exposes 4 channels
INPUT_CHANNEL = 1  # 0-indexed; audio on channel 1 (Scarlett Input 2)
DEVICE_NAME = "Scarlett"

# Govee LAN API
LAN_PORT_SEND = 4003
LAN_PORT_RECV = 4002
LAN_MULTICAST = "239.255.255.250"
LAN_SCAN_PORT = 4001

# Cloud API fallback
API_KEY = json.load(open(os.path.expanduser("~/.config/govee/credentials.json")))["api_key"]
CLOUD_URL = "https://openapi.api.govee.com/router/api/v1"

# Color palette — 10 colors curated for house music vibes
PALETTE = [
    (148, 0, 211),    # violet
    (75, 0, 130),     # indigo
    (0, 0, 255),      # blue
    (0, 128, 255),    # cyan-blue
    (0, 255, 128),    # teal
    (255, 0, 128),    # hot pink
    (255, 0, 0),      # red
    (255, 64, 0),     # orange-red
    (255, 128, 0),    # orange
    (128, 0, 255),    # purple
]

# ===== GLOBALS =====
running = True
lan_devices = {}  # ip -> {device, sku, ...}
energy_history = deque(maxlen=100)
beat_times = deque(maxlen=60)
color_index = 0
current_bpm = 0.0
last_bpm = 0.0


def rgb_to_int(r, g, b):
    return (r << 16) | (g << 8) | b


def rgb_to_govee(r, g, b):
    """RGB tuple to Govee LAN color command value."""
    return {"r": r, "g": g, "b": b}


# ===== GOVEE LAN API =====

def lan_scan():
    """Discover Govee devices with LAN control enabled."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(4)

    recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    recv_sock.settimeout(4)
    recv_sock.bind(("", LAN_PORT_RECV))

    scan_msg = json.dumps({"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}})

    # Send to multicast, broadcast, and subnet broadcast
    sock.sendto(scan_msg.encode(), (LAN_MULTICAST, LAN_SCAN_PORT))
    sock.sendto(scan_msg.encode(), ("255.255.255.255", LAN_SCAN_PORT))
    # Try common subnet broadcasts
    sock.sendto(scan_msg.encode(), ("172.17.118.255", LAN_SCAN_PORT))
    sock.sendto(scan_msg.encode(), ("192.168.8.255", LAN_SCAN_PORT))

    devices = {}
    try:
        while True:
            data, addr = recv_sock.recvfrom(4096)
            msg = json.loads(data.decode())
            ip = addr[0]
            if "msg" in msg and "data" in msg["msg"]:
                d = msg["msg"]["data"]
                device_id = d.get("device", "unknown")
                sku = d.get("sku", "unknown")
                devices[ip] = {"device": device_id, "sku": sku, "ip": ip}
                print(f"  📡 {sku} @ {ip} ({device_id})")
    except socket.timeout:
        pass

    sock.close()
    recv_sock.close()
    return devices


def lan_send(ip, cmd, data):
    """Send a LAN command to a device."""
    msg = json.dumps({"msg": {"cmd": cmd, "data": data}})
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(msg.encode(), (ip, LAN_PORT_SEND))
        sock.close()
    except Exception:
        pass


def lan_turn(ip, on_off):
    """Turn device on/off via LAN."""
    lan_send(ip, "turn", {"value": 1 if on_off else 0})


def lan_brightness(ip, level):
    """Set brightness via LAN (0-100)."""
    lan_send(ip, "brightness", {"value": max(1, min(100, int(level)))})


def lan_color(ip, r, g, b):
    """Set color via LAN."""
    lan_send(ip, "colorwc", {"color": rgb_to_govee(r, g, b), "colorTemInKelvin": 0})


def set_all_color_lan(r, g, b):
    """Set all LAN devices to a color."""
    for ip in lan_devices:
        lan_color(ip, r, g, b)


def set_all_brightness_lan(level):
    """Set all LAN devices brightness."""
    for ip in lan_devices:
        lan_brightness(ip, level)


def set_group_color_lan(skus, r, g, b):
    """Set devices matching SKU list to a color."""
    for ip, info in lan_devices.items():
        if info["sku"] in skus:
            lan_color(ip, r, g, b)


# ===== CLOUD API FALLBACK =====

def cloud_send(device_id, sku, cap_type, instance, value):
    """Cloud API command (fallback if no LAN devices)."""
    from urllib.request import Request, urlopen
    payload = {
        "requestId": f"reactive-{int(time.time()*1000)}",
        "payload": {
            "sku": sku, "device": device_id,
            "capability": {"type": cap_type, "instance": instance, "value": value}
        }
    }
    def _send():
        try:
            req = Request(f"{CLOUD_URL}/device/control",
                          data=json.dumps(payload).encode(),
                          headers={"Govee-API-Key": API_KEY, "Content-Type": "application/json"},
                          method="POST")
            urlopen(req, timeout=2)
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()

# Cloud device lists (fallback)
CLOUD_COBS = [
    ("0A:6A:DD:99:82:46:7B:6B", "H61E5"), ("72:66:DD:99:80:86:28:0F", "H61E5"),
    ("0A:AB:D6:50:85:C6:10:42", "H61E5"), ("15:87:DB:C3:42:C6:3A:73", "H61E5"),
]
CLOUD_BULBS = [
    ("BF:D5:98:17:3C:73:C0:B4", "H6010"), ("08:E0:98:17:3C:74:BF:6A", "H6010"),
    ("16:09:98:17:3C:72:61:24", "H6010"), ("8C:64:98:17:3C:72:3F:30", "H6010"),
]
cloud_last_color = {}
cloud_last_brightness = {}
CLOUD_THROTTLE = 500  # ms

def set_all_color_cloud(r, g, b):
    rgb_int = rgb_to_int(r, g, b)
    for dev_id, sku in CLOUD_COBS + CLOUD_BULBS:
        cloud_send(dev_id, sku, "devices.capabilities.color_setting", "colorRgb", rgb_int)

def set_all_brightness_cloud(level):
    level = max(1, min(100, int(level)))
    now = time.time() * 1000
    for dev_id, sku in CLOUD_COBS + CLOUD_BULBS:
        if dev_id in cloud_last_brightness and (now - cloud_last_brightness[dev_id]) < CLOUD_THROTTLE:
            continue
        cloud_last_brightness[dev_id] = now
        cloud_send(dev_id, sku, "devices.capabilities.range", "brightness", level)


# ===== UNIFIED COMMANDS =====

use_lan = False

def set_color(r, g, b):
    if use_lan:
        set_all_color_lan(r, g, b)
    else:
        set_all_color_cloud(r, g, b)

def set_cobs_color(r, g, b):
    if use_lan:
        set_group_color_lan(["H61E5"], r, g, b)
    else:
        rgb_int = rgb_to_int(r, g, b)
        for dev_id, sku in CLOUD_COBS:
            cloud_send(dev_id, sku, "devices.capabilities.color_setting", "colorRgb", rgb_int)

def set_bulbs_color(r, g, b):
    if use_lan:
        set_group_color_lan(["H6010"], r, g, b)
    else:
        rgb_int = rgb_to_int(r, g, b)
        for dev_id, sku in CLOUD_BULBS:
            cloud_send(dev_id, sku, "devices.capabilities.color_setting", "colorRgb", rgb_int)

def set_brightness(level):
    if use_lan:
        set_all_brightness_lan(level)
    else:
        set_all_brightness_cloud(level)


# ===== AUDIO ANALYSIS =====

BANDS = {
    "sub":    (20, 80),
    "kick":   (80, 160),
    "snare":  (160, 400),
    "mid":    (400, 2500),
    "hat":    (2500, 8000),
    "air":    (8000, 16000),
}


def get_band_energy(magnitudes, freqs, low, high):
    mask = (freqs >= low) & (freqs < high)
    if not np.any(mask):
        return 0.0
    return float(np.mean(magnitudes[mask]))


def detect_beat(kick_energy, threshold=1.5):
    energy_history.append(kick_energy)
    if len(energy_history) < 10:
        return False
    avg = np.mean(energy_history)
    return kick_energy > avg * threshold


def estimate_bpm():
    if len(beat_times) < 4:
        return 0.0
    intervals = [beat_times[i] - beat_times[i-1] for i in range(1, len(beat_times))]
    # Filter: 70-180 BPM range
    intervals = [i for i in intervals if 0.333 < i < 0.857]
    if len(intervals) < 3:
        return 0.0
    # Use median for stability
    med_interval = float(np.median(intervals))
    return 60.0 / med_interval


# ===== MAIN LOOP =====

def find_scarlett(p):
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if DEVICE_NAME.lower() in info["name"].lower() and info["maxInputChannels"] > 0:
            print(f"🎤 Found: {info['name']} (index {i}, {info['maxInputChannels']} ch)")
            return i
    return None


def audio_reactive_loop(stream):
    global color_index, current_bpm, last_bpm, running

    print("\n🎧 Audio-Reactive Mode ACTIVE")
    print(f"   Mode: {'LAN (UDP ~10ms)' if use_lan else 'Cloud API (~300ms)'}")
    print(f"   Devices: {len(lan_devices) if use_lan else len(CLOUD_COBS) + len(CLOUD_BULBS)}")
    print("   Press Ctrl+C to stop\n")

    # Init: lights on, 5% brightness
    subprocess.run([os.path.expanduser("~/.local/bin/govee"), "on"], capture_output=True)
    time.sleep(0.3)
    set_brightness(5)

    last_beat_time = 0
    last_color_change = time.time()
    last_brightness_update = 0
    last_status = 0
    frame_count = 0
    locked_bpm = 0.0
    candidate_bpm = None
    candidate_start = 0

    while running:
        try:
            data = stream.read(CHUNK, exception_on_overflow=False)
            all_samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
            samples = all_samples[INPUT_CHANNEL::CHANNELS]
            samples = samples / 32768.0

            # FFT
            fft = np.fft.rfft(samples)
            magnitudes = np.abs(fft) / len(samples)
            freqs = np.fft.rfftfreq(len(samples), 1.0 / RATE)

            # Band energies
            energies = {}
            for name, (lo, hi) in BANDS.items():
                energies[name] = get_band_energy(magnitudes, freqs, lo, hi)

            total_energy = sum(energies.values())
            kick_energy = energies["sub"] + energies["kick"]

            now = time.time()

            # Beat detection
            is_beat = detect_beat(kick_energy)
            if is_beat and (now - last_beat_time) > 0.25:
                beat_times.append(now)
                last_beat_time = now

                new_bpm = estimate_bpm()
                if new_bpm > 0:
                    current_bpm = new_bpm

                    # BPM change detection:
                    # If BPM shifts by >=1 from locked BPM, start a candidate timer.
                    # If candidate holds stable for 5+ seconds, lock it in.
                    if locked_bpm > 0 and abs(current_bpm - locked_bpm) >= 1:
                        if candidate_bpm is None or abs(current_bpm - candidate_bpm) >= 2:
                            # New candidate
                            candidate_bpm = current_bpm
                            candidate_start = now
                        elif now - candidate_start >= 5.0:
                            # Candidate held for 5+ seconds — lock it
                            print(f"\n  🔄 BPM CHANGE: {locked_bpm:.0f} → {candidate_bpm:.0f}")
                            locked_bpm = candidate_bpm
                            candidate_bpm = None
                            candidate_start = 0
                            # Shift palette on BPM change
                            color_index = (color_index + 3) % len(PALETTE)
                    else:
                        # BPM is back within range of locked — cancel candidate
                        candidate_bpm = None
                        candidate_start = 0

                    # Initial lock
                    if locked_bpm == 0 and len(beat_times) >= 10:
                        locked_bpm = current_bpm
                        print(f"\n  🔒 BPM LOCKED: {locked_bpm:.0f}")

            # Color shift every 8 bars (15s at 128 BPM)
            if locked_bpm > 0:
                bars_duration = (8 * 4 * 60.0) / locked_bpm
            else:
                bars_duration = 15.0

            if now - last_color_change >= bars_duration:
                color_index = (color_index + 1) % len(PALETTE)
                r, g, b = PALETTE[color_index]
                comp_idx = (color_index + len(PALETTE) // 2) % len(PALETTE)
                cr, cg, cb = PALETTE[comp_idx]

                print(f"\n  🎨 COLOR CHANGE → COBs: ({r},{g},{b}) | Bulbs: ({cr},{cg},{cb}) | Next in {bars_duration:.1f}s")
                set_cobs_color(r, g, b)
                set_bulbs_color(cr, cg, cb)
                last_color_change = now

            # Brightness: smooth energy mapping (3-15%), update every 500ms
            if now - last_brightness_update > 0.5:
                brightness = int(np.clip(total_energy * 500, 3, 15))
                set_brightness(brightness)
                last_brightness_update = now

            # Status
            if now - last_status > 2.0:
                bpm_str = f"{locked_bpm:.0f}" if locked_bpm > 0 else (f"~{current_bpm:.0f}" if current_bpm > 0 else "...")
                cand_str = f" → {candidate_bpm:.0f}?" if candidate_bpm else ""
                bar_len = int(min(total_energy * 200, 30))
                bar = "█" * bar_len
                mode = "LAN" if use_lan else "CLOUD"
                print(f"  [{mode}] BPM: {bpm_str:>4}{cand_str} | Energy: {bar:<30} | "
                      f"Kick: {kick_energy:.4f} | Hat: {energies['hat']:.4f}")
                last_status = now

            frame_count += 1

        except Exception as e:
            if running:
                print(f"⚠️  Error: {e}")
                time.sleep(0.1)


def main():
    global running, use_lan, lan_devices

    print("=" * 60)
    print("🎛️  AUDIO-REACTIVE GOVEE LIGHTING ENGINE v2")
    print("=" * 60)

    # Try LAN discovery
    print("\n📡 Scanning for LAN devices...")
    lan_devices = lan_scan()

    if lan_devices:
        use_lan = True
        print(f"\n✅ LAN mode: {len(lan_devices)} devices found")
    else:
        use_lan = False
        print("\n⚠️  No LAN devices found. Falling back to Cloud API (~300ms latency).")
        print("   To enable LAN: Govee App → Device Settings → LAN Control → ON")

    # Find audio
    p = pyaudio.PyAudio()
    device_idx = find_scarlett(p)
    if device_idx is None:
        print("❌ Scarlett not found!")
        p.terminate()
        sys.exit(1)

    signal.signal(signal.SIGINT, lambda s, f: setattr(sys.modules[__name__], 'running', False) or print("\n🛑 Stopping..."))
    signal.signal(signal.SIGTERM, lambda s, f: setattr(sys.modules[__name__], 'running', False))

    try:
        stream = p.open(format=pyaudio.paInt16, channels=CHANNELS, rate=RATE,
                        input=True, input_device_index=device_idx, frames_per_buffer=CHUNK)
        audio_reactive_loop(stream)
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        print("Restoring lights...")
        subprocess.run([os.path.expanduser("~/.local/bin/govee"), "warm"], capture_output=True)
        subprocess.run([os.path.expanduser("~/.local/bin/govee"), "brightness", "10"], capture_output=True)
        p.terminate()
        print("✅ Done.")


if __name__ == "__main__":
    main()
