"""Install and run a local model server, without sending the user elsewhere.

The assistant needs two things that were previously the buyer's problem: a
program that serves a model over HTTP, and a model for it to serve. Telling
someone who has just paid for this to go and install Ollama, run a command
they have never seen, and then copy an address into a settings page is three
chances to give up. This does it from a button.

Three decisions worth knowing about:

* **Our own copy, in our own directory.** The runtime is unpacked into the
  data directory rather than installed system-wide. No administrator prompt,
  no installer window, nothing left behind on a machine where The Edge is
  deleted, and no argument with an Ollama the user already had.
* **Our own port.** 11435, one above the default, for the same reason: a
  user who already runs Ollama on 11434 keeps their models, their settings
  and their port.
* **The download URL is resolved, not remembered.** Asset names change --
  ``ollama-darwin`` became ``ollama-darwin.tgz`` -- and a hard-coded URL that
  rots turns a paid feature into a dead button. The release list is asked
  what it has and matched by pattern, with the documented download pages as
  the fallback and an honest error as the floor.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import get_config

# One above Ollama's default, so an Ollama the user installed themselves keeps
# its port, its models and its settings.
PORT = 11435
HOST = f"127.0.0.1:{PORT}"
ENDPOINT = f"http://{HOST}/v1"

RELEASES = "https://api.github.com/repos/ollama/ollama/releases/latest"

# Matched against asset names in order, so a self-contained archive is always
# preferred over an installer: an installer needs a window, a click and
# somewhere system-wide to write. Keyed by (system, architecture), because a
# build for the wrong architecture does not warn -- it simply will not run.
ASSETS: dict[tuple[str, str], tuple[str, ...]] = {
    # macOS builds are universal, so the architecture does not pick between
    # them; both keys are here so the lookup never has to special-case it.
    ("darwin", "amd64"): ("ollama-darwin.tgz", "ollama-darwin.zip", "ollama-darwin"),
    ("darwin", "arm64"): ("ollama-darwin.tgz", "ollama-darwin.zip", "ollama-darwin"),
    ("windows", "amd64"): ("ollama-windows-amd64.zip",),
    ("windows", "arm64"): ("ollama-windows-arm64.zip",),
    ("linux", "amd64"): ("ollama-linux-amd64.tgz",),
    ("linux", "arm64"): ("ollama-linux-arm64.tgz",),
}

# Used only when the release list cannot be reached, which is a guess at a name
# rather than a fact about one -- hence the order above being tried first.
FALLBACK_HOST = "https://ollama.com/download"

# Offered in the setup panel. The box is editable, because a tag that has been
# retired should cost the user five seconds rather than the whole feature.
MODELS = (
    {"name": "qwen3.5:4b", "size": "about 3 GB",
     "note": "The default. Plenty for reading a page of numbers, and it runs "
             "on a laptop without a graphics card."},
    {"name": "llama3.2:1b", "size": "about 1 GB",
     "note": "For an older or smaller machine. Faster, and noticeably worse "
             "at arithmetic."},
)
DEFAULT_MODEL = MODELS[0]["name"]


class SetupError(RuntimeError):
    """Something the user can act on, phrased for the panel rather than a log."""


# --------------------------------------------------------------- where things are

def runtime_dir() -> Path:
    return get_config().data_dir / "runtime"


def models_dir() -> Path:
    return runtime_dir() / "models"


def _exe(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def managed_binary() -> Path | None:
    """Our own copy, if setup has run. Archives vary in shape -- a bare binary,
    ``bin/ollama``, ``ollama.exe`` at the root -- so this looks rather than
    assumes."""
    root = runtime_dir()
    for candidate in (root / _exe("ollama"), root / "bin" / _exe("ollama")):
        if candidate.is_file():
            return candidate
    if root.is_dir():
        for found in root.rglob(_exe("ollama")):
            if found.is_file() and "models" not in found.parts:
                return found
    return None


def system_binary() -> Path | None:
    """An Ollama the user installed themselves, wherever their platform puts it."""
    found = shutil.which("ollama")
    if found:
        return Path(found)
    home = Path.home()
    candidates = {
        "win32": [Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
                  / "Programs" / "Ollama" / "ollama.exe"],
        "darwin": [Path("/Applications/Ollama.app/Contents/Resources/ollama"),
                   home / "Applications/Ollama.app/Contents/Resources/ollama",
                   Path("/usr/local/bin/ollama")],
    }.get(sys.platform, [Path("/usr/local/bin/ollama"), home / ".local/bin/ollama"])
    return next((c for c in candidates if c.is_file()), None)


def binary() -> Path | None:
    return managed_binary() or system_binary()


# ------------------------------------------------------------------- running it

_process: subprocess.Popen | None = None
_process_lock = threading.Lock()


def serving(timeout: float = 2.0) -> bool:
    try:
        import httpx

        return httpx.get(f"http://{HOST}/api/version", timeout=timeout).status_code == 200
    except Exception:                                          # noqa: BLE001
        return False


def start(wait: float = 20.0) -> bool:
    """Start the server if it is not already answering.

    Idempotent and safe to call from anywhere: something already listening on
    our port is the desired state however it got there.
    """
    global _process
    if serving():
        return True
    exe = binary()
    if not exe:
        return False
    with _process_lock:
        if serving():
            return True
        env = dict(os.environ)
        env["OLLAMA_HOST"] = HOST
        # Keep the weights with the rest of our data, so a backup covers them
        # and an uninstall takes them. A user's own Ollama models stay theirs.
        env["OLLAMA_MODELS"] = str(models_dir())
        models_dir().mkdir(parents=True, exist_ok=True)
        # A windowed build has no console; on Windows a child process would
        # open one, which is a black box flashing up on a user's screen.
        creation = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        _process = subprocess.Popen(                           # noqa: S603
            [str(exe), "serve"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creation)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if serving():
            return True
        time.sleep(0.4)
    return False


def stop() -> None:
    """Shut our own server down. Anything we did not start is left alone."""
    global _process
    with _process_lock:
        if _process and _process.poll() is None:
            _process.terminate()
            with contextlib.suppress(Exception):
                _process.wait(timeout=5)
        _process = None


def installed_models() -> list[str]:
    try:
        import httpx

        r = httpx.get(f"http://{HOST}/api/tags", timeout=5.0)
        r.raise_for_status()
        return [m.get("name", "") for m in (r.json().get("models") or [])]
    except Exception:                                          # noqa: BLE001
        return []


# ------------------------------------------------------------------ the job

@dataclass
class Progress:
    """What the panel draws. One object, replaced rather than mutated, so a
    poll can never read a half-written update."""

    phase: str = "idle"            # idle | runtime | model | starting | done | error
    message: str = ""
    done: int = 0
    total: int = 0
    model: str = ""
    error: str = ""
    finished_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "phase": self.phase, "message": self.message, "done": self.done,
            "total": self.total, "model": self.model, "error": self.error,
            "percent": round(100 * self.done / self.total, 1) if self.total else None,
            "running": self.phase in {"runtime", "model", "starting"},
        }


@dataclass
class _Job:
    progress: Progress = field(default_factory=Progress)
    thread: threading.Thread | None = None


_job = _Job()


def progress() -> dict:
    return _job.progress.as_dict()


def _set(**kwargs) -> None:
    current = _job.progress
    _job.progress = Progress(**{**current.__dict__, **kwargs})


def state() -> dict:
    """Everything the panel needs to decide what to show."""
    exe = binary()
    return {
        "runtime_installed": exe is not None,
        "runtime_managed": managed_binary() is not None,
        "runtime_path": str(exe) if exe else "",
        "serving": serving(timeout=1.0),
        "models": installed_models(),
        "endpoint": ENDPOINT,
        "port": PORT,
        "choices": list(MODELS),
        "default_model": DEFAULT_MODEL,
        "directory": str(runtime_dir()),
        "progress": progress(),
    }


def begin(model: str | None = None) -> dict:
    """Start the setup in the background. One at a time."""
    if _job.thread and _job.thread.is_alive():
        return progress()
    name = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    _job.progress = Progress(phase="runtime", message="Starting…", model=name)
    _job.thread = threading.Thread(target=_run, args=(name,), daemon=True,
                                   name="assistant-setup")
    _job.thread.start()
    return progress()


def _run(model: str) -> None:
    try:
        if binary() is None:
            install_runtime()
        _set(phase="starting", message="Starting the model server…", done=0, total=0)
        if not start():
            raise SetupError(
                "The model server did not answer after starting. If another "
                f"program is using port {PORT}, close it and try again.")
        pull(model)
        _set(phase="starting", message="Saving settings…", done=0, total=0)
        _save_settings(model)
        _set(phase="done", message=f"{model} is ready.", finished_at=time.time())
    except Exception as exc:                                   # noqa: BLE001
        _set(phase="error", error=str(exc), message="Setup stopped.")


def _save_settings(model: str) -> None:
    from . import settings as settings_module

    settings_module.save({
        "NFLPICKER_LLM_URL": ENDPOINT,
        "NFLPICKER_LLM_MODEL": model,
    })


# ---------------------------------------------------------------- downloading

def _fetch_release() -> dict[str, str]:
    """{asset name: download url} for the newest release."""
    import httpx

    response = httpx.get(RELEASES, timeout=20.0, follow_redirects=True,
                         headers={"Accept": "application/vnd.github+json"})
    response.raise_for_status()
    return {a["name"]: a["browser_download_url"]
            for a in (response.json().get("assets") or [])}


def _platform() -> tuple[str, str]:
    """(system, architecture) in the words the release assets are named in."""
    system = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "amd64"
    return system, arch


def _asset_url() -> tuple[str, str]:
    """(url, filename) for this machine, from the release list where possible."""
    system, arch = _platform()
    wanted = ASSETS.get((system, arch)) or ASSETS[(system, "amd64")]
    try:
        assets = _fetch_release()
    except Exception:                                          # noqa: BLE001
        assets = {}
    for name in wanted:
        if name in assets:
            return assets[name], name
    # Named exactly or not at all is too strict for a list that renames things,
    # and one plausible asset for this machine beats a dead button. An asset
    # for the other architecture is not plausible: it would download happily
    # and then refuse to run, which is the worst of both.
    for name, url in sorted(assets.items()):
        lower = name.lower()
        other = "amd64" if arch == "arm64" else "arm64"
        if (system in lower and other not in lower
                and not lower.endswith((".sig", ".sha256", ".txt"))):
            return url, name
    name = wanted[0]
    return f"{FALLBACK_HOST}/{name}", name


def install_runtime() -> Path:
    """Download the runtime and unpack it into our own directory."""
    import httpx

    url, filename = _asset_url()
    root = runtime_dir()
    root.mkdir(parents=True, exist_ok=True)
    archive = root / filename
    _set(phase="runtime", message="Downloading the model server…", done=0, total=0)

    with httpx.stream("GET", url, timeout=60.0, follow_redirects=True) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length") or 0)
        written = 0
        with archive.open("wb") as out:
            for chunk in response.iter_bytes(1 << 20):
                out.write(chunk)
                written += len(chunk)
                _set(phase="runtime", done=written, total=total,
                     message="Downloading the model server…")

    _set(phase="runtime", message="Unpacking…", done=0, total=0)
    _unpack(archive, root)
    with contextlib.suppress(OSError):
        archive.unlink()

    exe = managed_binary()
    if not exe:
        raise SetupError(
            f"{filename} downloaded but no ollama program was found inside it.")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if sys.platform == "darwin":
        # We fetched this ourselves over TLS from the project's own release,
        # and macOS would otherwise refuse to run it without a click nobody is
        # there to make.
        with contextlib.suppress(Exception):
            subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(runtime_dir())],  # noqa: S603, S607
                           check=False, timeout=30)
    return exe


def _unpack(archive: Path, into: Path) -> None:
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            _safe_extract_zip(zf, into)
    elif name.endswith((".tgz", ".tar.gz")):
        with tarfile.open(archive, "r:gz") as tf:
            _safe_extract_tar(tf, into)
    else:
        # A bare binary, downloaded under its own name.
        target = into / _exe("ollama")
        if archive != target:
            shutil.move(str(archive), target)


def _inside(root: Path, member: str) -> Path:
    """Where a member may be written, refusing anything that climbs out.

    An archive entry named ``../../.bashrc`` is a real and old attack, and the
    fact that this one comes from a project we trust is not the same as the
    fact that the bytes on the wire came from them.
    """
    target = (root / member).resolve()
    if not str(target).startswith(str(root.resolve())):
        raise SetupError(f"The archive contains an unsafe path: {member}")
    return target


def _safe_extract_zip(zf: zipfile.ZipFile, into: Path) -> None:
    for member in zf.namelist():
        _inside(into, member)
    zf.extractall(into)                                        # noqa: S202


def _safe_extract_tar(tf: tarfile.TarFile, into: Path) -> None:
    for member in tf.getmembers():
        _inside(into, member.name)
        if member.issym() or member.islnk():
            _inside(into, member.linkname)
    tf.extractall(into)                                        # noqa: S202


# --------------------------------------------------------------- the weights

def pull(model: str) -> None:
    """Pull a model, reporting bytes as they land.

    Ollama streams newline-delimited JSON with `total` and `completed` on each
    line, which is the whole reason the progress bar can be honest rather than
    a spinner with a guess attached.
    """
    import httpx

    _set(phase="model", message=f"Downloading {model}…", done=0, total=0, model=model)
    with httpx.stream("POST", f"http://{HOST}/api/pull", timeout=None,
                      json={"model": model, "stream": True}) as response:
        if response.status_code >= 400:
            response.read()
            raise SetupError(
                f"The model server refused to pull {model}. Check the name "
                "against ollama.com/library — a tag that has been retired "
                "fails here and a different one will work.")
        for line in response.iter_lines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("error"):
                raise SetupError(str(event["error"]))
            # Only the layer lines carry byte counts. "pulling manifest" and
            # the closing "success" do not, and zeroing the bar on those sent
            # it back to the start at the exact moment it finished.
            counts = ({"done": int(event.get("completed") or 0),
                       "total": int(event["total"])}
                      if event.get("total") else {})
            _set(phase="model", model=model,
                 message=str(event.get("status") or f"Downloading {model}…"),
                 **counts)
