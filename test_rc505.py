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


def make_fake_drive(drive: Path) -> None:
    """Memory 1: tracks 1 and 3. Memory 2: empty. Memory 3: track 5, renamed "My Song?"."""
    roland = drive / "ROLAND"
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


class RC505Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.drive = Path(self.tmp.name)
        make_fake_drive(self.drive)

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

    def test_track_gains_scale_each_layer(self):
        tracks = {1: np.ones((4, 2), dtype=np.float32), 2: np.full((4, 2), 10, dtype=np.float32)}
        block, _ = player.mix_block(tracks, {}, set(), 2, {1: 1.5, 2: 0.5})
        np.testing.assert_allclose(block[:, 0], [6.5, 6.5])
        block, _ = player.mix_block(tracks, {}, set(), 2, {2: 0.0})
        np.testing.assert_allclose(block[:, 0], [1, 1])

    def test_balance_evens_out_layer_loudness(self):
        quiet = np.full((100, 2), 0.1, dtype=np.float32)
        loud = np.full((100, 2), 0.4, dtype=np.float32)
        gains = player.balance_gains({1: quiet, 2: loud})
        self.assertAlmostEqual(gains[1], 2.0, places=4)  # target is the geometric mean, 0.2
        self.assertAlmostEqual(gains[2], 0.5, places=4)

    def test_balance_ignores_silence_and_caps_gain(self):
        sparse = np.zeros((100, 2), dtype=np.float32)
        sparse[::10] = 0.2  # a few hits: measured on the hits, not diluted by the gaps
        steady = np.full((100, 2), 0.2, dtype=np.float32)
        gains = player.balance_gains({1: sparse, 2: steady})
        self.assertAlmostEqual(gains[1], 1.0, places=4)
        whisper = np.full((100, 2), 0.002, dtype=np.float32)  # -54 dBFS: audible, 40 dB below the other layer
        gains = player.balance_gains({1: whisper, 2: steady})
        self.assertAlmostEqual(gains[1], 10 ** (12 / 20), places=4)  # capped at +12 dB
        silent = np.zeros((100, 2), dtype=np.float32)
        self.assertEqual(player.balance_gains({1: silent, 2: steady}), {1: 1.0, 2: 1.0})

    def test_fader_gains_stack_on_balance(self):
        self.assertEqual(player.combine_gains({1: 2.0, 2: 0.5}, {2: 0.5, 3: 1.5}), {1: 2.0, 2: 0.25, 3: 1.5})

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

    @staticmethod
    def channel_mode(data: bytes) -> int:
        """Channel mode of the first MPEG frame header: 3 = mono, anything else = stereo."""
        for i in range(len(data) - 3):
            if data[i] == 0xFF and data[i + 1] & 0xE0 == 0xE0:
                return data[i + 3] >> 6
        raise AssertionError("no MP3 frame header found")

    def test_mono_writes_a_mono_mp3(self):
        player.export_mix_mp3(self.tracks, set(), 44100, self.path, mono=True, bitrate=128)
        self.assertEqual(self.channel_mode(self.path.read_bytes()), 3)
        stereo = self.path.with_name("stereo.mp3")
        player.export_mix_mp3(self.tracks, set(), 44100, stereo, bitrate=128)
        self.assertNotEqual(self.channel_mode(stereo.read_bytes()), 3)

    def test_normalize_raises_a_quiet_mix_to_target_peak(self):
        encoded = []

        class FakeEncoder:  # capture the PCM handed to LAME instead of decoding the MP3
            def __getattr__(self, name):
                return lambda *a: None

            def encode(self, pcm):
                encoded.append(np.frombuffer(pcm, dtype="<i2"))
                return b""

            def flush(self):
                return b""

        import lameenc
        real = lameenc.Encoder
        lameenc.Encoder = FakeEncoder
        try:
            quiet = {n: a * 0.05 for n, a in self.tracks.items()}
            player.export_mix_mp3(quiet, set(), 44100, self.path)
            plain_peak = max(np.abs(e).max() for e in encoded) / 32767
            encoded.clear()
            player.export_mix_mp3(quiet, set(), 44100, self.path, balance=True)
            normalized_peak = max(np.abs(e).max() for e in encoded) / 32767
        finally:
            lameenc.Encoder = real
        self.assertLess(plain_peak, 0.2)  # left alone without the option
        self.assertAlmostEqual(normalized_peak, player.NORMALIZE_PEAK, places=3)

    def test_all_muted_is_an_error(self):
        with self.assertRaises(rc505.RC505Error):
            player.export_mix_mp3(self.tracks, {1, 2}, 44100, self.path)
        self.assertFalse(self.path.exists())

    def test_cancel_removes_partial_file(self):
        self.assertFalse(player.export_mix_mp3(self.tracks, set(), 44100, self.path, should_cancel=lambda: True))
        self.assertFalse(self.path.exists())


@unittest.skipIf(player is None, "numpy/sounddevice not installed")
class MixdownBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import lameenc  # noqa: F401
        except ImportError:
            self.skipTest("lameenc not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.drive = Path(self.tmp.name)
        make_fake_drive(self.drive)
        self.memories = rc505.scan(self.drive)
        self.out = self.drive / "bundle"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_filename_notes_muted_tracks(self):
        m1 = self.memories[0]
        self.assertEqual(player.mixdown_filename(m1, set()), "Memory 001 mixdown.mp3")
        self.assertEqual(player.mixdown_filename(m1, {3}), "Memory 001 mixdown (tracks 1).mp3")
        self.assertEqual(player.mixdown_filename(m1, {4}), "Memory 001 mixdown.mp3")  # 4 isn't recorded

    def test_one_mp3_per_memory_in_bundle_root(self):
        seen = []
        written = player.export_mixdowns(self.memories, self.out, {1: {3}}, progress=lambda f, n: seen.append(f))
        self.assertEqual(
            sorted(p.name for p in self.out.iterdir()),
            ["Memory 001 mixdown (tracks 1).mp3", "Memory 003 - My Song_ mixdown.mp3"],
        )
        self.assertEqual(len(written), 2)  # empty memory 2 is skipped
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], 1.0)

    def test_track_at_zero_percent_is_left_out_like_a_mute(self):
        player.export_mixdowns(self.memories, self.out, {}, gains_by_memory={1: {3: 0.0}, 3: {5: 0.5}})
        self.assertEqual(
            sorted(p.name for p in self.out.iterdir()),
            ["Memory 001 mixdown (tracks 1).mp3", "Memory 003 - My Song_ mixdown.mp3"],
        )

    def test_cancel_stops_before_next_memory(self):
        written = player.export_mixdowns(self.memories, self.out, {}, should_cancel=lambda: True)
        self.assertEqual(written, [])
        self.assertEqual(list(self.out.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
