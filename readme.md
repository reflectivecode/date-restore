# Date Restore

A small filesystem watcher that preserves the original modification time of movies and TV episodes when they are replaced by an upgraded version with the same or different filename.

This is useful when upgrading a movie or episode causes the new file to receive a new filesystem modification time. Media servers such as Jellyfin may use filesystem timestamps when determining when media was added.

*Important:* Configure Jellyfin to "Use file creation date" to determine the date added behavior for new content. This setting can be found under Dashboard -> Libraries -> Display

Date Restore maintains an index of the original modification time for each movie and episode. When an existing media file is replaced, the original timestamp is restored.

## How It Works

Date Restore watches two media directory trees (configurable):

```text
/media/movies
/media/tv
```

Movies are expected to have this structure:

```text
movies/
└── Movie Title (2025)/
    └── Movie Title (2025) - metadata.mkv
```

TV shows are expected to have this structure:

```text
tv/
└── Show Title (2025)/
    └── Season 01/
        └── Show Title (2025) - S01E01 - Episode Title - metadata.mkv
```

The directory depth is fixed. Video files at any other depth, such as extras in a subdirectory of a movie, are ignored.

Only files with extensions configured by `VIDEO_EXTENSIONS` are considered. By default `.mkv` and `.mp4`

### Movie identity

A movie is identified by its movie directory:

```text
movies/Movie Title (2025)
```

All video files directly inside a movie directory share the same entry, and therefore the same timestamp.

### TV episode identity

An episode is identified by the show directory and episode number:

```text
tv/Show Title (2025)/S01E01
```

The episode number must be in the `S01E01` form. Seasons and episodes have at least two digits, and may have more, such as `S01E100` or `S2024E01`. Matching is case-insensitive.

Multi-episode files are kept as a single entry:

```text
tv/Show Title (2025)/S01E01-E02
```

The filename's other release metadata is not relevant.

## Index

The project maintains a JSON index containing the original modification time for each movie and episode.

Example:

```json
{
  "movies/Another Movie (2024)": 1724198400.0,
  "movies/The First Movie (2001)": 1755304302.123,
  "tv/Best Show (2025)/S01E08": 1754684111.456,
  "tv/Best Show (2025)/S01E09": 1754684112.789,
  "tv/Best Show (2025)/S01E10-E11": 1754684113.654
}
```

The index is loaded into memory when the program starts, and saved after each scan that adds new entries. It is also saved once at startup, so a misconfigured index location is reported immediately.

On the first run, every existing movie and episode is indexed with its current modification time. Timestamps lost to upgrades before Date Restore was installed are not recovered.

Entries are never removed. If a movie or episode is deleted and later added again, it gets its original timestamp back.

### Periodic scan

The filesystem is scanned at startup and then every `SCAN_INTERVAL` seconds (300s by default).

For each video file:

* If it is not in the index, its current modification time is recorded.
* If it is already in the index, its modification time is restored from the index.

The periodic scan is the **only mechanism that adds entries to the index**.

This is intentional. It prevents a newly imported file from establishing a new timestamp through a race with the filesystem watcher.

### Filesystem watcher

The inotify watcher handles changes immediately.

When a video file is created, moved into a watched directory, or closed after being written:

* If the movie/episode exists in the index, its original modification time is restored.
* If it does not exist in the index, nothing is done.

The next periodic scan will discover and index new media.

Handling files when they are closed means files that are copied in, or overwritten in place, are restored as soon as writing finishes.

A media directory that does not exist at startup is watched once it appears, from the next periodic scan.

inotify does not report changes made by other machines to a network filesystem such as NFS or SMB. On those, only the periodic scan applies.

The watcher uses one inotify watch per directory. Very large libraries can exceed the host's `fs.inotify.max_user_watches` limit. Date Restore then exits with a message naming the setting to raise.

## Environment Variables

### `MOVIES_DIR`

Root directory containing movies.

Default: `/media/movies`

### `TV_DIR`

Root directory containing TV shows.

Default: `/media/tv`

### `INDEX_FILE`

Path to the JSON index.

Default: `/media/media-timestamps.json`

The directory containing the index must be writable, because the index is saved by writing a temporary file next to it and renaming it into place. Do not mount the index file on its own as a Docker volume; mount its directory instead.

### `SCAN_INTERVAL`

Number of seconds between filesystem scans. Must be a whole number, 1 or more.

Default: `300`

### `VIDEO_EXTENSIONS`

Comma-separated list of video file extensions. Matching is case-insensitive.

Default: `mkv,mp4`

The value can be specified with or without the leading period.

For example:

```text
VIDEO_EXTENSIONS=mkv,mp4
```

or:

```text
VIDEO_EXTENSIONS=.mkv,.mp4
```

Both result in:

```text
.mkv
.mp4
```

Additional formats can be configured:

```text
VIDEO_EXTENSIONS=mkv,mp4,m4v,avi
```


## Docker

The latest Docker image is automatically built for `linux/amd64` and `linux/arm64` and pushed to both of these registries:

```text
ghcr.io/reflectivecode/date-restore:latest
```

