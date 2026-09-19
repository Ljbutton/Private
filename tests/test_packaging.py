"""What the packaged build needs to exist before PyInstaller runs.

These are cheap checks for expensive failures: a macOS build takes minutes to
reach the step that needs the icon, and the only symptom of getting it wrong
is a .app that never gets written.
"""

import struct
from pathlib import Path

import pytest

ASSETS = Path("assets")
SPEC = Path("nflpicker.spec")


def test_both_icon_formats_are_present():
    """macOS refuses a .ico outright and PyInstaller's conversion needs Pillow
    on the build machine, which a CI runner does not have. Shipping one format
    fails the Mac build at BUNDLE, which is exactly what happened."""
    assert (ASSETS / "icon.ico").exists(), "Windows icon missing"
    assert (ASSETS / "icon.icns").exists(), "macOS icon missing"


def test_the_icns_is_really_an_icns():
    """It is written by hand rather than by `iconutil`, which only exists on
    macOS -- so the header is worth checking somewhere that is not macOS."""
    data = (ASSETS / "icon.icns").read_bytes()
    assert data[:4] == b"icns"
    declared = struct.unpack(">I", data[4:8])[0]
    assert declared == len(data), "the length header must match the file"
    # At least one PNG-typed entry, at a size a Dock icon actually uses.
    assert b"ic09" in data or b"ic08" in data


def test_the_ico_carries_the_small_sizes_windows_asks_for():
    from PIL import Image

    sizes = sorted(Image.open(ASSETS / "icon.ico").info.get("sizes", []))
    assert (16, 16) in sizes, "the taskbar and title bar use 16px"
    assert (256, 256) in sizes, "Explorer's large view uses 256px"


def test_the_spec_picks_the_icon_by_platform():
    """The bug was one icon constant used on both platforms."""
    spec = SPEC.read_text()
    assert "icon.icns" in spec and "icon.ico" in spec
    assert "MACOS" in spec.split("def _icon")[1].split("ICON = _icon()")[0]


@pytest.mark.parametrize("path", ["nflpicker/web", "data/models"])
def test_the_spec_ships_what_the_app_reads_at_runtime(path):
    spec = SPEC.read_text()
    assert path in spec, f"{path} is not packaged"


def test_both_platforms_build_one_folder():
    """A onefile Windows build extracted a quarter of a gigabyte to a temp
    directory on every launch -- a stall before the window, and the exact
    behavioural signature heuristic antivirus is built to notice. macOS never
    had the choice, because a .app is already a folder; Windows did, and the
    choice was wrong.

    Pinned as a test because the symptom of a regression is "the app feels
    slow to start", which nobody files as a packaging bug.
    """
    spec = SPEC.read_text(encoding="utf-8")
    assert "COLLECT(" in spec
    # The onefile form passes binaries and datas straight into EXE. The
    # one-folder form does not, and sets exclude_binaries instead.
    assert "exclude_binaries=True" in spec
    assert "EXE(pyz, a.scripts, a.binaries, a.datas" not in spec, (
        "that is the onefile signature")


def test_the_spec_has_one_exe_and_one_collect():
    """Not a per-platform branch that could drift back apart."""
    spec = SPEC.read_text(encoding="utf-8")
    assert spec.count("exe = EXE(") == 1
    assert spec.count("coll = COLLECT(") == 1
