#!/usr/bin/env python3
"""
Audio-Reactive DMX + Govee Lighting Engine

Listens to Scarlett audio input, analyzes energy/frequency in real-time,
and picks DMX scenes from the categorized library based on what it hears.

Usage:
  python3 reactive_engine.py              # auto-detect Scarlett
  python3 reactive_engine.py --bpm 130    # lock BPM manually
  python3 reactive_engine.py --no-govee   # DMX only, skip Govee

Architecture:
  Scarlett Audio → FFT Analysis → Energy Classification → Scene Selection → DMX Output
                                                                          → Govee Output
"""

import pyaudio
import numpy as np
import threading
import subprocess
import signal
import time
import math
import sys
import os
import argparse
from collections import deque

sys.path.insert(0, os.path.dirname(__file__))
from dmx_controller import DMX
from scenes_v2 import SCENES, get_scene_for_energy, get_scenes_by_category

# ===== AUDIO CONFIG =====
RATE = 44100
CHUNK = 2048          # ~46ms per frame
CHANNELS = 4          # Scarlett exposes 4 channels
INPUT_CHANNEL = 1     # 0-indexed; audio on channel 1 (Scarlett Input 2)
DEVICE_NAME = "Scarlett"

# ===== ANALYSIS CONFIG =====
BANDS = {
    "sub":    (20, 80),
    "kick":   (80, 160),
    "snare":  (160, 400),
    "mid":    (400, 2500),
    "hat":    (2500, 8000),
    "air":    (8000, 16000),
}

# Energy smoothing
ENERGY_WINDOW = 80        # frames (~3.7s at 46ms/frame)
ENERGY_SMOOTH = 0.15      # EMA factor for energy level

# Scene change timing
MIN_SCENE_TIME = 8.0      # minimum seconds before changing scene
ENERGY_SHIFT_THRESHOLD = 15  # energy must shift by this much to trigger early change

# Energy → category mapping
CATEGORY_THRESHOLDS = [
    (10,  'breakdown'),
    (25,  'ambient'),
    (40,  'groove'),
    (60,  'drive'),
    (80,  'peak'),
    (100, 'drop'),
]


class AudioAnalyzer:
    def __init__(self):
        self.energy_history = deque(maxlen=ENERGY_WINDOW)
        self.beat_times = deque(maxlen=60)
        self.current_energy = 0.0      # smoothed 0-100
        self.current_bpm = 130.0
        self.kick_energy = 0.0
        self.hat_energy = 0.0
        self.total_energy = 0.0
        self.is_beat = False
        self.is_drop = False
        self.is_breakdown = False
        self._drop_cooldown = 0
        self._energy_baseline = None
    
    def analyze(self, samples):
        """Analyze a chunk of audio samples. Returns energy level 0-100."""
        # FFT
        fft = np.fft.rfft(samples)
        magnitudes = np.abs(fft) / len(samples)
        freqs = np.fft.rfftfreq(len(samples), 1.0 / RATE)
        
        # Band energies
        energies = {}
        for name, (lo, hi) in BANDS.items():
            mask = (freqs >= lo) & (freqs < hi)
            energies[name] = float(np.mean(magnitudes[mask])) if np.any(mask) else 0.0
        
        self.kick_energy = energies["sub"] + energies["kick"]
        self.hat_energy = energies["hat"]
        self.total_energy = sum(energies.values())
        
        # Energy history for relative scaling
        self.energy_history.append(self.total_energy)
        
        if len(self.energy_history) < 10:
            return self.current_energy
        
        # Normalize energy relative to recent history
        avg = np.mean(self.energy_history)
        peak = np.max(self.energy_history)
        
        if peak > 0:
            normalized = (self.total_energy / peak) * 100
        else:
            normalized = 0
        
        # Smooth
        self.current_energy += ENERGY_SMOOTH * (normalized - self.current_energy)
        
        # Beat detection
        self.is_beat = self._detect_beat()
        
        # Drop detection: sudden spike after low energy
        self._detect_drop()
        
        # Breakdown detection: energy drops significantly below average
        self.is_breakdown = self.current_energy < 15 and avg > 0
        
        return self.current_energy
    
    def _detect_beat(self):
        if len(self.energy_history) < 10:
            return False
        avg = np.mean(list(self.energy_history)[-20:])
        if self.kick_energy > avg * 1.5:
            now = time.time()
            if not self.beat_times or (now - self.beat_times[-1]) > 0.25:
                self.beat_times.append(now)
                self._update_bpm()
                return True
        return False
    
    def _update_bpm(self):
        if len(self.beat_times) < 4:
            return
        intervals = [self.beat_times[i] - self.beat_times[i-1] 
                     for i in range(1, len(self.beat_times))]
        intervals = [i for i in intervals if 0.333 < i < 0.857]  # 70-180 BPM
        if len(intervals) >= 3:
            med = float(np.median(intervals))
            self.current_bpm = 60.0 / med
    
    def _detect_drop(self):
        if self._drop_cooldown > 0:
            self._drop_cooldown -= 1
            self.is_drop = False
            return
        
        if len(self.energy_history) < 40:
            self.is_drop = False
            return
        
        recent = list(self.energy_history)
        last_10 = np.mean(recent[-10:])
        prev_30 = np.mean(recent[-40:-10])
        
        # Drop = energy spikes up significantly after being low
        if last_10 > prev_30 * 2.5 and self.current_energy > 70:
            self.is_drop = True
            self._drop_cooldown = 80  # ~3.7 seconds cooldown
        else:
            self.is_drop = False
    
    def get_category(self):
        """Map current energy to a scene category."""
        if self.is_drop:
            return 'drop'
        if self.is_breakdown:
            return 'breakdown'
        
        for threshold, cat in CATEGORY_THRESHOLDS:
            if self.current_energy <= threshold:
                return cat
        return 'drop'


