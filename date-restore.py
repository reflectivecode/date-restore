#!/usr/bin/env python3

import ctypes
import errno
import json
import math
import os
import re
import signal
import sys
from collections import Counter
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


MOVIES = Path(os.environ.get("MOVIES_DIR", "/media/movies"))
TV = Path(os.environ.get("TV_DIR", "/media/tv"))
INDEX_FILE = Path(os.environ.get("INDEX_FILE", "/media/media-timestamps.json"))

try:
    SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "300"))
except ValueError:
    SCAN_INTERVAL = 0
if SCAN_INTERVAL < 1:
    sys.exit(
        f"SCAN_INTERVAL must be a whole number of seconds, 1 or more "
        f"(got {os.environ['SCAN_INTERVAL']!r}). Exiting."
    )

VIDEO_EXTENSIONS = {
    ext.strip().lower()
    if ext.strip().startswith(".")
    else "." + ext.strip().lower()
    for ext in os.environ.get("VIDEO_EXTENSIONS", "mkv,mp4").split(",")
    if ext.strip()
}

EPISODE_RE = re.compile(r"(S\d{2,}E\d{2,}(?:-E\d{2,})?)", re.IGNORECASE)

# Used to check the inotify watch limit
LIBC = ctypes.CDLL(None, use_errno=True)
IN_ATTRIB = 0x00000004

# Control characters, and undecodable filename bytes (surrogate escapes)
UNPRINTABLE_RE = re.compile("[\x00-\x1f\x7f-\x9f\udc80-\udcff]")


def main():
    # PID 1 in a container ignores SIGTERM unless a handler is installed
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))

    index = load_index()

    # Start watching before the first scan so no changes are missed.
    # The watcher only restores indexed entries, so this is safe.
    handler = MediaHandler(index)

    observer = Observer()
    watched = {}
    watch_new_dirs(observer, handler, watched)
    try:
        observer.start()
    except OSError as e:
        exit_watch_error(e)

    try:
        scan(index)

        # Always save once so a misconfigured index location fails immediately
        if not save_index(index):
            sys.exit(
                f"Cannot write index file {INDEX_FILE}. Its directory must be "
                f"writable, and the file must not be mounted on its own. Exiting."
            )
        unsaved = False

        print(f"Watching for file changes. Scan every {SCAN_INTERVAL}s")

        while observer.is_alive():
            observer.join(SCAN_INTERVAL)
            if watch_limit_reached():
                exit_watch_error(OSError(errno.ENOSPC, "inotify watch limit reached"))
            watch_new_dirs(observer, handler, watched)
            if scan(index) or unsaved:
                unsaved = not save_index(index)
    finally:
        print("Stopping")
        observer.stop()
        observer.join()


def watch_new_dirs(observer, handler, watched):
    # A media directory missing at startup is watched once it appears. One
    # that is replaced is watched again: a watch follows a moved directory,
    # and stops when its directory is deleted.
    for path in (MOVIES, TV):
        try:
            stat = path.stat()
        except OSError:
            continue
        identity = (stat.st_dev, stat.st_ino)

        if path in watched:
            watch, watched_identity = watched[path]
            if identity == watched_identity and is_watching(observer, watch):
                continue
            print(f"Watching replaced directory {path}")
            try:
                observer.unschedule(watch)
            except KeyError:
                pass
        else:
            print(f"Watching {path}")

        try:
            watch = observer.schedule(handler, str(path), recursive=True)
        except OSError as e:
            exit_watch_error(e)
        watched[path] = (watch, identity)


def is_watching(observer, watch):
    return any(e.watch == watch and e.is_alive() for e in observer.emitters)


def watch_limit_reached():
    # Once the limit is reached, watchdog silently skips new directories,
    # so check whether the kernel will still grant a watch of our own
    fd = LIBC.inotify_init1(os.O_CLOEXEC)
    if fd < 0:
        return False
    try:
        if LIBC.inotify_add_watch(fd, b"/", IN_ATTRIB) >= 0:
            return False
        return ctypes.get_errno() == errno.ENOSPC
    finally:
        os.close(fd)


def exit_watch_error(error):
    sysctl = {
        errno.ENOSPC: "fs.inotify.max_user_watches",
        errno.EMFILE: "fs.inotify.max_user_instances",
    }.get(error.errno)
    hint = f" Raise the {sysctl} sysctl on the host." if sysctl else ""
    sys.exit(f"Cannot watch media directories: {error}.{hint} Exiting.")


