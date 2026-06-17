META=project=livinggaul-x-downloader;version=0.1.3;prompt=392814
PURPOSE=reusable_host_cli_downloads_LivingGaul_X_videos_max_480p
STACK=python3,yt-dlp,gallery-dl_user_venv,ffmpeg,ffprobe,sqlite3
ENTRYPOINT=script livinggaul-x-downloader installed globally at ~/.local/bin/livinggaul-x-downloader
OUTPUT=videos at ~/Videos/LivingGaul_X
STATE=~/.local/state/livinggaul-x-downloader
DB=~/.local/state/livinggaul-x-downloader/download_history.sqlite
ARCHIVE=~/.local/state/livinggaul-x-downloader/yt-dlp-download-archive.txt
LOGS=~/.local/state/livinggaul-x-downloader/logs
AUTH=cookies_mode auto tries no_login then yt-dlp cookies-from-browser firefox; manual mode requests browser login without storing secrets
QUALITY=yt-dlp format bestvideo height<=480 plus audio, fallback best, ffmpeg downscale if final height exceeds 480
IDEMPOTENCE=SQLite downloaded rows and yt-dlp archive prevent redownloads; downloaded row requires existing file before skip; normal_run_skips_previous_failed_until_retry_failed
DISCOVERY=gallery-dl_X_timeline_with_firefox_cookies,public_X_HTML,Nitter_RSS_HTML,optional_url_file; X/Nitter access limits are logged explicitly
CLI=--run,--dry-run,--status,--list-failed,--retry-failed,--cookies-mode,--output-dir,--db-path,--url-file
SECURITY=no_passwords_or_tokens_saved; browser cookies passed only to yt-dlp
TEST=bash_syntax_not_applicable_python; py_compile required; dry_run,status,db_schema,idempotence_logic
RISK=X_profile_discovery_can_be_blocked_or_empty; use --url-file after manual browser discovery if needed
