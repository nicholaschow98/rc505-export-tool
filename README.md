# RC-505 USB Storage Export Tool

A Tkinter app that pulls loop-memory stems off a BOSS RC-505 MK2 (in USB Storage Mode) and bundles them into DAW-ready folders.

## Usage

```
python app.py
```

1. On launch you're prompted for the RC-505 folder. Pick the `ROLAND` folder (e.g. `D:\ROLAND`) or the drive itself.
2. Tick the loop memories to export. Memories with no recordings are hidden by default.
3. Choose where to save and name the bundle folder, then click **Export**.

Requires Python 3.10+ (standard library only).

## Output

```
<Save in>/<Bundle folder name>/
    Memory 002/
        002_1.WAV  002_2.WAV  002_3.WAV  002_4.WAV
    Memory 005 - My Song/        <- name appended if the memory was renamed on the pedal
        005_1.WAV ...
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

The WAVs are 32-bit float, stereo, 44.1 kHz.

## Tests

```
python -m unittest -v test_rc505
```

`test_data/ROLAND` is a full copy of the pedal's storage for manual testing (point the app at `test_data`).
