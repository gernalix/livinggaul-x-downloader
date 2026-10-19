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

When a post embeds a video from another X account, version 0.4.3 falls back
to public post metadata from `api.fxtwitter.com` to resolve the quoted video.
Only public post IDs are sent to this third party; browser cookies are not sent.
Video files are downloaded from `video.twimg.com`. The best MP4 at or below
480 pixels in height is preferred; larger-only videos are downscaled using
an available Fedora-compatible FFmpeg encoder.

For a full historical timeline, gallery-dl needs an authenticated X browser
session. Without usable cookies, the public X HTML fallback may expose only
the most recent few posts. Use `--url-file` for additional known post links.

## Content deduplication

Version 0.4.2 also deduplicates videos independently of the X post URL:

- before download it extracts a stable key from `video.twimg.com` asset URLs when available;
- after download it checks SHA-256 for byte-identical files;
- as a conservative fallback it compares five perceptual dHash frame signatures plus duration/luminance statistics, so straightforward X re-encodes can be recognized;
- when a duplicate is detected after transfer, the new file is deleted and the database row points to the canonical existing download;
- `--reindex-content` backfills these identifiers for files downloaded by older versions.

File size is stored as metadata but is not treated as a unique identifier.

## Metadata catalog

Version 0.4.2 expands the SQLite history into a metadata catalog. Each processed X video can store:

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
- `duplicate_count`, the number of duplicate X posts/videos that were detected and ignored in favor of that canonical downloaded file;
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

Duplicate counts are derived from the persistent `skipped_duplicate` rows linked through `duplicate_of_id`, so they stay consistent with the deduplication history without maintaining a second counter. Example:

```sql
SELECT
  final_filename,
  duplicate_count
FROM video_catalog
WHERE status = 'downloaded'
ORDER BY duplicate_count DESC;
```

### Source availability percentage

Version 0.4.2 can also check whether the original X link for each already-downloaded video still exposes a downloadable video:

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

### Hourly Fedora systemd timer

The repo includes a resilient **systemd user service + timer** for this exact availability check:

```bash
systemd/install-user-service.sh
```

The installer:

- installs the units under `~/.config/systemd/user/`;
- enables an hourly calendar timer with `Persistent=true`, so a missed run while the machine is off is caught after startup;
- enables systemd **linger** for the user, so the timer continues without an active graphical/login session;
- runs one smoke execution immediately;
- keeps output in the systemd journal.

The service uses a filesystem lock to prevent overlapping availability runs, retries failed executions after 5 minutes with systemd rate limiting, kills a hung run after 50 minutes, and checks never-seen/least-recently-checked rows first so partial runs eventually make progress.

Useful commands:

```bash
systemctl --user status livinggaul-x-source-availability.timer
systemctl --user status livinggaul-x-source-availability.service
systemctl --user list-timers livinggaul-x-source-availability.timer
journalctl --user -u livinggaul-x-source-availability.service
```

## Versioning

- Current version: `0.4.2`
- Prompt: `#392814`
- Script first line keeps the version marker.

## Data Policy

The repo contains code and docs only. Video files, logs, the yt-dlp archive, and
SQLite state are intentionally outside the repo.

## Oracle and Seagate deployment

After P4 cutover Oracle owns the writable catalog. Fedora's installed CLI
forwards status/run/retry/reindex to Oracle; the Fedora pull timer archives
verified files on Seagate and acknowledges them before Oracle cleanup.
Archived videos remain downloaded even when absent from Oracle staging.

See [runtime and recovery](docs/oracle-archive.md) and
[P4 acceptance evidence](docs/P4-task-state.md). Runtime data, video files and
Telegram credentials stay outside this repository.

## Download dei video linkati su Telegram

```bash
video_downloader CHAT_ID
video_downloader -- CHAT_ID  # usa questa forma anche per ID negativi
video_downloader --foreground CHAT_ID  # resta nel terminale corrente
```

`CHAT_ID` è un segnaposto: inserire l'ID numerico reale, positivo per un canale
oppure completo con prefisso `-100`. Il comando apre automaticamente un terminale
Fedora con la dashboard. Lettura Telegram, download e aggiornamento del catalogo
avvengono interamente su Oracle: Fedora deve restare disponibile solo per vedere
la finestra. La sessione e il lavoro remoto sono legati al comando avviato; non
è un nuovo servizio periodico né un job persistente dopo la chiusura.

Nella mini-app Comandi, **Scarica video da Telegram** mostra solo il campo
`chat_id`: **OK** apre lo stesso terminale. Occorrono la sessione grafica Fedora
attiva e l'accesso SSH `oracle-vm`; dal telefono la finestra compare su Fedora.
La ricevuta della mini-app conferma la richiesta di apertura del terminale:
l'esito dei download rimane nella dashboard e nel catalogo.

La dashboard mostra messaggi letti, link unici, video confermati finora,
scaricati, duplicati, errori/non video, titolo/link corrente e percentuale del
trasferimento in corso. Il progresso globale è la percentuale dei **link
elaborati**, non una stima dei byte complessivi ancora da scoprire. La percentuale
del download usa la dimensione fornita o stimata da yt-dlp; se manca resta ignota.

Riusa `/srv/livinggaul/repo/livinggaul-x-downloader`, il lock
`/home/livinggaul/runtime.lock`, lo staging e il catalogo principale
`/home/livinggaul/.local/state/livinggaul-x-downloader/download_history.sqlite`.
Gli altri siti supportati da yt-dlp usano lo stesso percorso di metadati,
ffprobe, hash e deduplicazione dei link X; hanno `tweet_id` NULL. Lo snapshot
JSON include `telegram_chat_id`. Non esegue la scoperta della timeline LivingGaul
e non invia notifiche: mantiene lo staging per la pipeline Oracle/Seagate.
Prima di ogni trasferimento controlla il link nel catalogo: salta i video già
scaricati e i link già classificati duplicati se il relativo file canonico è
presente o archiviato con ACK. Se il file manca, tenta il recupero. Per link nuovi
la deduplicazione per contenuto può richiedere il trasferimento. Per riprendere,
rilanciare lo stesso canale; gli errori vengono ritentati.

La cronologia è quella ancora accessibile all'account. Include link HTTP/HTTPS
nel testo, nelle didascalie, nei link nascosti e nei pulsanti URL; gli allegati
Telegram senza link non sono inclusi. Siti privati, non supportati o link senza
video possono fallire. Non usa cookie browser su Oracle.

Prima installazione: aggiungere `requirements-video-downloader.txt` al runtime
Python Oracle esistente. Il modulo viene installato separatamente in
`/srv/livinggaul/video-downloader/telegram_video_downloader.py` e il launcher in
`~/.local/libexec/video_downloader/video_downloader`, collegato in `~/.local/bin`.
Configurazione API privata: `/home/livinggaul/.config/video_downloader/telegram.json`
con `api_id` e `api_hash`; sessione dedicata in
`/home/livinggaul/.local/state/video_downloader/telegram.session` (permessi privati).
Non copiare sessioni account o salvare segreti nel repository.

```bash
video_downloader --login  # QR nel terminale, eventuale 2FA inserita solo lì
video_downloader --check  # presenza configurazione/sessione, senza rete Telegram
video_downloader --demo   # dashboard con dati fittizi, senza download o SQLite reale
```

Il launcher usa unità systemd transitorie con i limiti di risorse Oracle già
previsti e durata massima 24 ore. Il programma prende prima il lock della
sessione Telegram e poi quello del catalogo per i download. Ctrl+C interrompe;
la chiusura del terminale interrompe anche il processo remoto.
