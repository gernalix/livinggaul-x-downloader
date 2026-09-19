# livinggaul-x-downloader

Reusable downloader for videos posted or shared by `https://x.com/LivingGaul`.

It stores downloaded files under `~/Videos/LivingGaul_X/` and persistent state
under `~/.local/state/livinggaul-x-downloader/`. The SQLite history database is
`~/.local/state/livinggaul-x-downloader/download_history.sqlite`.

## Commands

```bash
livinggaul-x-downloader --run
livinggaul-x-downloader --dry-run
livinggaul-x-downloader --status
livinggaul-x-downloader --list-failed
livinggaul-x-downloader --retry-failed
livinggaul-x-downloader --reindex-content
livinggaul-x-downloader --check-source-availability
```

Cookie modes:

```bash
livinggaul-x-downloader --run --cookies-mode auto
livinggaul-x-downloader --run --cookies-mode firefox
livinggaul-x-downloader --run --cookies-mode none
livinggaul-x-downloader --cookies-mode manual --run
```

`auto` first probes with no login, then retries individual posts through
`yt-dlp --cookies-from-browser firefox`. No password, token, or cookie value is
stored in this project.

Discovery first tries `gallery-dl` against the X timeline with Firefox cookies,
then falls back to public X HTML and Nitter RSS/HTML. When X/Nitter blocks
profile discovery, put post URLs in a text file and run:

```bash
livinggaul-x-downloader --run --url-file urls.txt
```

The downloader still deduplicates through SQLite and the yt-dlp archive.

## Content deduplication

Version 0.4.0 also deduplicates videos independently of the X post URL:

- before download it extracts a stable key from `video.twimg.com` asset URLs when available;
- after download it checks SHA-256 for byte-identical files;
- as a conservative fallback it compares five perceptual dHash frame signatures plus duration/luminance statistics, so straightforward X re-encodes can be recognized;
- when a duplicate is detected after transfer, the new file is deleted and the database row points to the canonical existing download;
- `--reindex-content` backfills these identifiers for files downloaded by older versions.

File size is stored as metadata but is not treated as a unique identifier.

## Metadata catalog

Version 0.4.0 expands the SQLite history into a metadata catalog. Each processed X video can store:

- X/yt-dlp metadata: uploader, uploader id, post text, title, publication/upload time, and one static snapshot of views, likes, reposts, and comments when exposed by X;
- final-file metadata from `ffprobe`: width, height, exact FPS numerator/denominator, video/audio codecs and bitrates, container, pixel format, sample rate, channels, duration, and size;
- integrity/deduplication data: SHA-256, X media key, perceptual fingerprint, and canonical duplicate linkage;
- one immutable raw `yt-dlp --dump-single-json` snapshot in `metadata_snapshots`, preserving metadata that is not normalized into columns.

Social counters are intentionally static snapshots; the downloader does not refresh them later. Metadata unavailable from X or ffprobe remains NULL. `--reindex-content` backfills file-derived metadata for historical local files without making network requests.

### Readable SQL view

The database also exposes a `video_catalog` view. It keeps the normalized metadata together and adds convenient derived fields including:

- `download_date` and `published_date`;
- `size_mb` using decimal MB (1 MB = 1,000,000 bytes);
- `size_mib` using binary MiB (1 MiB = 1,048,576 bytes);
- `duration_seconds`, `duration_minutes`, `resolution`, `fps`;
- video/audio bitrate in kbps;
- the raw yt-dlp JSON snapshot.

Example: downloaded storage per day:

```sql
SELECT
  download_date,
  COUNT(*) AS videos,
  ROUND(SUM(size_mb), 2) AS downloaded_mb
FROM video_catalog
WHERE status = 'downloaded'
GROUP BY download_date
ORDER BY download_date;
```

Because `download_date` falls back to the row creation date when an older row has no explicit `downloaded_at`, migrated historical rows remain groupable.

### Source availability percentage

Version 0.4.0 can also check whether the original X link for each already-downloaded video still exposes a downloadable video:

```bash
livinggaul-x-downloader --check-source-availability
```

The check never downloads the video. A successful yt-dlp metadata probe with video formats is stored as `available`; recognized permanent conditions such as a deleted/unavailable post or HTTP 404 are stored as `unavailable`; rate limits, authentication problems, timeouts, and other ambiguous failures are stored as `unknown`.

The single-value view `source_unavailable_percentage` contains exactly one column:

```sql
SELECT unavailable_percentage
FROM source_unavailable_percentage;
```

Its value is:

```text
100 × downloaded rows currently marked unavailable / all downloaded rows
```

Rows that are `unknown` or have never been checked remain in the denominator but not the numerator. When there are no downloaded videos, the value is NULL rather than a misleading 0%. The value is only as fresh as the last `--check-source-availability` run.

## Versioning

- Current version: `0.4.0`
- Prompt: `#392814`
- Script first line keeps the version marker.

## Data Policy

The repo contains code and docs only. Video files, logs, the yt-dlp archive, and
SQLite state are intentionally outside the repo.
