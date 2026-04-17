#!/usr/bin/env python3
"""lightd Dashboard v4 — Waveform-based scene map visualization."""

import http.server
import json
import os
import socket
import sys
import threading
import time

# Add project dir to path for scenes import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scenes_v2 import SCENES

SOCKET_PATH = '/tmp/lightd.sock'
PORT = 8420

state = {}
state_lock = threading.Lock()


def poll_lightd():
    global state
    while True:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(SOCKET_PATH)
            sock.sendall(b"status\n")
            # Read all data until server closes connection
            chunks = []
            sock.settimeout(2.0)
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            data = b''.join(chunks).decode().strip()
            sock.close()
            with state_lock:
                state = json.loads(data)
        except Exception:
            with state_lock:
                state = {"error": "lightd not connected"}
        time.sleep(0.05)


HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>lightd</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #0a0a0e;
    color: #fff;
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    user-select: none;
  }

  /* Top Bar (slim) */
  .top-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 16px;
    background: #0c0c12;
    border-bottom: 1px solid #1a1a22;
    flex-shrink: 0;
  }
  .top-left { display: flex; align-items: center; gap: 20px; }
  .top-right { display: flex; align-items: center; gap: 12px; }
  .bpm-big {
    font-size: 28px;
    font-weight: 200;
    color: #0f0;
  }
  .clock-source {
    font-size: 10px;
    color: #666;
    letter-spacing: 1px;
    text-transform: uppercase;
  }
  .clock-source.prodjlink { color: #0f0; }
  .clock-source.fft { color: #ffaa22; }
  .status-dots { display: flex; gap: 8px; align-items: center; }
  .status-dot { display: flex; align-items: center; gap: 4px; font-size: 9px; color: #444; }
  .dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
  .dot.on { background: #0f0; }
  .dot.off { background: #f00; }

  /* Deck Section */
  .deck-section {
    display: flex;
    gap: 12px;
    padding: 12px 16px;
    background: #0e0e14;
    border-bottom: 1px solid #1a1a22;
    flex-shrink: 0;
  }
  .deck {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .deck-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 4px;
  }
  .deck-title {
    font-size: 13px;
    font-weight: 400;
    color: #fff;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .deck-artist {
    font-size: 10px;
    color: #666;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-bottom: 6px;
  }
  .deck-status {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
  }
  .status-badge {
    font-size: 9px;
    padding: 2px 6px;
    border-radius: 3px;
    letter-spacing: 0.5px;
    font-weight: 500;
  }
  .status-badge.ready {
    background: #0f0;
    color: #000;
  }
  .status-badge.analyzing {
    background: #ffaa22;
    color: #000;
    animation: pulse 1.5s ease-in-out infinite;
  }
  .status-badge.nodata {
    background: #444;
    color: #888;
  }
  .status-badge.failed {
    background: #f00;
    color: #fff;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
  }
  .color-theme {
    display: flex;
    gap: 4px;
  }
  .color-swatch {
    width: 12px;
    height: 12px;
    border-radius: 2px;
    border: 1px solid #1a1a22;
  }

  /* Waveform Canvas Area */
  .waveform-container {
    position: relative;
    height: 45vh;
    min-height: 300px;
    background: #0a0a0e;
    flex-shrink: 0;
    border-bottom: 1px solid #1a1a22;
  }
  .waveform-canvas {
    width: 100%;
    height: 100%;
    display: block;
  }
  .waveform-placeholder {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    font-size: 14px;
    color: #444;
    letter-spacing: 1px;
    text-align: center;
  }

  /* Current State Bar */
  .state-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    background: #0c0c12;
    border-bottom: 1px solid #1a1a22;
    flex-shrink: 0;
  }
  .state-section {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .state-label {
    font-size: 8px;
    color: #444;
    text-transform: uppercase;
    letter-spacing: 1px;
  }
  .state-value {
    font-size: 16px;
    font-weight: 400;
  }
  .state-value.large {
    font-size: 20px;
    font-weight: 300;
    letter-spacing: 2px;
  }
  .section-type-breakdown { color: #9b59b6; }
  .section-type-ambient { color: #4a90d9; }
  .section-type-buildup { color: #e67e22; }
  .section-type-drop { color: #e74c3c; }
  .section-type-groove { color: #2ecc71; }
  .section-type-intro { color: #4a90d9; }
  .section-type-outro { color: #4a90d9; }

  /* Bottom Stats Area (flexible space) */
  .bottom-stats {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 0;
    overflow: hidden;
  }
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 24px;
    padding: 20px;
  }
  .stat-item {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
  }
  .stat-label {
    font-size: 9px;
    color: #444;
    text-transform: uppercase;
    letter-spacing: 1.5px;
  }
  .stat-value {
    font-size: 32px;
    font-weight: 200;
    color: #888;
  }
</style>
</head>
<body>

<!-- Top Bar -->
<div class="top-bar">
  <div class="top-left">
    <div class="bpm-big" id="bpmDisplay">--</div>
    <div class="clock-source" id="clockSource">Clock: FFT</div>
  </div>
  <div class="top-right">
    <div class="status-dots">
      <div class="status-dot"><span class="dot" id="prodjlinkDot"></span>Link</div>
      <div class="status-dot"><span class="dot" id="dmxDot"></span>DMX</div>
      <div class="status-dot"><span class="dot" id="goveeDot"></span>Govee</div>
    </div>
  </div>
</div>

<!-- Deck Section (dual deck layout) -->
<div class="deck-section">
  <div class="deck" id="deck1">
    <div class="deck-header">
      <div style="flex: 1; min-width: 0;">
        <div class="deck-title" id="deck1-title">Deck 1</div>
        <div class="deck-artist" id="deck1-artist">No Track</div>
      </div>
    </div>
    <div class="deck-status">
      <div class="status-badge nodata" id="deck1-status">No Data</div>
      <div class="color-theme" id="deck1-theme"></div>
    </div>
  </div>
  <div class="deck" id="deck2">
    <div class="deck-header">
      <div style="flex: 1; min-width: 0;">
        <div class="deck-title" id="deck2-title">Deck 2</div>
        <div class="deck-artist" id="deck2-artist">No Track</div>
      </div>
    </div>
    <div class="deck-status">
      <div class="status-badge nodata" id="deck2-status">No Data</div>
      <div class="color-theme" id="deck2-theme"></div>
    </div>
  </div>
</div>

<!-- Waveform Visualization -->
<div class="waveform-container">
  <canvas class="waveform-canvas" id="waveformCanvas"></canvas>
  <div class="waveform-placeholder" id="waveformPlaceholder">Load a track to see analysis</div>
</div>

<!-- Current State Bar -->
<div class="state-bar">
  <div class="state-section">
    <div class="state-label">Section</div>
    <div class="state-value large" id="currentSectionType">--</div>
  </div>
  <div class="state-section">
    <div class="state-label">Scene</div>
    <div class="state-value" id="currentScene">--</div>
  </div>
  <div class="state-section">
    <div class="state-label">Position</div>
    <div class="state-value" id="beatPosition">-- / --</div>
  </div>
  <div class="state-section">
    <div class="state-label">Time in Section</div>
    <div class="state-value" id="sectionTime">--</div>
  </div>
</div>

<!-- Bottom Stats -->
<div class="bottom-stats">
  <div class="stats-grid">
    <div class="stat-item">
      <div class="stat-label">Energy</div>
      <div class="stat-value" id="energyStat">--</div>
    </div>
    <div class="stat-item">
      <div class="stat-label">Mode</div>
      <div class="stat-value" id="modeStat" style="font-size: 20px; text-transform: uppercase;">--</div>
    </div>
    <div class="stat-item">
      <div class="stat-label">Phrase</div>
      <div class="stat-value" id="phraseStat">--</div>
    </div>
  </div>
</div>

<script>
const canvas = document.getElementById('waveformCanvas');
const ctx = canvas.getContext('2d');

// State
let currentSceneMap = null;
let lastTrackId = null;
let playheadPos = 0;  // 0-1 normalized position
let targetPlayheadPos = 0;
let animationFrameId = null;

// Section colors
const SECTION_COLORS = {
  intro: '#4a90d9',
  outro: '#4a90d9',
  breakdown: '#9b59b6',
  buildup: '#e67e22',
  drop: '#e74c3c',
  groove: '#2ecc71'
};

function resizeCanvas() {
  const container = canvas.parentElement;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = container.clientWidth * dpr;
  canvas.height = container.clientHeight * dpr;
  canvas.style.width = container.clientWidth + 'px';
  canvas.style.height = container.clientHeight + 'px';
  ctx.scale(dpr, dpr);
}

window.addEventListener('resize', () => {
  resizeCanvas();
  drawWaveform();
});

resizeCanvas();

function drawWaveform() {
  const w = canvas.width / (window.devicePixelRatio || 1);
  const h = canvas.height / (window.devicePixelRatio || 1);
  
  ctx.clearRect(0, 0, w, h);
  
  if (!currentSceneMap || !currentSceneMap.sections || currentSceneMap.sections.length === 0) {
    document.getElementById('waveformPlaceholder').style.display = 'block';
    return;
  }
  
  document.getElementById('waveformPlaceholder').style.display = 'none';
  
  const sections = currentSceneMap.sections;
  const waveform = currentSceneMap.waveform || [];
  const totalBeats = sections[sections.length - 1].end_beat;
  
  // Draw sections as colored regions
  sections.forEach(section => {
    const startX = (section.start_beat / totalBeats) * w;
    const endX = (section.end_beat / totalBeats) * w;
    const sectionWidth = endX - startX;
    
    const color = SECTION_COLORS[section.type] || '#555';
    
    // Section background
    ctx.fillStyle = color + '22';
    ctx.fillRect(startX, 0, sectionWidth, h);
    
    // Section border
    ctx.strokeStyle = color + '44';
    ctx.lineWidth = 1;
    ctx.strokeRect(startX, 0, sectionWidth, h);
    
    // Draw waveform energy for this section (one entry per beat)
    if (waveform.length > 0) {
      const startBeatIdx = section.start_beat;
      const endBeatIdx = Math.min(section.end_beat, waveform.length);
      
      ctx.fillStyle = color;
      for (let i = startBeatIdx; i < endBeatIdx; i++) {
        const x = (i / totalBeats) * w;
        const energy = waveform[i] || 0;
        const barHeight = energy * h * 0.8;
        const y = h - barHeight;
        const barWidth = Math.max(1, w / totalBeats);
        ctx.fillRect(x, y, barWidth, barHeight);
      }
    }
    
    // Section label
    ctx.fillStyle = '#fff';
    ctx.font = '10px monospace';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    const label = section.type.toUpperCase();
    ctx.fillText(label, startX + 4, 4);
    
    // Scene name (below section label)
    ctx.fillStyle = '#aaa';
    ctx.font = '9px monospace';
    ctx.fillText(section.scene || '', startX + 4, 18);
  });
  
  // Draw playhead
  const playheadX = playheadPos * w;
  ctx.strokeStyle = '#fff';
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(playheadX, 0);
  ctx.lineTo(playheadX, h);
  ctx.stroke();
  
  // Playhead shadow for visibility
  ctx.strokeStyle = '#000';
  ctx.lineWidth = 4;
  ctx.globalAlpha = 0.3;
  ctx.beginPath();
  ctx.moveTo(playheadX, 0);
  ctx.lineTo(playheadX, h);
  ctx.stroke();
  ctx.globalAlpha = 1.0;
}

function animatePlayhead() {
  // Fast interpolation — snap quickly to target
  playheadPos += (targetPlayheadPos - playheadPos) * 0.5;
  
  if (Math.abs(targetPlayheadPos - playheadPos) > 0.0001) {
    drawWaveform();
  }
  
  animationFrameId = requestAnimationFrame(animatePlayhead);
}

animatePlayhead();

function updateDeckDisplay(deckNum, data) {
  const prefix = 'deck' + deckNum;
  const titleEl = document.getElementById(prefix + '-title');
  const artistEl = document.getElementById(prefix + '-artist');
  const statusEl = document.getElementById(prefix + '-status');
  const themeEl = document.getElementById(prefix + '-theme');
  
  if (!data || !data.title) {
    titleEl.textContent = 'Deck ' + deckNum;
    artistEl.textContent = 'No Track';
    statusEl.className = 'status-badge nodata';
    statusEl.textContent = 'No Data';
    themeEl.innerHTML = '';
    return;
  }
  
  titleEl.textContent = data.title || 'Unknown Track';
  artistEl.textContent = data.artist || '--';
  
  // Analysis status
  const analysisStatus = data.analysis_status;
  if (analysisStatus === 'ready') {
    statusEl.className = 'status-badge ready';
    statusEl.innerHTML = 'Ready &#10003;';
  } else if (analysisStatus === 'analyzing') {
    statusEl.className = 'status-badge analyzing';
    statusEl.textContent = 'Analyzing...';
  } else if (analysisStatus === 'failed') {
    statusEl.className = 'status-badge failed';
    statusEl.textContent = 'Failed';
  } else {
    statusEl.className = 'status-badge nodata';
    statusEl.textContent = 'No Data';
  }
  
  // Color theme
  themeEl.innerHTML = '';
  if (data.color_theme) {
    const primary = data.color_theme.primary;
    const secondary = data.color_theme.secondary;
    
    if (primary) {
      const s1 = document.createElement('div');
      s1.className = 'color-swatch';
      s1.style.background = `rgb(${primary[0]}, ${primary[1]}, ${primary[2]})`;
      themeEl.appendChild(s1);
    }
    
    if (secondary) {
      const s2 = document.createElement('div');
      s2.className = 'color-swatch';
      s2.style.background = `rgb(${secondary[0]}, ${secondary[1]}, ${secondary[2]})`;
      themeEl.appendChild(s2);
    }
  }
}

function update() {
  fetch('/api/status')
    .then(r => r.json())
    .then(d => {
      if (d.error) return;
      
      // Top bar
      document.getElementById('bpmDisplay').textContent = d.bpm ? d.bpm.toFixed(1) : '--';
      
      const clockSourceEl = document.getElementById('clockSource');
      if (d.bpm_source === 'prodjlink') {
        clockSourceEl.textContent = 'Clock: Pro DJ Link';
        clockSourceEl.className = 'clock-source prodjlink';
      } else {
        clockSourceEl.textContent = 'Clock: FFT';
        clockSourceEl.className = 'clock-source fft';
      }
      
      // Status dots
      const prodjlinkConnected = d.prodjlink && d.prodjlink.connected;
      document.getElementById('prodjlinkDot').className = 'dot ' + (prodjlinkConnected ? 'on' : 'off');
      document.getElementById('dmxDot').className = 'dot ' + (d.dmx ? 'on' : 'off');
      document.getElementById('goveeDot').className = 'dot ' + (d.govee ? 'on' : 'off');
      
      // Deck displays
      if (d.prodjlink && d.prodjlink.decks) {
        if (d.prodjlink.decks[1]) {
          updateDeckDisplay(1, d.prodjlink.decks[1]);
        }
        if (d.prodjlink.decks[2]) {
          updateDeckDisplay(2, d.prodjlink.decks[2]);
        }
      }
      
      // Scene map (assume active deck for now; could be enhanced to track master deck)
      if (d.scene_map) {
        // Always use the latest scene_map from the API
        const trackId = (d.scene_map.title || '') + '|' + (d.scene_map.artist || '') + '|' + (d.scene_map.deck || '');
        if (trackId !== lastTrackId) {
          // New track or deck change
          currentSceneMap = d.scene_map;
          lastTrackId = trackId;
          playheadPos = 0;
          targetPlayheadPos = 0;
          drawWaveform();
        } else {
          // Update sections if they changed (re-analysis)
          currentSceneMap = d.scene_map;
        }
        
        // Update playhead position from master deck
        const masterDeck = d.prodjlink && d.prodjlink.master_deck;
        const masterDeckData = masterDeck && d.prodjlink.decks && d.prodjlink.decks[masterDeck];
        const totalBeats = d.scene_map.total_beats 
          || (d.scene_map.sections && d.scene_map.sections.length > 0
            ? d.scene_map.sections[d.scene_map.sections.length - 1].end_beat
            : 1);
        // Use audio_beat_count (from Scarlett audio detection) for playhead
        // This is the most accurate real-time position
        const audioBeat = d.audio_beat_count || 0;
        const bpm = (masterDeckData && masterDeckData.bpm) || d.bpm || 130;
        
        // Interpolate between server polls using local time
        if (!window._lastBeatUpdate || audioBeat !== window._lastBeatCount) {
          window._lastBeatUpdate = Date.now();
          window._lastBeatCount = audioBeat;
        }
        const msSinceUpdate = Date.now() - (window._lastBeatUpdate || Date.now());
        const extraBeats = (msSinceUpdate / 1000) * (bpm / 60);
        const interpolatedBeat = audioBeat + extraBeats;
        targetPlayheadPos = totalBeats > 0 ? Math.min(1, Math.max(0, interpolatedBeat / totalBeats)) : 0;
      } else {
        if (lastTrackId !== null) {
          // Track unloaded
          currentSceneMap = null;
          lastTrackId = null;
          playheadPos = 0;
          targetPlayheadPos = 0;
          drawWaveform();
        }
      }
      
      // Current state bar
      if (d.current_section) {
        const sectionTypeEl = document.getElementById('currentSectionType');
        sectionTypeEl.textContent = (d.current_section.type || '--').toUpperCase();
        sectionTypeEl.className = 'state-value large section-type-' + (d.current_section.type || '');
        
        document.getElementById('currentScene').textContent = d.current_section.scene || '--';
        
        const masterDeck2 = d.prodjlink && d.prodjlink.master_deck;
        const masterDeckData2 = masterDeck2 && d.prodjlink.decks && d.prodjlink.decks[masterDeck2];
        const beatCount = (masterDeckData2 && masterDeckData2.beat_count) || d.beat_count || 0;
        const totalBeats = d.scene_map ? (d.scene_map.total_beats || (d.scene_map.sections && d.scene_map.sections.length > 0
          ? d.scene_map.sections[d.scene_map.sections.length - 1].end_beat : 0)) : 0;
        document.getElementById('beatPosition').textContent = beatCount + ' / ' + totalBeats;
        
        // Time in section (rough estimate based on BPM and beat position)
        const startBeat = d.current_section.start_beat || 0;
        const beatsInSection = beatCount - startBeat;
        const bpm = d.bpm || '--';
        const secondsInSection = (beatsInSection / bpm) * 60;
        document.getElementById('sectionTime').textContent = secondsInSection.toFixed(1) + 's';
      } else {
        document.getElementById('currentSectionType').textContent = '--';
        document.getElementById('currentSectionType').className = 'state-value large';
        document.getElementById('currentScene').textContent = '--';
        document.getElementById('beatPosition').textContent = '-- / --';
        document.getElementById('sectionTime').textContent = '--';
      }
      
      // Bottom stats
      document.getElementById('energyStat').textContent = d.energy ? d.energy.toFixed(0) : '--';
      document.getElementById('modeStat').textContent = d.mode ? d.mode.toUpperCase() : '--';
      document.getElementById('phraseStat').textContent = d.viz && d.viz.phrase ? d.viz.phrase : '--';
    })
    .catch(() => {});
}

setInterval(update, 50);
update();
</script>
</body>
</html>"""


SCENES_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>lightd — Scene Browser</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #0a0a0e;
    color: #fff;
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    padding: 16px;
    overflow-y: auto;
  }
  h1 { font-size: 20px; font-weight: 300; margin-bottom: 8px; letter-spacing: 2px; }
  .nav { margin-bottom: 20px; font-size: 12px; }
  .nav a { color: #0f0; text-decoration: none; }
  .nav a:hover { text-decoration: underline; }
  .category { margin-bottom: 24px; }
  .cat-header {
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 2px;
    text-transform: uppercase;
    padding: 6px 14px;
    border-radius: 4px;
    display: inline-block;
    margin-bottom: 10px;
  }
  .cat-ambient .cat-header { background: #1a3a5c; color: #4a90d9; }
  .cat-breakdown .cat-header { background: #2d1a3e; color: #9b59b6; }
  .cat-buildup .cat-header { background: #3e2a0e; color: #e67e22; }
  .cat-groove .cat-header { background: #0e2e1a; color: #2ecc71; }
  .cat-drop .cat-header { background: #3e0e0e; color: #e74c3c; }
  .scene-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 8px;
  }
  .scene-card {
    background: #14141a;
    border: 1px solid #222;
    border-radius: 6px;
    padding: 12px 14px;
    cursor: pointer;
    transition: all 0.15s ease;
    position: relative;
  }
  .scene-card:hover {
    border-color: #555;
    background: #1a1a22;
    transform: translateY(-1px);
  }
  .scene-card.active {
    border-color: #0f0;
    box-shadow: 0 0 12px rgba(0, 255, 0, 0.2);
  }
  .scene-name {
    font-size: 14px;
    font-weight: 400;
    margin-bottom: 4px;
  }
  .scene-status {
    font-size: 10px;
    color: #555;
    text-transform: uppercase;
    letter-spacing: 1px;
  }
  .scene-card.active .scene-status {
    color: #0f0;
  }
  .now-playing {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    background: #0c0c12;
    border-top: 1px solid #1a1a22;
    padding: 10px 16px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 13px;
    z-index: 10;
  }
  .now-label { color: #555; font-size: 10px; text-transform: uppercase; letter-spacing: 1px; }
  .now-scene { font-size: 16px; font-weight: 300; }
  .now-cat { font-size: 12px; padding: 2px 8px; border-radius: 3px; }
  .stop-btn {
    background: #2a1a1a;
    color: #e74c3c;
    border: 1px solid #e74c3c33;
    padding: 6px 16px;
    border-radius: 4px;
    cursor: pointer;
    font-family: inherit;
    font-size: 12px;
    letter-spacing: 1px;
  }
  .stop-btn:hover { background: #3a1a1a; }
  body { padding-bottom: 60px; }
</style>
</head>
<body>
<div class="nav"><a href="/">&larr; Dashboard</a></div>
<h1>SCENE BROWSER</h1>
<div id="scenes"></div>
<div class="now-playing">
  <div>
    <div class="now-label">Now Playing</div>
    <div><span class="now-scene" id="np-scene">—</span> <span class="now-cat" id="np-cat"></span></div>
  </div>
  <button class="stop-btn" onclick="sendCmd('blackout')">BLACKOUT</button>
</div>
<script>
const CATEGORY_ORDER = ['ambient', 'breakdown', 'buildup', 'groove', 'drop'];
const CATEGORY_COLORS = {
  ambient: '#4a90d9', breakdown: '#9b59b6', buildup: '#e67e22',
  groove: '#2ecc71', drop: '#e74c3c'
};

let scenes = [];
let currentScene = '';

async function loadScenes() {
  try {
    const r = await fetch('/api/scenes');
    scenes = await r.json();
    renderScenes();
  } catch(e) { console.error('Failed to load scenes', e); }
}

function renderScenes() {
  const container = document.getElementById('scenes');
  container.innerHTML = '';
  const grouped = {};
  scenes.forEach(s => {
    if (!grouped[s.category]) grouped[s.category] = [];
    grouped[s.category].push(s);
  });
  CATEGORY_ORDER.forEach(cat => {
    if (!grouped[cat]) return;
    const div = document.createElement('div');
    div.className = 'category cat-' + cat;
    div.innerHTML = '<div class="cat-header">' + cat.toUpperCase() + ' (' + grouped[cat].length + ')</div>';
    const grid = document.createElement('div');
    grid.className = 'scene-grid';
    grouped[cat].forEach(s => {
      const card = document.createElement('div');
      card.className = 'scene-card';
      card.id = 'scene-' + s.name.replace(/\s+/g, '-');
      card.innerHTML = '<div class="scene-name">' + s.name + '</div><div class="scene-status">Click to preview</div>';
      card.onclick = () => triggerScene(s.name);
      grid.appendChild(card);
    });
    div.appendChild(grid);
    container.appendChild(div);
  });
}

async function triggerScene(name) {
  try {
    await fetch('/api/scene', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({scene: name})
    });
  } catch(e) { console.error('Failed to trigger scene', e); }
}

async function sendCmd(cmd) {
  try {
    await fetch('/api/command', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({command: cmd})
    });
  } catch(e) { console.error('Failed', e); }
}

function pollStatus() {
  fetch('/api/status').then(r => r.json()).then(d => {
    const scene = d.scene || '';
    const cat = d.category || '';
    if (scene !== currentScene) {
      currentScene = scene;
      document.querySelectorAll('.scene-card').forEach(c => c.classList.remove('active'));
      const id = 'scene-' + scene.replace(/\s+/g, '-');
      const el = document.getElementById(id);
      if (el) {
        el.classList.add('active');
        el.querySelector('.scene-status').textContent = 'ACTIVE';
      }
      document.querySelectorAll('.scene-card:not(.active) .scene-status').forEach(s => { s.textContent = 'Click to preview'; });
    }
    document.getElementById('np-scene').textContent = scene || '—';
    const npCat = document.getElementById('np-cat');
    npCat.textContent = cat.toUpperCase();
    npCat.style.background = (CATEGORY_COLORS[cat] || '#333') + '33';
    npCat.style.color = CATEGORY_COLORS[cat] || '#888';
  }).catch(() => {});
  setTimeout(pollStatus, 200);
}

loadScenes();
pollStatus();
</script>
</body>
</html>"""  # noqa: E501


def get_scenes_list():
    """Get scene list from scenes_v2 for the API."""
    return [{'name': s['name'], 'category': s['category']} for s in SCENES]


def send_lightd_command(cmd):
    """Send a command to lightd and return response."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(SOCKET_PATH)
        sock.sendall((cmd + '\n').encode())
        resp = sock.recv(65536).decode().strip()
        sock.close()
        return resp
    except Exception as e:
        return json.dumps({'error': str(e)})


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/status':
            with state_lock:
                data = json.dumps(state)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-cache, no-store')
            self.end_headers()
            self.wfile.write(data.encode())
        elif self.path == '/api/scenes':
            data = json.dumps(get_scenes_list())
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data.encode())
        elif self.path == '/scenes':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(SCENES_HTML.encode())
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Cache-Control', 'no-cache, no-store')
            self.end_headers()
            self.wfile.write(HTML.encode())

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode() if content_length > 0 else '{}'
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}

        if self.path == '/api/scene':
            scene_name = data.get('scene', '')
            resp = send_lightd_command(f'scene {scene_name}')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'result': resp}).encode())
        elif self.path == '/api/command':
            cmd = data.get('command', '')
            resp = send_lightd_command(cmd)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'result': resp}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    threading.Thread(target=poll_lightd, daemon=True).start()
    class S(http.server.HTTPServer):
        allow_reuse_address = True
    server = S(('0.0.0.0', PORT), DashboardHandler)
    print(f"🖥️  Dashboard: http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
