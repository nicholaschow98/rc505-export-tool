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
HEADROOM = 0.98  # peak level after scaling down a mix that would clip
NORMALIZE_PEAK = 10 ** (-1 / 20)  # -1 dBFS: normalisation target, leaves room for MP3 overshoot
BALANCE_LIMIT_DB = 12.0  # auto-balance never moves a layer further than this
SILENCE = 10 ** (-60 / 20)  # samples quieter than -60 dBFS don't count towards a layer's loudness


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
    tracks: dict[int, np.ndarray],
    positions: dict[int, int],
    muted: set[int],
    frames: int,
    gains: dict[int, float] | None = None,
) -> tuple[np.ndarray, dict[int, int]]:
    """Mix the next `frames` frames, each track wrapping at its own length and scaled by its gain (default 1).

    Muted tracks still advance so they stay in time when unmuted.
    """
    block = np.zeros((frames, CHANNELS), dtype=np.float32)
    new_positions = {}
    for number, audio in tracks.items():
        length = len(audio)
        start = positions.get(number, 0)
        gain = gains.get(number, 1.0) if gains else 1.0
        if number not in muted and gain > 0:
            chunk = audio.take(np.arange(start, start + frames), axis=0, mode="wrap")
            block += chunk if gain == 1.0 else chunk * np.float32(gain)
        new_positions[number] = (start + frames) % length
    return block, new_positions


def track_loudness(audio: np.ndarray) -> float | None:
    """RMS of the non-silent part of a layer, so sparse parts (a few hits in a loop) aren't under-measured."""
    mono = audio.mean(axis=1)
    active = mono[np.abs(mono) > SILENCE]
    if active.size == 0:
        return None
    return float(np.sqrt(np.mean(np.square(active, dtype=np.float64))))


def balance_gains(tracks: dict[int, np.ndarray]) -> dict[int, float]:
    """Per-layer gains that bring every layer to the same loudness.

    The target is the geometric mean of the layers' loudness, so the overall level stays about the same.
    Gains are capped at +/-BALANCE_LIMIT_DB; silent layers are left alone.
    """
    levels = {n: track_loudness(audio) for n, audio in tracks.items()}
    measured = [level for level in levels.values() if level]
    if len(measured) < 2:
        return {n: 1.0 for n in tracks}
    target = math.exp(sum(math.log(level) for level in measured) / len(measured))
    limit = 10 ** (BALANCE_LIMIT_DB / 20)
    return {n: min(max(target / level, 1 / limit), limit) if level else 1.0 for n, level in levels.items()}


def combine_gains(*gain_maps: dict[int, float] | None) -> dict[int, float]:
    combined: dict[int, float] = {}
    for gains in gain_maps:
        for number, gain in (gains or {}).items():
            combined[number] = combined.get(number, 1.0) * gain
    return combined


def to_mono(block: np.ndarray) -> np.ndarray:
    return block.mean(axis=1, keepdims=True)


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


def _render_chunks(tracks: dict[int, np.ndarray], gains: dict[int, float] | None, total_frames: int):
    positions: dict[int, int] = {}
    done = 0
    while done < total_frames:
        frames = min(RENDER_CHUNK, total_frames - done)
        block, positions = mix_block(tracks, positions, set(), frames, gains)
        done += frames
        yield block, done


