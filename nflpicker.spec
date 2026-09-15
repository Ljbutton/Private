# PyInstaller spec for a single-file NFL Picker desktop build.
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
if sys.platform == "darwin":
    hidden += ["webview.platforms.cocoa"]
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
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
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

# macOS wants an .app bundle, not a bare Unix executable. Double-clicking a
# bare binary in Finder opens it in Terminal, which is not an application --
# and without a bundle there is nowhere to say the app has no Dock tile to
# share, no document types, and a window of its own.
if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="NFLPicker.app",
        icon=None,
        bundle_identifier="com.nflpicker.desktop",
        info_plist={
            "CFBundleName": "NFL Picker",
            "CFBundleDisplayName": "NFL Picker",
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "0.1.0",
            # The dashboard renders in WKWebView against a loopback server, so
            # the app never reaches the network itself and needs no exception.
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