class ReactiveEngine:
    def __init__(self, manual_bpm=None, use_govee=True):
        self.analyzer = AudioAnalyzer()
        self.dmx = DMX()
        self.manual_bpm = manual_bpm
        self.use_govee = use_govee
        self.running = False
        
        # Scene state
        self.current_scene = None
        self.current_category = None
        self.scene_start = 0
        self.scene_thread = None
        self._scene_stop = threading.Event()
        
        # Stats
        self.frame_count = 0
        self.last_status = 0
    
    def start(self):
        self.dmx.open()
        self.running = True
        
        # Find Scarlett
        p = pyaudio.PyAudio()
        device_idx = self._find_device(p)
        if device_idx is None:
            print("❌ Scarlett not found!")
            p.terminate()
            return
        
        stream = p.open(
            format=pyaudio.paInt16, channels=CHANNELS, rate=RATE,
            input=True, input_device_index=device_idx, frames_per_buffer=CHUNK
        )
        
        bpm = self.manual_bpm or 130
        print(f"\n🎛️  REACTIVE LIGHTING ENGINE ACTIVE")
        print(f"   Audio: Scarlett (ch {INPUT_CHANNEL + 1})")
        print(f"   BPM: {'manual ' + str(bpm) if self.manual_bpm else 'auto-detect'}")
        print(f"   Govee: {'ON' if self.use_govee else 'OFF'}")
        print(f"   Scenes: {len(SCENES)} loaded")
        print(f"   Press Ctrl+C to stop\n")
        
        try:
            while self.running:
                data = stream.read(CHUNK, exception_on_overflow=False)
                all_samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                samples = all_samples[INPUT_CHANNEL::CHANNELS] / 32768.0
                
                # Analyze
                energy = self.analyzer.analyze(samples)
                category = self.analyzer.get_category()
                bpm = self.manual_bpm or self.analyzer.current_bpm
                
                # Check if we should change scene
                now = time.time()
                should_change = False
                
                if self.current_scene is None:
                    should_change = True
                elif category != self.current_category:
                    time_in_scene = now - self.scene_start
                    energy_shift = abs(energy - (self.current_scene.get('energy', 50)))
                    
                    if category == 'drop' or category == 'breakdown':
                        # Immediate change for drops and breakdowns
                        should_change = True
                    elif time_in_scene >= MIN_SCENE_TIME:
                        should_change = True
                    elif energy_shift > ENERGY_SHIFT_THRESHOLD and time_in_scene > 3.0:
                        should_change = True
                
                if should_change:
                    self._switch_scene(category, bpm)
                
                # Update BPM for current scene if auto-detecting
                # (scene functions use the bpm passed at start)
                
                # Status output
                if now - self.last_status > 2.0:
                    self._print_status(energy, category, bpm)
                    self.last_status = now
                
                self.frame_count += 1
                
        except KeyboardInterrupt:
            pass
        finally:
            print("\n🛑 Stopping...")
            self._stop_scene()
            self.dmx.blackout()
            self.dmx.send_for(0.5)
            self.dmx.close()
            stream.close()
            p.terminate()
            if self.use_govee:
                subprocess.run([os.path.expanduser("~/.local/bin/govee"), "warm"], capture_output=True)
                subprocess.run([os.path.expanduser("~/.local/bin/govee"), "brightness", "10"], capture_output=True)
            print("✅ Restored lights")
    
    def _switch_scene(self, category, bpm):
        """Switch to a new scene from the given category."""
        # Stop current scene
        self._stop_scene()
        
        # Pick new scene
        scene = get_scene_for_energy(
            {'breakdown': 5, 'ambient': 12, 'groove': 30, 
             'drive': 50, 'peak': 75, 'drop': 95}.get(category, 50)
        )
        
        self.current_scene = scene
        self.current_category = category
        self.scene_start = time.time()
        
        print(f"\n  🎨 → {scene['name']} [{category.upper()}] (energy {scene['energy']})")
        
        # Start scene in background thread
        self._scene_stop = threading.Event()
        
        def run():
            # Monkey-patch dmx methods to check stop flag
            orig_send_for = self.dmx.send_for
            orig_send_hold = self.dmx.send_hold
            orig_send_frame = self.dmx.send_frame
            
            class SceneStopped(Exception):
                pass
            
            def checked_send_for(seconds):
                start = time.time()
                while time.time() - start < seconds:
                    if self._scene_stop.is_set():
                        raise SceneStopped()
                    self.dmx.send_frame()
                    time.sleep(0.023)
            
            def checked_send_hold():
                while not self._scene_stop.is_set():
                    self.dmx.send_frame()
                    time.sleep(0.023)
                raise SceneStopped()
            
            def checked_send_frame():
                if self._scene_stop.is_set():
                    raise SceneStopped()
                orig_send_frame()
            
            self.dmx.send_for = checked_send_for
            self.dmx.send_hold = checked_send_hold
            self.dmx.send_frame = checked_send_frame
            
            try:
                scene['fn'](self.dmx, bpm)
            except SceneStopped:
                pass
            except Exception as e:
                if not self._scene_stop.is_set():
                    print(f"  ⚠️  Scene error: {e}")
            finally:
                self.dmx.send_for = orig_send_for
                self.dmx.send_hold = orig_send_hold
                self.dmx.send_frame = orig_send_frame
        
        self.scene_thread = threading.Thread(target=run, daemon=True)
        self.scene_thread.start()
        
        # Govee: set matching mood
        if self.use_govee:
            self._set_govee_mood(category)
    
    def _stop_scene(self):
        if self.scene_thread and self.scene_thread.is_alive():
            self._scene_stop.set()
            self.scene_thread.join(timeout=2)
        self.dmx.blackout()
    
    def _set_govee_mood(self, category):
        """Set Govee lights to complement the DMX scene."""
        govee = os.path.expanduser("~/.local/bin/govee")
        try:
            if category in ('breakdown', 'ambient'):
                subprocess.Popen([govee, "brightness", "3"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.Popen([govee, "warm"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif category == 'groove':
                subprocess.Popen([govee, "brightness", "5"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif category == 'drive':
                subprocess.Popen([govee, "brightness", "8"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif category in ('peak', 'drop'):
                subprocess.Popen([govee, "brightness", "12"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    
    def _print_status(self, energy, category, bpm):
        bar_len = int(min(energy, 100) / 2)
        bar = "█" * bar_len + "░" * (50 - bar_len)
        scene_name = self.current_scene['name'] if self.current_scene else '---'
        drop_ind = " 💥DROP" if self.analyzer.is_drop else ""
        bd_ind = " 🔽BREAK" if self.analyzer.is_breakdown else ""
        beat_ind = "♪" if self.analyzer.is_beat else " "
        
        print(f"  {beat_ind} BPM:{bpm:5.0f} | Energy:{energy:5.1f} [{bar}] | "
              f"{category.upper():>10} → {scene_name}{drop_ind}{bd_ind}")
    
    def _find_device(self, p):
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if DEVICE_NAME.lower() in info["name"].lower() and info["maxInputChannels"] > 0:
                print(f"🎤 Found: {info['name']} (index {i}, {info['maxInputChannels']} ch)")
                return i
        return None


def main():
    parser = argparse.ArgumentParser(description="Audio-Reactive DMX Lighting")
    parser.add_argument("--bpm", type=float, help="Lock BPM manually (default: auto-detect)")
    parser.add_argument("--no-govee", action="store_true", help="DMX only, skip Govee control")
    args = parser.parse_args()
    
    engine = ReactiveEngine(manual_bpm=args.bpm, use_govee=not args.no_govee)
    
    signal.signal(signal.SIGINT, lambda s, f: setattr(engine, 'running', False))
    signal.signal(signal.SIGTERM, lambda s, f: setattr(engine, 'running', False))
    
    engine.start()


if __name__ == '__main__':
    main()
