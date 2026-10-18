# livinggaul-x-downloader

## Purpose and boundaries
This CLI downloads videos published or shared by the configured X profile, stores local files and history, and exposes a catalog for deduplication and source-availability checks. MegaVault owns project identity and runtime inventory; C2 owns work-item lifecycle. X/Nitter, yt-dlp, gallery-dl and the browser cookie store are external dependencies.

## Architecture and data
The `livinggaul-x-downloader` Python CLI discovers posts, downloads selected media, deduplicates content, and updates the local SQLite history. A user systemd timer can periodically check source availability without downloading video. See `README.md` and `dev/ai/PROJECT.md` for paths, schema, behavior and operations.

## Safe validation and data handling
Never commit videos, logs, cookies or the SQLite/archive state under the user's home directories. Do not read, copy, log or expose browser cookie values. Source validation must not contact X/Nitter, download media, inspect the real output directory, or enable/trigger the user timer. Unit tests use temporary databases. The systemd installer enables linger/timer and immediately starts a live source-availability probe; treat it as a separately gated host operation.
