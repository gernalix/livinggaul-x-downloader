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

## Versioning

- Current version: `0.1.3`
- Prompt: `#392814`
- Script first line keeps the version marker.

## Data Policy

The repo contains code and docs only. Video files, logs, the yt-dlp archive, and
SQLite state are intentionally outside the repo.