def load_index():
    if not INDEX_FILE.exists():
        print(f"Existing index not found at {INDEX_FILE}")
        return {}
    print(f"Loading index from {INDEX_FILE}")
    try:
        with INDEX_FILE.open() as f:
            index = json.load(f)
        if not isinstance(index, dict):
            raise ValueError("expected a JSON object")
        for key, value in index.items():
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"timestamp for {key!r} is not a number")
    except ValueError as e:
        sys.exit(
            f"Index file {INDEX_FILE} is corrupt: {e}\n"
            f"Fix or restore the file, or delete it to rebuild the index "
            f"from current modification times. Exiting."
        )
    return index


def save_index(index):
    print(f"Saving index to {INDEX_FILE}")
    temp_file = INDEX_FILE.with_suffix(".tmp")
    try:
        with temp_file.open("w") as f:
            json.dump(index, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, INDEX_FILE)
    except OSError as e:
        print(f"Error saving index to {INDEX_FILE}: {e}")
        try:
            temp_file.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def scan(index):
    counts = Counter()

    if MOVIES.exists():
        print(f"Scanning {MOVIES}")
        for movie_dir in list_dir(MOVIES):
            if not movie_dir.is_dir():
                continue

            for filepath in list_dir(movie_dir):
                scan_file(index, filepath, counts)

    if TV.exists():
        print(f"Scanning {TV}")
        for show_dir in list_dir(TV):
            if not show_dir.is_dir():
                continue

            for season_dir in list_dir(show_dir):
                if not season_dir.is_dir():
                    continue

                for filepath in list_dir(season_dir):
                    scan_file(index, filepath, counts)

    print(
        f"Scan complete: "
        f"{counts['found']} found, "
        f"{counts['new']} new, "
        f"{counts['restored']} restored, "
        f"{counts['kept']} kept, "
        f"{counts['failed']} failed, "
        f"{len(index)} indexed"
    )
    return counts["new"]


def scan_file(index, filepath, counts):
    key = get_key(filepath)
    if not key:
        return

    mtime = get_mtime(filepath)
    if mtime is None:
        return

    counts["found"] += 1

    if key not in index:
        index[key] = mtime
        counts["new"] += 1
        print(f"New: {escape(key)}")
    elif mtime != index[key]:
        if restore_mtime(filepath, index[key]):
            counts["restored"] += 1
        else:
            counts["failed"] += 1
    else:
        counts["kept"] += 1


def list_dir(path):
    try:
        return list(path.iterdir())
    except FileNotFoundError:
        return []
    except OSError as e:
        print(f"Error listing {escape(path)}: {e}")
        return []


def get_key(filepath):
    if filepath.suffix.lower() not in VIDEO_EXTENSIONS:
        return None

    # Movie: /movies/TITLE (YEAR)/TITLE (YEAR) - METADATA.EXT
    if filepath.parent.parent == MOVIES:
        return f"movies/{filepath.parent.name}"

    # TV: /tv/TITLE YEAR/Season XX/TITLE YEAR - SXXEXX - METADATA.EXT
    if filepath.parent.parent.parent == TV:
        match = EPISODE_RE.search(filepath.stem)
        if match:
            episode = match.group(1).upper()
            return f"tv/{filepath.parent.parent.name}/{episode}"

    return None


def get_mtime(filepath):
    try:
        return filepath.stat().st_mtime
    except FileNotFoundError:
        return None
    except OSError as e:
        print(f"Error reading {escape(filepath)}: {e}")
        return None


def escape(value):
    # Keep one log line per message, whatever the filename contains
    return UNPRINTABLE_RE.sub(lambda m: f"\\x{ord(m[0]) & 0xff:02x}", str(value))


def restore_mtime(filepath, timestamp):
    try:
        stat = filepath.stat()
        if stat.st_mtime != timestamp:
            print(f"Changing mtime of {escape(filepath)} to {timestamp}")
            os.utime(filepath, (stat.st_atime, timestamp))
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"Error changing mtime of {escape(filepath)}: {e}")
        return False
    return True


class MediaHandler(FileSystemEventHandler):
    def __init__(self, index):
        self.index = index

    def on_created(self, event):
        self.handle(event, event.src_path)

    def on_moved(self, event):
        self.handle(event, event.dest_path)

    # A file copied or written in place keeps changing mtime after it is created
    def on_closed(self, event):
        self.handle(event, event.src_path)

    def handle(self, event, path):
        if event.is_directory:
            return

        filepath = Path(path)
        key = get_key(filepath)
        if key is None:
            return

        timestamp = self.index.get(key)

        if timestamp is None:
            print(f"Ignoring new video until next full scan: {escape(filepath)}")
            return

        restore_mtime(filepath, timestamp)

if __name__ == "__main__":
    main()
