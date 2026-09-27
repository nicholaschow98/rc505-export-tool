"""Generate THIRD_PARTY_NOTICES.txt for the components bundled into the Windows exe.

Run with the build venv's Python (build.ps1 does this) so versions and license texts
come from exactly what PyInstaller bundles.
"""

from __future__ import annotations

import sys
import tkinter
from importlib import metadata
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_URL = "https://github.com/nicholaschow98/rc505-export-tool"
RULE = "=" * 78

PORTAUDIO_LICENSE = """\
PortAudio Portable Real-Time Audio Library
Copyright (c) 1999-2011 Ross Bencina, Phil Burk

Permission is hereby granted, free of charge, to any person obtaining
a copy of this software and associated documentation files
(the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge,
publish, distribute, sublicense, and/or sell copies of the Software,
and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR
ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF
CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""


def package_license_texts(dist_name: str) -> list[tuple[str, str]]:
    """(relative path, text) of every license file shipped in a package's dist-info."""
    dist = metadata.distribution(dist_name)
    texts = []
    for file in dist.files or []:
        parts = [p.lower() for p in file.parts]
        if not any(p.endswith(".dist-info") for p in parts[:1]):
            continue
        if "licenses" in parts or any(k in parts[-1] for k in ("license", "copying")):
            texts.append(("/".join(file.parts[1:]), Path(file.locate()).read_text(encoding="utf-8", errors="replace")))
    return sorted(texts, key=lambda item: (item[0].count("/"), item[0]))


def section(title: str, details: list[str], texts: list[tuple[str, str]]) -> str:
    out = [RULE, title, RULE, *details, ""]
    for name, text in texts:
        if len(texts) > 1:
            out += [f"--- {name} ---", ""]
        out += [text.strip(), ""]
    return "\n".join(out) + "\n"


def main(output: Path) -> None:
    python_version = sys.version.split()[0]
    tk_version = tkinter.Tcl().call("info", "patchlevel")
    versions = {name: metadata.version(name) for name in ("numpy", "sounddevice", "cffi", "lameenc", "pyinstaller")}
    python_license = (Path(sys.base_prefix) / "LICENSE.txt").read_text(encoding="utf-8", errors="replace")

    parts = [
        f"""RC-505 USB Storage Export Tool - third-party notices
{RULE}

The Windows executable of this tool bundles the open-source components below.
Each remains under its own license, reproduced in full in this file. The tool's
own code is under the MIT License (see LICENSE in {REPO_URL}).

  Python {python_version:<12} PSF License, plus the bundled libraries listed in its license
                      (bzip2, libffi, Zstandard, OpenSSL, zlib and others)
  Tcl/Tk {tk_version:<12} Tcl/Tk License (included in the Python license text)
  numpy {versions['numpy']:<13} BSD-3-Clause and others (incl. OpenBLAS); see its section
  sounddevice {versions['sounddevice']:<7} MIT
  PortAudio           MIT (libportaudio64bit.dll, shipped with sounddevice)
  cffi {versions['cffi']:<14} MIT-0
  lameenc {versions['lameenc']:<11} LGPL-3.0-or-later; statically includes the LAME MP3 encoder
                      (LGPL-2.0-or-later, used here under LGPL-3.0)
  PyInstaller {versions['pyinstaller']:<7} bootloader: GPL-2.0-or-later with the PyInstaller exception

""",
        section(
            f"lameenc {versions['lameenc']} and the LAME MP3 encoder - GNU LGPL v3",
            [
                "lameenc (Python bindings for LAME, including LAME itself): https://github.com/chrisstaite/lameenc",
                f"  exact version used: {versions['lameenc']} (tag v{versions['lameenc']})",
                "LAME: https://lame.sourceforge.io/ - Copyright (c) the LAME project, LGPL-2.0-or-later.",
                "",
                "LGPL notice: you may modify or replace lameenc/LAME in this program. The complete source of",
                f"this tool and its build script are at {REPO_URL}. To rebuild with a modified library, install",
                "your version of lameenc into the build environment (.venv) and run build.ps1.",
                "The LGPL v3 below incorporates the GNU GPL v3, which follows it.",
            ],
            package_license_texts("lameenc")
            + [("GPL-3.0.txt", (HERE / "licenses" / "GPL-3.0.txt").read_text(encoding="utf-8"))],
        ),
        section(
            f"Python {python_version} (including Tcl/Tk {tk_version} and the libraries of the Windows build)",
            ["https://www.python.org/"],
            [("LICENSE.txt", python_license)],
        ),
        section(f"numpy {versions['numpy']}", ["https://numpy.org/"], package_license_texts("numpy")),
        section(
            f"sounddevice {versions['sounddevice']}",
            ["https://github.com/spatialaudio/python-sounddevice"],
            package_license_texts("sounddevice"),
        ),
        section(
            "PortAudio",
            ["http://www.portaudio.com/ - binary from https://github.com/spatialaudio/portaudio-binaries"],
            [("LICENSE", PORTAUDIO_LICENSE)],
        ),
        section(f"cffi {versions['cffi']}", ["https://github.com/python-cffi/cffi"], package_license_texts("cffi")),
        section(
            f"PyInstaller {versions['pyinstaller']} bootloader",
            [
                "https://pyinstaller.org/ - The bootloader is licensed GPL-2.0-or-later with an exception",
                "that allows distributing programs built with it under any license. Full text:",
            ],
            package_license_texts("pyinstaller"),
        ),
    ]
    output.write_text("".join(parts), encoding="utf-8", newline="\r\n")
    print(f"Wrote {output} ({output.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "THIRD_PARTY_NOTICES.txt")
