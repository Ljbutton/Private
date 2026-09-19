"""Installing and running the model server from inside the app.

Everything interesting here is a download, a subprocess or another program's
HTTP API, none of which exist in a test run — so the seams are exercised
directly with fakes. What is actually being checked is the part that would be
silently wrong on a stranger's machine: which asset is chosen, that an archive
cannot write outside the directory it is unpacked into, that the progress the
panel draws is the progress the download reported, and that a failure lands as
a message rather than a traceback.
"""

from __future__ import annotations

import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from nflpicker import localmodel


@pytest.fixture(autouse=True)
def _own_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(localmodel, "runtime_dir", lambda: tmp_path / "runtime")
    localmodel._job = localmodel._Job()
    yield


# ------------------------------------------------------------------ assets

def _on(monkeypatch, system: str, arch: str = "amd64") -> None:
    """Pretend to be a machine.

    Both halves, always. Setting `sys.platform` alone left the runner's real
    CPU underneath, which is how a test written on an Intel machine passed
    there and failed on GitHub's Apple silicon: it claimed to be Windows on an
    arm64 processor, which is a real combination and not the one under test.
    """
    monkeypatch.setattr(localmodel, "_platform", lambda: (system, arch))


def test_the_release_list_decides_the_url(monkeypatch):
    """Asset names change — ollama-darwin became ollama-darwin.tgz — so the
    list is asked rather than a URL remembered."""
    _on(monkeypatch, "darwin", "arm64")
    monkeypatch.setattr(localmodel, "_fetch_release", lambda: {
        "ollama-darwin.tgz": "https://example.test/ollama-darwin.tgz",
        "OllamaSetup.exe": "https://example.test/OllamaSetup.exe",
    })
    url, name = localmodel._asset_url()
    assert name == "ollama-darwin.tgz"
    assert url == "https://example.test/ollama-darwin.tgz"


def test_an_archive_beats_an_installer(monkeypatch):
    """An installer needs a window and somewhere system-wide to write. The
    self-contained archive needs neither, so it wins when both are offered."""
    _on(monkeypatch, "windows")
    monkeypatch.setattr(localmodel, "_fetch_release", lambda: {
        "OllamaSetup.exe": "https://example.test/OllamaSetup.exe",
        "ollama-windows-amd64.zip": "https://example.test/win.zip",
    })
    _, name = localmodel._asset_url()
    assert name == "ollama-windows-amd64.zip"


def test_the_architecture_decides_which_build(monkeypatch):
    """A build for the wrong architecture does not warn. It downloads happily
    and then refuses to run, which is the worst of both."""
    _on(monkeypatch, "windows", "arm64")
    monkeypatch.setattr(localmodel, "_fetch_release", lambda: {
        "ollama-windows-amd64.zip": "https://example.test/x64.zip",
        "ollama-windows-arm64.zip": "https://example.test/arm.zip",
    })
    url, _ = localmodel._asset_url()
    assert url == "https://example.test/arm.zip"


@pytest.mark.parametrize(("machine", "expected"), [
    ("AMD64", "amd64"), ("x86_64", "amd64"),
    ("arm64", "arm64"), ("aarch64", "arm64"), ("ARM64", "arm64"),
])
def test_the_machine_name_maps_to_an_asset_name(monkeypatch, machine, expected):
    """Every platform spells its own CPU differently and none of them spell it
    the way the release assets do."""
    import platform as platform_module

    monkeypatch.setattr(platform_module, "machine", lambda: machine)
    assert localmodel._platform()[1] == expected


def test_an_unreachable_release_list_falls_back(monkeypatch):
    def boom():
        raise OSError("no network")

    _on(monkeypatch, "darwin", "arm64")
    monkeypatch.setattr(localmodel, "_fetch_release", boom)
    url, name = localmodel._asset_url()
    assert name == "ollama-darwin.tgz"
    assert url.startswith(localmodel.FALLBACK_HOST)


def test_a_renamed_asset_is_still_found(monkeypatch):
    """Nothing matches by name, but exactly one asset is for this platform.
    Guessing it beats failing, because the alternative is a dead button."""
    _on(monkeypatch, "darwin", "arm64")
    monkeypatch.setattr(localmodel, "_fetch_release", lambda: {
        "ollama-darwin-v2.tgz": "https://example.test/new.tgz",
        "ollama-linux-amd64.tgz": "https://example.test/linux.tgz",
    })
    url, _ = localmodel._asset_url()
    assert url == "https://example.test/new.tgz"


def test_the_guess_never_picks_the_other_architecture(monkeypatch):
    """The last-resort match is a guess, and a guess that lands on a binary
    for the other CPU is worse than no guess at all."""
    _on(monkeypatch, "linux", "arm64")
    monkeypatch.setattr(localmodel, "_fetch_release", lambda: {
        "ollama-linux-amd64-rebuilt.tgz": "https://example.test/x64.tgz",
    })
    url, name = localmodel._asset_url()
    assert name == "ollama-linux-arm64.tgz"
    assert url.startswith(localmodel.FALLBACK_HOST)


# --------------------------------------------------------------- unpacking

def _tar_with(tmp_path: Path, names: dict[str, bytes]) -> Path:
    archive = tmp_path / "in.tgz"
    with tarfile.open(archive, "w:gz") as tf:
        for name, data in names.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return archive


