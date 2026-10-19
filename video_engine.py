#!/usr/bin/env python3
# prompt=392814 shared-video recovery and Fedora encoder fallback
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import email.utils
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import signal
import uuid
from catalog_access import file_lock, lock_path
import livinggaul_pipeline as pipeline
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_nv_raw = (Path(__file__).resolve().parent / "NV.txt").read_bytes()
if not re.fullmatch(rb"[1-9][0-9]*\n", _nv_raw):
    raise ValueError("NV.txt must contain a positive integer followed by one LF")
VERSION = _nv_raw[:-1].decode("ascii")
PROFILE_URL = "https://x.com/LivingGaul"
SCREEN_NAME = "LivingGaul"
DEFAULT_OUTPUT_DIR = Path(os.environ.get("LIVINGGAUL_OUTPUT", str(Path.home() / "Videos" / "LivingGaul_X")))
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "livinggaul-x-downloader"
DEFAULT_DB_PATH = DEFAULT_STATE_DIR / "download_history.sqlite"
DEFAULT_ARCHIVE = DEFAULT_STATE_DIR / "yt-dlp-download-archive.txt"
DEFAULT_LOG_DIR = DEFAULT_STATE_DIR / "logs"
YTDLP = str(Path.home() / ".local" / "bin" / "yt-dlp")
FORMAT_480 = "bestvideo[height<=480]+bestaudio/best[height<=480]/best"
TWEET_RE = re.compile(
    r"(?:https?://(?:x|twitter)\.com/[^/\"'<>\s]+/status/)?(?P<id>\d{15,25})"
)
STATUS_HREF_RE = re.compile(
    r"(?:https?:)?//(?:x|twitter)\.com/(?P<user>[A-Za-z0-9_]+)/status/(?P<id>\d{15,25})"
    r"|href=[\"']/(?P<path_user>[A-Za-z0-9_]+)/status/(?P<path_id>\d{15,25})"
)
NITTER_INSTANCES = (
    "https://xcancel.com",
    "https://nitter.net",
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://nitter.tiekoetter.com",
)
X_MEDIA_URL_RE = re.compile(
    r"https?://video\.twimg\.com/(?P<kind>ext_tw_video|amplify_video|tweet_video)/(?P<asset>[^/?#]+)",
    re.IGNORECASE,
)
FINGERPRINT_PREFIX = "dhash16-v1"
FINGERPRINT_FRAME_COUNT = 5
SOURCE_UNAVAILABLE_PATTERNS = (
    "this tweet is unavailable",
    "this post is unavailable",
    "tweet is not available",
    "post is not available",
    "this content is not available",
    "no video could be found in this tweet",
    "no video could be found in this post",
    "does not exist",
    "has been deleted",
    "was deleted",
    "account is suspended",
    "user is suspended",
    "http error 404",
    "status code 404",
)


@dataclass(frozen=True)
class Candidate:
    tweet_id: str
    url: str
    published_at: str | None = None
    source: str = "unknown"
    source_context: str = ""
    source_references: tuple[str, ...] = ()


@dataclass
class Summary:
    posts_analyzed: int = 0
    videos_found: int = 0
    skipped_duplicate: int = 0
    downloaded: int = 0
    failed: int = 0
    dry_run_matches: int = 0
    skipped_previous_failed: int = 0


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def mkdirs(output_dir: Path, db_path: Path) -> None:
    DEFAULT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_ARCHIVE.touch(mode=0o600, exist_ok=True)
    with contextlib.suppress(PermissionError):
        os.chmod(DEFAULT_STATE_DIR, 0o700)
        os.chmod(DEFAULT_LOG_DIR, 0o700)
        os.chmod(db_path.parent, 0o700)


def connect_db(db_path: Path) -> sqlite3.Connection:
    with file_lock(Path(db_path).with_suffix('.schema.lock')):
        return _connect_db(db_path)


