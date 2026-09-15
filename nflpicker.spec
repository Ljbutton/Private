# PyInstaller spec for the NFL Picker desktop build.
#
# Two profiles, because the dependency footprint is dominated by two packages:
#   full  — everything, including downloading play-by-play and retraining
#   lite  — excludes pyarrow (~160MB); runs the dashboard and a pre-trained
#           model, but cannot read nflverse parquet, so no training or EPA
#
# Choose with:  pyinstaller nflpicker.spec -- --profile lite
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
    hidden += ["webview.platforms.edgechromium", "webview.platforms.winforms"]

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
    datas=[("nflpicker/web", "nflpicker/web")],
    hiddenimports=hidden,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

common = dict(
    name="NFLPicker",
    debug=False,
    strip=False,
    # UPX is off deliberately. It saves some size, but packed executables are
    # a well-known antivirus false-positive trigger, and an unsigned build is
    # already starting from a position of suspicion on Windows.
    upx=False,
    console=False,          # no terminal window behind the app
    disable_windowed_traceback=False,
)

if MACOS:
    # One directory, not one file. A .app is already a folder the user drags
    # around as a single icon, so onefile buys nothing there and costs a great
    # deal: a onefile build unpacks its entire payload to a temporary directory
    # on *every* launch, and this payload is scipy, scikit-learn, pandas and
    # pyarrow. That is a quarter of a gigabyte of extraction between the
    # double-click and the window, every time, which reads as a hung app.
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **common)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="NFLPicker")

    # macOS wants an .app bundle, not a bare Unix executable. Double-clicking a
    # bare binary in Finder opens it in Terminal, which is not an application.
    app = BUNDLE(
        coll,
        name="NFLPicker.app",
        icon=None,
        bundle_identifier="com.nflpicker.desktop",
        info_plist={
            "CFBundleName": "NFL Picker",
            "CFBundleDisplayName": "NFL Picker",
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
else:
    # Windows gets a single .exe, which is the whole point there: one file to
    # download and double-click, with no folder to keep it next to.
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **common)