def test_a_tarball_unpacks_and_the_binary_is_found(tmp_path):
    """The binary is named for the platform the app is running on, not the
    platform the test was written on: Ollama's Windows archive carries
    ollama.exe and its macOS one carries ollama, and the lookup asks for
    whichever this machine would have downloaded."""
    exe = localmodel._exe("ollama")
    archive = _tar_with(tmp_path, {f"bin/{exe}": b"#!/bin/sh\n", "lib/ollama/x.so": b"x"})
    root = localmodel.runtime_dir()
    root.mkdir(parents=True)
    localmodel._unpack(archive, root)
    found = localmodel.managed_binary()
    assert found is not None and found.name == exe


def test_an_archive_cannot_write_outside_its_directory(tmp_path):
    """`../../.bashrc` is an old attack and still a real one. That the project
    is trustworthy is not the same as the bytes on the wire being theirs."""
    archive = _tar_with(tmp_path, {"../escaped": b"no"})
    root = localmodel.runtime_dir()
    root.mkdir(parents=True)
    with pytest.raises(localmodel.SetupError, match="unsafe path"):
        localmodel._unpack(archive, root)
    assert not (tmp_path / "escaped").exists()


def test_a_zip_cannot_write_outside_its_directory(tmp_path):
    archive = tmp_path / "in.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escaped", "no")
    root = localmodel.runtime_dir()
    root.mkdir(parents=True)
    with pytest.raises(localmodel.SetupError, match="unsafe path"):
        localmodel._unpack(archive, root)


def test_a_bare_binary_is_named_correctly(tmp_path):
    root = localmodel.runtime_dir()
    root.mkdir(parents=True)
    archive = root / "ollama-darwin"
    archive.write_bytes(b"binary")
    localmodel._unpack(archive, root)
    assert (root / localmodel._exe("ollama")).read_bytes() == b"binary"


# ---------------------------------------------------------------- progress

class _FakeStream:
    """Enough of httpx's streaming response for the pull loop."""

    def __init__(self, lines, status=200):
        self._lines = lines
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def iter_lines(self):
        yield from self._lines

    def read(self):
        return b""


def test_the_pull_reports_the_bytes_it_was_told(monkeypatch):
    lines = [
        json.dumps({"status": "pulling manifest"}),
        json.dumps({"status": "pulling 1a2b", "completed": 500, "total": 2000}),
        "",
        json.dumps({"status": "pulling 1a2b", "completed": 2000, "total": 2000}),
        json.dumps({"status": "success"}),
    ]
    import httpx

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeStream(lines))
    localmodel.pull("qwen3.5:4b")
    p = localmodel.progress()
    assert p["done"] == 2000
    assert p["total"] == 2000
    assert p["percent"] == 100.0
    assert p["message"] == "success"


def test_a_pull_error_line_stops_it(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeStream(
        [json.dumps({"error": "model 'nope:1b' not found"})]))
    with pytest.raises(localmodel.SetupError, match="not found"):
        localmodel.pull("nope:1b")


def test_a_refused_pull_says_what_to_do(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeStream([], status=404))
    with pytest.raises(localmodel.SetupError, match="ollama.com/library"):
        localmodel.pull("retired:1b")


def test_progress_without_a_total_is_not_a_percentage():
    """Ollama sends no total while it resolves a manifest. A bar at 0% for
    twenty seconds reads as a hang, so the panel is told to sweep instead."""
    localmodel._set(phase="model", message="pulling manifest", done=0, total=0)
    assert localmodel.progress()["percent"] is None
    assert localmodel.progress()["running"] is True


def test_a_failure_lands_as_a_message_not_a_traceback(monkeypatch):
    def boom():
        raise localmodel.SetupError("the download stopped halfway")

    monkeypatch.setattr(localmodel, "binary", lambda: None)
    monkeypatch.setattr(localmodel, "install_runtime", boom)
    localmodel._run("qwen3.5:4b")
    p = localmodel.progress()
    assert p["phase"] == "error"
    assert p["error"] == "the download stopped halfway"
    assert p["running"] is False


def test_a_finished_run_writes_the_settings(monkeypatch):
    """The point of the button: the user never sees the endpoint or types the
    model name. If this does not happen the download was for nothing."""
    saved = {}
    monkeypatch.setattr(localmodel, "binary", lambda: Path("/somewhere/ollama"))
    monkeypatch.setattr(localmodel, "start", lambda wait=20.0: True)
    monkeypatch.setattr(localmodel, "pull", lambda model: None)
    monkeypatch.setattr(localmodel, "_save_settings", lambda model: saved.update(model=model))
    localmodel._run("llama3.2:1b")
    assert localmodel.progress()["phase"] == "done"
    assert saved == {"model": "llama3.2:1b"}


def test_two_runs_do_not_overlap(monkeypatch):
    """The button is clickable while a download is in flight on a slow poll,
    and two Ollamas pulling into one directory is not a state worth having."""
    import threading

    gate = threading.Event()
    monkeypatch.setattr(localmodel, "binary", lambda: Path("/somewhere/ollama"))
    monkeypatch.setattr(localmodel, "start", lambda wait=20.0: True)
    monkeypatch.setattr(localmodel, "pull", lambda model: gate.wait(5))
    monkeypatch.setattr(localmodel, "_save_settings", lambda model: None)
    localmodel.begin("qwen3.5:4b")
    localmodel.begin("llama3.2:1b")
    gate.set()
    localmodel._job.thread.join(timeout=5)
    assert localmodel.progress()["model"] == "qwen3.5:4b"


def test_an_already_running_server_is_not_started_twice(monkeypatch):
    """Something answering on our port is the desired state however it got
    there — starting a second one is how a port conflict happens."""
    started = []
    monkeypatch.setattr(localmodel, "serving", lambda timeout=2.0: True)
    monkeypatch.setattr(localmodel, "binary", lambda: started.append(1))
    assert localmodel.start() is True
    assert started == []
