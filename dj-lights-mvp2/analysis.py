from __future__ import annotations

from typing import Any, Dict, List

PSSI_HIGH_MOOD = {
    1: "intro",
    2: "buildup",
    3: "breakdown",
    5: "drop",
    6: "outro",
}

PSSI_MID_MOOD = {
    1: "intro",
    2: "groove",
    3: "groove",
    5: "drop",
    6: "breakdown",
    7: "outro",
}

PSSI_LOW_MOOD = {
    1: "intro",
    2: "groove",
    3: "groove",
    5: "drop",
    6: "breakdown",
    7: "outro",
}


def mode_map_for_mood(mood: str | int | None) -> Dict[int, str]:
    if str(mood) in {"high", "1"}:
        return PSSI_HIGH_MOOD
    if str(mood) in {"mid", "2"}:
        return PSSI_MID_MOOD
    return PSSI_LOW_MOOD


def normalize_phrases(pssi: Dict[str, Any]) -> List[Dict[str, Any]]:
    mood = pssi.get("mood", "low")
    phrase_map = mode_map_for_mood(mood)
    raw_phrases = []
    for phrase in pssi.get("phrases", []):
        phrase_type = phrase.get("type")
        mode = phrase_map.get(phrase_type, "groove")
        start_beat = int(phrase.get("start_beat", 0))
        end_beat = int(phrase.get("end_beat", 0))
        if end_beat <= start_beat:
            continue
        raw_phrases.append(
            {
                "start_beat": start_beat,
                "end_beat": end_beat,
                "mode": mode,
                "raw_type": phrase_type,
            }
        )

    if not raw_phrases:
        return []

    raw_phrases.sort(key=lambda p: p["start_beat"])
    first_start = raw_phrases[0]["start_beat"]
    if first_start > 0:
        raw_phrases[0]["start_beat"] = 0

    merged: List[Dict[str, Any]] = []
    for phrase in raw_phrases:
        if merged and merged[-1]["mode"] == phrase["mode"]:
            merged[-1]["end_beat"] = max(merged[-1]["end_beat"], phrase["end_beat"])
        else:
            merged.append(dict(phrase))

    squashed: List[Dict[str, Any]] = []
    min_section_beats = 16
    for phrase in merged:
        length = phrase["end_beat"] - phrase["start_beat"]
        if squashed and length < min_section_beats:
            squashed[-1]["end_beat"] = phrase["end_beat"]
        else:
            squashed.append(dict(phrase))

    if squashed:
        squashed[0]["start_beat"] = 0
        for i in range(len(squashed) - 1):
            squashed[i]["end_beat"] = squashed[i + 1]["start_beat"]

    return squashed


def simplify_waveform(raw_waveform: List[int], target_points: int = 240) -> List[int]:
    if not raw_waveform:
        return []
    if len(raw_waveform) <= target_points:
        return raw_waveform
    bucket = max(1, len(raw_waveform) // target_points)
    out = []
    for i in range(0, len(raw_waveform), bucket):
        out.append(max(raw_waveform[i:i + bucket]))
    return out[:target_points]


def build_track_analysis(track: Dict[str, Any], pssi: Dict[str, Any], waveform: List[int]) -> Dict[str, Any]:
    return {
        "track": {
            "track_id": track.get("track_id"),
            "title": track.get("title"),
            "artist": track.get("artist"),
            "deck": track.get("deck"),
            "duration": track.get("duration"),
            "bpm": track.get("bpm"),
        },
        "mood": pssi.get("mood", "low"),
        "phrases": normalize_phrases(pssi),
        "waveform": simplify_waveform(waveform),
    }
