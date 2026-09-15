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
]

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
