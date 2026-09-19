import runpy
import sqlite3
import tempfile
import unittest
from pathlib import Path

MODULE = runpy.run_path(str(Path(__file__).resolve().parents[1] / "livinggaul-x-downloader"))


class ContentDedupeTests(unittest.TestCase):
    def test_media_key_ignores_cdn_variant_and_query(self):
        fn = MODULE["media_key_from_url"]
        first = fn("https://video.twimg.com/ext_tw_video/123456789/pu/vid/480x270/a.mp4?tag=12")
        second = fn("https://video.twimg.com/ext_tw_video/123456789/pu/vid/720x404/b.mp4?tag=99")
        self.assertEqual(first, "ext_tw_video:123456789")
        self.assertEqual(first, second)

    def test_metadata_media_key_reads_formats(self):
        fn = MODULE["metadata_media_key"]
        meta = {
            "formats": [
                {"url": "https://video.twimg.com/amplify_video/987654321/vid/avc1/480x270/a.mp4?tag=1"}
            ]
        }
        self.assertEqual(fn(meta), "amplify_video:987654321")

    def test_fingerprint_match_is_conservative_but_tolerates_small_changes(self):
        match = MODULE["fingerprints_match"]
        base_hash = "0" * 64
        one_bit = ("0" * 63) + "1"
        far_hash = "f" * 64
        prefix = MODULE["FINGERPRINT_PREFIX"]
        left = prefix + "|" + "|".join([f"{base_hash}:80:20"] * 5)
        close = prefix + "|" + "|".join([f"{one_bit}:82:21"] * 5)
        far = prefix + "|" + "|".join([f"{far_hash}:80:20"] * 5)
        self.assertTrue(match(left, close))
        self.assertFalse(match(left, far))

    def test_source_metadata_maps_static_social_counters_and_aliases(self):
        Candidate = MODULE["Candidate"]
        fields = MODULE["source_metadata_fields"](
            {
                "uploader": "LivingGaul",
                "uploader_id": "123",
                "description": "post text",
                "title": "post title",
                "timestamp": 1_700_000_000,
                "view_count": 100,
                "like_count": 10,
                "retweet_count": 7,
                "reply_count": 3,
            },
            Candidate(tweet_id="1234567890123456789", url="https://x.com/LivingGaul/status/1234567890123456789"),
        )
        self.assertEqual(fields["uploader"], "LivingGaul")
        self.assertEqual(fields["repost_count"], 7)
        self.assertEqual(fields["comment_count"], 3)
        self.assertEqual(fields["view_count"], 100)
        self.assertEqual(fields["like_count"], 10)
        self.assertIsNotNone(fields["published_at"])

    def test_fraction_parser_preserves_exact_fps(self):
        parse = MODULE["parse_fraction"]
        self.assertEqual(parse("30000/1001"), (30000, 1001))
        self.assertEqual(parse("0/0"), (None, None))
        self.assertEqual(parse(None), (None, None))

    def test_existing_database_migrates_to_schema_v3(self):
        connect_db = MODULE["connect_db"]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "history.sqlite"
            raw = sqlite3.connect(db_path)
            raw.execute(
                """
                CREATE TABLE downloads (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  original_url TEXT NOT NULL,
                  direct_video_url TEXT,
                  tweet_id TEXT,
                  published_at TEXT,
                  final_filename TEXT,
                  file_path TEXT,
                  sha256 TEXT,
                  size_bytes INTEGER,
                  quality TEXT,
                  status TEXT NOT NULL CHECK(status IN ('downloaded','skipped_duplicate','failed')),
                  error TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            raw.commit()
            raw.close()
            con = connect_db(db_path)
            columns = {row["name"] for row in con.execute("PRAGMA table_info(downloads)")}
            expected = {
                "media_key", "content_fingerprint", "duration_ms", "duplicate_of_id",
                "uploader", "uploader_id", "tweet_text", "title", "upload_timestamp",
                "downloaded_at", "view_count", "like_count", "repost_count", "comment_count",
                "width", "height", "fps_num", "fps_den", "video_codec", "audio_codec",
                "video_bitrate", "audio_bitrate", "container_format", "pixel_format",
                "audio_sample_rate", "audio_channels", "source_availability", "source_checked_at", "source_check_error",
            }
            self.assertTrue(expected.issubset(columns))
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("metadata_snapshots", tables)
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 4)
            views = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='view'")}
            self.assertIn("video_catalog", views)
            self.assertIn("source_unavailable_percentage", views)
            con.close()

    def test_upsert_persists_normalized_and_raw_metadata_once(self):
        connect_db = MODULE["connect_db"]
        Candidate = MODULE["Candidate"]
        upsert_status = MODULE["upsert_status"]
        with tempfile.TemporaryDirectory() as tmp:
            con = connect_db(Path(tmp) / "history.sqlite")
            candidate = Candidate(
                tweet_id="1234567890123456789",
                url="https://x.com/LivingGaul/status/1234567890123456789",
            )
            meta = {
                "uploader": "LivingGaul",
                "description": "hello",
                "view_count": 42,
                "like_count": 5,
            }
            file_meta = {
                "width": 854,
                "height": 480,
                "fps_num": 30000,
                "fps_den": 1001,
                "video_codec": "h264",
                "audio_codec": "aac",
                "video_bitrate": 500000,
                "audio_bitrate": 96000,
                "container_format": "mov,mp4,m4a,3gp,3g2,mj2",
                "pixel_format": "yuv420p",
                "audio_sample_rate": 48000,
                "audio_channels": 2,
            }
            upsert_status(
                con,
                candidate,
                "downloaded",
                source_metadata=meta,
                file_metadata=file_meta,
                downloaded_at="2026-09-19T00:00:00Z",
            )
            row = con.execute("SELECT * FROM downloads").fetchone()
            self.assertEqual(row["uploader"], "LivingGaul")
            self.assertEqual(row["view_count"], 42)
            self.assertEqual(row["width"], 854)
            self.assertEqual(row["video_codec"], "h264")
            snap = con.execute("SELECT metadata_json FROM metadata_snapshots").fetchone()
            self.assertIn('"view_count":42', snap["metadata_json"])
            upsert_status(con, candidate, "downloaded", source_metadata={"view_count": 999})
            self.assertEqual(con.execute("SELECT view_count FROM downloads").fetchone()[0], 42)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM metadata_snapshots").fetchone()[0], 1)
            con.close()

    def test_video_catalog_exposes_readable_sizes_and_daily_grouping(self):
        connect_db = MODULE["connect_db"]
        Candidate = MODULE["Candidate"]
        upsert_status = MODULE["upsert_status"]
        with tempfile.TemporaryDirectory() as tmp:
            con = connect_db(Path(tmp) / "history.sqlite")
            rows = [
                ("1234567890123456781", "2026-09-18T10:00:00Z", 1_500_000),
                ("1234567890123456782", "2026-09-18T12:00:00Z", 500_000),
                ("1234567890123456783", "2026-09-19T09:00:00Z", 3_000_000),
            ]
            for tweet_id, downloaded_at, size_bytes in rows:
                candidate = Candidate(
                    tweet_id=tweet_id,
                    url=f"https://x.com/LivingGaul/status/{tweet_id}",
                )
                upsert_status(
                    con,
                    candidate,
                    "downloaded",
                    size_bytes=size_bytes,
                    downloaded_at=downloaded_at,
                    source_metadata={"title": f"video {tweet_id}"},
                )

            catalog = con.execute(
                "SELECT download_date, size_mb, size_mib FROM video_catalog ORDER BY id LIMIT 1"
            ).fetchone()
            self.assertEqual(catalog["download_date"], "2026-09-18")
            self.assertAlmostEqual(catalog["size_mb"], 1.5)
            self.assertAlmostEqual(catalog["size_mib"], 1_500_000 / 1_048_576)

            daily = con.execute(
                """
                SELECT download_date, COUNT(*) AS videos, ROUND(SUM(size_mb), 3) AS total_mb
                FROM video_catalog
                WHERE status='downloaded'
                GROUP BY download_date
                ORDER BY download_date
                """
            ).fetchall()
            self.assertEqual(
                [(row["download_date"], row["videos"], row["total_mb"]) for row in daily],
                [("2026-09-18", 2, 2.0), ("2026-09-19", 1, 3.0)],
            )
            con.close()

    def test_source_error_classification_is_conservative(self):
        classify = MODULE["source_error_means_unavailable"]
        self.assertTrue(classify("ERROR: This Tweet is unavailable"))
        self.assertTrue(classify("HTTP Error 404: Not Found"))
        self.assertFalse(classify("HTTP Error 429: Too Many Requests"))
        self.assertFalse(classify("Login required; cookies expired"))

    def test_unavailable_percentage_view_has_one_value(self):
        connect_db = MODULE["connect_db"]
        Candidate = MODULE["Candidate"]
        upsert_status = MODULE["upsert_status"]
        with tempfile.TemporaryDirectory() as tmp:
            con = connect_db(Path(tmp) / "history.sqlite")
            empty = con.execute("SELECT * FROM source_unavailable_percentage")
            self.assertEqual(len(empty.description), 1)
            self.assertIsNone(empty.fetchone()[0])

            statuses = ["available", "unavailable", "unknown", "available"]
            for index, availability in enumerate(statuses, start=1):
                tweet_id = f"12345678901234567{index:02d}"
                candidate = Candidate(
                    tweet_id=tweet_id,
                    url=f"https://x.com/LivingGaul/status/{tweet_id}",
                )
                upsert_status(con, candidate, "downloaded")
                con.execute(
                    "UPDATE downloads SET source_availability=?, source_checked_at=? WHERE tweet_id=?",
                    (availability, "2026-09-19T08:00:00Z", tweet_id),
                )
            con.commit()
            cursor = con.execute("SELECT * FROM source_unavailable_percentage")
            self.assertEqual(len(cursor.description), 1)
            self.assertEqual(cursor.fetchone()[0], 25.0)
            con.close()

    def test_refresh_source_availability_updates_downloaded_rows_only(self):
        connect_db = MODULE["connect_db"]
        Candidate = MODULE["Candidate"]
        upsert_status = MODULE["upsert_status"]
        refresh = MODULE["refresh_source_availability"]

        class Args:
            cookies_mode = "none"
            firefox_profile = None

        with tempfile.TemporaryDirectory() as tmp:
            con = connect_db(Path(tmp) / "history.sqlite")
            expected = {}
            for index, availability in enumerate(("available", "unavailable", "unknown"), start=1):
                tweet_id = f"12345678901234568{index:02d}"
                candidate = Candidate(tweet_id=tweet_id, url=f"https://x.com/LivingGaul/status/{tweet_id}")
                upsert_status(con, candidate, "downloaded")
                expected[tweet_id] = availability
            failed_id = "1234567890123456899"
            upsert_status(
                con,
                Candidate(tweet_id=failed_id, url=f"https://x.com/LivingGaul/status/{failed_id}"),
                "failed",
            )

            def fake_probe(args, candidate, logfile):
                status = expected[candidate.tweet_id]
                return status, "temporary failure" if status == "unknown" else None

            counts = refresh(Args(), con, None, probe=fake_probe)
            self.assertEqual(counts, {"total": 3, "available": 1, "unavailable": 1, "unknown": 1})
            rows = con.execute(
                "SELECT tweet_id, source_availability, source_checked_at FROM downloads WHERE status='downloaded'"
            ).fetchall()
            self.assertEqual({row["tweet_id"]: row["source_availability"] for row in rows}, expected)
            self.assertTrue(all(row["source_checked_at"] for row in rows))
            self.assertIsNone(
                con.execute("SELECT source_availability FROM downloads WHERE tweet_id=?", (failed_id,)).fetchone()[0]
            )
            con.close()

    def test_availability_refresh_prioritizes_never_and_oldest_checked(self):
        connect_db = MODULE["connect_db"]
        Candidate = MODULE["Candidate"]
        upsert_status = MODULE["upsert_status"]
        refresh = MODULE["refresh_source_availability"]

        class Args:
            cookies_mode = "none"
            firefox_profile = None

        with tempfile.TemporaryDirectory() as tmp:
            con = connect_db(Path(tmp) / "history.sqlite")
            ids = []
            for suffix in ("01", "02", "03"):
                tweet_id = f"12345678901234569{suffix}"
                ids.append(tweet_id)
                upsert_status(
                    con,
                    Candidate(tweet_id=tweet_id, url=f"https://x.com/LivingGaul/status/{tweet_id}"),
                    "downloaded",
                )
            con.execute(
                "UPDATE downloads SET source_checked_at='2026-09-19T10:00:00Z' WHERE tweet_id=?",
                (ids[0],),
            )
            con.execute(
                "UPDATE downloads SET source_checked_at='2026-09-19T09:00:00Z' WHERE tweet_id=?",
                (ids[1],),
            )
            con.commit()
            seen = []

            def fake_probe(args, candidate, logfile):
                seen.append(candidate.tweet_id)
                return "available", None

            refresh(Args(), con, None, probe=fake_probe)
            self.assertEqual(seen, [ids[2], ids[1], ids[0]])
            con.close()

    def test_video_catalog_counts_ignored_duplicates_per_canonical_video(self):
        connect_db = MODULE["connect_db"]
        Candidate = MODULE["Candidate"]
        upsert_status = MODULE["upsert_status"]
        with tempfile.TemporaryDirectory() as tmp:
            con = connect_db(Path(tmp) / "history.sqlite")
            canonical_id = "1234567890123457001"
            canonical = Candidate(
                tweet_id=canonical_id,
                url=f"https://x.com/LivingGaul/status/{canonical_id}",
            )
            upsert_status(con, canonical, "downloaded", size_bytes=1_000_000)
            canonical_row = con.execute(
                "SELECT id FROM downloads WHERE tweet_id=?", (canonical_id,)
            ).fetchone()
            self.assertIsNotNone(canonical_row)

            for suffix in ("02", "03"):
                tweet_id = f"12345678901234570{suffix}"
                upsert_status(
                    con,
                    Candidate(tweet_id=tweet_id, url=f"https://x.com/LivingGaul/status/{tweet_id}"),
                    "skipped_duplicate",
                    duplicate_of_id=canonical_row["id"],
                    size_bytes=1_000_000,
                )

            row = con.execute(
                "SELECT duplicate_count FROM video_catalog WHERE id=?",
                (canonical_row["id"],),
            ).fetchone()
            self.assertEqual(row["duplicate_count"], 2)

            duplicate_rows = con.execute(
                """
                SELECT duplicate_count
                FROM video_catalog
                WHERE status='skipped_duplicate'
                ORDER BY id
                """
            ).fetchall()
            self.assertEqual([r["duplicate_count"] for r in duplicate_rows], [0, 0])
            con.close()


if __name__ == "__main__":
    unittest.main()
