import struct
import tempfile
import unittest
from pathlib import Path

import rc505

try:
    import numpy as np
    import player
except Exception:  # preview dependencies are optional
    player = None

FRAME_BYTES = 8  # stereo 32-bit float


def make_wav(path: Path, seconds: float = 0, sample_rate: int = 44100, samples=None) -> None:
    """Stereo 32-bit float WAV, the format the RC-505 MK2 writes."""
    channels = 2
    byte_rate = sample_rate * channels * 4
    if samples is None:
        data = b"\x00" * int(seconds * byte_rate)
    else:
        data = struct.pack(f"<{len(samples)}f", *samples)
    fmt = struct.pack("<HHIIHH", 3, channels, sample_rate, byte_rate, channels * 4, 32)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


def make_memory_file(path: Path, name: str, count: int, loop_frames: dict | None = None, tempo_x10: int = 1200) -> None:
    letters = "ABCDEFGHIJKL"
    chars = "".join(f"\t<{letters[i]}>{ord(c)}</{letters[i]}>\n" for i, c in enumerate(name.ljust(12)))
    tracks = ""
    for n in range(1, 7):
        frames = (loop_frames or {}).get(n, 0)
        tracks += f"<TRACK{n}>\n\t<U>{tempo_x10}</U>\n\t<W>{int(bool(frames))}</W>\n\t<X>{frames}</X>\n</TRACK{n}>\n"
    path.write_text(
        f'<?xml version="1.0" encoding="utf-8"?>\n<database name="RC-505MK2" revision="0">\n'
        f'<mem id="0">\n<NAME>\n{chars}</NAME>\n{tracks}'
        # Later sections reuse TRACKn tag names; they must not override the memory's own values.
        f"<ICTL1_TRACK1_FX><A>0</A></ICTL1_TRACK1_FX>\n<TRACK1><X>999</X></TRACK1>\n"
        f"</mem>\n</database>\n<count>{count:04d}</count>\n"
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
        # Memory 1 records its real loop lengths (shorter than the padded files) at 130 BPM.
        make_memory_file(data / "MEMORY001A.RC0", "Memory01", 2, {1: 22050, 3: 44100}, tempo_x10=1300)
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
        self.assertEqual(m1.folder_name, "Memory 001")
        self.assertEqual(m3.name, "My Song?")
        self.assertEqual(m3.folder_name, "Memory 003 - My Song_")

    def test_loop_length_and_tempo_come_from_memory_file(self):
        m1 = rc505.scan(self.drive)[0]
        self.assertEqual(m1.tempo, 130.0)
        self.assertEqual([t.loop_frames for t in m1.tracks], [22050, 44100])
        self.assertAlmostEqual(m1.longest_duration, 1.0, places=3)

    def test_loop_length_falls_back_to_file_length(self):
        m3 = rc505.scan(self.drive)[2]
        self.assertEqual(m3.tracks[0].loop_frames, 22050)
        self.assertIsNone(m3.tempo)

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


@unittest.skipIf(player is None, "numpy/sounddevice not installed")
class PlayerTests(unittest.TestCase):
    def test_load_trims_padding_to_loop_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "001_1.WAV"
            make_wav(path, samples=[0.1, -0.1, 0.2, -0.2, 0.3, -0.3, 9.0, 9.0])  # last frame is padding
            track = rc505.Track(1, path, path.stat().st_size, None, loop_frames=3)
            audio, rate = player.load_track_audio(track)
        self.assertEqual(rate, 44100)
        np.testing.assert_allclose(audio, [[0.1, -0.1], [0.2, -0.2], [0.3, -0.3]], rtol=1e-6)

    def test_tracks_loop_independently(self):
        short = np.array([[1, 1], [2, 2]], dtype=np.float32)
        long = np.array([[10, 10], [20, 20], [30, 30]], dtype=np.float32)
        tracks = {1: short, 2: long}
        block, positions = player.mix_block(tracks, {}, set(), 4)
        np.testing.assert_array_equal(block[:, 0], [11, 22, 31, 12])
        self.assertEqual(positions, {1: 0, 2: 1})
        block, positions = player.mix_block(tracks, positions, set(), 2)
        np.testing.assert_array_equal(block[:, 0], [21, 32])

    def test_muted_tracks_are_silent_but_stay_in_time(self):
        tracks = {1: np.ones((4, 2), dtype=np.float32), 2: np.full((4, 2), 5, dtype=np.float32)}
        block, positions = player.mix_block(tracks, {}, {2}, 3)
        np.testing.assert_array_equal(block[:, 0], [1, 1, 1])
        self.assertEqual(positions, {1: 3, 2: 3})

    def test_cycle_is_where_all_loops_realign(self):
        def track(n):
            return np.zeros((n, 2), dtype=np.float32)

        self.assertEqual(player.cycle_frames({1: track(100), 2: track(400)}), 400)
        self.assertEqual(player.cycle_frames({1: track(200), 2: track(300)}), 600)
        # Unrelated lengths would give a huge cycle; use the longest track instead.
        self.assertEqual(player.cycle_frames({1: track(997), 2: track(1009)}), 1009)


@unittest.skipIf(player is None, "numpy/sounddevice not installed")
class Mp3ExportTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import lameenc  # noqa: F401
        except ImportError:
            self.skipTest("lameenc not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "mix.mp3"
        t = np.arange(44100, dtype=np.float32) / 44100
        tone = np.sin(2 * np.pi * 440 * t)[:, None].repeat(2, axis=1).astype(np.float32)
        self.tracks = {1: tone * 0.9, 2: tone[:22050] * 0.9}  # sums to 1.8: would clip

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_writes_mp3_of_expected_length(self):
        seen = []
        self.assertTrue(player.export_mix_mp3(self.tracks, set(), 44100, self.path, repeats=2, bitrate=128,
                                              progress=seen.append))
        data = self.path.read_bytes()
        self.assertTrue(data[:3] == b"ID3" or (data[0] == 0xFF and data[1] & 0xE0 == 0xE0))
        # 2 s at 128 kbps is about 32 KB.
        self.assertAlmostEqual(len(data) / 32000, 1, delta=0.15)
        self.assertEqual(seen[-1], 1.0)

    def test_all_muted_is_an_error(self):
        with self.assertRaises(rc505.RC505Error):
            player.export_mix_mp3(self.tracks, {1, 2}, 44100, self.path)
        self.assertFalse(self.path.exists())

    def test_cancel_removes_partial_file(self):
        self.assertFalse(player.export_mix_mp3(self.tracks, set(), 44100, self.path, should_cancel=lambda: True))
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
