import errno
import json
import os
import shutil
import signal
import time

import pytest

from conftest import is_root, load_app, wait_for


OLD = 1_600_000_000
NEWER = 1_700_000_000


def mtime(path):
    return path.stat().st_mtime


# get_key


def test_movie_key(app, media):
    path = media.movies / "Movie (2020)" / "Movie (2020) - 1080p.mkv"
    assert app.get_key(path) == "movies/Movie (2020)"


@pytest.mark.parametrize(
    "filename, episode",
    [
        ("Show - S01E01 - Title.mkv", "S01E01"),
        ("Show - s01e01 - Title.mkv", "S01E01"),
        ("Show - S01E01-E02 - Title.mkv", "S01E01-E02"),
        ("Show - S01E100 - Title.mkv", "S01E100"),
        ("Show - S2024E01 - Title.mkv", "S2024E01"),
    ],
)
def test_episode_key(app, media, filename, episode):
    path = media.tv / "Show (2020)" / "Season 01" / filename
    assert app.get_key(path) == f"tv/Show (2020)/{episode}"


def test_episode_100_does_not_collide_with_episode_10(app, media):
    season = media.tv / "Show (2020)" / "Season 01"
    assert app.get_key(season / "Show - S01E10.mkv") != app.get_key(
        season / "Show - S01E100.mkv"
    )


@pytest.mark.parametrize(
    "relative",
    [
        "movies/Movie.mkv",
        "movies/Movie (2020)/Extras/Trailer.mkv",
        "tv/Show (2020)/Show - S01E01.mkv",
        "tv/Show (2020)/Season 01/Show - Episode 1.mkv",
        "movies/Movie (2020)/Movie (2020).srt",
    ],
)
def test_ignored_paths(app, media, relative):
    assert app.get_key(media.root / relative) is None


def test_video_extensions(media, monkeypatch):
    monkeypatch.setenv("VIDEO_EXTENSIONS", ".M4V, mkv")
    app = load_app()
    movie = media.movies / "Movie (2020)"
    assert app.get_key(movie / "Movie.m4v") == "movies/Movie (2020)"
    assert app.get_key(movie / "Movie.MKV") == "movies/Movie (2020)"
    assert app.get_key(movie / "Movie.mp4") is None


# escape


@pytest.mark.parametrize(
    "value, escaped",
    [
        ("Two\nLines", "Two\\x0aLines"),
        ("x\r\nFAKE", "x\\x0d\\x0aFAKE"),
        ("Esc\x1b[31m", "Esc\\x1b[31m"),
        ("C1\x9b", "C1\\x9b"),
        (os.fsdecode(b"Bad\xffByte"), "Bad\\xffByte"),
        ("Amélie (2001)", "Amélie (2001)"),
    ],
)
def test_escape(app, value, escaped):
    assert app.escape(value) == escaped


# load_index


def test_load_missing_index(app):
    assert app.load_index() == {}


def test_load_valid_index(app, media):
    media.index_file.write_text('{"movies/A (2020)": 1600000000, "movies/B": 1.5}')
    assert app.load_index() == {"movies/A (2020)": 1600000000, "movies/B": 1.5}


@pytest.mark.parametrize(
    "content",
    [
        '{"movies/A":',
        "[1, 2]",
        '{"movies/A": "yesterday"}',
        '{"movies/A": true}',
        '{"movies/A": NaN}',
        '{"movies/A": null}',
    ],
)
def test_load_corrupt_index(app, media, content):
    media.index_file.write_text(content)
    with pytest.raises(SystemExit, match="is corrupt"):
        app.load_index()


# SCAN_INTERVAL


@pytest.mark.parametrize("value", ["abc", "0", "-5", "1.5", ""])
def test_invalid_scan_interval(media, monkeypatch, value):
    monkeypatch.setenv("SCAN_INTERVAL", value)
    with pytest.raises(SystemExit, match="SCAN_INTERVAL must be"):
        load_app()


def test_scan_interval_with_spaces(media, monkeypatch):
    monkeypatch.setenv("SCAN_INTERVAL", " 60 ")
    assert load_app().SCAN_INTERVAL == 60


def test_default_scan_interval(media, monkeypatch):
    monkeypatch.delenv("SCAN_INTERVAL")
    assert load_app().SCAN_INTERVAL == 300


# scan


def test_first_scan_indexes_current_mtimes(app, media):
    media.make_movie("Movie (2020)", OLD)
    media.make_episode("Show (2020)", "Show - S01E01 - Title.mkv", OLD + 1)
    index = {}
    assert app.scan(index) == 2
    assert index == {"movies/Movie (2020)": OLD, "tv/Show (2020)/S01E01": OLD + 1}


