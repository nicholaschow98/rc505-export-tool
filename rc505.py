"""Reading the BOSS RC-505 (MK2) USB storage layout and exporting stems.

Layout on the device (as mounted in USB Storage Mode):

    ROLAND/
        DATA/
            MEMORY001A.RC0, MEMORY001B.RC0, ...   memory settings (A/B save slots)
            SYSTEM1.RC0, SYSTEM2.RC0, RHYTHM.RC0
        WAVE/
            001_1/001_1.WAV   memory 001, track 1
            001_2/            empty folder = nothing recorded on that track
            ...
            099_5/
            TEMP/             device scratch space, never exported
"""

from __future__ import annotations

import re
import shutil
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

TRACK_DIR_RE = re.compile(r"^(\d{3})_(\d)$")
NAME_BLOCK_RE = re.compile(r"<NAME>(.*?)</NAME>", re.S)
NAME_CHAR_RE = re.compile(r"<[A-Z]>(\d+)</[A-Z]>")
COUNT_RE = re.compile(r"<count>(\d+)</count>")
INVALID_PATH_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class Track:
    number: int
    path: Path
    size: int
    duration: float | None  # seconds, None if the header could not be read


@dataclass
class Memory:
    number: int
    name: str
    tracks: list[Track] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.number:03d}"

    @property
    def has_custom_name(self) -> bool:
        # Factory default names are "Memory01".."Memory99".
        return self.name != "" and self.name != f"Memory{self.number:02d}"

    @property
    def folder_name(self) -> str:
        base = f"Memory {self.label}"
        if self.has_custom_name:
            base += f" - {sanitize_filename(self.name)}"
        return base

    @property
    def total_size(self) -> int:
        return sum(t.size for t in self.tracks)

    @property
    def longest_duration(self) -> float | None:
        durations = [t.duration for t in self.tracks if t.duration is not None]
        return max(durations) if durations else None


class RC505Error(Exception):
    pass


def sanitize_filename(name: str) -> str:
    cleaned = INVALID_PATH_CHARS_RE.sub("_", name).strip().rstrip(".")
    return cleaned or "_"


def find_roland_root(path: str | Path) -> Path:
    """Accept either the ROLAND folder itself or the drive/folder containing it."""
    path = Path(path)
    for candidate in (path, path / "ROLAND"):
        if (candidate / "WAVE").is_dir():
            return candidate
    raise RC505Error(
        f"'{path}' does not look like an RC-505 storage folder.\n"
        "Select the ROLAND folder (or the drive that contains it); it must contain a WAVE folder."
    )


def read_memory_name(data_dir: Path, number: int) -> str:
    """Read the memory name, preferring whichever A/B save slot was written most recently."""
    best_name, best_count = "", -1
    for slot in ("A", "B"):
        file = data_dir / f"MEMORY{number:03d}{slot}.RC0"
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        block = NAME_BLOCK_RE.search(text)
        if not block:
            continue
        name = "".join(chr(int(c)) for c in NAME_CHAR_RE.findall(block.group(1)) if 32 <= int(c) < 127)
        count_match = COUNT_RE.search(text)
        count = int(count_match.group(1)) if count_match else 0
        if count > best_count:
            best_name, best_count = name.strip(), count
    return best_name


def wav_duration(path: Path) -> float | None:
    """Duration from the RIFF header. The stdlib wave module can't read the RC-505's 32-bit float WAVs."""
    try:
        with path.open("rb") as f:
            riff, _, wave_id = struct.unpack("<4sI4s", f.read(12))
            if riff != b"RIFF" or wave_id != b"WAVE":
                return None
            byte_rate = None
            while True:
                header = f.read(8)
                if len(header) < 8:
                    return None
                chunk_id, chunk_size = struct.unpack("<4sI", header)
                if chunk_id == b"fmt ":
                    fmt = f.read(chunk_size)
                    byte_rate = struct.unpack_from("<I", fmt, 8)[0]
                    if chunk_size % 2:
                        f.seek(1, 1)
                elif chunk_id == b"data":
                    return chunk_size / byte_rate if byte_rate else None
                else:
                    f.seek(chunk_size + (chunk_size % 2), 1)
    except (OSError, struct.error):
        return None


def scan(root: str | Path) -> list[Memory]:
    """Return every memory slot found under the ROLAND folder, including empty ones."""
    root = find_roland_root(root)
    wave_dir, data_dir = root / "WAVE", root / "DATA"

    memories: dict[int, Memory] = {}
    for track_dir in sorted(wave_dir.iterdir()):
        match = TRACK_DIR_RE.match(track_dir.name)
        if not match or not track_dir.is_dir():
            continue  # skips TEMP and anything unexpected
        mem_no, track_no = int(match.group(1)), int(match.group(2))
        memory = memories.get(mem_no)
        if memory is None:
            memory = memories[mem_no] = Memory(mem_no, read_memory_name(data_dir, mem_no))
        for wav in sorted(track_dir.glob("*.[wW][aA][vV]")):
            size = wav.stat().st_size
            if size > 0:
                memory.tracks.append(Track(track_no, wav, size, wav_duration(wav)))

    return [memories[n] for n in sorted(memories)]


ProgressCallback = Callable[[int, int, str], None]  # (bytes_done, bytes_total, current_file)


def export(
    memories: Iterable[Memory],
    bundle_dir: str | Path,
    progress: ProgressCallback | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[Path]:
    """Copy each memory's stems into bundle_dir/<memory folder>/. Returns the copied file paths."""
    memories = [m for m in memories if m.tracks]
    bundle_dir = Path(bundle_dir)
    total = sum(m.total_size for m in memories)
    done = 0
    copied: list[Path] = []

    for memory in memories:
        target_dir = bundle_dir / memory.folder_name
        target_dir.mkdir(parents=True, exist_ok=True)
        for track in memory.tracks:
            if should_cancel and should_cancel():
                return copied
            target = target_dir / track.path.name
            if progress:
                progress(done, total, str(target))
            shutil.copy2(track.path, target)
            copied.append(target)
            done += track.size

    if progress:
        progress(done, total, "")
    return copied


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}:{secs:02d}"


def format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
