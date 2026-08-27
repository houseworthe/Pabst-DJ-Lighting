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

# Cap any drop phrase at 32 bars (128 beats). The remainder gets downgraded to
# groove — PSSI never emits `groove` on high-mood tracks, so the groove scene
# pool is otherwise dead inventory. Threshold for creating the tail: only split
# if the remainder is at least 16 beats, matching normalize_phrases'
# min_section_beats — otherwise we'd flicker to a 2-beat groove sliver.
MAX_DROP_BEATS = 128
MIN_SPLIT_TAIL_BEATS = 16


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

    return _coalesce_builds(_cap_long_drops(squashed))


# Modes that make up the "climb toward a drop". A run of these that contains at
# least one buildup is collapsed into a single `build` *scene* section (so the
# scene is picked once and held — no mid-climb re-pick), while each sub-phrase
# keeps its own `intensity_mode` so the auto-curve still dips on the breakdown
# and ramps on the buildup. See _coalesce_builds.
_BUILD_FAMILY = {"buildup", "breakdown"}


def _coalesce_builds(phrases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Tag breakdown→buildup (and buildup→breakdown→buildup fake-outs) as one
    held `build` section without merging the sub-phrases.

    Every phrase gets `intensity_mode` = its original mode (drives the
    per-mode intensity curve). Then, for each maximal run of consecutive
    {buildup, breakdown} phrases that contains at least one buildup, the phrases
    from the run start through the LAST buildup get `mode = "build"` (the
    scene-pick key). Because they all share mode "build", main.py picks one
    scene and holds it across the whole climb; because each keeps its
    intensity_mode, the curve still dips during the breakdown and ramps during
    the buildup.

    Trailing breakdowns after the last buildup are left as `breakdown` — that's
    a wind-down (e.g. `buildup breakdown outro`), not part of the build, so it
    keeps its own calm look.
    """
    for p in phrases:
        p.setdefault("intensity_mode", p["mode"])

    n = len(phrases)
    i = 0
    while i < n:
        if phrases[i]["mode"] not in _BUILD_FAMILY:
            i += 1
            continue
        j = i
        while j < n and phrases[j]["mode"] in _BUILD_FAMILY:
            j += 1
        run = range(i, j)
        last_buildup = max(
            (k for k in run if phrases[k]["mode"] == "buildup"), default=None
        )
        if last_buildup is not None:
            for k in range(i, last_buildup + 1):
                phrases[k]["intensity_mode"] = phrases[k]["mode"]
                phrases[k]["mode"] = "build"
        i = j
    return phrases


def _cap_long_drops(phrases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Split any drop phrase > MAX_DROP_BEATS into drop[:cap] + groove[cap:end]."""
    out: List[Dict[str, Any]] = []
    for p in phrases:
        if p["mode"] != "drop":
            out.append(p)
            continue
        length = p["end_beat"] - p["start_beat"]
        if length - MAX_DROP_BEATS < MIN_SPLIT_TAIL_BEATS:
            out.append(p)
            continue
        split = p["start_beat"] + MAX_DROP_BEATS
        out.append({**p, "end_beat": split})
        out.append({
            "start_beat": split,
            "end_beat": p["end_beat"],
            "mode": "groove",
            "raw_type": p.get("raw_type"),
        })
    return out


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
