"""Fetch and parse time-synced lyrics for the currently playing track.

Uses the free, keyless LRCLIB API (https://lrclib.net) which serves
community-contributed synced lyrics in standard LRC format. No account,
API key, or scraping involved.

This module is intentionally standalone: it knows nothing about Spotify,
the matrix, or threading. spotify_matrix.py calls fetch_synced_lyrics()
with the track metadata it already has and gets back a list of
(timestamp, line) pairs, or None if nothing was found.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from urllib.error import HTTPError, URLError

LRCLIB_GET_URL = "https://lrclib.net/api/get"
LRCLIB_SEARCH_URL = "https://lrclib.net/api/search"

# Matches LRC timestamp lines like "[01:23.45]Some lyric text"
_LRC_LINE_RE = re.compile(r"\[(\d{2}):(\d{2})(?:\.(\d{1,3}))?\](.*)")


@dataclass
class LyricsLine:
    time_seconds: float
    text: str


def fetch_synced_lyrics(
    artist: str,
    title: str,
    album: str | None = None,
    duration_seconds: float | None = None,
    timeout: float = 8.0,
) -> list[LyricsLine] | None:
    """Look up synced lyrics for a track. Returns None if none are found."""
    if not artist or not title:
        return None

    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration_seconds:
        params["duration"] = str(int(round(duration_seconds)))

    data = _get_json(f"{LRCLIB_GET_URL}?{urllib.parse.urlencode(params)}", timeout)

    if not data:
        # Exact lookup (which is picky about duration/album matching) missed.
        # Fall back to fuzzy search and take the top result.
        query = urllib.parse.urlencode({"artist_name": artist, "track_name": title})
        results = _get_json(f"{LRCLIB_SEARCH_URL}?{query}", timeout)
        if isinstance(results, list) and results:
            data = results[0]

    if not data:
        return None

    synced = data.get("syncedLyrics")
    if not synced:
        return None

    parsed = _parse_lrc(synced)
    return parsed or None


def current_line_index(lines: list[LyricsLine], position_seconds: float) -> int:
    """Index of the lyric line active at position_seconds, or -1 before the first line."""
    index = -1
    for i, line in enumerate(lines):
        if line.time_seconds <= position_seconds:
            index = i
        else:
            break
    return index


def _parse_lrc(lrc_text: str) -> list[LyricsLine]:
    lines: list[LyricsLine] = []
    for raw_line in lrc_text.splitlines():
        match = _LRC_LINE_RE.match(raw_line.strip())
        if not match:
            continue
        minutes, seconds, frac, text = match.groups()
        total = int(minutes) * 60 + int(seconds)
        if frac:
            total += int(frac.ljust(3, "0")) / 1000.0
        text = text.strip()
        if text:
            lines.append(LyricsLine(total, text))
    lines.sort(key=lambda line: line.time_seconds)
    return lines


def _get_json(url: str, timeout: float):
    request = urllib.request.Request(url, headers={"User-Agent": "spotify-matrix-lyrics/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError):
        return None
