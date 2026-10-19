"""X timeline discovery and CLI orchestration, using the shared archive engine."""
from video_engine import *


def discover_from_public_web(logfile: Path | None) -> list[Candidate]:
    candidates: dict[str, Candidate] = {}
    urls = [PROFILE_URL, f"https://x.com/search?q=from%3A{SCREEN_NAME}%20filter%3Anative_video&src=typed_query&f=live"]
    for url in urls:
        try:
            html = http_get(url)
            candidates.update(parse_status_ids(html, f"x-html:{url}"))
            log(f"discovery x-html url={url} candidates={len(candidates)}", logfile)
        except Exception as exc:
            log(f"discovery x-html failed url={url} error={exc}", logfile)
    for base in NITTER_INSTANCES:
        for suffix in (f"/{SCREEN_NAME}/rss", f"/{SCREEN_NAME}/with_replies", f"/search?f=tweets&q=from%3A{SCREEN_NAME}%20filter%3Anative_video"):
            url = base + suffix
            try:
                text = http_get(url)
                parsed = parse_rss(text, f"rss:{url}") if suffix.endswith("/rss") else parse_status_ids(text, f"html:{url}")
                candidates.update(parsed)
                log(f"discovery nitter url={url} added={len(parsed)} total={len(candidates)}", logfile)
                if candidates:
                    break
            except Exception as exc:
                log(f"discovery nitter failed url={url} error={exc}", logfile)
        if candidates:
            break
    return sorted(candidates.values(), key=lambda c: int(c.tweet_id), reverse=True)


def discover_from_gallery_dl(args: argparse.Namespace, logfile: Path | None) -> list[Candidate]:
    try:
        gallery_dl = tool_path("gallery-dl")
    except SystemExit:
        log("discovery gallery-dl unavailable", logfile)
        return []
    cmd = [gallery_dl, "-j", "--range", "1-999999"]
    if args.cookies_mode in {"auto", "firefox"}:
        if args.firefox_profile:
            cmd.extend(["--cookies-from-browser", f"firefox:{args.firefox_profile}"])
        else:
            cmd.extend(["--cookies-from-browser", "firefox"])
    cmd.append(PROFILE_URL)
    log("discovery gallery-dl start", logfile)
    try:
        result = run(cmd, timeout=420)
    except subprocess.TimeoutExpired as exc:
        log(f"discovery gallery-dl timeout error={exc}", logfile)
        return []
    if result.returncode != 0:
        log(f"discovery gallery-dl failed error={(result.stderr or result.stdout).strip()[-1000:]}", logfile)
    candidates: dict[str, Candidate] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("["):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = json.dumps(payload, ensure_ascii=True)
        for tweet_id in TWEET_RE.findall(text):
            add_candidate(candidates, tweet_id, "gallery-dl")
    log(f"discovery gallery-dl candidates={len(candidates)}", logfile)
    return sorted(candidates.values(), key=lambda c: int(c.tweet_id), reverse=True)


def candidates_from_url_file(path: Path) -> list[Candidate]:
    candidates: dict[str, Candidate] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = TWEET_RE.search(line)
        if match:
            add_candidate(candidates, match.group("id"), f"url-file:{path}")
    return sorted(candidates.values(), key=lambda c: int(c.tweet_id), reverse=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download LivingGaul X videos at max 480p with SQLite history.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--run", action="store_true", help="discover and download new videos")
    action.add_argument("--dry-run", action="store_true", help="discover and show videos without downloading")
    action.add_argument("--status", action="store_true", help="show DB and output summary")
    action.add_argument("--list-failed", action="store_true", help="list failed DB rows")
    action.add_argument("--retry-failed", action="store_true", help="retry only failed rows")
    action.add_argument(
        "--reindex-content",
        action="store_true",
        help="backfill hashes, perceptual fingerprints, and ffprobe metadata for existing downloaded files",
    )
    action.add_argument(
        "--check-source-availability",
        action="store_true",
        help="probe each downloaded video's original X link without downloading and store current availability",
    )
    parser.add_argument("--cookies-mode", choices=["auto", "firefox", "manual", "none"], default="auto")
    parser.add_argument("--firefox-profile", help="optional Firefox profile path/name for yt-dlp cookies")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--url-file", type=Path, help="optional file with X post URLs, one per line")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if (args.status or args.list_failed) and args.db_path.exists():
        con = sqlite3.connect(args.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        if args.status: print_status(con, args.output_dir, args.db_path)
        else: list_failed(con)
        con.close()
        return 0
    mkdirs(args.output_dir, args.db_path)
    logfile = log_path()
    con = connect_db(args.db_path)
    if args.status:
        print_status(con, args.output_dir, args.db_path)
        return 0
    if args.list_failed:
        list_failed(con)
        return 0
    if args.reindex_content:
        scanned, updated, missing, failed = reindex_content(con, logfile)
        print(f"reindex scanned: {scanned}")
        print(f"reindex updated: {updated}")
        print(f"reindex missing_files: {missing}")
        print(f"reindex failed: {failed}")
        return 0 if failed == 0 else 1
    if args.check_source_availability:
        if args.cookies_mode == "manual":
            print("Manual login mode cannot probe availability; use --cookies-mode firefox or auto.", file=sys.stderr)
            return 2
        counts = refresh_source_availability(args, con, logfile)
        percentage = con.execute(
            "SELECT unavailable_percentage FROM source_unavailable_percentage"
        ).fetchone()[0]
        print(f"availability total: {counts['total']}")
        print(f"availability available: {counts['available']}")
        print(f"availability unavailable: {counts['unavailable']}")
        print(f"availability unknown: {counts['unknown']}")
        print(f"source unavailable percentage: {percentage}")
        return 0
    if args.cookies_mode == "manual":
        print("Manual login mode: open Firefox, log in to X, then rerun with --cookies-mode firefox or auto.", file=sys.stderr)
        return 2
    if args.retry_failed:
        candidates = retry_failed_candidates(con)
    else:
        candidates = []
        if args.url_file:
            candidates.extend(candidates_from_url_file(args.url_file))
        discovered = discover_from_gallery_dl(args, logfile)
        if not discovered:
            discovered = discover_from_public_web(logfile)
        seen = {c.tweet_id for c in candidates}
        candidates.extend(c for c in discovered if c.tweet_id not in seen)
    summary = Summary()
    log(f"start version={VERSION} candidates={len(candidates)} output={args.output_dir} db={args.db_path}", logfile)
    if not candidates:
        log("no post candidates discovered; X/Nitter may be access-limited, use --url-file after manual browser discovery", logfile)
    for candidate in candidates:
        download_one(args, con, candidate, logfile, summary)
        time.sleep(1)
    size = sum(p.stat().st_size for p in args.output_dir.glob("**/*") if p.is_file()) if args.output_dir.exists() else 0
    print("Final report")
    print(f"tweet/post analyzed: {summary.posts_analyzed}")
    print(f"videos found: {summary.videos_found}")
    print(f"already present skipped: {summary.skipped_duplicate}")
    print(f"previous failures skipped: {summary.skipped_previous_failed}")
    print(f"new videos downloaded: {summary.downloaded}")
    print(f"errors: {summary.failed}")
    print(f"space used bytes: {size}")
    print(f"db path: {args.db_path}")
    print(f"video path: {args.output_dir}")
    print(f"log path: {logfile}")
    print("future rerun: livinggaul-x-downloader --run")
    return 0 if summary.failed == 0 else 1

