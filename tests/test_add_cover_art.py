# -*- coding: utf-8 -*-
"""add_cover_art のユニットテスト (標準ライブラリの unittest のみ使用).

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import add_cover_art as aca  # noqa: E402
import synthetic  # noqa: E402

from mutagen.id3 import ID3, TALB, TIT2, TPE1  # noqa: E402
from mutagen.mp4 import MP4  # noqa: E402


def default_options(**overrides) -> argparse.Namespace:
    options = aca.build_parser().parse_args(["dummy"])
    for key, value in overrides.items():
        setattr(options, key, value)
    return options


def fresh_caches() -> dict:
    return {
        "album": {},
        "itunes_limiter": aca.RateLimiter(0),
        "mb_limiter": aca.RateLimiter(0),
    }


class NormalizeTest(unittest.TestCase):
    def test_case_and_width_are_folded(self):
        self.assertEqual(aca.normalize("Ｄａｆｔ　Ｐｕｎｋ"), "daft punk")

    def test_decorations_are_removed(self):
        self.assertEqual(aca.normalize("Discovery (Deluxe Edition)"), "discovery")
        self.assertEqual(aca.normalize("Song feat. Someone"), "song")
        self.assertEqual(aca.normalize("Track - Single"), "track")

    def test_similarity(self):
        self.assertEqual(aca.similarity("Discovery", "discovery"), 1.0)
        self.assertGreater(aca.similarity("Discovery", "Discovery (Remastered)"), 0.9)
        self.assertLess(aca.similarity("Discovery", "まったく別のアルバム"), 0.4)
        self.assertEqual(aca.similarity("", "anything"), 0.5)


class FilenameTest(unittest.TestCase):
    def test_artist_and_title(self):
        self.assertEqual(aca.parse_filename("/m/Daft Punk - One More Time.mp3"),
                         ("Daft Punk", "One More Time"))

    def test_leading_track_number(self):
        self.assertEqual(aca.parse_filename("/m/01 - Daft Punk - Aerodynamic.m4a"),
                         ("Daft Punk", "Aerodynamic"))
        self.assertEqual(aca.parse_filename("/m/03. Veridis Quo.mp3"), ("", "Veridis Quo"))
        self.assertEqual(aca.parse_filename("/m/1-05 - Artist - Song.mp3"), ("Artist", "Song"))

    def test_title_only(self):
        self.assertEqual(aca.parse_filename("/m/Something_Nice.mp3"), ("", "Something Nice"))

    def test_album_from_folder(self):
        self.assertEqual(aca.guess_album_from_folder("/m/Daft Punk - Discovery/01.mp3"), "Discovery")
        self.assertEqual(aca.guess_album_from_folder("/m/2001 - Discovery/01.mp3"), "Discovery")
        self.assertEqual(aca.guess_album_from_folder("/m/Discovery (2001)/01.mp3"), "Discovery")
        self.assertEqual(aca.guess_album_from_folder("/m/Music/01.mp3"), "")


class ImageTest(unittest.TestCase):
    def test_sniff(self):
        self.assertEqual(aca.sniff_image(synthetic.fake_jpeg()), "image/jpeg")
        self.assertEqual(aca.sniff_image(synthetic.fake_png()), "image/png")
        self.assertIsNone(aca.sniff_image(b"<html>not an image</html>"))

    def test_upscale_itunes_url(self):
        url = "https://is1.mzstatic.com/image/thumb/Music/x/source/100x100bb.jpg"
        self.assertTrue(aca.upscale_itunes_url(url, 600).endswith("/600x600bb.jpg"))
        self.assertTrue(
            aca.upscale_itunes_url(url.replace(".jpg", ".png"), 1200).endswith("/1200x1200bb.png")
        )


class TagRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_mp3_without_cover(self):
        path = synthetic.write_mp3(os.path.join(self.dir, "a.mp3"))
        self.assertFalse(aca.has_cover(path))
        cover = aca.Cover(data=synthetic.fake_jpeg(), mime="image/jpeg")
        aca.embed_cover(path, cover)
        self.assertTrue(aca.has_cover(path))
        self.assertEqual(ID3(path).getall("APIC")[0].data, cover.data)

    def test_m4a_without_cover(self):
        path = synthetic.write_m4a(os.path.join(self.dir, "a.m4a"))
        self.assertFalse(aca.has_cover(path))
        cover = aca.Cover(data=synthetic.fake_png(), mime="image/png")
        aca.embed_cover(path, cover)
        self.assertTrue(aca.has_cover(path))
        self.assertEqual(bytes(MP4(path).tags["covr"][0]), cover.data)

    def test_force_replaces_existing_apic(self):
        path = synthetic.write_mp3(os.path.join(self.dir, "a.mp3"))
        aca.embed_cover(path, aca.Cover(data=synthetic.fake_jpeg(5000), mime="image/jpeg"))
        new = synthetic.fake_jpeg(9000)
        aca.embed_cover(path, aca.Cover(data=new, mime="image/jpeg"), replace=True)
        frames = ID3(path).getall("APIC")
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].data, new)

    def test_read_track_info_from_tags(self):
        path = synthetic.write_mp3(os.path.join(self.dir, "99 - junk.mp3"))
        tags = ID3()
        tags.add(TPE1(encoding=3, text=["Daft Punk"]))
        tags.add(TALB(encoding=3, text=["Discovery"]))
        tags.add(TIT2(encoding=3, text=["One More Time"]))
        tags.save(path)
        info = aca.read_track_info(path)
        self.assertTrue(info.from_tags)
        self.assertEqual((info.artist, info.album, info.title),
                         ("Daft Punk", "Discovery", "One More Time"))

    def test_read_track_info_falls_back_to_filename(self):
        folder = os.path.join(self.dir, "Daft Punk - Discovery")
        os.makedirs(folder)
        path = synthetic.write_m4a(os.path.join(folder, "01 - Daft Punk - Aerodynamic.m4a"))
        info = aca.read_track_info(path)
        self.assertFalse(info.from_tags)
        self.assertEqual(info.artist, "Daft Punk")
        self.assertEqual(info.title, "Aerodynamic")
        self.assertEqual(info.album, "Discovery")


class LocalCoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_finds_cover_jpg(self):
        path = synthetic.write_mp3(os.path.join(self.dir, "a.mp3"))
        with open(os.path.join(self.dir, "cover.jpg"), "wb") as handle:
            handle.write(synthetic.fake_jpeg())
        cover = aca.find_local_cover(path)
        self.assertIsNotNone(cover)
        self.assertEqual(cover.mime, "image/jpeg")
        self.assertEqual(cover.source, "local")

    def test_ignores_unrelated_and_broken_images(self):
        path = synthetic.write_mp3(os.path.join(self.dir, "a.mp3"))
        with open(os.path.join(self.dir, "screenshot.jpg"), "wb") as handle:
            handle.write(synthetic.fake_jpeg())
        with open(os.path.join(self.dir, "folder.jpg"), "wb") as handle:
            handle.write(b"not an image")
        self.assertIsNone(aca.find_local_cover(path))


class ScoringTest(unittest.TestCase):
    def test_matching_album_scores_high(self):
        info = aca.TrackInfo(path="x", artist="Daft Punk", album="Discovery")
        self.assertGreater(aca.score_candidate(info, "Daft Punk", "Discovery", ""), 0.95)

    def test_wrong_artist_scores_low(self):
        info = aca.TrackInfo(path="x", artist="Daft Punk", album="Discovery")
        self.assertLess(aca.score_candidate(info, "全然違う人", "別のアルバム", ""), 0.4)

    def test_query_order_and_dedupe(self):
        info = aca.TrackInfo(path="x", artist="A", album="B", title="C")
        self.assertEqual(aca.itunes_queries(info), [("album", "A B"), ("song", "A C")])
        self.assertEqual(aca.itunes_queries(aca.TrackInfo(path="x")), [])


class FakeNetwork:
    """http_get / http_get_json を差し替えるためのスタブ."""

    def __init__(self, results, image=None):
        self.results = results
        self.image = image if image is not None else synthetic.fake_jpeg()
        self.json_calls = []
        self.get_calls = []

    def get_json(self, url, timeout=20.0):
        self.json_calls.append(url)
        return {"resultCount": len(self.results), "results": self.results}

    def get(self, url, timeout=20.0, retries=3):
        self.get_calls.append(url)
        return self.image


class ProcessFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self._orig = (aca.http_get, aca.http_get_json)

        def restore():
            aca.http_get, aca.http_get_json = self._orig

        self.addCleanup(restore)

    def install(self, network):
        aca.http_get = network.get
        aca.http_get_json = network.get_json
        return network

    def make_tagged_mp3(self, name="track.mp3", artist="Daft Punk", album="Discovery"):
        path = synthetic.write_mp3(os.path.join(self.dir, name))
        tags = ID3()
        tags.add(TPE1(encoding=3, text=[artist]))
        tags.add(TALB(encoding=3, text=[album]))
        tags.save(path)
        return path

    def test_embeds_from_itunes(self):
        path = self.make_tagged_mp3()
        net = self.install(FakeNetwork([{
            "artistName": "Daft Punk",
            "collectionName": "Discovery",
            "artworkUrl100": "https://example.com/source/100x100bb.jpg",
        }]))
        result = aca.process_file(path, default_options(), fresh_caches())
        self.assertEqual(result.status, "embedded")
        self.assertEqual(result.source, "itunes:album")
        self.assertTrue(aca.has_cover(path))
        self.assertIn("600x600bb.jpg", net.get_calls[0])

    def test_dry_run_does_not_modify(self):
        path = self.make_tagged_mp3()
        self.install(FakeNetwork([{
            "artistName": "Daft Punk",
            "collectionName": "Discovery",
            "artworkUrl100": "https://example.com/source/100x100bb.jpg",
        }]))
        result = aca.process_file(path, default_options(dry_run=True), fresh_caches())
        self.assertEqual(result.status, "dry-run")
        self.assertFalse(aca.has_cover(path))

    def test_low_score_candidate_is_rejected(self):
        path = self.make_tagged_mp3()
        self.install(FakeNetwork([{
            "artistName": "無関係なアーティスト",
            "collectionName": "無関係なアルバム",
            "artworkUrl100": "https://example.com/source/100x100bb.jpg",
        }]))
        result = aca.process_file(path, default_options(), fresh_caches())
        self.assertEqual(result.status, "not-found")
        self.assertFalse(aca.has_cover(path))

    def test_non_image_response_is_rejected(self):
        path = self.make_tagged_mp3()
        self.install(FakeNetwork(
            [{"artistName": "Daft Punk", "collectionName": "Discovery",
              "artworkUrl100": "https://example.com/source/100x100bb.jpg"}],
            image=b"<html>error page</html>",
        ))
        result = aca.process_file(path, default_options(), fresh_caches())
        self.assertEqual(result.status, "not-found")
        self.assertFalse(aca.has_cover(path))

    def test_existing_cover_is_skipped(self):
        path = self.make_tagged_mp3()
        aca.embed_cover(path, aca.Cover(data=synthetic.fake_jpeg(), mime="image/jpeg"))
        net = self.install(FakeNetwork([]))
        result = aca.process_file(path, default_options(), fresh_caches())
        self.assertEqual(result.status, "skipped-has-cover")
        self.assertEqual(net.json_calls, [])

    def test_local_cover_is_preferred_over_network(self):
        path = self.make_tagged_mp3()
        with open(os.path.join(self.dir, "cover.jpg"), "wb") as handle:
            handle.write(synthetic.fake_jpeg())
        net = self.install(FakeNetwork([]))
        result = aca.process_file(path, default_options(), fresh_caches())
        self.assertEqual(result.status, "embedded")
        self.assertEqual(result.source, "local")
        self.assertEqual(net.json_calls, [])

    def test_offline_mode_never_calls_network(self):
        path = self.make_tagged_mp3()
        net = self.install(FakeNetwork([]))
        result = aca.process_file(path, default_options(offline=True, no_local=True), fresh_caches())
        self.assertEqual(result.status, "not-found")
        self.assertEqual(net.json_calls, [])

    def test_album_cache_avoids_repeat_lookups(self):
        first = self.make_tagged_mp3("01.mp3")
        second = self.make_tagged_mp3("02.mp3")
        net = self.install(FakeNetwork([{
            "artistName": "Daft Punk",
            "collectionName": "Discovery",
            "artworkUrl100": "https://example.com/source/100x100bb.jpg",
        }]))
        caches = fresh_caches()
        options = default_options(no_local=True)
        self.assertEqual(aca.process_file(first, options, caches).status, "embedded")
        calls_after_first = len(net.json_calls)
        self.assertEqual(aca.process_file(second, options, caches).status, "embedded")
        self.assertEqual(len(net.json_calls), calls_after_first)

    def test_backup_is_created(self):
        path = self.make_tagged_mp3()
        self.install(FakeNetwork([{
            "artistName": "Daft Punk",
            "collectionName": "Discovery",
            "artworkUrl100": "https://example.com/source/100x100bb.jpg",
        }]))
        aca.process_file(path, default_options(backup=True), fresh_caches())
        self.assertTrue(os.path.exists(path + ".bak"))
        self.assertFalse(aca.has_cover(path + ".bak"))

    def test_m4a_end_to_end(self):
        folder = os.path.join(self.dir, "Daft Punk - Discovery")
        os.makedirs(folder)
        path = synthetic.write_m4a(os.path.join(folder, "01 - Daft Punk - Aerodynamic.m4a"))
        self.install(FakeNetwork(
            [{"artistName": "Daft Punk", "collectionName": "Discovery",
              "artworkUrl100": "https://example.com/source/100x100bb.jpg"}],
            image=synthetic.fake_png(),
        ))
        result = aca.process_file(path, default_options(), fresh_caches())
        self.assertEqual(result.status, "embedded")
        self.assertEqual(bytes(MP4(path).tags["covr"][0])[:8], synthetic.fake_png()[:8])


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_recursive_and_extension_filter(self):
        os.makedirs(os.path.join(self.dir, "sub"))
        synthetic.write_mp3(os.path.join(self.dir, "a.mp3"))
        synthetic.write_m4a(os.path.join(self.dir, "sub", "b.m4a"))
        open(os.path.join(self.dir, "notes.txt"), "w").close()
        open(os.path.join(self.dir, "c.flac"), "w").close()

        found = sorted(os.path.basename(p) for p in aca.iter_audio_files([self.dir]))
        self.assertEqual(found, ["a.mp3", "b.m4a"])

        shallow = sorted(os.path.basename(p) for p in aca.iter_audio_files([self.dir], recursive=False))
        self.assertEqual(shallow, ["a.mp3"])

    def test_single_file_argument(self):
        path = synthetic.write_mp3(os.path.join(self.dir, "a.mp3"))
        self.assertEqual(list(aca.iter_audio_files([path])), [os.path.abspath(path)])


class ReportTest(unittest.TestCase):
    def test_csv_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = os.path.join(tmp, "report.csv")
            aca.write_report(
                [aca.Result(path="/m/a.mp3", status="embedded", source="itunes:album", score=0.98)],
                destination,
            )
            with open(destination, encoding="utf-8-sig") as handle:
                lines = handle.read().splitlines()
            self.assertTrue(lines[0].startswith("file,status,source"))
            self.assertIn("embedded", lines[1])


class CliTest(unittest.TestCase):
    def test_dry_run_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            synthetic.write_mp3(os.path.join(tmp, "a.mp3"))
            report = os.path.join(tmp, "r.csv")
            code = aca.main([tmp, "--dry-run", "--offline", "--quiet", "--report", report])
            self.assertEqual(code, 0)
            self.assertTrue(os.path.exists(report))


if __name__ == "__main__":
    unittest.main()
