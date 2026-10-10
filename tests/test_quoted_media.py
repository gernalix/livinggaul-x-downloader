import io
import json
import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE = runpy.run_path(str(Path(__file__).resolve().parents[1] / "livinggaul-x-downloader"))
BASE = "https://video.twimg.com/ext_tw_video/123/pu/vid/"


class QuotedMediaTests(unittest.TestCase):
    def test_prefers_largest_mp4_not_above_480p(self):
        choose = MODULE["fxtwitter_video_url"]
        videos = [{"formats": [
            {"container": "mp4", "url": BASE + "320x180/small.mp4"},
            {"container": "mp4", "url": BASE + "480x270/good.mp4"},
            {"container": "mp4", "url": BASE + "1280x720/large.mp4"},
        ]}]
        self.assertEqual(choose({"media": {"videos": videos}}), BASE + "480x270/good.mp4")

    def test_tall_video_uses_smallest_variant_for_downscaling(self):
        choose = MODULE["fxtwitter_video_url"]
        videos = [{"formats": [
            {"url": BASE + "480x840/large.mp4"},
            {"url": BASE + "320x560/small.mp4"},
        ]}]
        self.assertEqual(choose({"media": {"videos": videos}}), BASE + "320x560/small.mp4")

    def test_does_not_follow_untrusted_media_domains(self):
        choose = MODULE["fxtwitter_video_url"]
        self.assertIsNone(choose({"media": {"videos": [
            {"formats": [{"url": "https://example.org/video/480x270/evil.mp4"},
                         {"url": "http://video.twimg.com/a/480x270/unsecured.mp4"}]}
        ]}}))

    def test_resolves_quote_without_overwriting_original_post_identity(self):
        candidate = MODULE["Candidate"](
            tweet_id="1234567890123456789",
            url="https://x.com/LivingGaul/status/1234567890123456789",
        )
        payload = {"tweet": {
            "id": candidate.tweet_id,
            "text": "shared video",
            "created_timestamp": 1700000000,
            "author": {"screen_name": "LivingGaul", "id": "123"},
            "quote": {"url": "https://x.com/source/status/2222222222222222222",
                      "media": {"videos": [{"formats": [{"url": BASE + "480x270/one.mp4"}]}]}}
        }}
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
            meta = MODULE["extract_fxtwitter_metadata"](candidate, None)
        self.assertEqual(meta["id"], candidate.tweet_id)
        self.assertEqual(meta["uploader"], "LivingGaul")
        self.assertTrue(meta["_download_no_cookies"])
        self.assertEqual(meta["_download_url"], BASE + "480x270/one.mp4")
        self.assertEqual(meta["quoted_source_url"], "https://x.com/source/status/2222222222222222222")


if __name__ == "__main__":
    unittest.main()