def test_scan_restores_changed_mtime(app, media, capsys):
    movie = media.make_movie("Movie (2020)", OLD)
    episode = media.make_episode("Show (2020)", "Show - S01E01.mkv", OLD)
    index = {}
    app.scan(index)
    os.utime(movie, (NEWER, NEWER))
    assert app.scan(index) == 0
    assert mtime(movie) == OLD
    assert mtime(episode) == OLD
    assert "1 restored, 1 kept, 0 failed" in capsys.readouterr().out


def test_scan_restores_upgrade_with_new_filename(app, media):
    old = media.make_movie("Movie (2020)", OLD, "Movie (2020) - 1080p.mkv")
    index = {}
    app.scan(index)
    old.unlink()
    new = media.make_movie("Movie (2020)", NEWER, "Movie (2020) - 2160p.mkv")
    app.scan(index)
    assert mtime(new) == OLD


@pytest.mark.skipif(is_root(), reason="root can read any directory")
def test_scan_skips_unreadable_directory(app, media, capsys):
    media.make_movie("Movie (2020)", OLD)
    locked = media.movies / "Locked (2020)"
    locked.mkdir()
    locked.chmod(0)
    try:
        assert app.scan({}) == 1
    finally:
        locked.chmod(0o755)
    assert "Error listing" in capsys.readouterr().out


def test_list_dir_missing_directory(app, media):
    assert app.list_dir(media.movies / "Deleted (2020)") == []


def test_scan_counts_failed_restore(app, media, monkeypatch, capsys):
    movie = media.make_movie("Movie (2020)", NEWER)
    index = {"movies/Movie (2020)": OLD}
    monkeypatch.setattr(os, "utime", raise_permission_error)
    app.scan(index)
    assert mtime(movie) == NEWER
    assert "0 restored, 0 kept, 1 failed" in capsys.readouterr().out


# restore_mtime


def raise_permission_error(*args, **kwargs):
    raise PermissionError(errno.EPERM, "Operation not permitted")


def test_restore_missing_file(app, media):
    assert app.restore_mtime(media.movies / "missing.mkv", OLD) is True


def test_restore_permission_error(app, media, monkeypatch, capsys):
    movie = media.make_movie("Movie (2020)", NEWER)
    monkeypatch.setattr(os, "utime", raise_permission_error)
    assert app.restore_mtime(movie, OLD) is False
    assert "Error changing mtime" in capsys.readouterr().out


# save_index


def test_save_index(app, media):
    assert app.save_index({"tv/B": 2.0, "movies/A": 1.0}) is True
    assert list(json.loads(media.index_file.read_text())) == ["movies/A", "tv/B"]
    assert list(media.root.glob("*.tmp")) == []


@pytest.mark.skipif(is_root(), reason="root can write any directory")
def test_save_index_unwritable_directory(app, media, capsys):
    media.root.chmod(0o555)
    try:
        assert app.save_index({"movies/A": 1.0}) is False
    finally:
        media.root.chmod(0o755)
    assert "Error saving index" in capsys.readouterr().out
    assert list(media.root.glob("*.tmp")) == []


# MediaHandler


@pytest.fixture
def watch(app, media):
    observer = app.Observer()

    def start(index):
        observer.schedule(app.MediaHandler(index), str(media.movies), recursive=True)
        observer.start()

    yield start

    if observer.is_alive():
        observer.stop()
        observer.join()


def test_watcher_restores_moved_file(media, watch, tmp_path):
    media.make_movie("Movie (2020)", OLD)
    watch({"movies/Movie (2020)": OLD})
    staged = tmp_path / "staged.mkv"
    staged.write_bytes(b"upgrade")
    target = media.movies / "Movie (2020)" / "Movie (2020) - 2160p.mkv"
    staged.rename(target)
    assert wait_for(lambda: mtime(target) == OLD)


def test_watcher_restores_file_after_writing(media, watch):
    media.make_movie("Movie (2020)", OLD)
    watch({"movies/Movie (2020)": OLD})
    target = media.movies / "Movie (2020)" / "Movie (2020) - 2160p.mkv"
    with target.open("wb") as f:
        for _ in range(3):
            f.write(os.urandom(100_000))
            f.flush()
            time.sleep(0.3)
    assert wait_for(lambda: mtime(target) == OLD)


