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

    def test_existing_database_is_migrated_with_content_columns(self):
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
            self.assertTrue(
                {"media_key", "content_fingerprint", "duration_ms", "duplicate_of_id"}.issubset(columns)
            )
            con.close()


if __name__ == "__main__":
    unittest.main()