def export_mix_mp3(
    tracks: dict[int, np.ndarray],
    muted: set[int],
    sample_rate: int,
    path: str | Path,
    repeats: int = 1,
    bitrate: int | None = None,  # default: 320 kbps stereo, 160 kbps mono (same quality per channel)
    progress: Callable[[float], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    gains: dict[int, float] | None = None,
    mono: bool = False,
    balance: bool = False,
) -> bool:
    """Render the unmuted tracks, looped `repeats` full cycles, to an MP3. Returns False if cancelled.

    Each track is scaled by its entry in `gains` (default 1). With `balance`, layers are first evened out
    (see balance_gains, measured over all tracks so muting one doesn't shift the others) and the finished
    mix is normalised to NORMALIZE_PEAK; otherwise it is only scaled down if it would clip.
    `mono` folds the mix to a single channel and writes a mono MP3.
    """
    try:
        import lameenc
    except ImportError as exc:
        raise rc505.RC505Error("MP3 export needs the lameenc package: pip install -r requirements.txt") from exc

    gains = gains or {}
    audible = {n: a for n, a in tracks.items() if n not in muted and gains.get(n, 1.0) > 0}
    if not audible:
        raise rc505.RC505Error("All tracks are muted; there is nothing to export.")
    mix_gains = combine_gains(balance_gains(tracks), gains) if balance else gains
    cycle = cycle_frames(audible)
    total = cycle * repeats

    def blocks(frames: int):
        for block, done in _render_chunks(audible, mix_gains, frames):
            yield (to_mono(block) if mono else block), done

    # Every cycle is identical, so one pass over a single cycle finds the peak.
    peak = max(float(np.abs(block).max()) for block, _ in blocks(cycle))
    if peak <= 0:
        gain = 1.0
    elif balance:
        gain = NORMALIZE_PEAK / peak
    else:
        gain = HEADROOM / peak if peak > HEADROOM else 1.0

    encoder = lameenc.Encoder()
    encoder.set_bit_rate(bitrate or (160 if mono else 320))
    encoder.set_in_sample_rate(sample_rate)
    encoder.set_channels(1 if mono else CHANNELS)
    encoder.set_quality(2)  # 2 = high quality, 7 = fast

    path = Path(path)
    try:
        with path.open("wb") as f:
            for block, done in blocks(total):
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


def mixdown_filename(memory: rc505.Memory, muted: set[int]) -> str:
    recorded = {t.number for t in memory.tracks}
    name = f"{memory.folder_name} mixdown"
    if muted & recorded:
        name += " (tracks " + "".join(str(n) for n in sorted(recorded - muted)) + ")"
    return name + ".mp3"


def export_mixdowns(
    memories: list[rc505.Memory],
    bundle_dir: str | Path,
    muted_by_memory: dict[int, set[int]],
    repeats: int = 1,
    progress: Callable[[float, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    gains_by_memory: dict[int, dict[int, float]] | None = None,
    mono: bool = False,
    balance: bool = False,
) -> list[Path]:
    """Write one MP3 mixdown per memory into bundle_dir. Stops early (returning what's done) if cancelled."""
    gains_by_memory = gains_by_memory or {}
    memories = [m for m in memories if m.tracks]
    bundle_dir = Path(bundle_dir)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    # Weight progress by each memory's length so long loops don't stall the bar.
    weights = [max(m.longest_duration or 1.0, 1.0) for m in memories]
    total = sum(weights)
    done = 0.0
    written: list[Path] = []

    for memory, weight in zip(memories, weights):
        if should_cancel and should_cancel():
            break
        gains = gains_by_memory.get(memory.number, {})
        # A track faded to 0% is left out just like a muted one (including from the file name).
        muted = muted_by_memory.get(memory.number, set()) | {n for n, g in gains.items() if g <= 0}
        path = bundle_dir / mixdown_filename(memory, muted)
        if progress:
            progress(done / total, path.name)
        tracks, sample_rate = load_memory_audio(memory)
        finished = export_mix_mp3(
            tracks,
            muted,
            sample_rate,
            path,
            repeats=repeats,
            progress=(lambda f, base=done, w=weight: progress((base + f * w) / total, path.name)) if progress else None,
            should_cancel=should_cancel,
            gains=gains,
            mono=mono,
            balance=balance,
        )
        if not finished:
            break
        written.append(path)
        done += weight

    if progress and not (should_cancel and should_cancel()):
        progress(1.0, "")
    return written


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
        self._user_gains: dict[int, float] = {}  # the faders
        self._balance: dict[int, float] = {}  # auto-balance, empty when off
        self._gains: dict[int, float] = {}  # what the mixer actually uses: user x balance
        self._volume = 0.8
        self._stream: sd.OutputStream | None = None
        self.sample_rate = 44100
        self.device: int | None = None  # None = system default
        self.mono = False

    def set_tracks(self, tracks: dict[int, np.ndarray], sample_rate: int) -> None:
        self.stop()
        with self._lock:
            self._tracks = tracks
            self._positions = {}
            self._muted = set()
            self._user_gains, self._balance, self._gains = {}, {}, {}
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

    def set_gain(self, track_number: int, gain: float) -> None:
        with self._lock:
            self._user_gains[track_number] = max(0.0, gain)
            self._gains = combine_gains(self._balance, self._user_gains)

    def set_balance(self, gains: dict[int, float] | None) -> None:
        """Auto-balance gains from balance_gains(), or None to turn balancing off."""
        with self._lock:
            self._balance = dict(gains or {})
            self._gains = combine_gains(self._balance, self._user_gains)

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
            block, self._positions = mix_block(self._tracks, self._positions, self._muted, frames, self._gains)
        if self.mono:
            block[:] = to_mono(block)
        block *= self._volume
        np.clip(block, -1.0, 1.0, out=outdata)
