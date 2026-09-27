# RC-505 USB Storage Export Tool

A Tkinter app that pulls loop-memory stems off a BOSS RC-505 MK2 (in USB Storage Mode) and bundles them into DAW-ready folders.

## Usage

```
python app.py
```

1. Click **Browse...** and pick the `ROLAND` folder (e.g. `D:\ROLAND`) or the drive itself.
2. Tick the ☐ box of each loop memory to export. Memories with no recordings are hidden by default.
3. Choose where to save and name the bundle folder, then click **Export**.

Requires Python 3.10+. Exporting uses only the standard library.

## Preview

Double-click a memory (or select it and press **Play** / Enter) to hear all its tracks mixed and looping, like on the pedal.
Each track loops at its own length, so a track recorded 4x longer than the others plays through while the others repeat.
Mute tracks with the Track 1-5 boxes, set the volume, and pick the output device (the RC-505 itself may be the Windows default).

Each track has a mute box and a level fader (0-150%; double-click a fader or use **Reset levels** to return to 100%).
Mutes and levels are remembered per memory while the app is open. The list shows a muted track as `(n)` and a track
with a changed level as `n*`. They apply to that memory's **Mixdown only**: stems are always the untouched files
from the pedal. **Listening volume** only changes how loud the preview plays.

## Export formats

Choose under **3. Export**:

- **Stems**: one WAV per track, in a folder per memory, for importing into a DAW.
- **Mixdown**: one 320 kbps MP3 per memory with its tracks mixed, for listening or sharing. It runs for 1-16 full
  loop cycles; a cycle ends when every track is back at its start together, which is the longest loop.
  Each track uses its preview level; the whole mix is only scaled down if it would clip.
  Two options:
  - **Mono**: folds the mix to one channel and writes a mono MP3 at 160 kbps, half the size.
  - **Auto-balance layers & normalize**: measures each layer's loudness (RMS, ignoring silence below -60 dBFS
    so sparse parts aren't over-boosted) and brings every layer to a common level, never moving one by more
    than 12 dB. Balance is measured over all tracks, so muting one doesn't shift the others. Your faders then
    apply on top, and the finished mix is normalized to a -1 dBFS peak.

  While Mixdown or Both is selected, the preview plays with these options, so you hear what you'll export.
  The Preview panel lists the dB change auto-balance makes to each track.
- **Both**

**Export** saves into the *Save in* folder, **Export to...** asks for a folder first, and **Open folder** opens the
last export in File Explorer.

Preview and Mixdown need three extra packages:

```
pip install -r requirements.txt
```

## Output

```
<Save in>/<Bundle folder name>/
    Memory 002/
        002_1.WAV  002_2.WAV  002_3.WAV  002_4.WAV
    Memory 002 mixdown.mp3                 <- Mixdown
    Memory 005 - My Song/                  <- name appended if the memory was renamed on the pedal
        005_1.WAV ...
    Memory 005 - My Song mixdown (tracks 124).mp3   <- tracks 3 and 5 were muted in Preview
```

Each memory gets one flat folder of stems, so you can drag the whole folder into a DAW. Files are copied, never moved: the pedal's storage is left untouched.

## RC-505 storage layout

```
ROLAND/
  DATA/MEMORY001A.RC0, MEMORY001B.RC0 ...  memory settings (XML), two alternating save slots;
                                           the trailing <count> marks which is newer
  WAVE/NNN_T/NNN_T.WAV                     memory NNN (001-099), track T (1-5); empty folder = no recording
  WAVE/TEMP/                               device scratch space, ignored
```

Useful fields in each memory file's `<TRACKn>` block:

| Field | Meaning |
|---|---|
| `X` | Loop length in sample frames. The WAV is padded past this, so the preview stops here. |
| `U` | Tempo × 10 (`1300` = 130 BPM) |
| `V` | Frames per measure |
| `W` | 1 if the track has a recording |

The WAVs are 32-bit float, stereo, 44.1 kHz.

## Tests

```
python -m unittest -v test_rc505
```

`test_data/ROLAND` is a full copy of the pedal's storage for manual testing (point the app at `test_data`).
