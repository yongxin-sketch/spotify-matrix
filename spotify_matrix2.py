#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
from io import BytesIO
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
from email.message import Message
from urllib.error import HTTPError
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

from lyrics import LyricsLine, current_line_index, fetch_synced_lyrics

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> None:
        return None


AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
CURRENTLY_PLAYING_URL = "https://api.spotify.com/v1/me/player/currently-playing"
SCOPE = "user-read-currently-playing"

# The two things the matrix can show. Switched at runtime via ModeControlServer,
# e.g. from an iPhone Home Screen widget hitting /mode/lyrics or /mode/art.
MODE_ART = "art"
MODE_LYRICS = "lyrics"


@dataclass
class PlaybackArt:
    key: str
    image_url: str
    is_playing: bool


@dataclass
class SharedPlaybackState:
    art_key: str | None = None
    image_url: str | None = None
    image: Image.Image | None = None
    is_playing: bool = False

    # Added for the lyrics display and the phone-widget mode switch.
    track_key: str | None = None
    duration_ms: int | None = None
    progress_ms: int = 0
    progress_captured_at: float = 0.0
    lyrics: list[LyricsLine] | None = None
    mode: str = MODE_ART


@dataclass
class HttpResponse:
    status: int
    headers: Message
    body: bytes

    def json(self) -> dict[str, Any]:
        return json.loads(self.body.decode("utf-8"))


