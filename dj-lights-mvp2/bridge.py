from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from analysis import build_track_analysis

BASE = Path(__file__).resolve().parent
CACHE = BASE / "cache"
CACHE.mkdir(exist_ok=True)


def cache_path(track_id: str | int) -> Path:
    return CACHE / f"{track_id}.json"


def save_analysis(analysis: Dict[str, Any]) -> Path:
    track_id = analysis["track"].get("track_id", "unknown")
    path = cache_path(track_id)
    path.write_text(json.dumps(analysis, indent=2))
    return path


def load_analysis(track_id: str | int) -> Dict[str, Any] | None:
    path = cache_path(track_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def ingest_track_payload(track: Dict[str, Any], pssi: Dict[str, Any], waveform: List[int]) -> Dict[str, Any]:
    existing = load_analysis(track.get("track_id"))
    existing_waveform = (existing or {}).get("waveform") or []
    incoming_waveform = waveform or []

    if existing and (existing_waveform or not incoming_waveform):
        return existing

    analysis = build_track_analysis(track, pssi, incoming_waveform)
    save_analysis(analysis)
    return analysis