and

```text
reflectivecode/date-restore:latest
```

The container needs access to the media directories because it reads and modifies their filesystem timestamps. It does not need any network access, and can run with a read-only filesystem as long as it can write to the media files and the index file's directory.

### Permissions

Setting a file's modification time to a specific value requires **owning the file**. Write permission, including group write permission, is not enough.

Run the container as the user that owns the media files, which is usually the user that Sonarr and Radarr run as. The image runs as root unless a user is specified, so specifying one is recommended.

For example:

```bash
docker run -d \
  --name date-restore \
  --restart unless-stopped \
  --read-only \
  --network=none \
  --user "$(id -u):$(id -g)" \
  -e MOVIES_DIR=/media/movies \
  -e TV_DIR=/media/tv \
  -e INDEX_FILE=/media/media-timestamps.json \
  -e SCAN_INTERVAL=300 \
  -e VIDEO_EXTENSIONS=mkv,mp4 \
  -v /host_path/media:/media \
  ghcr.io/reflectivecode/date-restore:latest
```

Replace `$(id -u):$(id -g)` with the UID and GID of the media files' owner if they are owned by a different user.

Files Date Restore cannot change are logged and skipped. See [Logs](#logs).

## Docker Compose

Example `compose.yml`:

```yaml
services:
  date-restore:
    image: ghcr.io/reflectivecode/date-restore:latest
    container_name: date-restore
    restart: unless-stopped
    read_only: true
    network_mode: none
    user: "${PUID}:${PGID}"
    environment:
      MOVIES_DIR: /media/movies
      TV_DIR: /media/tv
      INDEX_FILE: /media/media-timestamps.json
      SCAN_INTERVAL: "300"
      VIDEO_EXTENSIONS: mkv,mp4

    volumes:
      - /host_path/media:/media

```
Create a `.env` file next to the Compose file:

```text
PUID=1000
PGID=1000
```

Replace those values with the UID and GID of the user that owns the media files.

## Running Without Docker

Date Restore requires Python 3.9 or later on Linux.

```bash
pip install -r requirements.txt
MOVIES_DIR=/path/to/movies TV_DIR=/path/to/tv INDEX_FILE=/path/to/media-timestamps.json python3 date-restore.py
```

## Development

The tests require Linux, and run against the real filesystem and inotify watcher.

```bash
pip install -r requirements-dev.txt
pytest
```

Some permission tests are skipped when running as root.

## Logs

Each scan ends with a summary:

```text
Scan complete: 1250 found, 2 new, 1 restored, 1245 kept, 2 failed, 1250 indexed
```

* `found`: video files matching the expected structure
* `new`: files added to the index
* `restored`: files whose modification time was restored
* `kept`: files whose modification time already matched the index
* `failed`: files whose modification time could not be restored
* `indexed`: total entries in the index

Problems with individual files or directories, such as a file owned by another user or an unreadable directory, are logged and skipped. They do not stop the program.

Control characters in filenames are logged as escape sequences such as `\x0a`, so each message is always a single line.

Date Restore exits with an error message for configuration problems:

* The index file is corrupt or contains a value that is not a timestamp. Fix or restore the file, or delete it to rebuild the index from current modification times.
* The index file cannot be written. Check that its directory is writable and that the file is not mounted on its own.
* The inotify watch or instance limit is reached.
* `SCAN_INTERVAL` is not a whole number of 1 or more.

## Important Notes

### Modification time, not creation time

Date Restore does **not** modify the filesystem creation/birth time.

Instead, it records the original file's **modification time** (`mtime`) and restores that timestamp when the media is upgraded.

The filesystem creation time remains unchanged.

This works with Jellyfin's "Use file creation date" setting because .NET, which Jellyfin is built on, does not read the birth time on Linux. It instead reports the earlier of the file's modification time and status change time (`ctime`). Restoring the modification time sets the status change time to the current time, so the restored modification time is the one reported.

### New media

New media is deliberately not handled immediately by the inotify watcher.

For example:

```text
New movie file added
        ↓
inotify detects it
        ↓
not in index
        ↓
nothing happens
        ↓
periodic scan
        ↓
movie is added to index
```

This prevents a race where the watcher could accidentally establish an incorrect timestamp.

### Upgraded media

For an existing movie:

```text
Original:
Movie (2025) - [1080p][x264]-Group.mkv
mtime = 2025-08-16 00:31:42 UTC

          ↓ Upgrade

New:
Movie (2025) - [2160p][x265]-OtherGroup.mkv
mtime = 2026-08-10 01:20:00 UTC

          ↓ Date Restore

New:
Movie (2025) - [2160p][x265]-OtherGroup.mkv
mtime = 2025-08-16 00:31:42 UTC
```

The index remains:

```json
{
  "movies/Movie (2025)": 1755304302.123
}
```

### Other changes to indexed files

The index records one timestamp per movie or episode, and every change to an indexed file's modification time is reverted, not only upgrades. For example, editing a file's tags in place also has its modification time restored.

Renaming a movie or show directory creates a new index entry, so the renamed media is indexed with its current modification time.