def _connect_db(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    if con.execute("PRAGMA user_version").fetchone()[0] > 6:
        con.close()
        raise RuntimeError("Catalog schema is newer than this engine")
    con.execute("BEGIN IMMEDIATE")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS downloads (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          original_url TEXT NOT NULL,
          direct_video_url TEXT,
          tweet_id TEXT,
          uploader TEXT,
          uploader_id TEXT,
          tweet_text TEXT,
          title TEXT,
          published_at TEXT,
          upload_timestamp INTEGER,
          downloaded_at TEXT,
          view_count INTEGER CHECK(view_count IS NULL OR view_count >= 0),
          like_count INTEGER CHECK(like_count IS NULL OR like_count >= 0),
          repost_count INTEGER CHECK(repost_count IS NULL OR repost_count >= 0),
          comment_count INTEGER CHECK(comment_count IS NULL OR comment_count >= 0),
          final_filename TEXT,
          file_path TEXT,
          sha256 TEXT,
          size_bytes INTEGER CHECK(size_bytes IS NULL OR size_bytes >= 0),
          media_key TEXT,
          content_fingerprint TEXT,
          duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms >= 0),
          duplicate_of_id INTEGER,
          width INTEGER CHECK(width IS NULL OR width > 0),
          height INTEGER CHECK(height IS NULL OR height > 0),
          fps_num INTEGER CHECK(fps_num IS NULL OR fps_num >= 0),
          fps_den INTEGER CHECK(fps_den IS NULL OR fps_den > 0),
          video_codec TEXT,
          audio_codec TEXT,
          video_bitrate INTEGER CHECK(video_bitrate IS NULL OR video_bitrate >= 0),
          audio_bitrate INTEGER CHECK(audio_bitrate IS NULL OR audio_bitrate >= 0),
          container_format TEXT,
          pixel_format TEXT,
          audio_sample_rate INTEGER CHECK(audio_sample_rate IS NULL OR audio_sample_rate > 0),
          audio_channels INTEGER CHECK(audio_channels IS NULL OR audio_channels > 0),
          source_availability TEXT CHECK(
            source_availability IS NULL OR source_availability IN ('available','unavailable','unknown')
          ),
          source_checked_at TEXT,
          source_check_error TEXT,
          quality TEXT,
          status TEXT NOT NULL CHECK(status IN ('downloaded','skipped_duplicate','failed')),
          error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(duplicate_of_id) REFERENCES downloads(id) ON DELETE SET NULL
        )
        """
    )
    existing_columns = {row["name"] for row in con.execute("PRAGMA table_info(downloads)")}
    migrations = {
        "media_key": "TEXT",
        "content_fingerprint": "TEXT",
        "duration_ms": "INTEGER",
        "duplicate_of_id": "INTEGER",
        "uploader": "TEXT",
        "uploader_id": "TEXT",
        "tweet_text": "TEXT",
        "title": "TEXT",
        "upload_timestamp": "INTEGER",
        "downloaded_at": "TEXT",
        "view_count": "INTEGER",
        "like_count": "INTEGER",
        "repost_count": "INTEGER",
        "comment_count": "INTEGER",
        "width": "INTEGER",
        "height": "INTEGER",
        "fps_num": "INTEGER",
        "fps_den": "INTEGER",
        "video_codec": "TEXT",
        "audio_codec": "TEXT",
        "video_bitrate": "INTEGER",
        "audio_bitrate": "INTEGER",
        "container_format": "TEXT",
        "pixel_format": "TEXT",
        "audio_sample_rate": "INTEGER",
        "audio_channels": "INTEGER",
        "source_availability": "TEXT",
        "source_checked_at": "TEXT",
        "source_check_error": "TEXT",
    }
    for column, ddl in migrations.items():
        if column not in existing_columns:
            con.execute(f"ALTER TABLE downloads ADD COLUMN {column} {ddl}")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS metadata_snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          download_id INTEGER NOT NULL,
          captured_at TEXT NOT NULL,
          source TEXT NOT NULL DEFAULT 'yt-dlp',
          metadata_json TEXT NOT NULL,
          FOREIGN KEY(download_id) REFERENCES downloads(id) ON DELETE CASCADE,
          CHECK(json_valid(metadata_json)),
          UNIQUE(download_id, source)
        )
        """
    )
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_downloads_original_url ON downloads(original_url)")
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_downloads_tweet_downloaded "
        "ON downloads(tweet_id) WHERE tweet_id IS NOT NULL AND status='downloaded'"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_downloads_status_updated ON downloads(status, updated_at)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_downloads_media_key ON downloads(media_key) "
        "WHERE media_key IS NOT NULL AND status='downloaded'"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_downloads_sha256 ON downloads(sha256) "
        "WHERE sha256 IS NOT NULL AND status='downloaded'"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_downloads_fingerprint_duration "
        "ON downloads(duration_ms, content_fingerprint) "
        "WHERE content_fingerprint IS NOT NULL AND status='downloaded'"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_downloads_duplicate_of ON downloads(duplicate_of_id) "
        "WHERE duplicate_of_id IS NOT NULL"
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_downloads_uploader ON downloads(uploader)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_downloads_uploader_id ON downloads(uploader_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_downloads_published ON downloads(published_at DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_downloads_downloaded_at ON downloads(downloaded_at DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_downloads_created_at ON downloads(created_at DESC)")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_downloads_source_availability "
        "ON downloads(status, source_availability)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_metadata_download_captured "
        "ON metadata_snapshots(download_id, captured_at DESC)"
    )
    pipeline.migrate(con)
    con.execute("""CREATE TABLE IF NOT EXISTS acquisition_sources (
        download_id INTEGER NOT NULL REFERENCES downloads(id),
        input TEXT NOT NULL, context TEXT NOT NULL DEFAULT '', reference TEXT NOT NULL,
        discovered_at TEXT NOT NULL,
        PRIMARY KEY(download_id,input,context,reference))""")
    con.execute("""INSERT OR IGNORE INTO acquisition_sources
        SELECT d.id,
          CASE WHEN json_extract(m.metadata_json,'$.telegram_chat_id') IS NOT NULL THEN 'telegram'
               WHEN d.original_url LIKE 'https://x.com/%' OR d.original_url LIKE 'https://twitter.com/%' THEN 'x'
               ELSE 'legacy' END,
          COALESCE(CAST(json_extract(m.metadata_json,'$.telegram_chat_id') AS TEXT),''),
          d.original_url,d.created_at
        FROM downloads d LEFT JOIN metadata_snapshots m ON m.download_id=d.id AND m.source='yt-dlp'
        WHERE NOT EXISTS (SELECT 1 FROM acquisition_sources a WHERE a.download_id=d.id)""")
    con.execute("""CREATE VIEW IF NOT EXISTS video_origins AS
        SELECT COALESCE(d.duplicate_of_id,d.id) video_id, d.id source_download_id,
               d.original_url, a.input, a.context, a.reference, a.discovered_at
        FROM downloads d JOIN acquisition_sources a ON a.download_id=d.id""")
    con.execute("DROP VIEW IF EXISTS video_catalog")
    con.execute(
        """
        CREATE VIEW video_catalog AS
        SELECT
          d.id,
          d.status,
          substr(COALESCE(d.downloaded_at, d.created_at), 1, 10) AS download_date,
          d.downloaded_at,
          substr(d.published_at, 1, 10) AS published_date,
          d.published_at,
          d.upload_timestamp,
          d.uploader,
          d.uploader_id,
          d.tweet_id,
          d.title,
          d.tweet_text,
          d.original_url,
          d.direct_video_url,
          d.final_filename,
          d.file_path,
          d.storage_state, d.archive_path, d.archive_ack_at,
          d.size_bytes,
          CASE WHEN d.size_bytes IS NULL THEN NULL ELSE d.size_bytes / 1000000.0 END AS size_mb,
          CASE WHEN d.size_bytes IS NULL THEN NULL ELSE d.size_bytes / 1048576.0 END AS size_mib,
          d.duration_ms,
          CASE WHEN d.duration_ms IS NULL THEN NULL ELSE d.duration_ms / 1000.0 END AS duration_seconds,
          CASE WHEN d.duration_ms IS NULL THEN NULL ELSE d.duration_ms / 60000.0 END AS duration_minutes,
          d.width,
          d.height,
          CASE
            WHEN d.width IS NULL OR d.height IS NULL THEN NULL
            ELSE CAST(d.width AS TEXT) || 'x' || CAST(d.height AS TEXT)
          END AS resolution,
          d.fps_num,
          d.fps_den,
          CASE
            WHEN d.fps_num IS NULL OR d.fps_den IS NULL OR d.fps_den = 0 THEN NULL
            ELSE CAST(d.fps_num AS REAL) / d.fps_den
          END AS fps,
          d.video_codec,
          d.audio_codec,
          d.video_bitrate,
          CASE WHEN d.video_bitrate IS NULL THEN NULL ELSE d.video_bitrate / 1000.0 END AS video_bitrate_kbps,
          d.audio_bitrate,
          CASE WHEN d.audio_bitrate IS NULL THEN NULL ELSE d.audio_bitrate / 1000.0 END AS audio_bitrate_kbps,
          d.container_format,
          d.pixel_format,
          d.audio_sample_rate,
          d.audio_channels,
          d.source_availability,
          d.source_checked_at,
          d.source_check_error,
          d.view_count,
          d.like_count,
          d.repost_count,
          d.comment_count,
          d.quality,
          d.media_key,
          d.sha256,
          d.content_fingerprint,
          d.duplicate_of_id,
          CASE WHEN d.duplicate_of_id IS NULL THEN 0 ELSE 1 END AS is_duplicate,
          (
            SELECT COUNT(*)
            FROM downloads AS duplicate
            WHERE duplicate.status='skipped_duplicate'
              AND duplicate.duplicate_of_id=d.id
          ) AS duplicate_count,
          d.error,
          d.created_at,
          d.updated_at,
          m.captured_at AS metadata_captured_at,
          m.metadata_json AS yt_dlp_metadata_json
        FROM downloads AS d
        LEFT JOIN metadata_snapshots AS m
          ON m.download_id = d.id
         AND m.source = 'yt-dlp'
        """
    )
    con.execute("DROP VIEW IF EXISTS source_unavailable_percentage")
    con.execute(
        """
        CREATE VIEW source_unavailable_percentage AS
        SELECT
          CASE
            WHEN COUNT(*) = 0 THEN NULL
            ELSE ROUND(
              100.0 * SUM(CASE WHEN source_availability='unavailable' THEN 1 ELSE 0 END)
              / COUNT(*),
              2
            )
          END AS unavailable_percentage
        FROM downloads
        WHERE status='downloaded'
        """
    )
    con.execute("""CREATE VIEW IF NOT EXISTS video_library AS
        SELECT v.*,
          (SELECT COUNT(*) FROM video_origins o WHERE o.video_id=v.id) AS origin_count
        FROM video_catalog v WHERE v.status='downloaded'""")
    con.execute("PRAGMA user_version=6")
    con.commit()
    with contextlib.suppress(PermissionError):
        os.chmod(db_path, 0o600)
    return con


def run(cmd: list[str], timeout: int = 180, *, text: bool = True) -> subprocess.CompletedProcess[str]:
    if os.environ.get("LIVINGGAUL_MANAGED") != "1":
        return subprocess.run(cmd, text=text, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=timeout)
    pipeline.resource_check(DEFAULT_OUTPUT_DIR)
    process = subprocess.Popen(cmd, text=text, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=5)
                return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                pipeline.resource_check(DEFAULT_OUTPUT_DIR)
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(cmd, timeout)
    except BaseException:
        os.killpg(process.pid, signal.SIGTERM)
        try: process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL); process.communicate()
        raise


def tool_path(name: str) -> str:
    if name == "gallery-dl":
        venv_gallery_dl = Path.home() / ".local" / "share" / "livinggaul-x-downloader-venv" / "bin" / "gallery-dl"
        if venv_gallery_dl.exists():
            return str(venv_gallery_dl)
    if name == "yt-dlp" and Path(YTDLP).exists():
        return YTDLP
    found = shutil.which(name)
    if not found:
        raise SystemExit(f"ERROR: required tool not found: {name}")
    return found


def log_path() -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return DEFAULT_LOG_DIR / f"run-{stamp}.log"


def log(message: str, logfile: Path | None) -> None:
    line = f"[{utc_now()}] {message}"
    print(line, file=sys.stderr)
    if logfile:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        with logfile.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def browser_cookie_args(mode: str, firefox_profile: str | None = None) -> list[str]:
    if mode in {"none", "manual"}:
        return []
    if mode not in {"auto", "firefox"}:
        raise SystemExit("ERROR: --cookies-mode must be auto, firefox, manual, or none")
    if firefox_profile:
        return ["--cookies-from-browser", f"firefox:{firefox_profile}"]
    return ["--cookies-from-browser", "firefox"]


def http_get(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:151.0) Gecko/20100101 Firefox/151.0",
            "Accept": "text/html,application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = response.read(3_000_000)
        charset = response.headers.get_content_charset() or "utf-8"
    return data.decode(charset, errors="replace")


def canonical_candidate(tweet_id: str, source: str, published_at: str | None = None) -> Candidate:
    return Candidate(
        tweet_id=tweet_id,
        url=f"https://x.com/{SCREEN_NAME}/status/{tweet_id}",
        published_at=published_at,
        source=source,
    )


def add_candidate(out: dict[str, Candidate], tweet_id: str | None, source: str, published_at: str | None = None) -> None:
    if not tweet_id:
        return
    if not re.fullmatch(r"\d{15,25}", tweet_id):
        return
    out.setdefault(tweet_id, canonical_candidate(tweet_id, source, published_at))


def parse_status_ids(text: str, source: str) -> dict[str, Candidate]:
    out: dict[str, Candidate] = {}
    for match in STATUS_HREF_RE.finditer(text):
        user = match.group("user") or match.group("path_user")
        tweet_id = match.group("id") or match.group("path_id")
        if user and user.lower() == SCREEN_NAME.lower():
            add_candidate(out, tweet_id, source)
    return out


def parse_rss(text: str, source: str) -> dict[str, Candidate]:
    out: dict[str, Candidate] = {}
    item_re = re.compile(r"<item\b.*?</item>", re.IGNORECASE | re.DOTALL)
    link_re = re.compile(r"<link>(.*?)</link>", re.IGNORECASE | re.DOTALL)
    date_re = re.compile(r"<pubDate>(.*?)</pubDate>", re.IGNORECASE | re.DOTALL)
    for item in item_re.findall(text):
        link = link_re.search(item)
        date = date_re.search(item)
        if not link:
            continue
        ids = TWEET_RE.findall(link.group(1))
        published_at = None
        if date:
            with contextlib.suppress(Exception):
                published_at = email.utils.parsedate_to_datetime(date.group(1)).astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
        for tweet_id in ids:
            add_candidate(out, tweet_id, source, published_at)
    return out


def row_has_existing_file(row: sqlite3.Row) -> bool:
    file_path = row["file_path"]
    return pipeline.archived_valid(row) or bool(file_path and Path(file_path).is_file())


def downloaded_by_media_key(con: sqlite3.Connection, media_key: str) -> sqlite3.Row | None:
    rows = con.execute(
        """
        SELECT * FROM downloads
        WHERE status='downloaded' AND media_key=?
        ORDER BY updated_at DESC
        """,
        (media_key,),
    ).fetchall()
    return next((row for row in rows if row_has_existing_file(row)), None)


def downloaded_by_sha256(con: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    rows = con.execute(
        """
        SELECT * FROM downloads
        WHERE status='downloaded' AND sha256=?
        ORDER BY updated_at DESC
        """,
        (sha256,),
    ).fetchall()
    return next((row for row in rows if row_has_existing_file(row)), None)


def already_downloaded(con: sqlite3.Connection, candidate: Candidate) -> sqlite3.Row | None:
    row = con.execute(
        """
        SELECT * FROM downloads
        WHERE status='downloaded'
          AND ((tweet_id IS NOT NULL AND tweet_id=?) OR original_url=?)
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (candidate.tweet_id, candidate.url),
    ).fetchone()
    if not row:
        row = con.execute("""SELECT canonical.* FROM downloads source
            JOIN downloads canonical ON canonical.id=source.duplicate_of_id
            WHERE source.original_url=? AND canonical.status='downloaded'""",
            (candidate.url,)).fetchone()
    if not row:
        return None
    if row_has_existing_file(row):
        return row
    return None


def previous_failed(con: sqlite3.Connection, candidate: Candidate) -> sqlite3.Row | None:
    return con.execute(
        """
        SELECT * FROM downloads
        WHERE status='failed'
          AND ((tweet_id IS NOT NULL AND tweet_id=?) OR original_url=?)
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (candidate.tweet_id, candidate.url),
    ).fetchone()


def source_metadata_fields(meta: dict[str, Any] | None, candidate: Candidate) -> dict[str, Any]:
    if not meta:
        return {"published_at": candidate.published_at}

    def as_text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def as_int(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None

    timestamp = as_int(meta.get("timestamp") or meta.get("release_timestamp"))
    published_at = candidate.published_at
    if not published_at and timestamp is not None:
        with contextlib.suppress(ValueError, OSError, OverflowError):
            published_at = dt.datetime.fromtimestamp(timestamp, dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    return {
        "uploader": as_text(meta.get("uploader") or meta.get("channel")),
        "uploader_id": as_text(meta.get("uploader_id") or meta.get("channel_id")),
        "tweet_text": as_text(meta.get("description")),
        "title": as_text(meta.get("title")),
        "published_at": published_at,
        "upload_timestamp": timestamp,
        "view_count": as_int(meta.get("view_count")),
        "like_count": as_int(meta.get("like_count")),
        "repost_count": as_int(meta.get("repost_count") if meta.get("repost_count") is not None else meta.get("retweet_count")),
        "comment_count": as_int(meta.get("comment_count") if meta.get("comment_count") is not None else meta.get("reply_count")),
    }


FILE_METADATA_COLUMNS = (
    "width",
    "height",
    "fps_num",
    "fps_den",
    "video_codec",
    "audio_codec",
    "video_bitrate",
    "audio_bitrate",
    "container_format",
    "pixel_format",
    "audio_sample_rate",
    "audio_channels",
)


def row_file_metadata(row: sqlite3.Row) -> dict[str, Any]:
    return {name: row[name] for name in FILE_METADATA_COLUMNS}


def record_origin(con, candidate):
    for reference in candidate.source_references or (candidate.url,):
        con.execute("""INSERT OR IGNORE INTO acquisition_sources
            (download_id,input,context,reference,discovered_at)
            SELECT id,?,?,?,? FROM downloads WHERE original_url=?""",
            ('telegram' if candidate.source == 'telegram' else 'x',
             candidate.source_context, reference, utc_now(), candidate.url))


def upsert_status(
    con: sqlite3.Connection,
    candidate: Candidate,
    status: str,
    *,
    direct_video_url: str | None = None,
    final_filename: str | None = None,
    file_path: str | None = None,
    sha256: str | None = None,
    size_bytes: int | None = None,
    media_key: str | None = None,
    content_fingerprint: str | None = None,
    duration_ms: int | None = None,
    duplicate_of_id: int | None = None,
    quality: str | None = None,
    error: str | None = None,
    source_metadata: dict[str, Any] | None = None,
    file_metadata: dict[str, Any] | None = None,
    downloaded_at: str | None = None,
) -> None:
    now = utc_now()
    source = source_metadata_fields(source_metadata, candidate)
    file_meta = file_metadata or {}
    con.execute(
        """
        INSERT INTO downloads (
          original_url, direct_video_url, tweet_id,
          uploader, uploader_id, tweet_text, title, published_at, upload_timestamp,
          downloaded_at, view_count, like_count, repost_count, comment_count,
          final_filename, file_path, sha256, size_bytes, media_key, content_fingerprint,
          duration_ms, duplicate_of_id, width, height, fps_num, fps_den, video_codec,
          audio_codec, video_bitrate, audio_bitrate, container_format, pixel_format,
          audio_sample_rate, audio_channels, quality, status, error, created_at, updated_at
        ) VALUES (
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(original_url) DO UPDATE SET
          direct_video_url=COALESCE(excluded.direct_video_url, downloads.direct_video_url),
          tweet_id=COALESCE(excluded.tweet_id, downloads.tweet_id),
          uploader=COALESCE(downloads.uploader, excluded.uploader),
          uploader_id=COALESCE(downloads.uploader_id, excluded.uploader_id),
          tweet_text=COALESCE(downloads.tweet_text, excluded.tweet_text),
          title=COALESCE(downloads.title, excluded.title),
          published_at=COALESCE(downloads.published_at, excluded.published_at),
          upload_timestamp=COALESCE(downloads.upload_timestamp, excluded.upload_timestamp),
          downloaded_at=COALESCE(downloads.downloaded_at, excluded.downloaded_at),
          view_count=COALESCE(downloads.view_count, excluded.view_count),
          like_count=COALESCE(downloads.like_count, excluded.like_count),
          repost_count=COALESCE(downloads.repost_count, excluded.repost_count),
          comment_count=COALESCE(downloads.comment_count, excluded.comment_count),
          final_filename=COALESCE(excluded.final_filename, downloads.final_filename),
          file_path=COALESCE(excluded.file_path, downloads.file_path),
          sha256=COALESCE(excluded.sha256, downloads.sha256),
          size_bytes=COALESCE(excluded.size_bytes, downloads.size_bytes),
          media_key=COALESCE(excluded.media_key, downloads.media_key),
          content_fingerprint=COALESCE(excluded.content_fingerprint, downloads.content_fingerprint),
          duration_ms=COALESCE(excluded.duration_ms, downloads.duration_ms),
          duplicate_of_id=excluded.duplicate_of_id,
          width=COALESCE(excluded.width, downloads.width),
          height=COALESCE(excluded.height, downloads.height),
          fps_num=COALESCE(excluded.fps_num, downloads.fps_num),
          fps_den=COALESCE(excluded.fps_den, downloads.fps_den),
          video_codec=COALESCE(excluded.video_codec, downloads.video_codec),
          audio_codec=COALESCE(excluded.audio_codec, downloads.audio_codec),
          video_bitrate=COALESCE(excluded.video_bitrate, downloads.video_bitrate),
          audio_bitrate=COALESCE(excluded.audio_bitrate, downloads.audio_bitrate),
          container_format=COALESCE(excluded.container_format, downloads.container_format),
          pixel_format=COALESCE(excluded.pixel_format, downloads.pixel_format),
          audio_sample_rate=COALESCE(excluded.audio_sample_rate, downloads.audio_sample_rate),
          audio_channels=COALESCE(excluded.audio_channels, downloads.audio_channels),
          quality=COALESCE(excluded.quality, downloads.quality),
          status=excluded.status,
          error=excluded.error,
          updated_at=excluded.updated_at
        """,
        (
            candidate.url,
            direct_video_url,
            candidate.tweet_id,
            source.get("uploader"),
            source.get("uploader_id"),
            source.get("tweet_text"),
            source.get("title"),
            source.get("published_at"),
            source.get("upload_timestamp"),
            downloaded_at,
            source.get("view_count"),
            source.get("like_count"),
            source.get("repost_count"),
            source.get("comment_count"),
            final_filename,
            file_path,
            sha256,
            size_bytes,
            media_key,
            content_fingerprint,
            duration_ms,
            duplicate_of_id,
            file_meta.get("width"),
            file_meta.get("height"),
            file_meta.get("fps_num"),
            file_meta.get("fps_den"),
            file_meta.get("video_codec"),
            file_meta.get("audio_codec"),
            file_meta.get("video_bitrate"),
            file_meta.get("audio_bitrate"),
            file_meta.get("container_format"),
            file_meta.get("pixel_format"),
            file_meta.get("audio_sample_rate"),
            file_meta.get("audio_channels"),
            quality,
            status,
            error,
            now,
            now,
        ),
    )
    if source_metadata is not None:
        row = con.execute("SELECT id FROM downloads WHERE original_url=?", (candidate.url,)).fetchone()
        if row:
            con.execute(
                """
                INSERT INTO metadata_snapshots(download_id, captured_at, source, metadata_json)
                VALUES (?, ?, 'yt-dlp', ?)
                ON CONFLICT(download_id, source) DO NOTHING
                """,
                (
                    row["id"],
                    now,
                    json.dumps(source_metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                ),
            )
    if os.environ.get("LIVINGGAUL_MANAGED") == "1":
        row = con.execute("SELECT * FROM downloads WHERE original_url=?", (candidate.url,)).fetchone()
        if status == "downloaded":
            if candidate.source == 'telegram':
                con.execute("UPDATE downloads SET storage_state='staged' WHERE id=?", (row['id'],))
            else:
                pipeline.record_staged(con, row, source_metadata)
        elif status == "failed":
            con.execute("UPDATE downloads SET storage_state='failed' WHERE id=?", (row['id'],))
    record_origin(con, candidate)
    con.commit()


def record_duplicate(
    con: sqlite3.Connection,
    candidate: Candidate,
    duplicate: sqlite3.Row,
    *,
    direct_video_url: str | None,
    media_key: str | None,
    source_metadata: dict[str, Any],
    content_fingerprint: str | None = None,
    duration_ms: int | None = None,
    reason: str,
) -> None:
    upsert_status(
        con,
        candidate,
        "skipped_duplicate",
        direct_video_url=direct_video_url,
        final_filename=duplicate["final_filename"],
        file_path=duplicate["file_path"],
        sha256=duplicate["sha256"],
        size_bytes=duplicate["size_bytes"],
        media_key=media_key or duplicate["media_key"],
        content_fingerprint=content_fingerprint or duplicate["content_fingerprint"],
        duration_ms=duration_ms or duplicate["duration_ms"],
        duplicate_of_id=duplicate["id"],
        file_metadata=row_file_metadata(duplicate),
        source_metadata=source_metadata,
        quality=duplicate["quality"],
        error=reason,
    )


def ytdlp_base_args(args: argparse.Namespace, *, use_cookies: bool = True) -> list[str]:
    cmd = [tool_path("yt-dlp"), "--no-playlist", "--retries", "5", "--fragment-retries", "5", "--sleep-requests", "1"]
    if os.environ.get("LIVINGGAUL_MANAGED") == "1":
        cmd.extend(["--max-filesize", "512M"])
    if use_cookies:
        cmd.extend(browser_cookie_args(args.cookies_mode, args.firefox_profile))
    return cmd


def fxtwitter_video_url(post: dict[str, Any]) -> str | None:
    """Choose the best MP4 at <=480 pixels high, or the smallest larger MP4."""
    media = post.get("media") or {}
    if not isinstance(media, dict):
        return None
    for video in media.get("videos") or []:
        if not isinstance(video, dict):
            continue
        choices: list[tuple[int, int, int, str]] = []
        formats = video.get("formats") or video.get("variants") or []
        for fmt in [*formats, video]:
            if not isinstance(fmt, dict):
                continue
            url = fmt.get("url")
            if not isinstance(url, str):
                continue
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname != "video.twimg.com" or not parsed.path.lower().endswith(".mp4"):
                continue
            size = re.search(r"/(\d+)x(\d+)/", parsed.path)
            height = int(size.group(2)) if size else 0
            width = int(size.group(1)) if size else 0
            if height and height <= 480:
                choices.append((0, -height, -width, url))
            elif height:
                choices.append((1, height, width, url))
            else:
                choices.append((2, 0, 0, url))
        if choices:
            return min(choices)[3]
    return None


def extract_fxtwitter_metadata(candidate: Candidate, logfile: Path | None) -> dict[str, Any]:
    """Resolve public videos embedded in a LivingGaul post, including quoted posts."""
    url = f"https://api.fxtwitter.com/{SCREEN_NAME}/status/{candidate.tweet_id}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "livinggaul-x-downloader/0.4"})
    with urllib.request.urlopen(req, timeout=25) as response:
        payload = json.loads(response.read(2_000_000))
    tweet = payload.get("tweet") if isinstance(payload, dict) else None
    if not isinstance(tweet, dict) or str(tweet.get("id")) != candidate.tweet_id:
        raise RuntimeError("public post metadata unavailable")
    source = tweet
    media_url = fxtwitter_video_url(source)
    if not media_url and isinstance(tweet.get("quote"), dict):
        source = tweet["quote"]
        media_url = fxtwitter_video_url(source)
    if not media_url:
        raise RuntimeError("no downloadable MP4 in post or quoted post")
    author = tweet.get("author") or {}
    log(f"metadata quoted-media fallback tweet_id={candidate.tweet_id} source=fxtwitter quoted={source is not tweet}", logfile)
    return {
        "id": candidate.tweet_id,
        "webpage_url": candidate.url,
        "title": tweet.get("text") or "Shared video",
        "description": tweet.get("text") or "",
        "uploader": author.get("screen_name") or SCREEN_NAME,
        "uploader_id": author.get("id"),
        "timestamp": tweet.get("created_timestamp"),
        "view_count": tweet.get("views"),
        "like_count": tweet.get("likes"),
        "retweet_count": tweet.get("retweets"),
        "reply_count": tweet.get("replies"),
        "formats": [{"url": media_url, "ext": "mp4", "vcodec": "h264", "acodec": "aac"}],
        "url": media_url,
        "ext": "mp4",
        "_download_url": media_url,
        "_download_no_cookies": True,
        "quoted_source_url": source.get("url") if source is not tweet else None,
        "metadata_provider": "fxtwitter",
    }


def extract_metadata(args: argparse.Namespace, candidate: Candidate, logfile: Path | None) -> dict[str, Any]:
    modes: list[tuple[str, list[str]]] = [("selected", browser_cookie_args(args.cookies_mode, args.firefox_profile))]
    if args.cookies_mode == "auto":
        modes = [("none", []), ("firefox", browser_cookie_args("firefox", args.firefox_profile))]
    last_error = ""
    for label, cookie_args in modes:
        cmd = [tool_path("yt-dlp"), "--dump-single-json", "--no-playlist", "--no-warnings", *cookie_args, candidate.url]
        log(f"metadata tweet_id={candidate.tweet_id} mode={label}", logfile)
        try:
            result = run(cmd, timeout=150)
        except subprocess.TimeoutExpired as exc:
            last_error = f"metadata timeout: {exc}"
            continue
        if result.returncode == 0 and result.stdout.strip():
            meta = json.loads(result.stdout)
            if metadata_has_video(meta):
                meta["_download_no_cookies"] = not bool(cookie_args)
                return meta
            last_error = "yt-dlp returned metadata without video formats"
        else:
            last_error = (result.stderr or result.stdout).strip()[-1200:]
    if candidate.tweet_id is None:
        raise RuntimeError(last_error or "No video metadata from site")
    try:
        return extract_fxtwitter_metadata(candidate, logfile)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        raise RuntimeError(f"{last_error or 'yt-dlp returned no metadata'}; quoted-media fallback: {exc}") from exc


def metadata_has_video(meta: dict[str, Any]) -> bool:
    formats = meta.get("formats") or []
    if any((f.get("vcodec") or "none") != "none" for f in formats):
        return True
    return bool(meta.get("url") and meta.get("ext") in {"mp4", "webm", "mkv", "mov"})


def source_error_means_unavailable(error: str) -> bool:
    normalized = error.lower()
    return any(pattern in normalized for pattern in SOURCE_UNAVAILABLE_PATTERNS)


def probe_source_availability(
    args: argparse.Namespace,
    candidate: Candidate,
    logfile: Path | None,
) -> tuple[str, str | None]:
    try:
        meta = extract_metadata(args, candidate, logfile)
        if metadata_has_video(meta):
            return "available", None
        return "unavailable", "source returned no downloadable video"
    except Exception as exc:
        error = str(exc)
        if source_error_means_unavailable(error):
            return "unavailable", error
        return "unknown", error


def refresh_source_availability(
    args: argparse.Namespace,
    con: sqlite3.Connection,
    logfile: Path | None,
    *,
    probe: Any = probe_source_availability,
) -> dict[str, int]:
    rows = con.execute(
        """
        SELECT id, original_url, tweet_id, published_at
        FROM downloads
        WHERE status='downloaded'
        ORDER BY
          CASE WHEN source_checked_at IS NULL THEN 0 ELSE 1 END,
          source_checked_at ASC,
          id ASC
        """
    ).fetchall()
    counts = {"total": len(rows), "available": 0, "unavailable": 0, "unknown": 0}
    for index, row in enumerate(rows):
        match = TWEET_RE.search(row["original_url"] or "")
        tweet_id = row["tweet_id"] or (match.group("id") if match else str(row["id"]))
        candidate = Candidate(
            tweet_id=str(tweet_id),
            url=row["original_url"],
            published_at=row["published_at"],
            source="availability-check",
        )
        status, error = probe(args, candidate, logfile)
        if status not in {"available", "unavailable", "unknown"}:
            raise RuntimeError(f"invalid source availability status: {status}")
        checked_at = utc_now()
        con.execute(
            """
            UPDATE downloads
            SET source_availability=?,
                source_checked_at=?,
                source_check_error=?,
                updated_at=?
            WHERE id=?
            """,
            (status, checked_at, error, checked_at, row["id"]),
        )
        con.commit()
        counts[status] += 1
        log(
            f"source availability tweet_id={candidate.tweet_id} status={status}"
            + (f" error={error}" if error else ""),
            logfile,
        )
        if index + 1 < len(rows):
            time.sleep(1)
    return counts


def parse_fraction(value: Any) -> tuple[int | None, int | None]:
    if not isinstance(value, str) or "/" not in value:
        return None, None
    try:
        numerator, denominator = (int(part) for part in value.split("/", 1))
    except (TypeError, ValueError):
        return None, None
    if numerator < 0 or denominator <= 0:
        return None, None
    return numerator, denominator


def media_key_from_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = X_MEDIA_URL_RE.search(value)
    if not match:
        return None
    kind = match.group("kind").lower()
    asset = urllib.parse.unquote(match.group("asset"))
    return f"{kind}:{asset}"


def metadata_media_key(meta: dict[str, Any]) -> str | None:
    urls: list[Any] = [meta.get("url"), meta.get("manifest_url")]
    for field in ("formats", "requested_formats", "requested_downloads"):
        for item in meta.get(field) or []:
            if isinstance(item, dict):
                urls.extend((item.get("url"), item.get("manifest_url")))
    keys = {key for value in urls if (key := media_key_from_url(value))}
    if not keys:
        return None
    # A Twitter/X asset normally exposes one stable asset id through many format URLs.
    # Sorting keeps the choice deterministic if yt-dlp returns more than one candidate.
    return sorted(keys)[0]


def stable_title(meta: dict[str, Any], candidate: Candidate) -> str:
    published = candidate.published_at or meta.get("upload_date") or meta.get("timestamp")
    if isinstance(published, int | float):
        prefix = dt.datetime.fromtimestamp(published, dt.UTC).strftime("%Y-%m-%d")
    elif isinstance(published, str) and re.fullmatch(r"\d{8}", published):
        prefix = f"{published[:4]}-{published[4:6]}-{published[6:8]}"
    elif isinstance(published, str) and re.match(r"\d{4}-\d{2}-\d{2}", published):
        prefix = published[:10]
    else:
        prefix = "unknown-date"
    title = meta.get("title") or meta.get("description") or "LivingGaul X video"
    title = re.sub(r"https?://\S+", "", str(title))
    title = re.sub(r"[^A-Za-z0-9._ -]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" ._-")
    if not title or title.lower() in {"twitter", "x"}:
        title = "LivingGaul X video"
    title = title[:90].strip()
    return f"{prefix} - {candidate.tweet_id} - {title}"


def newest_output_file(output_dir: Path, before: set[Path]) -> Path | None:
    candidates = [p for p in output_dir.glob("*") if p.is_file() and p not in before and not p.name.endswith((".part", ".ytdl"))]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def ffprobe_height(path: Path) -> int | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    result = run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height", "-of", "csv=p=0", str(path)],
        timeout=60,
    )
    if result.returncode != 0:
        return None
    with contextlib.suppress(ValueError):
        return int(result.stdout.strip().splitlines()[0])
    return None


def ensure_max_480(path: Path, logfile: Path | None) -> tuple[Path, str]:
    height = ffprobe_height(path)
    if height is None:
        return path, "480p-or-unknown"
    if height <= 480:
        return path, f"{height}p"
    ffmpeg = tool_path("ffmpeg")
    encoders = run([ffmpeg, "-hide_banner", "-encoders"], timeout=15).stdout
    if re.search(r"\blibx264\b", encoders):
        video_args = ["-c:v", "libx264", "-crf", "23", "-preset", "veryfast"]
    elif re.search(r"\blibopenh264\b", encoders):
        video_args = ["-c:v", "libopenh264", "-b:v", "900k"]
    elif re.search(r"\bmpeg4\b", encoders):
        video_args = ["-c:v", "mpeg4", "-q:v", "5"]
    else:
        raise RuntimeError("ffmpeg has no supported MP4 video encoder")
    tmp = path.with_suffix(f".480p.tmp{path.suffix}")
    result = run(
        [ffmpeg, "-y", "-i", str(path), "-vf", "scale=-2:480", *video_args, "-c:a", "copy", str(tmp)],
        timeout=1800,
    )
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg downscale failed: " + result.stderr[-1200:])
    tmp.replace(path)
    log(f"downscaled file={path} from_height={height} target=480p", logfile)
    return path, "480p-downscaled"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ffprobe_file_metadata(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {}
    result = run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration,format_name,bit_rate:"
            "stream=codec_type,codec_name,width,height,pix_fmt,avg_frame_rate,r_frame_rate,bit_rate,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        timeout=60,
    )
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}

    streams = payload.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    fmt = payload.get("format") or {}

    def positive_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    duration_ms = None
    try:
        seconds = float(fmt.get("duration"))
        if seconds > 0:
            duration_ms = int(round(seconds * 1000))
    except (TypeError, ValueError):
        pass

    fps_num, fps_den = parse_fraction(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    return {
        "duration_ms": duration_ms,
        "width": positive_int(video.get("width")),
        "height": positive_int(video.get("height")),
        "fps_num": fps_num,
        "fps_den": fps_den,
        "video_codec": video.get("codec_name") or None,
        "audio_codec": audio.get("codec_name") or None,
        "video_bitrate": positive_int(video.get("bit_rate")),
        "audio_bitrate": positive_int(audio.get("bit_rate")),
        "container_format": fmt.get("format_name") or None,
        "pixel_format": video.get("pix_fmt") or None,
        "audio_sample_rate": positive_int(audio.get("sample_rate")),
        "audio_channels": positive_int(audio.get("channels")),
    }


def ffprobe_duration_ms(path: Path) -> int | None:
    return ffprobe_file_metadata(path).get("duration_ms")


def frame_dhash_signature(path: Path, timestamp: float) -> str | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        result = run(
            [
                ffmpeg,
                "-threads", "1",
                "-v",
                "error",
                "-i",
                str(path),
                "-ss",
                f"{max(0.0, timestamp):.3f}",
                "-frames:v",
                "1",
                "-vf",
                "scale=17:16:flags=area,format=gray",
                "-f",
                "rawvideo",
                "pipe:1",
            ],
            text=False,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return None
    frame_size = 17 * 16
    if result.returncode != 0 or len(result.stdout) < frame_size:
        return None
    frame = result.stdout[:frame_size]
    bits = 0
    for y in range(16):
        base = y * 17
        for x in range(16):
            bits = (bits << 1) | int(frame[base + x] > frame[base + x + 1])
    mean = sum(frame) // frame_size
    variance = sum((value - mean) ** 2 for value in frame) // frame_size
    stdev = min(255, int(variance ** 0.5))
    return f"{bits:064x}:{mean:02x}:{stdev:02x}"


def video_content_fingerprint(path: Path, duration_ms: int | None) -> str | None:
    if not duration_ms or duration_ms <= 0:
        return None
    duration = duration_ms / 1000.0
    upper = max(0.0, duration - 0.05)
    timestamps = [min(upper, duration * fraction) for fraction in (0.10, 0.30, 0.50, 0.70, 0.90)]
    parts: list[str] = []
    for timestamp in timestamps:
        signature = frame_dhash_signature(path, timestamp)
        if not signature:
            return None
        parts.append(signature)
    return FINGERPRINT_PREFIX + "|" + "|".join(parts)


def parse_content_fingerprint(value: str | None) -> list[tuple[int, int, int]] | None:
    if not value:
        return None
    parts = value.split("|")
    if len(parts) != FINGERPRINT_FRAME_COUNT + 1 or parts[0] != FINGERPRINT_PREFIX:
        return None
    decoded: list[tuple[int, int, int]] = []
    try:
        for part in parts[1:]:
            hash_hex, mean_hex, stdev_hex = part.split(":")
            decoded.append((int(hash_hex, 16), int(mean_hex, 16), int(stdev_hex, 16)))
    except (ValueError, TypeError):
        return None
    return decoded


def fingerprints_match(first: str | None, second: str | None) -> bool:
    left = parse_content_fingerprint(first)
    right = parse_content_fingerprint(second)
    if not left or not right or len(left) != len(right):
        return False
    hamming = [(a[0] ^ b[0]).bit_count() for a, b in zip(left, right)]
    mean_delta = [abs(a[1] - b[1]) for a, b in zip(left, right)]
    stdev_delta = [abs(a[2] - b[2]) for a, b in zip(left, right)]
    # Conservative thresholds: false positives are worse than retaining one duplicate.
    return (
        sum(hamming) / len(hamming) <= 18
        and max(hamming) <= 40
        and sum(mean_delta) / len(mean_delta) <= 10
        and sum(stdev_delta) / len(stdev_delta) <= 12
    )


def downloaded_by_fingerprint(
    con: sqlite3.Connection,
    content_fingerprint: str,
    duration_ms: int,
) -> sqlite3.Row | None:
    tolerance_ms = min(1500, max(500, int(duration_ms * 0.015)))
    rows = con.execute(
        """
        SELECT * FROM downloads
        WHERE status='downloaded'
          AND content_fingerprint IS NOT NULL
          AND duration_ms BETWEEN ? AND ?
        ORDER BY updated_at DESC
        """,
        (duration_ms - tolerance_ms, duration_ms + tolerance_ms),
    ).fetchall()
    for row in rows:
        if row_has_existing_file(row) and fingerprints_match(content_fingerprint, row["content_fingerprint"]):
            return row
    return None


def reindex_content(con: sqlite3.Connection, logfile: Path | None) -> tuple[int, int, int, int]:
    rows = con.execute(
        "SELECT * FROM downloads WHERE status='downloaded' ORDER BY id"
    ).fetchall()
    updated = missing = failed = 0
    for row in rows:
        file_path = row["file_path"]
        if pipeline.archived_valid(row):
            continue
        if not file_path or not Path(file_path).exists():
            missing += 1
            continue
        path = Path(file_path)
        try:
            file_meta = ffprobe_file_metadata(path)
            duration_ms = row["duration_ms"] or file_meta.get("duration_ms")
            sha256 = row["sha256"] or sha256_file(path)
            fingerprint = row["content_fingerprint"] or video_content_fingerprint(path, duration_ms)
            media_key = row["media_key"] or media_key_from_url(row["direct_video_url"])
            size_bytes = row["size_bytes"] or path.stat().st_size
            con.execute(
                """
                UPDATE downloads SET
                  sha256=COALESCE(sha256, ?),
                  size_bytes=COALESCE(size_bytes, ?),
                  media_key=COALESCE(media_key, ?),
                  content_fingerprint=COALESCE(content_fingerprint, ?),
                  duration_ms=COALESCE(duration_ms, ?),
                  width=COALESCE(width, ?),
                  height=COALESCE(height, ?),
                  fps_num=COALESCE(fps_num, ?),
                  fps_den=COALESCE(fps_den, ?),
                  video_codec=COALESCE(video_codec, ?),
                  audio_codec=COALESCE(audio_codec, ?),
                  video_bitrate=COALESCE(video_bitrate, ?),
                  audio_bitrate=COALESCE(audio_bitrate, ?),
                  container_format=COALESCE(container_format, ?),
                  pixel_format=COALESCE(pixel_format, ?),
                  audio_sample_rate=COALESCE(audio_sample_rate, ?),
                  audio_channels=COALESCE(audio_channels, ?),
                  updated_at=?
                WHERE id=?
                """,
                (
                    sha256,
                    size_bytes,
                    media_key,
                    fingerprint,
                    duration_ms,
                    file_meta.get("width"),
                    file_meta.get("height"),
                    file_meta.get("fps_num"),
                    file_meta.get("fps_den"),
                    file_meta.get("video_codec"),
                    file_meta.get("audio_codec"),
                    file_meta.get("video_bitrate"),
                    file_meta.get("audio_bitrate"),
                    file_meta.get("container_format"),
                    file_meta.get("pixel_format"),
                    file_meta.get("audio_sample_rate"),
                    file_meta.get("audio_channels"),
                    utc_now(),
                    row["id"],
                ),
            )
            con.commit()
            updated += 1
        except Exception as exc:
            failed += 1
            log(f"reindex failed id={row['id']} file={path} error={exc}", logfile)
    return len(rows), updated, missing, failed


def analyze_download(path, logfile):
    path, quality = ensure_max_480(path, logfile)
    sha = sha256_file(path)
    metadata = ffprobe_file_metadata(path)
    fingerprint = video_content_fingerprint(path, metadata.get('duration_ms'))
    return path, quality, sha, metadata, fingerprint


def download_one(args, con, candidate, logfile, summary, *, hooks=None, runtime_lock=None):
    """Shared engine: URL claim -> private download -> atomic catalog publication."""
    db = Path(con.execute('PRAGMA database_list').fetchone()[2])
    catalog = Path(runtime_lock) if runtime_lock else lock_path(db)
    claims = db.parent / 'download-claims'
    claim = claims / (hashlib.sha256(candidate.url.encode()).hexdigest() + '.lock')
    work = args.output_dir / '.work'
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(claim):
        # Leave incomplete work in its own directory for recovery; never sweep user files.
        workspace = work / hashlib.sha256(candidate.url.encode()).hexdigest()
        workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        isolated = argparse.Namespace(**vars(args))
        isolated.publish_dir = args.output_dir
        isolated.output_dir = workspace / 'files'
        isolated.output_dir.mkdir(mode=0o700, exist_ok=True)
        failed_before = summary.failed
        _download_one(isolated, con, candidate, logfile, summary, hooks or {}, catalog)
        if summary.failed > failed_before:
            return
        # Only remove empty workspaces. Failed or interrupted transfers stay recoverable.
        for item in workspace.glob('download-archive.txt'):
            item.unlink()
        with contextlib.suppress(OSError):
            isolated.output_dir.rmdir()
            workspace.rmdir()


def _download_one(args, con, candidate, logfile, summary, hooks, catalog):
    extract = hooks.get('extract_metadata', extract_metadata)
    transfer = hooks.get('run', run)
    log = hooks.get('log', globals()['log'])
    summary.posts_analyzed += 1
    row = already_downloaded(con, candidate)
    if row:
        with file_lock(catalog):
            record_origin(con, candidate)
            con.commit()
        summary.skipped_duplicate += 1
        log(f"skip downloaded tweet_id={candidate.tweet_id} file={row['file_path']}", logfile)
        return
    failed_row = previous_failed(con, candidate)
    if failed_row and not args.retry_failed:
        summary.skipped_previous_failed += 1
        log(f"skip previous_failed tweet_id={candidate.tweet_id} use --retry-failed to retry", logfile)
        return
    meta: dict[str, Any] | None = None
    try:
        meta = extract(args, candidate, logfile)
        if not metadata_has_video(meta):
            raise RuntimeError("no video formats found")
        summary.videos_found += 1
        direct = meta.get("url") or next((f.get("url") for f in meta.get("formats", []) if pipeline.safe_url(f.get("url"), "video.twimg.com")), None)
        media_key = metadata_media_key(meta)
        with file_lock(catalog):
            if media_key:
                duplicate = downloaded_by_media_key(con, media_key)
                if duplicate:
                    record_duplicate(
                        con,
                        candidate,
                        duplicate,
                        direct_video_url=direct,
                        media_key=media_key,
                        source_metadata=meta,
                        reason=f"duplicate media asset: {media_key}",
                    )
                    summary.skipped_duplicate += 1
                    log(
                        f"skip duplicate media_key={media_key} tweet_id={candidate.tweet_id} "
                        f"canonical={duplicate['file_path']}",
                        logfile,
                    )
                    return
        title = stable_title(meta, candidate)
        if candidate.tweet_id is None:
            title += '-' + hashlib.sha256(candidate.url.encode()).hexdigest()[:16]
        if args.dry_run:
            summary.dry_run_matches += 1
            print(f"DRY-RUN would download: {candidate.url} -> {title}.%(ext)s")
            return
        before = {p for p in args.output_dir.glob("*") if p.is_file()}
        outtmpl = str(args.output_dir / f"{title}.%(ext)s")
        download_target = meta.get("_download_url") or candidate.url
        cmd = [
            *ytdlp_base_args(args, use_cookies=not meta.get("_download_no_cookies", False)),
            "--format",
            FORMAT_480,
            "--merge-output-format",
            "mp4",
            "--download-archive",
            str(args.output_dir.parent / "download-archive.txt"),
            "--continue",
            "--no-overwrites",
            "--output",
            outtmpl,
            download_target,
        ]
        log(f"download tweet_id={candidate.tweet_id}", logfile)
        result = transfer(cmd, timeout=3600)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip()[-1600:])
        final = newest_output_file(args.output_dir, before)
        if final is None:
            possible = sorted(args.output_dir.glob(f"*{candidate.tweet_id}*"), key=lambda p: p.stat().st_mtime if p.is_file() else 0, reverse=True)
            final = possible[0] if possible else None
        if final is None or not final.exists():
            raise RuntimeError("download completed but final file was not found")
        analyze = hooks.get('analyze', analyze_download)
        final, quality, sha256, file_metadata, content_fingerprint = analyze(final, logfile)
        duration_ms = file_metadata.get('duration_ms')
        with file_lock(catalog):
            duplicate = already_downloaded(con, candidate) or downloaded_by_sha256(con, sha256)
            if not duplicate and media_key:
                duplicate = downloaded_by_media_key(con, media_key)
            duplicate_reason = "duplicate exact content: sha256"
            if not duplicate and content_fingerprint and duration_ms:
                duplicate = downloaded_by_fingerprint(con, content_fingerprint, duration_ms)
                duplicate_reason = "duplicate perceptual video content"
            if duplicate:
                final.unlink(missing_ok=True)
                record_duplicate(
                    con,
                    candidate,
                    duplicate,
                    direct_video_url=direct,
                    media_key=media_key,
                    source_metadata=meta,
                    content_fingerprint=content_fingerprint,
                    duration_ms=duration_ms,
                    reason=duplicate_reason,
                )
                summary.skipped_duplicate += 1
                log(
                    f"removed duplicate tweet_id={candidate.tweet_id} reason={duplicate_reason} "
                    f"canonical={duplicate['file_path']}",
                    logfile,
                )
                return
            published = args.publish_dir / (uuid.uuid4().hex + '-' + final.name)
            with final.open("rb") as stream:
                os.fsync(stream.fileno())
            final.rename(published)
            pipeline.fsync_dir(published.parent)
            final = published
            upsert_status(
                con,
                candidate,
                "downloaded",
                direct_video_url=direct,
                final_filename=final.name,
                file_path=str(final),
                sha256=sha256,
                size_bytes=final.stat().st_size,
                media_key=media_key,
                content_fingerprint=content_fingerprint,
                duration_ms=duration_ms,
                duplicate_of_id=None,
                source_metadata=meta,
                file_metadata=file_metadata,
                downloaded_at=utc_now(),
                quality=quality,
                error=None,
            )
        summary.downloaded += 1
        log(f"downloaded tweet_id={candidate.tweet_id} file={final}", logfile)
    except Exception as exc:
        if not args.dry_run:
            with file_lock(catalog):
                # A later failure must never downgrade an existing canonical file.
                if not already_downloaded(con, candidate):
                    upsert_status(con, candidate, "failed", source_metadata=meta, error=str(exc))
        summary.failed += 1
        log(f"failed tweet_id={candidate.tweet_id} error={exc}", logfile)


def retry_failed_candidates(con: sqlite3.Connection) -> list[Candidate]:
    rows = con.execute(
        "SELECT original_url, tweet_id, published_at FROM downloads WHERE status='failed' ORDER BY updated_at"
    ).fetchall()
    out = []
    for row in rows:
        tweet_id = row["tweet_id"] or (TWEET_RE.search(row["original_url"]).group("id") if TWEET_RE.search(row["original_url"]) else None)
        if tweet_id:
            out.append(Candidate(tweet_id=tweet_id, url=row["original_url"], published_at=row["published_at"], source="retry-failed"))
    return out


def print_status(con: sqlite3.Connection, output_dir: Path, db_path: Path) -> None:
    counts = {row["status"]: row["n"] for row in con.execute("SELECT status, COUNT(*) n FROM downloads GROUP BY status")}
    indexed = con.execute(
        "SELECT COUNT(*) FROM downloads WHERE status='downloaded' AND content_fingerprint IS NOT NULL"
    ).fetchone()[0]
    media_indexed = con.execute(
        "SELECT COUNT(*) FROM downloads WHERE status='downloaded' AND media_key IS NOT NULL"
    ).fetchone()[0]
    technical_indexed = con.execute(
        "SELECT COUNT(*) FROM downloads WHERE status='downloaded' AND width IS NOT NULL AND video_codec IS NOT NULL"
    ).fetchone()[0]
    metadata_snapshots = con.execute("SELECT COUNT(*) FROM metadata_snapshots").fetchone()[0]
    availability = con.execute(
        "SELECT unavailable_percentage FROM source_unavailable_percentage"
    ).fetchone()[0]
    size = sum(p.stat().st_size for p in output_dir.glob("**/*") if p.is_file()) if output_dir.exists() else 0
    print(f"version: {VERSION}")
    print(f"db: {db_path}")
    print(f"output_dir: {output_dir}")
    print(f"archive: {DEFAULT_ARCHIVE}")
    print(f"downloaded: {counts.get('downloaded', 0)}")
    for storage in con.execute("SELECT storage_state,COUNT(*) FROM downloads WHERE status='downloaded' GROUP BY storage_state"):
        print(f"storage_{storage[0] or 'local'}: {storage[1]}")
    print(f"skipped_duplicate: {counts.get('skipped_duplicate', 0)}")
    print(f"failed: {counts.get('failed', 0)}")
    print(f"content_fingerprint_indexed: {indexed}")
    print(f"media_key_indexed: {media_indexed}")
    print(f"technical_metadata_indexed: {technical_indexed}")
    print(f"yt_dlp_metadata_snapshots: {metadata_snapshots}")
    print(f"source_unavailable_percentage: {availability}")
    print(f"output_size_bytes: {size}")


def list_failed(con: sqlite3.Connection) -> None:
    rows = con.execute(
        "SELECT updated_at, tweet_id, original_url, error FROM downloads WHERE status='failed' ORDER BY updated_at DESC"
    ).fetchall()
    for row in rows:
        print(f"{row['updated_at']} tweet_id={row['tweet_id']} url={row['original_url']} error={row['error']}")


