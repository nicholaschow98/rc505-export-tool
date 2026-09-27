"""Looping playback of an RC-505 memory: every track loops at its own length, mixed together."""

from __future__ import annotations

import math
import threading
from pathlib import Path
from typing import Callable

import numpy as np
import sounddevice as sd

import rc505

CHANNELS = 2
RENDER_CHUNK = 65536  # frames per step when rendering to a file
HEADROOM = 0.98  # peak level after normalising a mix that would clip


def load_track_audio(track: rc505.Track) -> tuple[np.ndarray, int]:
    """Return (audio, sample_rate); audio is float32 of shape (loop_frames, 2)."""
    info = rc505.wav_info(track.path)
    if info is None:
        raise rc505.RC505Error(f"{track.path.name} is not a readable WAV file.")
    if info.format_tag in (rc505.WAVE_FORMAT_IEEE_FLOAT, rc505.WAVE_FORMAT_EXTENSIBLE) and info.bits_per_sample == 32:
        dtype, scale = "<f4", None
    elif info.format_tag in (rc505.WAVE_FORMAT_PCM, rc505.WAVE_FORMAT_EXTENSIBLE) and info.bits_per_sample == 16:
        dtype, scale = "<i2", 1 / 32768
    else:
        raise rc505.RC505Error(
            f"{track.path.name}: unsupported WAV format (tag {info.format_tag}, {info.bits_per_sample}-bit)."
        )

    frames = min(track.loop_frames or info.frames, info.frames)
    samples = np.fromfile(track.path, dtype=dtype, count=frames * info.channels, offset=info.data_offset)
    audio = samples.reshape(-1, info.channels).astype(np.float32)
    if scale:
        audio *= scale
    if info.channels == 1:
        audio = np.repeat(audio, CHANNELS, axis=1)
    elif info.channels > CHANNELS:
        audio = audio[:, :CHANNELS]
    return np.ascontiguousarray(audio), info.sample_rate


def load_memory_audio(memory: rc505.Memory) -> tuple[dict[int, np.ndarray], int]:
    tracks: dict[int, np.ndarray] = {}
    sample_rate = 44100
    for track in memory.tracks:
        audio, sample_rate = load_track_audio(track)
        if len(audio):
            tracks[track.number] = audio
    return tracks, sample_rate


def mix_block(
    tracks: dict[int, np.ndarray], positions: dict[int, int], muted: set[int], frames: int
) -> tuple[np.ndarray, dict[int, int]]:
    """Mix the next `frames` frames, each track wrapping at its own length.

    Muted tracks still advance so they stay in time when unmuted.
    """
    block = np.zeros((frames, CHANNELS), dtype=np.float32)
    new_positions = {}
    for number, audio in tracks.items():
        length = len(audio)
        start = positions.get(number, 0)
        if number not in muted:
            block += audio.take(np.arange(start, start + frames), axis=0, mode="wrap")
        new_positions[number] = (start + frames) % length
    return block, new_positions


def cycle_frames(tracks: dict[int, np.ndarray]) -> int:
    """Frames until every track is back at its start together.

    RC-505 loops are whole multiples of each other, so this is normally the longest track.
    Unrelated lengths could make the true cycle enormous; fall back to the longest track then.
    """
    lengths = [len(audio) for audio in tracks.values()]
    if not lengths:
        return 0
    cycle = math.lcm(*lengths)
    return cycle if cycle <= 16 * max(lengths) else max(lengths)


def _render_chunks(tracks: dict[int, np.ndarray], muted: set[int], total_frames: int):
    positions: dict[int, int] = {}
    done = 0
    while done < total_frames:
        frames = min(RENDER_CHUNK, total_frames - done)
        block, positions = mix_block(tracks, positions, muted, frames)
        done += frames
        yield block, done