def http_request(
    method: str,
    url: str,
    *,
    params: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
) -> HttpResponse:
    if params:
        separator = "&" if urllib.parse.urlparse(url).query else "?"
        url = f"{url}{separator}{urllib.parse.urlencode(params)}"

    encoded_data = urllib.parse.urlencode(data).encode("utf-8") if data else None
    request = urllib.request.Request(
        url,
        data=encoded_data,
        headers=headers or {},
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(response.status, response.headers, response.read())
    except HTTPError as exc:
        return HttpResponse(exc.code, exc.headers, exc.read())


def raise_http_error(response: HttpResponse, context: str) -> None:
    body = response.body.decode("utf-8", errors="replace")
    raise RuntimeError(f"{context} failed with HTTP {response.status}: {body}")


class SpotifyClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        token_cache: Path,
        open_browser: bool,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.token_cache = token_cache
        self.open_browser = open_browser
        self.token = self._load_token()

    def get_currently_playing(self) -> dict[str, Any] | None:
        token = self._valid_access_token()
        response = http_request(
            "GET",
            CURRENTLY_PLAYING_URL,
            params={"additional_types": "track,episode"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )

        if response.status == 204:
            return None
        if response.status == 401:
            self._refresh_access_token()
            return self.get_currently_playing()
        if response.status == 429:
            retry_after = int(response.headers.get("Retry-After", "5"))
            time.sleep(max(retry_after, 1))
            return None
        if response.status != 200:
            raise_http_error(response, "Spotify currently-playing request")

        return response.json()

    def authorize(self) -> None:
        self._valid_access_token()

    def _valid_access_token(self) -> str:
        if not self.token:
            self.token = self._authorize()

        if time.time() >= float(self.token.get("expires_at", 0)):
            self._refresh_access_token()

        return str(self.token["access_token"])

    def _load_token(self) -> dict[str, Any] | None:
        if not self.token_cache.exists():
            return None

        with self.token_cache.open("r", encoding="utf-8") as token_file:
            return json.load(token_file)

    def _save_token(self, token: dict[str, Any]) -> None:
        self.token_cache.parent.mkdir(parents=True, exist_ok=True)
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600)) - 60

        previous_refresh_token = self.token.get("refresh_token") if self.token else None
        if previous_refresh_token and "refresh_token" not in token:
            token["refresh_token"] = previous_refresh_token

        with self.token_cache.open("w", encoding="utf-8") as token_file:
            json.dump(token, token_file, indent=2)

        self.token = token

    def _authorize(self) -> dict[str, Any]:
        state = secrets.token_urlsafe(18)
        parsed_redirect = urllib.parse.urlparse(self.redirect_uri)
        if parsed_redirect.hostname not in {"127.0.0.1", "localhost"}:
            raise RuntimeError("This script expects a localhost Spotify redirect URI.")

        callback = LocalCallbackServer(
            host=parsed_redirect.hostname or "127.0.0.1",
            port=parsed_redirect.port or 80,
            path=parsed_redirect.path or "/callback",
            expected_state=state,
        )

        query = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "response_type": "code",
                "redirect_uri": self.redirect_uri,
                "scope": SCOPE,
                "state": state,
            }
        )
        auth_url = f"{AUTH_URL}?{query}"

        print("Authorize Spotify in your browser:")
        print(auth_url)
        if self.open_browser:
            webbrowser.open(auth_url)

        code = callback.wait_for_code()
        token = self._post_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
            }
        )
        self._save_token(token)
        return token

    def _refresh_access_token(self) -> None:
        refresh_token = self.token.get("refresh_token") if self.token else None
        if not refresh_token:
            self.token = self._authorize()
            return

        token = self._post_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        self._save_token(token)

    def _post_token(self, data: dict[str, str]) -> dict[str, Any]:
        credentials = f"{self.client_id}:{self.client_secret}".encode("utf-8")
        basic_auth = base64.b64encode(credentials).decode("ascii")
        response = http_request(
            "POST",
            TOKEN_URL,
            data=data,
            headers={
                "Authorization": f"Basic {basic_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=10,
        )
        if response.status != 200:
            raise_http_error(response, "Spotify token request")
        return response.json()


class LocalCallbackServer:
    def __init__(self, host: str, port: int, path: str, expected_state: str) -> None:
        self.code: str | None = None
        self.error: str | None = None
        self.state_error: str | None = None
        self.path = path
        self.expected_state = expected_state

        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)

                if parsed.path != parent.path:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"Wrong callback path.")
                    return

                returned_state = params.get("state", [""])[0]
                if returned_state != parent.expected_state:
                    parent.state_error = "Spotify callback state did not match."
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"State mismatch.")
                    return

                if "error" in params:
                    parent.error = params["error"][0]
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Spotify authorization failed.")
                    return

                parent.code = params.get("code", [None])[0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Spotify authorization complete. You can close this tab.")

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.server = HTTPServer((host, port), Handler)

    def wait_for_code(self) -> str:
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        try:
            while not self.code and not self.error and not self.state_error:
                time.sleep(0.1)
        finally:
            self.server.shutdown()
            self.server.server_close()

        if self.state_error:
            raise RuntimeError(self.state_error)
        if self.error:
            raise RuntimeError(f"Spotify authorization failed: {self.error}")
        if not self.code:
            raise RuntimeError("Spotify authorization did not return a code.")
        return self.code


class ModeControlServer:
    """Tiny local HTTP server so a phone widget can switch the matrix between
    the spinning album art and scrolling lyrics. Independent of the Spotify
    OAuth callback server above -- this one just flips SharedPlaybackState.mode.

    GET /mode          -> {"mode": "art"|"lyrics"}
    GET /mode/art      -> switch to album art, returns the new mode
    GET /mode/lyrics   -> switch to lyrics, returns the new mode
    GET /mode/toggle   -> flip between the two, returns the new mode
    """

    def __init__(self, host: str, port: int, state: SharedPlaybackState, state_lock: threading.Lock) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = urllib.parse.urlparse(self.path).path

                if path == "/mode/art":
                    with state_lock:
                        state.mode = MODE_ART
                elif path == "/mode/lyrics":
                    with state_lock:
                        state.mode = MODE_LYRICS
                elif path == "/mode/toggle":
                    with state_lock:
                        state.mode = MODE_LYRICS if state.mode == MODE_ART else MODE_ART
                elif path != "/mode":
                    self.send_response(404)
                    self.end_headers()
                    return

                with state_lock:
                    body = json.dumps({"mode": state.mode}).encode("utf-8")

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                return

        try:
            self.server = HTTPServer((host, port), Handler)
        except OSError as exc:
            if exc.errno == 98:  # EADDRINUSE
                raise SystemExit(
                    f"Could not start the mode-control server on {host}:{port} -- that port is "
                    "already in use. This usually means a previous spotify_matrix.py process is "
                    "still running in the background. Find it with `ps aux | grep spotify_matrix` "
                    "and stop it with `sudo pkill -f spotify_matrix.py`, or pass a different "
                    "--control-port."
                ) from exc
            raise
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class MatrixDisplay:
    def __init__(self, args: argparse.Namespace) -> None:
        try:
            from rgbmatrix import RGBMatrix, RGBMatrixOptions
        except ImportError as exc:
            raise RuntimeError(
                "The rgbmatrix Python bindings are not installed. "
                "Install hzeller/rpi-rgb-led-matrix on the Pi, or run with --mock-output."
            ) from exc

        options = RGBMatrixOptions()
        options.rows = args.rows
        options.cols = args.cols
        options.chain_length = args.chain_length
        options.parallel = args.parallel
        options.brightness = args.brightness
        options.gpio_slowdown = args.gpio_slowdown
        options.hardware_mapping = args.hardware_mapping
        options.pwm_bits = args.pwm_bits
        options.limit_refresh_rate_hz = args.limit_refresh_rate_hz
        options.disable_hardware_pulsing = args.no_hardware_pulse
        # The library drops root -> "daemon" by default right after hardware init.
        # That silently broke writing the Spotify token cache (and anything else on
        # disk) for the rest of the process's life, since "daemon" typically can't
        # reach a user's home directory. Keep full privileges unless opted out.
        options.drop_privileges = args.matrix_drop_privileges

        self.matrix = RGBMatrix(options=options)
        self.canvas = self.matrix.CreateFrameCanvas()

    def show(self, image: Image.Image) -> None:
        self.canvas.SetImage(image.convert("RGB"))
        self.canvas = self.matrix.SwapOnVSync(self.canvas)

    def clear(self) -> None:
        self.matrix.Clear()


class MockDisplay:
    def __init__(self, output: Path) -> None:
        self.output = output
        self.output.parent.mkdir(parents=True, exist_ok=True)

    def show(self, image: Image.Image) -> None:
        image.save(self.output)

    def clear(self) -> None:
        return


def demo_album_art(size: int) -> Image.Image:
    image = Image.new("RGB", (size, size), (18, 18, 18))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, size // 2, size // 2), fill=(238, 70, 60))
    draw.rectangle((size // 2, 0, size, size // 2), fill=(245, 180, 40))
    draw.rectangle((0, size // 2, size // 2, size), fill=(35, 150, 235))
    draw.rectangle((size // 2, size // 2, size, size), fill=(65, 185, 95))
    draw.line((0, 0, size, size), fill=(255, 255, 255), width=max(2, size // 18))
    draw.line((size, 0, 0, size), fill=(0, 0, 0), width=max(2, size // 22))
    return image


def playback_art_from_response(playback: dict[str, Any] | None) -> PlaybackArt | None:
    if not playback:
        return None

    item = playback.get("item")
    if not item:
        return None

    item_type = item.get("type")
    if item_type == "track":
        images = item.get("album", {}).get("images", [])
    else:
        images = item.get("images", [])

    if not images:
        return None

    image = max(images, key=lambda candidate: candidate.get("width") or 0)
    item_id = item.get("id") or item.get("uri") or image["url"]
    return PlaybackArt(
        key=str(item_id),
        image_url=image["url"],
        is_playing=bool(playback.get("is_playing")),
    )


@dataclass
class TrackMeta:
    key: str
    artist: str
    title: str
    album: str | None
    duration_ms: int | None
    progress_ms: int


def track_meta_from_response(playback: dict[str, Any] | None) -> TrackMeta | None:
    """Extract the fields needed for a lyrics lookup. Separate from
    playback_art_from_response so that function stays exactly as it was."""
    if not playback:
        return None

    item = playback.get("item")
    if not item or item.get("type") != "track":
        return None  # Lyrics lookup only makes sense for tracks, not podcast episodes.

    artists = item.get("artists") or []
    artist = artists[0].get("name") if artists else ""
    title = item.get("name") or ""
    album = (item.get("album") or {}).get("name")
    item_id = item.get("id") or item.get("uri")

    return TrackMeta(
        key=str(item_id),
        artist=artist,
        title=title,
        album=album,
        duration_ms=item.get("duration_ms"),
        progress_ms=int(playback.get("progress_ms") or 0),
    )


def estimate_position_seconds(
    duration_ms: int | None,
    progress_ms: int,
    progress_captured_at: float,
    is_playing: bool,
) -> float:
    """Interpolate playback position between Spotify polls so lyrics scroll smoothly."""
    if not duration_ms:
        return 0.0
    elapsed_ms = (time.monotonic() - progress_captured_at) * 1000.0 if is_playing else 0.0
    position_ms = progress_ms + elapsed_ms
    return max(0.0, min(position_ms, duration_ms)) / 1000.0


def download_image(url: str) -> Image.Image:
    import requests

    response = requests.get(url, timeout=15)
    response.raise_for_status()
    return Image.open(BytesIO(response.content)).convert("RGB")


def render_record(art: Image.Image | None, angle: float, size: int) -> Image.Image:
    frame = Image.new("RGBA", (size, size), (0, 0, 0, 255))
    if art is None:
        return frame.convert("RGB")

    margin = max(2, size // 32)
    disc_size = size - margin * 2
    # The album art is the record surface: rotate it first, then cut it into a circular disk.
    art_square = ImageOps.fit(art, (disc_size, disc_size), method=Image.Resampling.LANCZOS)
    rotated = art_square.rotate(angle, resample=Image.Resampling.BICUBIC)

    disc_mask = Image.new("L", (disc_size, disc_size), 0)
    mask_draw = ImageDraw.Draw(disc_mask)
    mask_draw.ellipse((0, 0, disc_size - 1, disc_size - 1), fill=255)
    frame.paste(rotated.convert("RGBA"), (margin, margin), disc_mask)

    draw = ImageDraw.Draw(frame, "RGBA")
    outer = (margin, margin, size - margin - 1, size - margin - 1)
    draw.ellipse(outer, outline=(6, 6, 6, 255), width=max(1, size // 32))

    center = size // 2
    label_radius = max(5, size // 11)
    hole_radius = max(2, size // 25)
    draw.ellipse(
        (
            center - label_radius,
            center - label_radius,
            center + label_radius,
            center + label_radius,
        ),
        fill=(16, 16, 16, 210),
        outline=(220, 220, 220, 90),
    )
    draw.ellipse(
        (
            center - hole_radius,
            center - hole_radius,
            center + hole_radius,
            center + hole_radius,
        ),
        fill=(0, 0, 0, 255),
    )
    return frame.convert("RGB")


def render_idle(size: int) -> Image.Image:
    frame = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(frame)
    margin = max(2, size // 32)
    draw.ellipse((margin, margin, size - margin - 1, size - margin - 1), outline=(55, 55, 55), width=2)
    center = size // 2
    radius = max(3, size // 18)
    draw.ellipse((center - radius, center - radius, center + radius, center + radius), fill=(18, 18, 18))
    return frame


def render_test_pattern(size: int, offset: int) -> Image.Image:
    frame = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(frame)
    colors = (
        (255, 0, 0),
        (255, 160, 0),
        (255, 255, 0),
        (0, 255, 0),
        (0, 120, 255),
        (80, 0, 255),
        (255, 255, 255),
        (0, 0, 0),
    )
    stripe_width = max(1, size // len(colors))
    for index, color in enumerate(colors):
        x0 = (index * stripe_width + offset) % size
        draw.rectangle((x0, 0, min(size - 1, x0 + stripe_width - 1), size - 1), fill=color)
        if x0 + stripe_width > size:
            draw.rectangle((0, 0, (x0 + stripe_width) % size, size - 1), fill=color)
    draw.rectangle((0, 0, size - 1, size - 1), outline=(255, 255, 255))
    return frame


def load_lyrics_font(pixel_size: int, font_path: Path | None) -> ImageFont.FreeTypeFont:
    if font_path:
        try:
            return ImageFont.truetype(str(font_path), pixel_size)
        except OSError:
            print(f"Could not load --lyrics-font {font_path}, falling back to the default font.", flush=True)
    try:
        return ImageFont.load_default(size=pixel_size)  # Pillow >= 9.2
    except TypeError:
        return ImageFont.load_default()


def _wrap_text(text: str, font: ImageFont.ImageFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    """Word-wrap text to fit max_width at the given (constant) font size."""
    words = text.split()
    if not words:
        return [text]

    wrapped: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            wrapped.append(current)
            current = word
    wrapped.append(current)
    return wrapped


def _ease_in_out(t: float) -> float:
    """Smootherstep easing (Perlin's improved curve) -- more gradual accelerate/
    decelerate than plain smoothstep, so the glide reads less abrupt."""
    t = min(1.0, max(0.0, t))
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)


def render_lyrics(
    lines: list[LyricsLine] | None,
    position_seconds: float,
    size: int,
    font: ImageFont.ImageFont,
    line_height: int,
    transition_seconds: float = 0.6,
) -> Image.Image:
    """Vertically scrolling lyrics, current line highlighted.

    Every row is drawn at the same font size -- only the color changes for
    the active line. Long lyric lines are word-wrapped (not shrunk) to fit
    within a margin on either side of the matrix. The active line holds
    steady while it plays, then glides up into the next line only during a
    short transition window right before the next line's timestamp -- so it
    tracks the song instead of creeping the whole time between lines. If a
    line wraps onto multiple rows, the whole wrapped block is centered (not
    just its first row), so none of it runs off the bottom edge.
    """
    frame = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(frame)

    if not lines:
        draw.text((size // 2, size // 2), "...", fill=(90, 90, 90), font=font, anchor="mm")
        return frame

    margin = max(2, size // 16)
    max_text_width = size - margin * 2

    current_index = current_line_index(lines, position_seconds)

    # Hold the line steady, then glide into place during a short window that
    # ends exactly as the next line starts -- capped so it never eats into
    # more than half the gap for back-to-back lyric lines.
    progress_within_line = 0.0
    if 0 <= current_index < len(lines) - 1:
        line_start = lines[current_index].time_seconds
        line_end = lines[current_index + 1].time_seconds
        span = max(0.01, line_end - line_start)
        transition = min(transition_seconds, span * 0.5)
        transition_start = line_end - transition
        if position_seconds >= transition_start:
            raw_progress = (position_seconds - transition_start) / transition
            progress_within_line = _ease_in_out(raw_progress)

    base_index = max(current_index, 0)
    center_y = size // 2

    # Wrap only the lyric lines actually visible around the active one -- cheap,
    # since it's at most ~10 lines per frame, not the whole song.
    window_start = max(0, base_index - 3)
    window_end = min(len(lines), base_index + 7)
    rows: list[tuple[int, str]] = []  # (original_line_index, wrapped sub-line text)
    for index in range(window_start, window_end):
        for sub_line in _wrap_text(lines[index].text, font, max_text_width, draw):
            rows.append((index, sub_line))

    # first_row_offset / row_count of each lyric line's wrapped block, so we can
    # center the whole block (not just its first row) at center_y.
    line_spans: dict[int, tuple[int, int]] = {}
    for row_offset, (original_index, _text) in enumerate(rows):
        first_offset, count = line_spans.get(original_index, (row_offset, 0))
        line_spans[original_index] = (first_offset, count + 1)

    def block_top_y(index: int) -> float:
        first_offset, count = line_spans.get(index, (0, 1))
        return center_y - (count - 1) / 2.0 * line_height - first_offset * line_height

    top_y_start = block_top_y(base_index)
    top_y_end = block_top_y(base_index + 1) if base_index + 1 in line_spans else top_y_start
    top_y = top_y_start + (top_y_end - top_y_start) * progress_within_line

    for row_offset, (original_index, text) in enumerate(rows):
        y = top_y + row_offset * line_height
        if y < -line_height or y > size + line_height:
            continue
        if original_index == current_index:
            color = (255, 255, 255)
        elif original_index < current_index:
            color = (55, 55, 65)
        else:
            color = (90, 90, 100)
        draw.text((size // 2, y), text, fill=color, font=font, anchor="mm")

    return frame


def _fetch_and_store_lyrics(
    state: SharedPlaybackState,
    state_lock: threading.Lock,
    meta: TrackMeta,
) -> None:
    """Runs on its own thread so a slow/failed lrclib lookup never blocks Spotify polling."""
    lyrics = fetch_synced_lyrics(
        meta.artist,
        meta.title,
        meta.album,
        (meta.duration_ms / 1000.0) if meta.duration_ms else None,
    )
    with state_lock:
        # Only apply if the user hasn't already skipped to a different track
        # while this lookup was in flight -- avoids showing stale lyrics.
        if state.track_key == meta.key:
            state.lyrics = lyrics
    print(
        f"Lyrics: {'found' if lyrics else 'not found'} for {meta.artist} - {meta.title}",
        flush=True,
    )


def _fetch_and_store_art_image(
    state: SharedPlaybackState,
    state_lock: threading.Lock,
    art: PlaybackArt,
) -> None:
    """Runs on its own thread so downloading album art never blocks Spotify polling."""
    try:
        image = download_image(art.image_url)
    except Exception as exc:
        print(f"Album art download failed: {exc}", flush=True)
        return
    with state_lock:
        # Only apply if this is still the current track's art -- avoids a late
        # download landing after the user has already skipped past it.
        if state.art_key == art.key:
            state.image = image


def poll_spotify(
    spotify: SpotifyClient,
    state: SharedPlaybackState,
    state_lock: threading.Lock,
    stop_event: threading.Event,
    poll_seconds: float,
) -> None:
    last_status: str | None = None

    while not stop_event.is_set():
        try:
            playback = spotify.get_currently_playing()
            art = playback_art_from_response(playback)

            if art:
                with state_lock:
                    needs_download = art.key != state.art_key or art.image_url != state.image_url
                    state.art_key = art.key
                    state.image_url = art.image_url
                    state.is_playing = art.is_playing

                if needs_download:
                    threading.Thread(
                        target=_fetch_and_store_art_image,
                        args=(state, state_lock, art),
                        daemon=True,
                    ).start()

                status = f"art found, is_playing={art.is_playing}"

                # Lyrics lookup, added alongside the existing art handling above.
                meta = track_meta_from_response(playback)
                if meta:
                    with state_lock:
                        state.progress_ms = meta.progress_ms
                        state.progress_captured_at = time.monotonic()
                        is_new_track = meta.key != state.track_key
                        state.track_key = meta.key
                        state.duration_ms = meta.duration_ms

                    if is_new_track:
                        with state_lock:
                            state.lyrics = None  # clear immediately -- don't show the previous track's lyrics
                        threading.Thread(
                            target=_fetch_and_store_lyrics,
                            args=(state, state_lock, meta),
                            daemon=True,
                        ).start()
            else:
                with state_lock:
                    state.art_key = None
                    state.image_url = None
                    state.image = None
                    state.is_playing = False
                    state.track_key = None
                    state.duration_ms = None
                    state.lyrics = None
                status = "no currently playing item"

            if status != last_status:
                print(f"Spotify: {status}", flush=True)
                last_status = status
        except Exception as exc:
            print(f"Spotify poll failed: {exc}", flush=True)

        stop_event.wait(poll_seconds)


def run(args: argparse.Namespace) -> None:
    if args.preview_frames:
        render_preview_frames(args.preview_frames)
        return

    load_dotenv()

    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    redirect_uri = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")

    missing = [
        name
        for name, value in (
            ("SPOTIFY_CLIENT_ID", client_id),
            ("SPOTIFY_CLIENT_SECRET", client_secret),
            ("SPOTIFY_REDIRECT_URI", redirect_uri),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"Missing required environment values: {', '.join(missing)}")

    spotify = SpotifyClient(
        client_id=client_id or "",
        client_secret=client_secret or "",
        redirect_uri=redirect_uri,
        token_cache=args.token_cache,
        open_browser=not args.no_browser,
    )

    if args.auth_only:
        spotify.authorize()
        print(f"Spotify token cached at {args.token_cache}")
        return

    display: MatrixDisplay | MockDisplay
    if args.mock_output:
        display = MockDisplay(args.mock_output)
    else:
        display = MatrixDisplay(args)

    size = min(args.rows, args.cols)

    if args.test_pattern:
        try:
            offset = 0
            while True:
                display.show(render_test_pattern(size, offset))
                offset = (offset + 1) % size
                time.sleep(1.0 / args.fps)
        except KeyboardInterrupt:
            pass
        finally:
            display.clear()
        return

    idle = render_idle(size)
    playback_state = SharedPlaybackState()
    playback_lock = threading.Lock()
    stop_event = threading.Event()
    poll_thread = threading.Thread(
        target=poll_spotify,
        args=(spotify, playback_state, playback_lock, stop_event, args.poll_seconds),
        daemon=True,
    )
    poll_thread.start()

    control_server = ModeControlServer(args.control_host, args.control_port, playback_state, playback_lock)
    control_server.start()
    print(
        f"Mode control listening on http://{args.control_host}:{args.control_port} "
        "(GET /mode, /mode/art, /mode/lyrics, /mode/toggle)",
        flush=True,
    )
    lyrics_font = load_lyrics_font(args.lyrics_font_size, args.lyrics_font)
    lyrics_line_height = args.lyrics_font_size + 2

    angle = 0.0
    last_frame = time.monotonic()

    try:
        while True:
            frame_start = time.monotonic()
            with playback_lock:
                current_art_image = playback_state.image
                is_playing = playback_state.is_playing
                mode = playback_state.mode
                lyrics_lines = playback_state.lyrics
                duration_ms = playback_state.duration_ms
                progress_ms = playback_state.progress_ms
                progress_captured_at = playback_state.progress_captured_at

            now = time.monotonic()
            delta = now - last_frame
            last_frame = now

            if is_playing and current_art_image is not None:
                angle = (angle - 360.0 * (args.rpm / 60.0) * delta) % 360.0

            if mode == MODE_LYRICS and lyrics_lines:
                position_seconds = (
                    estimate_position_seconds(duration_ms, progress_ms, progress_captured_at, is_playing)
                    + args.lyrics_offset
                )
                image = render_lyrics(
                    lyrics_lines,
                    position_seconds,
                    size,
                    lyrics_font,
                    lyrics_line_height,
                    args.lyrics_transition_seconds,
                )
            else:
                image = render_record(current_art_image, angle, size) if current_art_image else idle
            display.show(image)

            if args.once:
                break

            sleep_for = max(0.0, (1.0 / args.fps) - (time.monotonic() - frame_start))
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        poll_thread.join(timeout=1)
        control_server.stop()
        display.clear()


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def render_preview_frames(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    art = demo_album_art(96)
    for index, angle in enumerate((0, 45, 90, 135)):
        render_record(art, angle, 64).save(directory / f"album-disk-{index:02d}.png")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Spin Spotify album art on a 64x64 RGB matrix.")
    parser.add_argument("--rows", type=int, default=64)
    parser.add_argument("--cols", type=int, default=64)
    parser.add_argument("--chain-length", type=int, default=1)
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--brightness", type=int, default=65)
    parser.add_argument("--gpio-slowdown", type=int, default=2)
    parser.add_argument("--hardware-mapping", default="regular")
    parser.add_argument("--pwm-bits", type=int, default=11)
    parser.add_argument("--limit-refresh-rate-hz", type=int, default=120)
    parser.add_argument(
        "--no-hardware-pulse",
        action="store_true",
        help="Avoid Pi onboard sound conflict at the cost of more possible flicker.",
    )
    parser.add_argument(
        "--matrix-drop-privileges",
        action="store_true",
        help="Let the LED matrix library drop root down to the 'daemon' user right after "
        "hardware init (its normal default). Off by default here, since 'daemon' usually "
        "can't reach a user's home directory -- which breaks writing the Spotify token "
        "cache (and anything else this script writes to disk) for the rest of the run.",
    )
    parser.add_argument("--poll-seconds", type=positive_float, default=2.0)
    parser.add_argument("--fps", type=positive_float, default=20.0)
    parser.add_argument("--rpm", type=positive_float, default=20.0)
    parser.add_argument(
        "--token-cache",
        type=Path,
        default=Path(__file__).resolve().parent / ".cache" / "spotify_token.json",
        help="Where the Spotify OAuth token is cached. Defaults to a fixed path next to this "
        "script, regardless of the process's working directory.",
    )
    parser.add_argument("--mock-output", type=Path, help="Write the current frame PNG instead of using RGB matrix hardware.")
    parser.add_argument("--preview-frames", type=Path, help="Render sample spinning-album-art disk frames and exit.")
    parser.add_argument("--auth-only", action="store_true", help="Authorize Spotify, cache the token, and exit without using the matrix.")
    parser.add_argument("--test-pattern", action="store_true", help="Show a bright moving color test pattern without using Spotify.")
    parser.add_argument("--once", action="store_true", help="Render one frame and exit.")
    parser.add_argument("--no-browser", action="store_true", help="Print the Spotify auth URL without trying to open a browser.")
    parser.add_argument("--control-host", default="0.0.0.0", help="Host for the phone-widget mode-switch server.")
    parser.add_argument("--control-port", type=int, default=8890, help="Port for the phone-widget mode-switch server.")
    parser.add_argument("--lyrics-font", type=Path, help="Path to a .ttf/.otf font for lyrics text (recommended; the bundled default font is small and blocky).")
    parser.add_argument("--lyrics-font-size", type=int, default=9, help="Pixel size for lyrics text. Stays constant -- only the active line's color changes.")
    parser.add_argument("--lyrics-offset", type=float, default=0.0, help="Seconds to shift lyrics timing forward (+) or back (-) to compensate for network/API lag.")
    parser.add_argument("--lyrics-transition-seconds", type=float, default=0.6, help="How long the glide into the next lyric line takes, right before it starts.")
    return parser


if __name__ == "__main__":
    run(build_parser().parse_args())
