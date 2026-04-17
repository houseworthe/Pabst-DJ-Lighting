#!/usr/bin/env python3
"""
lightd — Unified DJ Lighting Daemon

Listens to audio from Scarlett, detects energy/vibe, selects and runs
appropriate DMX scenes + Govee colors. Runs as a persistent service.

Control via Unix socket at /tmp/lightd.sock:
  echo "start" | nc -U /tmp/lightd.sock
  echo "stop" | nc -U /tmp/lightd.sock
  echo "scene Ocean Drift" | nc -U /tmp/lightd.sock
  echo "strobe 180" | nc -U /tmp/lightd.sock
  echo "blackout" | nc -U /tmp/lightd.sock
  echo "status" | nc -U /tmp/lightd.sock
  echo "brightness 50" | nc -U /tmp/lightd.sock
  echo "bpm 128" | nc -U /tmp/lightd.sock    # manual BPM override
  echo "warm" | nc -U /tmp/lightd.sock
  echo "govee on|off" | nc -U /tmp/lightd.sock
  echo "quit" | nc -U /tmp/lightd.sock

Also provides CLI for convenience:
  python3 lightd.py                # start daemon
  python3 lightd.py --no-audio     # start without audio (manual control only)
"""

import os
import sys
import json
import math
import time
import signal
import socket
import random
import threading
import subprocess
from collections import deque

# Add project dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dmx_controller import DMX
from scenes_v2 import (
    SCENES, get_scene_for_energy, get_scenes_by_category,
)

# ===== CONFIG =====
SOCKET_PATH = '/tmp/lightd.sock'
RATE = 44100
CHUNK = 2048
CHANNELS = 4  # Scarlett exposes 4 channels
INPUT_CHANNEL = 1  # 0-indexed; audio on channel 1 (Scarlett Input 2)
DEVICE_NAME = "Scarlett"

# Energy smoothing
ENERGY_WINDOW = 80  # frames (~2 seconds at 40fps)
ENERGY_CHANGE_THRESHOLD = 15  # energy must shift this much to trigger category change
PHRASES_PER_EVAL = 1  # evaluate scene every N phrases (1 phrase = 8 bars)

# Govee
GOVEE_CLI = os.path.expanduser("~/.local/bin/govee")
GOVEE_PALETTE = [
    (148, 0, 211), (75, 0, 130), (0, 0, 255), (0, 128, 255),
    (0, 255, 128), (255, 0, 128), (255, 0, 0), (255, 64, 0),
    (255, 128, 0), (128, 0, 255),
]

# Audio analysis bands
BANDS = {
    "sub":   (20, 80),
    "kick":  (80, 160),
    "snare": (160, 400),
    "mid":   (400, 2500),
    "hat":   (2500, 8000),
    "air":   (8000, 16000),
}