def test_watcher_restores_file_overwritten_in_place(media, watch):
    movie = media.make_movie("Movie (2020)", OLD)
    watch({"movies/Movie (2020)": OLD})
    movie.write_bytes(b"replaced")
    assert wait_for(lambda: mtime(movie) == OLD)


def test_watches_directory_created_later(app, media):
    media.movies.rmdir()
    observer = app.Observer()
    handler = app.MediaHandler({"movies/Movie (2020)": OLD})
    watched = {}
    observer.start()
    try:
        app.watch_new_dirs(observer, handler, watched)
        assert set(watched) == {app.TV}

        media.make_movie("Movie (2020)", OLD)
        app.watch_new_dirs(observer, handler, watched)
        assert set(watched) == {app.MOVIES, app.TV}

        target = media.movies / "Movie (2020)" / "Movie (2020) - 2160p.mkv"
        target.write_bytes(b"upgrade")
        assert wait_for(lambda: mtime(target) == OLD)
    finally:
        observer.stop()
        observer.join()


@pytest.mark.parametrize("replace", ["rename", "delete"])
def test_watches_replaced_directory(app, media, tmp_path, replace):
    media.make_movie("Movie (2020)", OLD)
    observer = app.Observer()
    handler = app.MediaHandler({"movies/Movie (2020)": OLD})
    watched = {}
    app.watch_new_dirs(observer, handler, watched)
    observer.start()
    try:
        if replace == "rename":
            media.movies.rename(tmp_path / "old-movies")
        else:
            shutil.rmtree(media.movies)
        media.make_movie("Movie (2020)", OLD)
        app.watch_new_dirs(observer, handler, watched)

        target = media.movies / "Movie (2020)" / "Movie (2020) - 2160p.mkv"
        target.write_bytes(b"upgrade")
        assert wait_for(lambda: mtime(target) == OLD)
    finally:
        observer.stop()
        observer.join()


def test_watcher_ignores_new_media(media, watch, capsys):
    watch({})
    movie = media.make_movie("Movie (2020)", NEWER)
    output = []

    def logged():
        output.append(capsys.readouterr().out)
        return "Ignoring new video" in "".join(output)

    assert wait_for(logged)
    assert mtime(movie) == NEWER


# main


def test_sigterm_exits_cleanly(media, run_app):
    media.make_movie("Movie (2020)", OLD)
    process = run_app()
    assert process.wait_for_log("Watching for file changes"), process.log
    assert "movies/Movie (2020)" in json.loads(media.index_file.read_text())
    process.process.send_signal(signal.SIGTERM)
    assert process.process.wait(timeout=5) == 0
    assert "Stopping" in process.log


def test_main_watches_directory_created_later(media, run_app, monkeypatch):
    monkeypatch.setenv("SCAN_INTERVAL", "1")
    media.movies.rmdir()
    process = run_app()
    assert process.wait_for_log("Watching for file changes"), process.log
    assert f"Watching {media.movies}" not in process.log
    media.movies.mkdir()
    assert process.wait_for_log(f"Watching {media.movies}"), process.log


@pytest.mark.skipif(is_root(), reason="root can write any directory")
def test_save_failure_is_retried(media, run_app, monkeypatch):
    monkeypatch.setenv("SCAN_INTERVAL", "1")
    process = run_app()
    assert process.wait_for_log("Watching for file changes"), process.log
    media.root.chmod(0o555)
    try:
        media.make_movie("Movie (2020)", OLD)
        assert process.wait_for_log("Error saving index"), process.log
    finally:
        media.root.chmod(0o755)
    assert wait_for(
        lambda: "movies/Movie (2020)" in media.index_file.read_text()
    ), process.log
    assert process.process.poll() is None


@pytest.mark.skipif(is_root(), reason="root can write any directory")
def test_unwritable_index_exits(app, media, tmp_path, monkeypatch):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    monkeypatch.setattr(app, "INDEX_FILE", locked / "index.json")
    try:
        with pytest.raises(SystemExit, match="Cannot write index file"):
            app.main()
    finally:
        locked.chmod(0o755)


@pytest.mark.parametrize(
    "error, sysctl",
    [
        (errno.ENOSPC, "fs.inotify.max_user_watches"),
        (errno.EMFILE, "fs.inotify.max_user_instances"),
    ],
)
def test_inotify_limit_exits(app, monkeypatch, error, sysctl):
    def start(self):
        raise OSError(error, "inotify limit reached")

    monkeypatch.setattr(app.Observer, "start", start)
    with pytest.raises(SystemExit, match=sysctl):
        app.main()
