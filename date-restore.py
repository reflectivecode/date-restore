#!/usr/bin/env python3

import json
import os
import re
import signal
import sys
import time
from collections import Counter
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


MOVIES = Path(os.environ.get("MOVIES_DIR", "/media/movies"))
TV = Path(os.environ.get("TV_DIR", "/media/tv"))
INDEX_FILE = Path(os.environ.get("INDEX_FILE", "/media/media-timestamps.json"))

SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "300"))

VIDEO_EXTENSIONS = {
    ext.strip().lower()
    if ext.strip().startswith(".")
    else "." + ext.strip().lower()
    for ext in os.environ.get("VIDEO_EXTENSIONS", "mkv,mp4").split(",")
    if ext.strip()
}

EPISODE_RE = re.compile(r"(S\d{2}E\d{2}(?:-E\d{2})?)", re.IGNORECASE)


def main():
    # PID 1 in a container ignores SIGTERM unless a handler is installed
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))

    index = load_index()
    scan(index)

    handler = MediaHandler(index)

    observer = Observer()
    if MOVIES.exists():
        print(f"Watching {MOVIES}")
        observer.schedule(handler, str(MOVIES), recursive=True)
    if TV.exists():
        print(f"Watching {TV}")
        observer.schedule(handler, str(TV), recursive=True)
    observer.start()

    print(f"Watching for file changes. Scan every {SCAN_INTERVAL}s")

    try:
        while observer.is_alive():
            observer.join(SCAN_INTERVAL)
            scan(index)
    finally:
        print("Stopping")
        observer.stop()
        observer.join()


def load_index():
    if not INDEX_FILE.exists():
        print(f"Existing index not found at {INDEX_FILE}")
        return {}
    print(f"Loading index from {INDEX_FILE}")
    with INDEX_FILE.open() as f:
        return json.load(f)


def save_index(index):
    print(f"Saving index to {INDEX_FILE}")
    temp_file = INDEX_FILE.with_suffix(".tmp")
    with temp_file.open("w") as f:
        json.dump(index, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_file, INDEX_FILE)


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

    if counts["new"]:
        save_index(index)

    print(
        f"Scan complete: "
        f"{counts['found']} found, "
        f"{counts['new']} new, "
        f"{counts['restored']} restored, "
        f"{counts['kept']} kept, "
        f"{counts['failed']} failed, "
        f"{len(index)} indexed"
    )


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
        print(f"New: {key}")
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
        print(f"Error listing {path}: {e}")
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
        print(f"Error reading {filepath}: {e}")
        return None


def restore_mtime(filepath, timestamp):
    try:
        stat = filepath.stat()
        if stat.st_mtime != timestamp:
            print(f"Changing mtime of {filepath} to {timestamp}")
            os.utime(filepath, (stat.st_atime, timestamp))
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"Error changing mtime of {filepath}: {e}")
        return False
    return True


class MediaHandler(FileSystemEventHandler):
    def __init__(self, index):
        self.index = index

    def on_created(self, event):
        self.handle(event, event.src_path)

    def on_moved(self, event):
        self.handle(event, event.dest_path)

    def handle(self, event, path):
        if event.is_directory:
            return

        filepath = Path(path)
        key = get_key(filepath)
        if key is None:
            return

        timestamp = self.index.get(key)

        if timestamp is None:
            print(f"Ignoring new video until next full scan: {filepath}")
            return

        restore_mtime(filepath, timestamp)

if __name__ == "__main__":
    main()
