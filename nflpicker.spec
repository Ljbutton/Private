# PyInstaller spec for The Edge desktop build.
#
# Two profiles, because the dependency footprint is dominated by two packages:
#   full  — everything, including downloading play-by-play and retraining
#   lite  — excludes pyarrow (~160MB); runs the dashboard and a pre-trained
#           model, but cannot read nflverse parquet, so no training or EPA
#
# Choose with:  pyinstaller nflpicker.spec -- --profile lite
import contextlib
import os
import sys

profile = "full"
if "--profile" in sys.argv:
    profile = sys.argv[sys.argv.index("--profile") + 1]

MACOS = sys.platform == "darwin"

hidden = [
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
    "sklearn.ensemble._hist_gradient_boosting", "sklearn.isotonic",
    "scipy.optimize", "scipy.special",
    # pywebview picks its backend at runtime, so the platform module is never
    # imported anywhere PyInstaller can see. Left out, the packaged app quietly
    # falls back to a browser tab -- which is the one thing packaging it was
    # meant to avoid.
    "webview",
]
if MACOS:
    # The Cocoa backend reaches the system frameworks through pyobjc, which
    # binds them lazily by name at runtime. None of these appear in any import
    # statement PyInstaller can follow, so each has to be asked for.
    hidden += [
        "webview.platforms.cocoa",
        "objc", "Foundation", "AppKit", "WebKit", "Quartz",
        "Security", "UniformTypeIdentifiers",
    ]
elif sys.platform == "win32":
    hidden += [
        "webview.platforms.edgechromium", "webview.platforms.winforms",
        # The Windows backends are .NET, reached through pythonnet. `clr` is
        # imported at the top of both and appears in no import statement
        # PyInstaller can follow from this application.
        "clr", "clr_loader",
    ]

# Data files. pywebview's Windows backend is the reason this is not just the
# web assets.
#
# edgechromium.py runs `clr.AddReference(interop_dll_path(...))` at *import
# time*, and interop_dll_path resolves those DLLs inside the package, at
# webview/lib. They ship in the wheel and PyInstaller does not collect them on
# its own, so the import raised FileNotFoundError, pywebview quietly fell back
# to the legacy MSHTML backend, and that opens no window at all: the packaged
# app exited 0 having shown nothing. A correctly installed WebView2 runtime on
# the machine does not help, because what is missing is the managed wrapper
# that talks to it.
datas = [("nflpicker/web", "nflpicker/web")]
# The trained model. Without this every packaged install fell back to power
# ratings -- the sidebar read "power-only" and the blind and blended columns
# tracked each other, because there was no model in the bundle to separate
# them. The artifact is ~1.3MB; the fallback cost was the whole feature.
if os.path.isdir("data/models"):
    datas += [("data/models", "data/models")]
if sys.platform == "win32":
    from PyInstaller.utils.hooks import collect_data_files

    # lib/ holds the WebView2 core and WinForms assemblies, the
    # WebBrowserInterop DLLs, and lib/runtimes/<arch>/native/WebView2Loader.dll,
    # which edgechromium puts on PATH by asking for the directory. The whole
    # tree has to keep its shape.
    webview_libs = collect_data_files("webview", includes=["lib/**"])
    if not webview_libs:
        raise SystemExit(
            "pywebview's lib/ directory collected nothing. Without it the "
            "packaged app cannot open a window on Windows."
        )
    datas += webview_libs
    # pythonnet carries Python.Runtime.dll and the .NET runtime config beside
    # it; clr_loader is what reads them.
    for package in ("pythonnet", "clr_loader"):
        with contextlib.suppress(Exception):
            datas += collect_data_files(package, include_py_files=False)

# Trimmed because nothing here imports them, and together they are large.
excludes = [
    "matplotlib", "tkinter", "IPython", "notebook", "jupyter", "pytest",
    "sphinx", "PIL", "PyQt5", "PyQt6", "test",
]
if profile == "lite":
    excludes += ["pyarrow"]

a = Analysis(
    ["scripts/desktop_entry.py"],
    pathex=["."],
    datas=datas,
    hiddenimports=hidden,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

# Generated from the same mark the sidebar draws, by scripts/make_icon.py.
#
# Per platform, and not interchangeable: macOS refuses a .ico outright, and
# PyInstaller's automatic conversion needs Pillow on the *build* machine,
# which a CI runner does not have -- so handing it the .ico failed the Apple
# Silicon build at the BUNDLE step with the .app never written.
#
# A missing icon must still not fail the build; PyInstaller falls back to its
# own when this is None.
def _icon() -> str | None:
    name = "assets/icon.icns" if MACOS else "assets/icon.ico"
    return name if os.path.exists(name) else None


ICON = _icon()

common = dict(
    name="TheEdge",
    icon=ICON,
    debug=False,
    strip=False,
    # UPX is off deliberately. It saves some size, but packed executables are
    # a well-known antivirus false-positive trigger, and an unsigned build is
    # already starting from a position of suspicion on Windows.
    upx=False,
    console=False,          # no terminal window behind the app
    disable_windowed_traceback=False,
)

# One directory, not one file, on both platforms.
#
# A onefile build unpacks its entire payload to a temporary directory on
# *every* launch, and this payload is scipy, scikit-learn, pandas and pyarrow.
# That is a quarter of a gigabyte of extraction between the double-click and
# the window, every time, which reads as a hung app -- and the behaviour
# itself, an executable writing a large payload somewhere and running it, is
# what heuristic antivirus is built to notice. An unsigned build starts from a
# position of suspicion already.
#
# macOS never had the choice: a .app is a folder the system displays as one
# icon, so onefile bought nothing there and cost all of the above. Windows has
# no equivalent disguise, which is the only reason the two differed -- one
# folder to keep together, in exchange for a launch that does not stall and a
# shape antivirus recognises as ordinary installed software.
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **common)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="TheEdge")

if MACOS:
    # macOS wants an .app bundle, not a bare Unix executable. Double-clicking a
    # bare binary in Finder opens it in Terminal, which is not an application.
    app = BUNDLE(
        coll,
        name="TheEdge.app",
        icon=ICON,
        bundle_identifier="com.theedge.desktop",
        info_plist={
            "CFBundleName": "The Edge",
            "CFBundleDisplayName": "The Edge",
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "0.1.0",
            # Without this the window renders at 1x and every line of text on
            # a Retina display is soft.
            "NSHighResolutionCapable": True,
            # It is a window with a menu bar, not a background agent.
            "LSBackgroundOnly": False,
            "LSMinimumSystemVersion": "11.0",
        },
    )