class LightDaemon:
    def __init__(self, no_audio=False):
        self.running = True
        self.no_audio = no_audio

        # State
        self.mode = 'idle'  # idle, auto, manual_scene, strobe, warm, blackout
        self.current_scene = None
        self.current_scene_name = ''
        self.current_category = ''
        self.scene_start_time = 0
        self.scene_thread = None
        self.scene_running = False

        # Audio state
        self.energy = 0.0
        self.smoothed_energy = 0.0
        self.energy_history = deque(maxlen=ENERGY_WINDOW)
        self.energy_max_history = deque(maxlen=4800)  # ~2 minutes of frames for adaptive ceiling
        self.beat_times = deque(maxlen=60)
        self.bpm = 0.0
        self.locked_bpm = 0  # no BPM until Pro DJ Link or FFT provides one
        self.manual_bpm = None
        self.beat_count = 0  # total beats counted
        self.last_phrase_beat = 0  # beat count at last phrase evaluation
        self.phrase_count = 0

        # Pro DJ Link state
        self.prodjlink_connected = False
        self.prodjlink_master_deck = None
        self.prodjlink_decks = {}  # deck_number -> state dict
        self.prodjlink_clock_source = False  # True = use Pro DJ Link for BPM/beats
        self.prodjlink_last_beat = 0
        self.prodjlink_last_beat_time = 0

        # Govee — single-shot commands only, with lock to prevent overlap
        self.govee_enabled = True
        self.govee_lock = threading.Lock()
        self.govee_busy = False
        self.govee_color_idx = 0
        self.last_govee_change = 0

        # DMX
        self.dmx = None
        self.dmx_lock = threading.Lock()
        self.brightness_override = None  # None = auto

        # Kick flash layer
        self.kick_flash_enabled = True
        self.kick_flash_active = False
        self.kick_flash_time = 0
        self.kick_threshold_mult = 1.8  # kick must be this × average to trigger flash
        self.kick_history = deque(maxlen=30)

        # Drop detection
        self.energy_trend = deque(maxlen=16)
        self.kick_trend = deque(maxlen=60)  # longer history for baseline
        self.last_trend_time = 0
        self.had_breakdown = False
        self.buildup_armed = False
        self.kick_energy_history = deque(maxlen=60)
        self.kick_energy_baseline = 0
        self.last_kick_check = 0
        
        # Audio-based beat counter for PSSI section tracking
        self.audio_beat_count = 0
        self.audio_beat_tracking = False  # True when we're counting beats for a known track
        self._initial_load_done = False  # Skip auto-master on first analysis batch
        self.breakdown_start = 0
        self.drop_detection_ready = False  # wait for baseline to build
        self.audio_start_time = 0
        self.drop_cooldown = 0
        self.in_drop_sequence = False
        self.drop_sequence_end = 0
        self.last_kick_energy = 0

        # Scene map (from waveform analysis)
        self.scene_maps = {1: None, 2: None}
        self.current_section = None
        self.current_track_id = None
        self.analysis_status = {1: None, 2: None}

        # Timed automation mode for mapped tracks
        self.track_timer_active = False
        self.track_timer_deck = None
        self.track_timer_track_id = None
        self.track_timer_start_time = 0
        self.armed_deck = None

        # Visualization data (shared with dashboard)
        self.viz_lock = threading.Lock()
        self.viz_waveform = []  # last chunk of audio samples
        self.viz_spectrum = []  # FFT magnitude bins
        self.viz_bands = {}  # named frequency band energies
        self.viz_beat_flash = False
        self.viz_beat_count = 0

        # Audio
        self.audio_stream = None
        self.pyaudio_instance = None

    def start(self):
        """Start the daemon."""
        print("=" * 60)
        print("🎛️  lightd — Unified DJ Lighting Daemon")
        print("=" * 60)

        # Open DMX
        try:
            self.dmx = DMX()
            self.dmx.open()
            print("✅ DMX connected")
        except Exception as e:
            print(f"❌ DMX failed: {e}")
            print("   Running in Govee-only mode")
            self.dmx = None

        # Open audio
        if not self.no_audio:
            try:
                self._open_audio()
                print("✅ Audio connected")
            except Exception as e:
                print(f"⚠️  Audio failed: {e}")
                print("   Running in manual mode (no audio reactivity)")
                self.no_audio = True

        # Start idle keepalive (prevents fixtures from reverting)
        if self.dmx:
            self._start_keepalive()

        # Start control socket
        self._start_socket()

        # Start audio thread
        if not self.no_audio:
            threading.Thread(target=self._audio_loop, daemon=True).start()

        print(f"\n🔌 Control: echo '<cmd>' | nc -U {SOCKET_PATH}")
        print("   Commands: start, stop, status, scene <name>, strobe [speed],")
        print("             blackout, warm, brightness <0-100>, bpm <num>,")
        print("             govee on|off, quit")
        self.audio_start_time = time.time()
        print(f"\n{'🎧 Listening to audio...' if not self.no_audio else '⏸️  Manual mode (no audio)'}")

        # Main loop
        try:
            while self.running:
                if self.mode == 'auto' and self.track_timer_active and self.track_timer_deck and self.scene_maps.get(self.track_timer_deck):
                    scene_map = self.scene_maps[self.track_timer_deck]
                    bpm = float(scene_map.get('bpm') or self.locked_bpm or 128.0)
                    elapsed = time.time() - self.track_timer_start_time
                    beat_now = int(elapsed * bpm / 60.0) + 1
                    sections = scene_map.get('sections', [])
                    for section in sections:
                        if section['start_beat'] <= beat_now < section['end_beat']:
                            if self.current_section is None or self.current_section.get('start_beat') != section['start_beat'] or self.current_track_id != scene_map.get('track_id'):
                                self.current_section = section
                                self.current_track_id = scene_map.get('track_id')
                                scene_name = section.get('scene', '')
                                print(f"  ⏱️  TIMED SECTION: {section['type']} -> {scene_name} (elapsed {elapsed:.1f}s, beat {beat_now})")
                                for s in SCENES:
                                    if s['name'] == scene_name:
                                        self._run_scene(s)
                                        break
                            break
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self):
        """Clean shutdown."""
        print("\n🛑 Shutting down...")
        self.running = False
        self.scene_running = False

        if self.dmx:
            with self.dmx_lock:
                try:
                    self.dmx.blackout()
                    self.dmx.send_frame()
                    time.sleep(0.1)
                except Exception:
                    pass
                # Skip dmx.close() — libftdi's ftdi_usb_close segfaults on macOS
                # The OS will clean up the USB handle on process exit

        if hasattr(self, '_sd_stream') and self._sd_stream:
            self._sd_stream.stop()
            self._sd_stream.close()

        # Restore Govee to warm on shutdown
        if self.govee_enabled:
            self.govee_busy = False
            self._govee_cmd(["warm"])

        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass

        print("✅ Shutdown complete")

    # ===== AUDIO =====

    def _open_audio(self):
        import sounddevice as sd
        self._sd = sd
        # Find Scarlett device
        devices = sd.query_devices()
        device_idx = None
        for i, d in enumerate(devices):
            if DEVICE_NAME.lower() in d['name'].lower() and d['max_input_channels'] > 0:
                device_idx = i
                break
        if device_idx is None:
            raise RuntimeError("Scarlett not found")
        
        print(f"\u2705 Audio: {devices[device_idx]['name']} (device {device_idx})")
        self._sd_device = device_idx
        self._sd_stream = sd.InputStream(
            device=device_idx, channels=CHANNELS, samplerate=RATE,
            blocksize=CHUNK, dtype='int16'
        )
        self._sd_stream.start()
        self.audio_stream = True  # flag that audio is open

    def _audio_loop(self):
        """Continuously read audio and compute energy."""
        import numpy as np
        last_scene_check = 0

        while self.running:
            try:
                data, overflowed = self._sd_stream.read(CHUNK)
                all_samples = data.flatten().astype(np.float32)
                samples = all_samples[INPUT_CHANNEL::CHANNELS] / 32768.0

                # FFT
                fft = np.fft.rfft(samples)
                magnitudes = np.abs(fft) / len(samples)
                freqs = np.fft.rfftfreq(len(samples), 1.0 / RATE)

                # Band energies
                energies = {}
                for name, (lo, hi) in BANDS.items():
                    mask = (freqs >= lo) & (freqs < hi)
                    energies[name] = float(np.mean(magnitudes[mask])) if np.any(mask) else 0.0

                total_energy = sum(energies.values())
                kick_energy = energies["sub"] + energies["kick"]
                self.last_kick_energy = kick_energy

                # Store viz data for dashboard
                with self.viz_lock:
                    # Downsample waveform to 128 points
                    step = max(1, len(samples) // 128)
                    self.viz_waveform = samples[::step][:128].tolist()
                    # Downsample spectrum to 64 bins (log-ish)
                    spec = magnitudes[:len(magnitudes)//2]
                    if len(spec) > 64:
                        step_s = max(1, len(spec) // 64)
                        self.viz_spectrum = spec[::step_s][:64].tolist()
                    else:
                        self.viz_spectrum = spec.tolist()
                    self.viz_bands = {k: float(v) for k, v in energies.items()}

                # Normalize energy to 0-100
                # Semi-adaptive: use a slow-moving reference (2 min window)
                # so it adapts to gain staging but won't flatten drops
                raw_energy = total_energy * 5000
                self.energy_max_history.append(raw_energy)
                
                # Use 98th percentile as ceiling, always
                # Starts working immediately, gets better over time
                ceiling = np.percentile(self.energy_max_history, 98)
                ceiling = max(ceiling, 30)  # floor so silence doesn't blow up
                normalized = np.clip((raw_energy / ceiling) * 80, 0, 100)
                
                self.energy = normalized
                self.energy_history.append(normalized)
                self.smoothed_energy = float(np.mean(self.energy_history))

                # Beat detection
                now = time.time()
                self.kick_history.append(kick_energy)

                if len(self.energy_history) >= 10:
                    avg_kick = np.mean(list(self.energy_history)[-20:]) if len(self.energy_history) >= 20 else np.mean(self.energy_history)
                    if kick_energy > avg_kick * 0.00015 and (not self.beat_times or now - self.beat_times[-1] > 0.25):
                        self.beat_times.append(now)
                        self.beat_count += 1
                        self._update_bpm()
                        with self.viz_lock:
                            self.viz_beat_flash = True
                            self.viz_beat_count = self.beat_count
                        
                        # PSSI section tracking via audio beat count
                        if self.audio_beat_tracking and not self._deck_is_looping():
                            self.audio_beat_count += 1
                            master = self.prodjlink_master_deck
                            if master and self.scene_maps.get(master):
                                scene_map = self.scene_maps[master]
                                end_beat = scene_map.get('total_beats', 9999)
                                # Don't count past end of track
                                if self.audio_beat_count > end_beat:
                                    self.audio_beat_count = end_beat
                                sections = scene_map.get('sections', [])
                                for section in sections:
                                    if section['start_beat'] <= self.audio_beat_count < section['end_beat']:
                                        if self.current_section is None or self.current_section.get('start_beat') != section['start_beat']:
                                            self.current_section = section
                                            scene_name = section.get('scene', '')
                                            print(f"  \U0001f3ac AUDIO SECTION: {section['type']} -> {scene_name} (audio beat {self.audio_beat_count})")
                                            for s in SCENES:
                                                if s['name'] == scene_name:
                                                    self._run_scene(s)
                                                    break
                                        break

                        # Kick flash: pulse lights on strong kicks
                        if self.kick_flash_enabled and self.dmx and len(self.kick_history) > 5:
                            avg_k = np.mean(self.kick_history)
                            if avg_k > 0 and kick_energy > avg_k * self.kick_threshold_mult:
                                self._kick_flash(kick_energy, avg_k)

                        # Drop detection moved to 1-sec timer below (kick energy based, not beat based)

                        # Check phrase boundary: every 32 beats = 8 bars
                        beats_since_eval = self.beat_count - self.last_phrase_beat
                        if beats_since_eval >= 32 and self.mode == 'auto':
                            self.last_phrase_beat = self.beat_count
                            self.phrase_count += 1
                            self._eval_phrase()

                # Kick flash decay
                if self.kick_flash_active and now - self.kick_flash_time > 0.08:
                    self.kick_flash_active = False

                # Structure detection: drop check every frame, breakdown on 1s timer
                if self.mode == 'auto':
                    self._check_drop_instant(kick_energy, now)
                    if now - last_scene_check > 1.0:
                        self._check_breakdown()
                        last_scene_check = now

                # When buildup is armed, switch to a buildup scene if not already in one
                # Wait 3s after breakdown starts so breakdown scene gets screen time
                if self.buildup_armed and not self.in_drop_sequence:
                    if self.current_category != 'buildup' and now - self.breakdown_start > 3.0:
                        print(f"  🔺 BUILDUP — switching to buildup scene")
                        self._pick_scene('buildup')
                        self._buildup_scene_start = now
                        self.last_phrase_beat = self.beat_count

            except Exception as e:
                if self.running:
                    time.sleep(0.1)

    def _update_bpm(self):
        if self.manual_bpm:
            self.bpm = self.manual_bpm
            self.locked_bpm = self.manual_bpm
            return

        if len(self.beat_times) < 4:
            return
        intervals = [self.beat_times[i] - self.beat_times[i-1] for i in range(1, len(self.beat_times))]
        intervals = [i for i in intervals if 0.333 < i < 0.857]  # 70-180 BPM
        # Remove outliers: keep intervals within 15% of median
        if len(intervals) >= 5:
            med = float(np.median(intervals))
            intervals = [i for i in intervals if abs(i - med) / med < 0.15]
        if len(intervals) < 3:
            return
        import numpy as np
        med = float(np.median(intervals))
        self.bpm = 60.0 / med
        # Lock BPM after stable readings
        if abs(self.bpm - self.locked_bpm) > 3 and len(intervals) > 8:
            self.locked_bpm = self.bpm

    # ===== SCENE MANAGEMENT =====

    def _check_breakdown(self):
        """Check for breakdown (energy dip). Runs every ~1s."""
        if self.in_drop_sequence:
            return  # don't interrupt timed drop

        target_cat = self._energy_to_category(self.smoothed_energy)

        # Transition to breakdown/ambient from groove or drop
        if target_cat in ('breakdown', 'ambient') and self.current_category not in ('breakdown', 'ambient', 'buildup'):
            print(f"  🌊 BREAKDOWN DETECTED (energy={self.smoothed_energy:.0f}, beat={self.beat_count})")
            self._pick_scene(target_cat)
            self.last_phrase_beat = self.beat_count

    def _eval_phrase(self):
        """Called every 32 beats (8 bars). Evaluate energy and decide scene."""
        if self.in_drop_sequence:
            return  # don't interrupt timed drop

        target_cat = self._energy_to_category(self.smoothed_energy)

        if target_cat != self.current_category:
            print(f"  🔄 Phrase {self.phrase_count} (beat {self.beat_count}): {self.current_category} → {target_cat} (energy={self.smoothed_energy:.0f})")
            self._pick_scene(target_cat)
        elif self.phrase_count % 4 == 0:
            # Every 4 phrases (~60s at 128bpm), variety swap within same category
            print(f"  🔄 Phrase {self.phrase_count} (beat {self.beat_count}): variety swap in {target_cat} (energy={self.smoothed_energy:.0f})")
            self._pick_scene(target_cat)
        else:
            print(f"  ⏸️  Phrase {self.phrase_count} (beat {self.beat_count}): holding {self.current_scene_name} ({self.current_category}, energy={self.smoothed_energy:.0f})")

    def _energy_to_category(self, energy):
        """Map energy to category. Buildup/drop are structure-detected, not energy-based."""
        if energy < 8:
            return 'breakdown'
        elif energy < 20:
            return 'ambient'
        else:
            return 'groove'

    def _pick_scene(self, category=None):
        """Pick and start a new scene."""
        # Don't let energy-based picking override PSSI audio tracking
        if self.audio_beat_tracking:
            return
        if category:
            pool = get_scenes_by_category(category)
            if not pool:
                pool = SCENES
        else:
            pool = [get_scene_for_energy(self.smoothed_energy)]

        # Avoid repeating same scene
        candidates = [s for s in pool if s['name'] != self.current_scene_name]
        if not candidates:
            candidates = pool

        scene = random.choice(candidates)
        self._run_scene(scene)

    def _run_scene(self, scene):
        """Start running a scene in its own thread."""
        # Stop current scene
        self._stop_scene()

        self.current_scene = scene
        self.current_scene_name = scene['name']
        self.current_category = scene['category']
        self.scene_start_time = time.time()
        self.scene_running = True
        self._scene_stop = threading.Event()

        bpm = self.manual_bpm or self.locked_bpm or 128.0

        print(f"  🎨 {scene['category'].upper()} → {scene['name']} (E:{self.smoothed_energy:.0f} BPM:{bpm:.0f})")

        # Wrap the DMX object so send_frame raises when stopped
        stop_event = self._scene_stop
        real_dmx = self.dmx

        class StoppableDMX:
            """Proxy that raises StopIteration when scene should end."""
            def __getattr__(self, name):
                if stop_event.is_set():
                    raise StopIteration("scene stopped")
                return getattr(real_dmx, name)
            def send_frame(self):
                if stop_event.is_set():
                    raise StopIteration("scene stopped")
                return real_dmx.send_frame()
            def send_for(self, seconds):
                start = time.time()
                while time.time() - start < seconds:
                    if stop_event.is_set():
                        raise StopIteration("scene stopped")
                    real_dmx.send_frame()
                    time.sleep(0.023)

        # Create callbacks for scenes
        govee_callback = self._govee_cmd if self.govee_enabled else None
        energy_callback = lambda: self.smoothed_energy

        def run():
            try:
                if real_dmx:
                    scene['fn'](StoppableDMX(), bpm, govee_cmd=govee_callback, get_energy=energy_callback)
            except (StopIteration, Exception):
                pass  # scene stopped or error

        self.scene_thread = threading.Thread(target=run, daemon=True)
        self.scene_thread.start()

    def _stop_scene(self):
        """Stop current scene."""
        self.scene_running = False
        if hasattr(self, '_scene_stop'):
            self._scene_stop.set()
        if self.scene_thread and self.scene_thread.is_alive():
            self.scene_thread.join(timeout=2.0)
        self.current_scene = None
        self.current_scene_name = ''

    # ===== DROP DETECTION =====

    def _deck_is_looping(self):
        """Check if the master deck is currently looping."""
        master = self.prodjlink_master_deck
        if master and master in self.prodjlink_decks:
            ps = self.prodjlink_decks[master].get('play_state')
            # play_state 4 = looping, also check string values
            if ps in (4, 'looping', 'loop') or str(ps) == '4':
                return True
            # Also check loop_active flag
            if self.prodjlink_decks[master].get('loop_active'):
                return True
        return False

    def _check_drop_instant(self, kick_energy, now):
        """Called on every detected beat. Uses KICK presence to detect structure."""
        import numpy as np

        # Track energy + kick every ~1 second
        if now - self.last_trend_time > 1.0:
            self.energy_trend.append(self.smoothed_energy)
            self.kick_trend.append(kick_energy)
            self.last_trend_time = now

        # Check if timed drop sequence ended
        if self.in_drop_sequence and now > self.drop_sequence_end:
            self._end_drop_sequence()
            return

        # Don't re-trigger during cooldown or active drop sequence
        if now < self.drop_cooldown or self.in_drop_sequence:
            return

        if len(self.kick_trend) < 3:
            return

        # KICK ENERGY drop detection
        # Track raw kick band energy (sub + kick frequencies).
        # In house music: breakdown = kick disappears, drop = kick returns.
        # We compare current kick energy against the established baseline.

        # Check if timed drop sequence ended
        if self.in_drop_sequence and now > self.drop_sequence_end:
            self._end_drop_sequence()
            return

        if now < self.drop_cooldown or self.in_drop_sequence:
            return

        # Sample kick energy every second for baseline/breakdown
        if now - self.last_kick_check > 1.0:
            self.last_kick_check = now
            if len(self.kick_history) >= 10:
                smoothed_kick = float(np.mean(list(self.kick_history)[-40:]))
            else:
                smoothed_kick = kick_energy
            self.kick_energy_history.append(smoothed_kick)

        if len(self.kick_energy_history) < 5:
            return

        # Don't detect drops for first 15 seconds — let baseline stabilize
        if self.audio_start_time and now - self.audio_start_time < 15:
            return

        kick_list = list(self.kick_energy_history)
        
        # Baseline = 75th percentile (represents "kick is hitting normally")
        if not self.had_breakdown:
            baseline = float(np.percentile(kick_list, 75))
            if baseline > 0:
                self.kick_energy_baseline = baseline
        
        if self.kick_energy_baseline <= 0:
            return

        # Use current frame's kick energy for instant trigger (not 1-sec smoothed)
        instant_kick_ratio = kick_energy / self.kick_energy_baseline
        smoothed_ratio = kick_list[-1] / self.kick_energy_baseline if kick_list else 0
        
        # BREAKDOWN: smoothed kick below 50% for 3 consecutive seconds
        if not self.had_breakdown:
            recent = kick_list[-3:]
            ratios = [k / self.kick_energy_baseline for k in recent]
            if all(r < 0.50 for r in ratios):
                self.had_breakdown = True
                self.buildup_armed = True
                self.breakdown_start = now
                print(f"  🔇 BREAKDOWN (kick ratios: {[f'{r:.2f}' for r in ratios]}, baseline={self.kick_energy_baseline:.6f})")
                # Pick a breakdown scene — visible but subdued
                self._pick_scene('breakdown')

        # DROP: fires on EVERY FRAME for instant response
        # Uses instant kick ratio (single frame) — the moment a big kick hits, fire
        buildup_scene_age = now - getattr(self, '_buildup_scene_start', 0)
        if self.buildup_armed and now - self.breakdown_start > 2.0 and (self.current_category != 'buildup' or buildup_scene_age > 2.0):
            # Tension detection: sudden silence/vocal cut during buildup → full blackout
            # This is the "everything cuts out for a bar" moment before the drop
            if not getattr(self, '_tension_blackout', False):
                if self.smoothed_energy < 8 and now - self.breakdown_start > 15.0:
                    self._tension_blackout = True
                    self._tension_time = now
                    print(f"  🤫 TENSION CUT — blackout (energy={self.smoothed_energy:.0f})")
                    self._stop_scene()
                    if self.dmx:
                        try:
                            self.dmx.set_all(0, 0, 0, 0, 0)
                            self.dmx.send_frame()
                        except Exception:
                            pass
                    if self.govee_enabled:
                        self.govee_busy = False
                        self._govee_cmd(["off"])

            kick_back = instant_kick_ratio > 0.80
            energy_back = self.smoothed_energy > 35
            if kick_back or energy_back:
                reason = f"kick={instant_kick_ratio:.0%}" if kick_back else f"energy={self.smoothed_energy:.0f}"
                tension = " (from tension)" if getattr(self, '_tension_blackout', False) else ""
                print(f"  💥 DROP!{tension} {reason} (silent {now - self.breakdown_start:.1f}s)")
                self.had_breakdown = False
                self.buildup_armed = False
                self._tension_blackout = False
                self._fire_drop(now)
                return

        # Fallback: energy-based detection for tracks without clean breakdowns
        trend = list(self.energy_trend)
        current = self.smoothed_energy

        if len(trend) >= 4:
            recent_min = min(trend[-4:])
            energy_jump = current - recent_min
            
            if energy_jump > 35 and current > 70 and not self.buildup_armed:
                print(f"  💥 ENERGY SPIKE (jump={energy_jump:.0f}, now={current:.0f})")
                self.buildup_armed = True
                self._fire_drop(now)

        # Disarm if nothing happens for a while
        if self.buildup_armed and not self.kick_absent:
            if len(trend) >= 6:
                spread = max(trend[-6:]) - min(trend[-6:])
                if spread < 10 and current < 55:
                    self.buildup_armed = False
                    self._tension_blackout = False
                    print(f"  ⏸️  Buildup disarmed (settled)")

    def _end_drop_sequence(self):
        """End drop sequence, back to groove."""
        self.in_drop_sequence = False
        print(f"  ⏱️  Drop sequence ended, back to auto")
        # Pick groove (most likely post-drop state) — scene will set its own Govee
        self._pick_scene('groove')

    def _fire_drop(self, now):
        """Fire a timed 8-bar drop sequence. DMX strobe + Govee party mode."""
        bpm = self.manual_bpm or self.locked_bpm or 128.0
        beat = 60.0 / bpm
        eight_bars = beat * 32

        self.buildup_armed = False
        self.in_drop_sequence = True
        self.drop_sequence_end = now + eight_bars
        self.drop_cooldown = now + eight_bars + 4

        print(f"  💥💥💥 DROP FIRED! 8 bars ({eight_bars:.1f}s) at {bpm:.0f} BPM")

        # Kill any running scene — drop sequence owns the lights now
        self._stop_scene()
        self.current_scene_name = "💥 DROP"
        self.current_category = "drop"

        # Govee: intense scene for drop
        if self.govee_enabled:
            self.govee_busy = False
            from scenes_v2 import GOVEE_DROP
            self._govee_cmd(["scene", str(random.choice(GOVEE_DROP))])

        # DMX: start drop sequence in a thread
        # Randomly pick a drop MODE — each one is a full 8-bar pattern
        def _drop_sequence():
            import random
            try:
                beat_dur = beat
                total_beats = 32
                
                # Pick a random drop mode — ALL use movement + blackout for max impact
                mode = random.choice([
                    'machine_gun',
                    'color_cannon',
                    'ping_pong',
                    'scatter_blast',
                    'split_strobe',
                    'blackout_kicks',
                ])
                self.current_scene_name = f"💥 {mode.replace('_', ' ').title()}"
                print(f"  🎆 Drop mode: {mode}")
                
                colors = [(255,0,0,0), (0,0,255,0), (255,0,200,0), (0,255,0,0), (255,128,0,0), (0,255,255,0)]
                
                for b in range(total_beats):
                    if not self.in_drop_sequence:
                        return
                    
                    if mode == 'machine_gun':
                        # 12th-beat chase (3x speed) — blazing fast zone cycle
                        twelfth = beat_dur / 12
                        for q in range(12):
                            if not self.in_drop_sequence:
                                return
                            zone = (b * 12 + q) % 5
                            self.dmx.set_12s(0, 0, 0, 0, 0)
                            for z in range(1, 5):
                                self.dmx.set_bar_zone(z, 0, 0, 0, 0, 0)
                            c = colors[(b * 12 + q) % len(colors)]
                            if zone == 0:
                                self.dmx.set_12s(*c, 64)
                            else:
                                self.dmx.set_bar_zone(zone, *c, 64)
                            self.dmx.send_frame()
                            time.sleep(twelfth)
                    
                    elif mode == 'color_cannon':
                        # 3 color slams per beat with blackout gaps
                        third = beat_dur / 3
                        for s in range(3):
                            if not self.in_drop_sequence:
                                return
                            c1 = colors[(b * 3 + s) % len(colors)]
                            c2 = colors[(b * 3 + s + 3) % len(colors)]
                            self.dmx.set_12s(*c1, 64)
                            for z in range(1, 5):
                                self.dmx.set_bar_zone(z, *c2, 64)
                            self.dmx.send_frame()
                            time.sleep(third * 0.15)
                            self.dmx.set_all(0, 0, 0, 0, 0)
                            self.dmx.send_frame()
                            time.sleep(third * 0.85)
                    
                    elif mode == 'ping_pong':
                        # 6th-beat alternation (3x speed) — wash/bar swap rapidly
                        sixth = beat_dur / 6
                        for s in range(6):
                            if not self.in_drop_sequence:
                                return
                            c = colors[(b * 6 + s) % len(colors)]
                            if s % 2 == 0:
                                self.dmx.set_12s(*c, 64)
                                for z in range(1, 5):
                                    self.dmx.set_bar_zone(z, 0, 0, 0, 0, 0)
                            else:
                                self.dmx.set_12s(0, 0, 0, 0, 0)
                                c2 = colors[(b * 6 + s + 2) % len(colors)]
                                for z in range(1, 5):
                                    self.dmx.set_bar_zone(z, *c2, 64)
                            self.dmx.send_frame()
                            time.sleep(sixth)
                    
                    elif mode == 'scatter_blast':
                        # 6 random blasts per beat (3x speed)
                        sixth = beat_dur / 6
                        for h in range(6):
                            if not self.in_drop_sequence:
                                return
                            c = colors[(b * 6 + h) % len(colors)]
                            on_12s = random.random() > 0.3
                            self.dmx.set_12s(*(c if on_12s else (0,0,0,0)), 127 if on_12s else 0)
                            for z in range(1, 5):
                                on = random.random() > 0.3
                                self.dmx.set_bar_zone(z, *(c if on else (0,0,0,0)), 127 if on else 0)
                            self.dmx.send_frame()
                            time.sleep(sixth)
                    
                    elif mode == 'split_strobe':
                        # 12th-beat center-outward chase (3x speed)
                        twelfth = beat_dur / 12
                        for s in range(3):
                            if not self.in_drop_sequence:
                                return
                            c = colors[(b * 3 + s) % len(colors)]
                            # 12s blast
                            self.dmx.set_12s(*c, 64)
                            for z in range(1, 5):
                                self.dmx.set_bar_zone(z, 0, 0, 0, 0, 0)
                            self.dmx.send_frame()
                            time.sleep(twelfth)
                            if not self.in_drop_sequence:
                                return
                            # Center zones
                            self.dmx.set_12s(0, 0, 0, 0, 0)
                            self.dmx.set_bar_zone(2, *c, 64)
                            self.dmx.set_bar_zone(3, *c, 64)
                            self.dmx.set_bar_zone(1, 0, 0, 0, 0, 0)
                            self.dmx.set_bar_zone(4, 0, 0, 0, 0, 0)
                            self.dmx.send_frame()
                            time.sleep(twelfth)
                            if not self.in_drop_sequence:
                                return
                            # Outer zones
                            self.dmx.set_bar_zone(2, 0, 0, 0, 0, 0)
                            self.dmx.set_bar_zone(3, 0, 0, 0, 0, 0)
                            self.dmx.set_bar_zone(1, *c, 64)
                            self.dmx.set_bar_zone(4, *c, 64)
                            self.dmx.send_frame()
                            time.sleep(twelfth)
                            if not self.in_drop_sequence:
                                return
                            # Blackout
                            self.dmx.set_all(0, 0, 0, 0, 0)
                            self.dmx.send_frame()
                            time.sleep(twelfth)
                    
                    elif mode == 'blackout_kicks':
                        # 3 stabs per beat (3x speed) — rapid fire slam/black
                        third = beat_dur / 3
                        for s in range(3):
                            if not self.in_drop_sequence:
                                return
                            c = colors[(b * 3 + s) % len(colors)]
                            self.dmx.set_all(*c, 64)
                            self.dmx.send_frame()
                            time.sleep(third * 0.1)
                            self.dmx.set_all(0, 0, 0, 0, 0)
                            self.dmx.send_frame()
                            time.sleep(third * 0.9)
                                
            except Exception as e:
                print(f"  ⚠️ Drop sequence error: {e}")

        self.drop_thread = threading.Thread(target=_drop_sequence, daemon=True)
        self.drop_thread.start()

    # ===== KICK FLASH =====

    def _kick_flash(self, kick_energy, avg_kick):
        """Flash lights on a strong kick hit. Runs in audio thread for minimum latency."""
        self.kick_flash_active = True
        self.kick_flash_time = time.time()

        # During drop sequence, kick flashes are MASSIVE
        if self.in_drop_sequence:
            try:
                self.dmx.set_12s(255, 255, 255, 255, 64)  # full white + amber, 50% dimmer
                self.dmx.send_frame()
            except Exception:
                pass
            return

        # Normal kick flash
        import numpy as np
        intensity = min(1.0, (kick_energy / avg_kick - 1.0) / 2.0)
        dim = int(40 + intensity * 24)  # 40-64 dimmer range

        try:
            self.dmx.set_12s(255, 255, 255, 0, dim)
            self.dmx.send_frame()
        except Exception:
            pass

    # ===== GOVEE =====

    def _govee_cmd(self, *cmds):
        """Run Govee commands sequentially in a thread. Skips if already busy.
        
        Reorders commands: brightness/on/off first, then scene/color/warm last.
        This prevents brightness from resetting an active scene to white.
        """
        if not self.govee_enabled or self.govee_busy:
            return
        
        # Reorder: non-visual commands first (brightness, on, off), 
        # visual commands last (scene, color, warm, party)
        visual = ['scene', 'color', 'warm', 'cool', 'party', 'red', 'green', 'blue', 'purple']
        first = [c for c in cmds if c[0] not in visual]
        last = [c for c in cmds if c[0] in visual]
        ordered = first + last
        
        def _run():
            self.govee_busy = True
            try:
                for cmd in ordered:
                    subprocess.run([GOVEE_CLI] + cmd, capture_output=True, timeout=8)
                    time.sleep(0.5)
            except Exception:
                pass
            finally:
                self.govee_busy = False
        threading.Thread(target=_run, daemon=True).start()

    # ===== DMX KEEPALIVE =====

    def _start_keepalive(self):
        """Background thread that sends DMX frames when no scene is running."""
        def keepalive():
            while self.running:
                if not self.scene_running and self.dmx:
                    with self.dmx_lock:
                        try:
                            self.dmx.send_frame()
                        except Exception:
                            pass
                time.sleep(0.05)  # 20fps keepalive
        threading.Thread(target=keepalive, daemon=True).start()

    # ===== PRO DJ LINK EVENT HANDLING =====

    def _handle_prodjlink_event(self, event):
        """Handle a Pro DJ Link event from the bridge."""
        event_type = event.get('type')
        
        if event_type == 'connection':
            status = event.get('status')
            self.prodjlink_connected = (status == 'connected')
            if self.prodjlink_connected:
                print(f"  🔗 Pro DJ Link connected (vCDJ {event.get('vcdj_number', '?')})")
                self.prodjlink_clock_source = True  # Switch to Pro DJ Link clock
            else:
                print(f"  🔌 Pro DJ Link disconnected, falling back to FFT")
                self.prodjlink_clock_source = False
                self.prodjlink_decks = {}
            return "OK: connection status updated"
        
        elif event_type == 'deck_update':
            deck = event.get('deck')
            changes = event.get('changes', {})
            state = event.get('state', {})
            
            # Store deck state
            self.prodjlink_decks[deck] = state
            
            # Track master deck state
            play_state = state.get('play_state')
            beat_count = changes.get('beat_count') or state.get('beat_count', 0)
            is_playing = play_state in ('playing', 3, 'playing') or str(play_state) == '3'

            # Trust explicit master flag from Pro DJ Link over local heuristics.
            if state.get('is_master') and deck != self.prodjlink_master_deck:
                self.prodjlink_master_deck = deck
                self.current_section = None
                self.audio_beat_count = 0
                self.audio_beat_tracking = False
                self.prodjlink_last_beat = 0
                print(f"  👑 Adopted explicit master deck {deck}")

            current_master_state = self.prodjlink_decks.get(self.prodjlink_master_deck, {}) if self.prodjlink_master_deck else {}
            
            # Enable audio tracking when master deck is active
            if deck == self.prodjlink_master_deck and not self.audio_beat_tracking:
                if is_playing or self._deck_is_looping():
                    self.audio_beat_tracking = True
                    self.audio_beat_count = 0
                    print(f"  \U0001f3a7 Audio beat tracking STARTED for deck {deck}")

            # Deterministic path: whichever deck was most recently analyzed/armed wins.
            # Start timed automation ONLY on an explicit transition into play.
            if 'play_state' in changes and is_playing and self.armed_deck == deck and self.scene_maps.get(deck):
                track_id = self.scene_maps[deck].get('track_id')
                if not self.track_timer_active or self.track_timer_deck != deck or self.track_timer_track_id != track_id:
                    self.track_timer_active = True
                    self.track_timer_deck = deck
                    self.track_timer_track_id = track_id
                    self.track_timer_start_time = time.time()
                    self.current_section = None
                    self.current_track_id = track_id
                    self.mode = 'auto'
                    print(f"  ⏱️  Timed automation STARTED for armed deck {deck}, track {track_id}")
            
            # Detect loop exit: was looping, now playing -> reset beat counter
            prev_ps = getattr(self, '_prev_play_state', None)
            if deck == self.prodjlink_master_deck and prev_ps in ('looping', 4, 'loop') and is_playing:
                # Use CDJ beat_count as starting point
                cdj_beat = beat_count if beat_count and beat_count > 0 else 1
                self.audio_beat_count = cdj_beat
                print(f"  \U0001f501 Loop exit detected, reset audio beat to {cdj_beat}")
            if deck == self.prodjlink_master_deck:
                setattr(self, '_prev_play_state', play_state)
            
            # Update BPM from master deck
            if state.get('bpm') and deck == self.prodjlink_master_deck:
                self.locked_bpm = float(state['bpm'])
                self.manual_bpm = self.locked_bpm
            
            # CDJ-based section tracking DISABLED — using audio beat counting instead
            # (PSSI section tracking happens in the audio callback above)
            
            # Track beat count from master deck
            if deck == self.prodjlink_master_deck and 'beat_count' in changes:
                new_beat_count = changes['beat_count']
                now = time.time()
                
                # On first Pro DJ Link beat, sync our phrase tracking to avoid jarring jump
                if self.prodjlink_last_beat == 0 and new_beat_count > 0:
                    self.last_phrase_beat = new_beat_count
                    self.phrase_count = 0
                    print(f"  🔗 Pro DJ Link beat sync: starting at beat {new_beat_count}")
                
                # Update our internal beat count
                if new_beat_count > self.prodjlink_last_beat:
                    self.prodjlink_last_beat = new_beat_count
                    self.prodjlink_last_beat_time = now
                    self.beat_count = new_beat_count
                    
                    # If we have a mapped section for the master deck and timed automation is NOT active,
                    # follow beat boundaries directly.
                    if self.mode == 'auto' and self.scene_maps.get(self.prodjlink_master_deck) and not self.track_timer_active:
                        scene_map = self.scene_maps[self.prodjlink_master_deck]
                        sections = scene_map.get('sections', [])
                        for section in sections:
                            if section['start_beat'] <= self.beat_count < section['end_beat']:
                                if self.current_section is None or self.current_section.get('start_beat') != section['start_beat']:
                                    self.current_section = section
                                    self.current_track_id = scene_map.get('track_id')
                                    scene_name = section.get('scene', '')
                                    print(f"  🎬 SECTION: {section['type']} -> {scene_name} (beat {self.beat_count})")
                                    for s in SCENES:
                                        if s['name'] == scene_name:
                                            self._run_scene(s)
                                            break
                                break
                    else:
                        # Legacy phrase fallback is disabled while deterministic timed automation is armed/active.
                        if not self.track_timer_active and self.armed_deck is None:
                            if self.prodjlink_master_deck is None or not self.scene_maps.get(self.prodjlink_master_deck):
                                beats_since_eval = self.beat_count - self.last_phrase_beat
                                if beats_since_eval >= 32 and self.mode == 'auto':
                                    self.last_phrase_beat = self.beat_count
                                    self.phrase_count += 1
                                    self._eval_phrase()
            
            # Loop detection
            if 'loop_active' in changes:
                if changes['loop_active']:
                    print(f"  🔄 Deck {deck} loop engaged")
                    # TODO: Hold current scene, escalate intensity
                else:
                    print(f"  🔄 Deck {deck} loop released")
            
            # Play state changes
            if 'play_state' in changes:
                print(f"  ▶️  Deck {deck}: {changes['play_state']}")
            
            return "OK: deck updated"
        
        elif event_type == 'track_load':
            deck = event.get('deck')
            title = event.get('title', 'Unknown')
            artist = event.get('artist', 'Unknown')
            bpm = event.get('bpm', 0)
            key = event.get('key', '')
            
            print(f"  💿 Deck {deck} loaded: {artist} - {title} ({bpm} BPM, {key})")
            
            # Store in deck state
            if deck not in self.prodjlink_decks:
                self.prodjlink_decks[deck] = {}
            self.prodjlink_decks[deck].update({
                'title': title,
                'artist': artist,
                'bpm': bpm,
                'key': key,
            })
            
            return "OK: track loaded"
        
        elif event_type == 'track_analysis':
            deck = event.get('deck')
            if deck:
                incoming_track_id = event.get('track_id')
                existing = self.scene_maps.get(deck)
                if existing and existing.get('track_id') == incoming_track_id and existing.get('sections') == event.get('sections'):
                    self.analysis_status[deck] = 'ready'
                    self.armed_deck = deck
                    return "OK: duplicate track_analysis ignored"

                self.scene_maps[deck] = event
                self.analysis_status[deck] = 'ready'
                self.armed_deck = deck
                self.track_timer_active = False
                self.track_timer_deck = None
                self.track_timer_track_id = None
                self.track_timer_start_time = 0
                print(f"  🎵 Deck {deck} analyzed: {len(event.get('sections', []))} sections -> ARMED")
            return "OK: track_analysis received"
        
        elif event_type == 'analysis_status':
            deck = event.get('deck')
            status = event.get('status')
            if deck:
                self.analysis_status[deck] = status
            return "OK: analysis_status updated"
        
        elif event_type == 'energy_update':
            deck = event.get('deck')
            energy = event.get('energy', 0)
            # Use waveform energy to drive scene selection
            if deck == self.prodjlink_master_deck or self.prodjlink_master_deck is None:
                self.smoothed_energy = float(energy)
                # Check for category change
                if self.mode == 'auto':
                    target_cat = self._energy_to_category(energy)
                    if target_cat != self.current_category:
                        print(f"  ⚡ Waveform energy: {energy:.0f} → {target_cat} (was {self.current_category})")
                        self._pick_scene(target_cat)
            return "OK: energy updated"

        elif event_type == 'master_change':
            master_deck = event.get('master_deck')
            bpm = event.get('bpm', 0)
            self.prodjlink_master_deck = master_deck
            self.prodjlink_last_beat = 0
            if bpm:
                self.locked_bpm = float(bpm)
                self.manual_bpm = self.locked_bpm
            print(f"  👑 Master deck changed to {master_deck} ({bpm} BPM)")
            return "OK: master changed"
        
        else:
            return f"WARN: unknown event type '{event_type}'"

    # ===== CONTROL SOCKET =====

    def _start_socket(self):
        """Start Unix socket for control commands."""
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(SOCKET_PATH)
            sock.listen(5)
            sock.settimeout(1.0)
            print(f"✅ Socket: {SOCKET_PATH}")
        except Exception as e:
            print(f"❌ Socket failed ({SOCKET_PATH}): {e}")
            # Fallback: try home dir
            alt = os.path.expanduser('~/.lightd.sock')
            try:
                os.unlink(alt)
            except OSError:
                pass
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(alt)
            sock.listen(5)
            sock.settimeout(1.0)
            print(f"✅ Socket (fallback): {alt}")

        def accept_loop():
            while self.running:
                try:
                    conn, _ = sock.accept()
                    # Read all available data (track_analysis can be >100KB)
                    chunks = []
                    conn.settimeout(0.5)
                    while True:
                        try:
                            chunk = conn.recv(65536)
                            if not chunk:
                                break
                            chunks.append(chunk)
                        except socket.timeout:
                            break
                    data = b''.join(chunks).decode().strip()
                    response = self._handle_command(data)
                    conn.sendall((response + "\n").encode())
                    conn.close()
                except socket.timeout:
                    continue
                except Exception as e:
                    print(f"⚠️  Socket error: {e}")
                    continue
            sock.close()

        threading.Thread(target=accept_loop, daemon=True).start()

    def _handle_command(self, cmd):
        """Handle a control command. Returns response string."""
        # Check if it's a JSON event (from Pro DJ Link bridge)
        if cmd.strip().startswith('{'):
            try:
                event = json.loads(cmd)
                return self._handle_prodjlink_event(event)
            except json.JSONDecodeError:
                pass
        
        parts = cmd.split(None, 1)
        if not parts:
            return "ERR: empty command"

        action = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ''

        if action == 'start':
            self.mode = 'auto'
            if not self.current_scene:
                self._pick_scene(self._energy_to_category(self.smoothed_energy))
            if self.govee_enabled:
                import random
                scene = random.randint(0, 9)
                self._govee_cmd(["on"], ["scene", str(scene)])
            return f"OK: auto mode (energy={self.smoothed_energy:.0f})"

        elif action == 'stop':
            self.mode = 'idle'
            self._stop_scene()
            if self.dmx:
                self.dmx.set_all(255, 180, 80, 200, 25)  # warm idle
            return "OK: stopped, warm idle"

        elif action == 'scene':
            name = arg.strip()
            scene = next((s for s in SCENES if s['name'].lower() == name.lower()), None)
            if not scene:
                # Fuzzy match
                matches = [s for s in SCENES if name.lower() in s['name'].lower()]
                if matches:
                    scene = matches[0]
            if scene:
                self.mode = 'manual_scene'
                self._run_scene(scene)
                return f"OK: {scene['name']} ({scene['category']})"
            else:
                names = ', '.join(s['name'] for s in SCENES)
                return f"ERR: unknown scene '{name}'. Available: {names}"

        elif action == 'strobe':
            speed = int(arg) if arg else 180
            self.mode = 'strobe'
            self._stop_scene()
            if self.dmx:
                self.dmx.set_all(255, 255, 255, 0, 255, speed)
            return f"OK: strobe speed={speed}"

        elif action == 'blackout':
            self.mode = 'blackout'
            self._stop_scene()
            if self.dmx:
                self.dmx.blackout()
            return "OK: blackout"

        elif action == 'warm':
            self.mode = 'warm'
            self._stop_scene()
            if self.dmx:
                self.dmx.set_all(255, 180, 80, 200, 25)
            if self.govee_enabled:
                self.govee_busy = False
                self._govee_cmd(["warm"])
            return "OK: warm"

        elif action == 'brightness':
            level = int(arg) if arg else 50
            self.brightness_override = level
            return f"OK: brightness={level}"

        elif action == 'bpm':
            if arg.lower() == 'auto':
                self.manual_bpm = None
                return "OK: BPM auto-detect"
            else:
                self.manual_bpm = float(arg)
                self.locked_bpm = self.manual_bpm
                return f"OK: BPM={self.manual_bpm}"

        elif action == 'govee':
            if arg.lower() == 'off':
                self.govee_enabled = False
                return "OK: Govee disabled"
            else:
                self.govee_enabled = True
                return "OK: Govee enabled"

        elif action == 'status':
            with self.viz_lock:
                viz_data = {
                    'waveform': self.viz_waveform,
                    'spectrum': self.viz_spectrum,
                    'bands': self.viz_bands,
                    'beat': self.viz_beat_flash,
                    'beat_count': self.beat_count,
                    'phrase': self.phrase_count,
                }
                self.viz_beat_flash = False
            # Determine BPM source
            if self.prodjlink_clock_source:
                bpm_source = 'prodjlink'
            elif self.manual_bpm:
                bpm_source = 'manual'
            else:
                bpm_source = 'fft'
            
            return json.dumps({
                'mode': self.mode,
                'scene': self.current_scene_name,
                'category': self.current_category,
                'energy': round(self.smoothed_energy, 1),
                'energy_raw': round(self.energy, 1),
                'bpm': round(self.locked_bpm, 1),
                'bpm_source': bpm_source,
                'govee': self.govee_enabled,
                'dmx': self.dmx is not None,
                'audio': not self.no_audio,
                'scene_age': round(time.time() - self.scene_start_time, 1) if self.scene_start_time else 0,
                'had_breakdown': self.had_breakdown,
                'buildup_armed': self.buildup_armed,
                'kick_ratio': float(sum(list(self.kick_history)[-40:]) / max(len(list(self.kick_history)[-40:]), 1)) / self.kick_energy_baseline if self.kick_energy_baseline > 0 and len(self.kick_history) >= 10 else 0,
                'in_drop': self.in_drop_sequence,
                'drop_remaining': round(max(0, self.drop_sequence_end - time.time()), 1) if self.in_drop_sequence else 0,
                'viz': viz_data,
                # Pro DJ Link state
                'prodjlink': {
                    'connected': self.prodjlink_connected,
                    'clock_source': self.prodjlink_clock_source,
                    'master_deck': self.prodjlink_master_deck,
                    'decks': self.prodjlink_decks,
                },
                'scene_map': self.scene_maps.get(self.prodjlink_master_deck or 1) or self.scene_maps.get(2) or self.scene_maps.get(1),
                'current_section': self.current_section,
                'analysis_status': self.analysis_status,
                'audio_beat_count': self.audio_beat_count,
                'audio_beat_tracking': self.audio_beat_tracking,
            })

        elif action == 'scenes':
            cat = arg.strip().lower() if arg else None
            if cat:
                scenes = get_scenes_by_category(cat)
            else:
                scenes = SCENES
            return '\n'.join(f"[{s['category']:10}] {s['name']}" for s in scenes)

        elif action == 'calibrate':
            # Reset energy history to recalibrate to current volume
            self.energy_max_history.clear()
            return "OK: energy calibration reset"

        elif action == 'quit':
            self.running = False
            return "OK: shutting down"

        else:
            return f"ERR: unknown command '{action}'. Commands: start, stop, status, scene, strobe, blackout, warm, brightness, bpm, govee, scenes, quit"


# ===== CLI WRAPPER =====

def lightctl(cmd):
    """Send a command to the daemon."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(SOCKET_PATH)
        sock.sendall((cmd + "\n").encode())
        response = sock.recv(4096).decode().strip()
        sock.close()
        return response
    except ConnectionRefusedError:
        return "ERR: lightd not running"
    except FileNotFoundError:
        return "ERR: lightd not running (no socket)"


def main():
    args = sys.argv[1:]

    # If daemon is running and we got a command, send it
    if args and args[0] != '--no-audio' and os.path.exists(SOCKET_PATH):
        print(lightctl(' '.join(args)))
        return

    no_audio = '--no-audio' in args

    daemon = LightDaemon(no_audio=no_audio)

    signal.signal(signal.SIGINT, lambda s, f: setattr(daemon, 'running', False))
    signal.signal(signal.SIGTERM, lambda s, f: setattr(daemon, 'running', False))

    daemon.start()


if __name__ == '__main__':
    main()
