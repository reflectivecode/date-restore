import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "date-restore.py"


def pytest_collection_modifyitems(items):
    # The watcher relies on inotify, so the suite only runs on Linux
    if sys.platform != "linux":
        skip = pytest.mark.skip(reason="requires Linux")
        for item in items:
            item.add_marker(skip)


def load_app():
    # The module reads its configuration from the environment on import
    spec = importlib.util.spec_from_file_location("date_restore", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def is_root():
    return os.geteuid() == 0


class Media:
    def __init__(self, root):
        self.root = root
        self.movies = root / "movies"
        self.tv = root / "tv"
        self.index_file = root / "index.json"

    def make_movie(self, name, mtime, filename=None):
        path = self.movies / name / (filename or f"{name} - 1080p.mkv")
        return self._make(path, mtime)

    def make_episode(self, show, filename, mtime, season="Season 01"):
        return self._make(self.tv / show / season / filename, mtime)

    def _make(self, path, mtime):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
        os.utime(path, (mtime, mtime))
        return path


@pytest.fixture
def media(tmp_path, monkeypatch):
    media = Media(tmp_path / "media")
    media.movies.mkdir(parents=True)
    media.tv.mkdir()
    monkeypatch.setenv("MOVIES_DIR", str(media.movies))
    monkeypatch.setenv("TV_DIR", str(media.tv))
    monkeypatch.setenv("INDEX_FILE", str(media.index_file))
    monkeypatch.setenv("SCAN_INTERVAL", "300")
    monkeypatch.delenv("VIDEO_EXTENSIONS", raising=False)
    return media


@pytest.fixture
def app(media, monkeypatch):
    module = load_app()
    # Keep main() from replacing pytest's own SIGTERM handling
    monkeypatch.setattr(signal, "signal", lambda signum, handler: None)
    return module


class Process:
    def __init__(self, log_file):
        self.log_file = log_file
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(SCRIPT)],
            stdout=log_file.open("w"),
            stderr=subprocess.STDOUT,
        )

    @property
    def log(self):
        return self.log_file.read_text()

    def wait_for_log(self, text, timeout=10):
        return wait_for(lambda: text in self.log, timeout)


@pytest.fixture
def run_app(media, tmp_path):
    # Runs the script as a separate process, so signals and exits behave
    # as they do in the container
    processes = []

    def start():
        process = Process(tmp_path / f"log{len(processes)}.txt")
        processes.append(process)
        return process

    yield start

    for process in processes:
        if process.process.poll() is None:
            process.process.kill()
            process.process.wait()
