META=project=livinggaul-x-downloader;version=0.4.2;prompt=392814
PURPOSE=reusable_host_cli_downloads_LivingGaul_X_videos_max_480p
STACK=python3,yt-dlp,gallery-dl_user_venv,ffmpeg,ffprobe,sqlite3
ENTRYPOINT=script livinggaul-x-downloader installed globally at ~/.local/bin/livinggaul-x-downloader
OUTPUT=videos at ~/Videos/LivingGaul_X
STATE=~/.local/state/livinggaul-x-downloader
DB=~/.local/state/livinggaul-x-downloader/download_history.sqlite; schema_v4 stores normalized X/social/file metadata, persistent skipped_duplicate rows linked by duplicate_of_id, source_availability/source_checked_at/source_check_error, immutable raw yt-dlp JSON snapshots; video_catalog exposes readable metadata plus derived duplicate_count; source_unavailable_percentage is a one-row/one-column aggregate view
ARCHIVE=~/.local/state/livinggaul-x-downloader/yt-dlp-download-archive.txt
LOGS=~/.local/state/livinggaul-x-downloader/logs
AUTH=cookies_mode auto tries no_login then yt-dlp cookies-from-browser firefox; manual mode requests browser login without storing secrets
QUALITY=yt-dlp format bestvideo height<=480 plus audio, fallback best, ffmpeg downscale if final height exceeds 480
IDEMPOTENCE=SQLite+yt-dlp_archive; cross-post content dedupe uses stable video.twimg.com media_key before transfer, then SHA256 and conservative 5-frame perceptual dHash+duration matching; duplicate transfers are removed and linked to canonical row; downloaded row requires existing file before skip
DISCOVERY=gallery-dl_X_timeline_with_firefox_cookies,public_X_HTML,Nitter_RSS_HTML,optional_url_file; X/Nitter access limits are logged explicitly
CLI=--run,--dry-run,--status,--list-failed,--retry-failed,--reindex-content,--check-source-availability,--cookies-mode,--output-dir,--db-path,--url-file; availability probe never downloads media and classifies permanent-unavailable vs transient-unknown conservatively; systemd user timer runs availability hourly with linger,persistence,flock,retry,timeout,journal
SECURITY=no_passwords_or_tokens_saved; browser cookies passed only to yt-dlp
TEST=py_compile,CLI_help,bash_n_systemd_installer,unittest_schema_v4_metadata_video_catalog_duplicate_count_availability_single_value_view_lru_order_and_systemd_assets; local_systemd_linger_install_and_live_probe_require_host_state
RISK=X_profile_discovery_can_be_blocked_or_empty; use --url-file after manual browser discovery if needed
