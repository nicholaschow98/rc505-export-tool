# RC-505 USB Storage Export Tool

A free Windows app that pulls loop-memory stems off a BOSS RC-505 MK2 (in USB Storage Mode) and bundles them into
DAW-ready folders. It can also preview each memory's loops and export MP3 mixdowns.

## Download

Get `RC505-Export-Tool-<version>.exe` from the
[latest release](https://github.com/nicholaschow98/rc505-export-tool/releases/latest). It's a single file with
nothing to install and runs on 64-bit Windows 10/11. The exe isn't code-signed, so Windows SmartScreen may say
"Windows protected your PC" the first time: click **More info → Run anyway**. The release notes list the exe's
SHA-256, which you can check with `Get-FileHash <file>` in PowerShell.

## Usage

Run the exe, or from source (Python 3.10+):

```
python app.py
```

1. Click **Browse...** and pick the `ROLAND` folder (e.g. `D:\ROLAND`) or the drive itself.
2. Tick the ☐ box of each loop memory to export. Memories with no recordings are hidden by default.
3. Choose where to save and name the bundle folder, then click **Export**.

From source, exporting stems uses only the standard library.

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

## Building the .exe

Double-click `build.bat`, or run `.\build.ps1` in PowerShell. It needs Python 3.10+ installed and:

1. creates `.venv` with only the pinned packages in `requirements-build.txt`;
2. runs the unit tests;
3. builds `dist\RC505 Export Tool.exe` with PyInstaller (`rc505_export_tool.spec`): a single windowed exe
   with no Python install needed to run it;
4. regenerates `THIRD_PARTY_NOTICES.txt` from the licenses of what is bundled (`make_notices.py`) and copies it
   and `LICENSE` into `dist\`;
5. runs the built exe with `--smoke-test` to check that its bundled Tk, numpy, PortAudio and LAME work;
6. prints the exe's size and SHA-256.

Run `.\build.ps1 -Clean` after changing `requirements-build.txt` to rebuild the venv from scratch.
Bump `__version__` in `app.py` for a new release; it sets the window title and the exe's version details.

## Tests

```
python -m unittest -v test_rc505
```

`test_data/ROLAND` is a full copy of the pedal's storage for manual testing (point the app at `test_data`).

## License

This tool's code is under the [MIT License](LICENSE). The Windows exe also bundles open-source components under
their own licenses, including the LAME MP3 encoder (LGPL) via lameenc; see
[THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt), which ships with every release.
