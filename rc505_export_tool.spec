# PyInstaller spec: builds dist/RC505 Export Tool.exe, a single windowed executable.
# Run through build.ps1, which sets up the pinned venv and tests the result.
import re
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

NAME = "RC505 Export Tool"
VERSION = re.search(r'^__version__ = "([\d.]+)"', Path("app.py").read_text(encoding="utf-8"), re.M).group(1)
version_tuple = tuple(int(part) for part in (VERSION.split(".") + ["0"] * 4)[:4])

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=version_tuple, prodvers=version_tuple),
    kids=[
        StringFileInfo([
            StringTable("040904B0", [
                StringStruct("ProductName", "RC-505 USB Storage Export Tool"),
                StringStruct("FileDescription", "Export BOSS RC-505 MK2 loop memories as stems or mixdowns"),
                StringStruct("FileVersion", VERSION),
                StringStruct("ProductVersion", VERSION),
                StringStruct("OriginalFilename", f"{NAME}.exe"),
                StringStruct("LegalCopyright", "Freeware"),
            ])
        ]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

a = Analysis(
    ["app.py"],
    # The PortAudio DLL inside sounddevice is collected by pyinstaller-hooks-contrib's sounddevice hook.
    excludes=["unittest", "pydoc", "doctest", "test_rc505"],
    noarchive=False,
)


def keep_binary(entry):
    dest = entry[0].replace("\\", "/").lower()
    if dest.startswith("_sounddevice_data/"):
        # Only the 64-bit PortAudio build the app loads. The *-asio builds contain Steinberg's
        # ASIO SDK (separately licensed); the 32-bit/ARM builds are never used on x64 Windows.
        return dest.endswith("/libportaudio64bit.dll")
    return "_multiarray_tests" not in dest  # numpy's internal test module


a.binaries = [entry for entry in a.binaries if keep_binary(entry)]
a.datas = [entry for entry in a.datas if keep_binary(entry)]  # the hook also adds the macOS .dylib as data
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name=NAME,
    version=version_info,
    console=False,  # GUI app: no console window
    upx=False,  # UPX-packed exes trigger far more antivirus false positives
)