def export_mix_mp3(
    tracks: dict[int, np.ndarray],
    muted: set[int],
    sample_rate: int,
    path: str | Path,
    repeats: int = 1,
    bitrate: int = 320,
    progress: Callable[[float], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> bool:
    """Render the unmuted tracks, looped `repeats` full cycles, to an MP3. Returns False if cancelled.

    The mix is at unity gain, scaled down only if the summed tracks would clip.
    """
    try:
        import lameenc
    except ImportError as exc:
        raise rc505.RC505Error("MP3 export needs the lameenc package: pip install -r requirements.txt") from exc

    audible = {n: a for n, a in tracks.items() if n not in muted}
    if not audible:
        raise rc505.RC505Error("All tracks are muted; there is nothing to export.")
    cycle = cycle_frames(audible)
    total = cycle * repeats

    # Every cycle is identical, so one pass over a single cycle finds the peak.
    peak = max(float(np.abs(block).max()) for block, _ in _render_chunks(audible, set(), cycle))
    gain = HEADROOM / peak if peak > HEADROOM else 1.0

    encoder = lameenc.Encoder()
    encoder.set_bit_rate(bitrate)
    encoder.set_in_sample_rate(sample_rate)
    encoder.set_channels(CHANNELS)
    encoder.set_quality(2)  # 2 = high quality, 7 = fast

    path = Path(path)
    try:
        with path.open("wb") as f:
            for block, done in _render_chunks(audible, set(), total):
                if should_cancel and should_cancel():
                    raise _Cancelled
                pcm = np.clip(block * (gain * 32767), -32768, 32767).astype("<i2")
                f.write(encoder.encode(pcm.tobytes()))
                if progress:
                    progress(done / total)
            f.write(encoder.flush())
    except _Cancelled:
        path.unlink(missing_ok=True)
        return False
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return True


class _Cancelled(Exception):
    pass


def output_devices() -> list[tuple[int, str]]:
    """Stereo output devices on the default host API, as (device index, name)."""
    hostapi = sd.default.hostapi
    return [
        (index, device["name"])
        for index, device in enumerate(sd.query_devices())
        if device["hostapi"] == hostapi and device["max_output_channels"] >= CHANNELS
    ]


def default_output_device() -> int | None:
    device = sd.default.device[1]
    return device if device is not None and device >= 0 else None


class LoopPlayer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tracks: dict[int, np.ndarray] = {}
        self._positions: dict[int, int] = {}
        self._muted: set[int] = set()
        self._volume = 0.8
        self._stream: sd.OutputStream | None = None
        self.sample_rate = 44100
        self.device: int | None = None  # None = system default

    def set_tracks(self, tracks: dict[int, np.ndarray], sample_rate: int) -> None:
        self.stop()
        with self._lock:
            self._tracks = tracks
            self._positions = {}
            self._muted = set()
        self.sample_rate = sample_rate

    def play(self) -> None:
        """Start from the top of every loop."""
        self.stop()
        with self._lock:
            self._positions = {}
        if not self._tracks:
            return
        self._stream = sd.OutputStream(
            samplerate=self.sample_rate,
            channels=CHANNELS,
            dtype="float32",
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()
            stream.close()

    @property
    def is_playing(self) -> bool:
        return self._stream is not None and self._stream.active

    def set_muted(self, track_number: int, muted: bool) -> None:
        with self._lock:
            if muted:
                self._muted.add(track_number)
            else:
                self._muted.discard(track_number)

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, volume))

    def position(self) -> tuple[float, float]:
        """(seconds into the longest loop, its length in seconds)."""
        with self._lock:
            if not self._tracks:
                return 0.0, 0.0
            number, audio = max(self._tracks.items(), key=lambda item: len(item[1]))
            pos = self._positions.get(number, 0)
        return pos / self.sample_rate, len(audio) / self.sample_rate

    def close(self) -> None:
        self.stop()

    def _callback(self, outdata: np.ndarray, frames: int, _time, _status) -> None:
        with self._lock:
            block, self._positions = mix_block(self._tracks, self._positions, self._muted, frames)
        block *= self._volume
        np.clip(block, -1.0, 1.0, out=outdata)
