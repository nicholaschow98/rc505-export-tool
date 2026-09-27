import struct
import tempfile
import unittest
from pathlib import Path

import rc505


def make_wav(path: Path, seconds: float, sample_rate: int = 44100, channels: int = 2) -> None:
    """32-bit float WAV, the format the RC-505 MK2 writes."""
    byte_rate = sample_rate * channels * 4
    data = b"\x00" * int(seconds * byte_rate)
    fmt = struct.pack("<HHIIHH", 3, channels, sample_rate, byte_rate, channels * 4, 32)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


def make_memory_file(path: Path, name: str, count: int) -> None:
    letters = "ABCDEFGHIJKL"
    chars = "".join(f"\t<{letters[i]}>{ord(c)}</{letters[i]}>\n" for i, c in enumerate(name.ljust(12)))
    path.write_text(
        f'<?xml version="1.0" encoding="utf-8"?>\n<database name="RC-505MK2" revision="0">\n'
        f'<mem id="0">\n<NAME>\n{chars}</NAME>\n</mem>\n</database>\n<count>{count:04d}</count>\n'
    )


class RC505Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.drive = Path(self.tmp.name)
        roland = self.drive / "ROLAND"
        wave, data = roland / "WAVE", roland / "DATA"
        (wave / "TEMP").mkdir(parents=True)
        (wave / "TEMP" / "001_1").write_bytes(b"junk")
        data.mkdir()
        for mem in (1, 2, 3):
            for track in range(1, 6):
                (wave / f"{mem:03d}_{track}").mkdir()
            make_memory_file(data / f"MEMORY{mem:03d}A.RC0", f"Memory{mem:02d}", 1)
            make_memory_file(data / f"MEMORY{mem:03d}B.RC0", f"Memory{mem:02d}", 1)
        make_wav(wave / "001_1" / "001_1.WAV", 1.0)
        make_wav(wave / "001_3" / "001_3.WAV", 2.0)
        make_wav(wave / "003_5" / "003_5.WAV", 0.5)
        # Memory 3 was renamed; the newer save is in slot B.
        make_memory_file(data / "MEMORY003B.RC0", "My Song?", 2)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_accepts_drive_or_roland_folder(self):
        self.assertEqual(rc505.find_roland_root(self.drive), self.drive / "ROLAND")
        self.assertEqual(rc505.find_roland_root(self.drive / "ROLAND"), self.drive / "ROLAND")
        with self.assertRaises(rc505.RC505Error):
            rc505.find_roland_root(self.drive / "ROLAND" / "DATA")

    def test_scan(self):
        memories = rc505.scan(self.drive)
        self.assertEqual([m.number for m in memories], [1, 2, 3])
        m1, m2, m3 = memories
        self.assertEqual([t.number for t in m1.tracks], [1, 3])
        self.assertEqual(m2.tracks, [])
        self.assertAlmostEqual(m1.longest_duration, 2.0, places=3)
        self.assertEqual(m1.folder_name, "Memory 001")
        self.assertEqual(m3.name, "My Song?")
        self.assertEqual(m3.folder_name, "Memory 003 - My Song_")

    def test_export_bundles_each_memory_into_its_own_folder(self):
        memories = rc505.scan(self.drive)
        out = self.drive / "out" / "bundle"
        calls = []
        copied = rc505.export(memories, out, progress=lambda d, t, c: calls.append((d, t)))
        self.assertEqual(
            sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()),
            ["Memory 001/001_1.WAV", "Memory 001/001_3.WAV", "Memory 003 - My Song_/003_5.WAV"],
        )
        self.assertEqual(len(copied), 3)
        self.assertFalse((out / "Memory 002").exists())  # empty memories get no folder
        self.assertEqual(calls[-1][0], calls[-1][1])

    def test_export_cancel(self):
        memories = rc505.scan(self.drive)
        copied = rc505.export(memories, self.drive / "out", should_cancel=lambda: True)
        self.assertEqual(copied, [])


if __name__ == "__main__":
    unittest.main()
