#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mp3 / m4a のカバーアート(ジャケット画像)を自動で付与するツール.

やること:
  1. 指定フォルダ以下の .mp3 / .m4a を再帰的に探す
  2. 既にカバーアートが埋め込まれているファイルはスキップ
  3. 無いものは タグ(アーティスト/アルバム/曲名) or ファイル名 から検索語を組み立て
     - 同じフォルダ内の cover.jpg / folder.jpg などをまず利用
     - 次に iTunes Search API (APIキー不要) で画像を検索
     - 見つからなければ MusicBrainz + Cover Art Archive にフォールバック
  4. 取得した画像を検証してファイルに埋め込む

外部依存は mutagen のみ (HTTP は標準ライブラリ urllib を使用)。

  pip install mutagen
  python add_cover_art.py "D:/Music" --dry-run
  python add_cover_art.py "D:/Music"
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional, Sequence

try:
    import preview  # 同じフォルダにある preview.py(候補画像の表示)
except ImportError:  # pragma: no cover - 配置ミスの案内のみ
    sys.stderr.write(
        "preview.py が見つかりません。add_cover_art.py と同じフォルダに置いてください。\n"
    )
    raise SystemExit(2)

try:
    from mutagen.id3 import APIC, ID3, ID3NoHeaderError
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4, MP4Cover
except ImportError:  # pragma: no cover - 起動時の案内のみ
    sys.stderr.write(
        "mutagen が必要です。次のコマンドでインストールしてください:\n"
        "    pip install mutagen\n"
    )
    raise SystemExit(2)


__version__ = "1.0.0"

AUDIO_EXTENSIONS = (".mp3", ".m4a")
LOCAL_COVER_NAMES = (
    "cover", "folder", "front", "album", "albumart", "albumartsmall", "jacket",
)
LOCAL_COVER_EXTS = (".jpg", ".jpeg", ".png")

USER_AGENT = f"add-cover-art/{__version__} (https://github.com/ORCA-IRUKA-222/jacket-image)"
ITUNES_ENDPOINT = "https://itunes.apple.com/search"
MUSICBRAINZ_ENDPOINT = "https://musicbrainz.org/ws/2/release/"
COVERART_ARCHIVE = "https://coverartarchive.org/release/{mbid}/front-{size}"

MIN_IMAGE_BYTES = 4 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024


# --------------------------------------------------------------------------
# データ構造
# --------------------------------------------------------------------------
@dataclass
class TrackInfo:
    """1 ファイルから読み取った(あるいは推定した)メタ情報."""

    path: str
    artist: str = ""
    album: str = ""
    title: str = ""
    album_artist: str = ""
    from_tags: bool = False

    @property
    def best_artist(self) -> str:
        return self.album_artist or self.artist

    def album_key(self) -> Optional[tuple]:
        """同一アルバムをまとめてキャッシュするためのキー."""
        if self.album and self.best_artist:
            return (normalize(self.best_artist), normalize(self.album))
        return None


@dataclass
class Cover:
    """埋め込む画像."""

    data: bytes
    mime: str
    source: str = ""
    url: str = ""
    score: float = 1.0
    label: str = ""  # 画面に出す「アーティスト / アルバム」の表示名


@dataclass
class Result:
    path: str
    # embedded / dry-run / skipped-has-cover / skipped-by-user / not-found
    # / no-metadata / error / quit
    status: str
    source: str = ""
    query: str = ""
    score: float = 0.0
    url: str = ""
    message: str = ""


@dataclass
class Stats:
    total: int = 0
    embedded: int = 0
    dry_run: int = 0
    skipped: int = 0
    user_skipped: int = 0
    not_found: int = 0
    errors: int = 0
    results: list = field(default_factory=list)

    def count(self, result: "Result") -> None:
        bucket = {
            "embedded": "embedded",
            "dry-run": "dry_run",
            "skipped-has-cover": "skipped",
            "skipped-by-user": "user_skipped",
            "error": "errors",
        }.get(result.status)
        if bucket is None:
            if result.status != "quit":
                self.not_found += 1
            return
        setattr(self, bucket, getattr(self, bucket) + 1)


# --------------------------------------------------------------------------
# 文字列の正規化・比較
# --------------------------------------------------------------------------
_NOISE_PATTERNS = (
    r"\(([^)]*(deluxe|remaster|edition|version|bonus|explicit|instrumental|live|single|ep)[^)]*)\)",
    r"\[([^\]]*(deluxe|remaster|edition|version|bonus|explicit|instrumental|live|single|ep)[^\]]*)\]",
    r"\b(feat|ft|featuring)\.?\s+.*$",
    r"\s-\s(single|ep)$",
)


def normalize(text: str) -> str:
    """比較用に文字列を正規化する(全角/半角、記号、装飾語の除去)."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text)).casefold().strip()
    for pattern in _NOISE_PATTERNS:
        text = re.sub(pattern, " ", text)
    text = re.sub(r"[^\w\s\u3040-\u30ff\u4e00-\u9fff]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def similarity(a: str, b: str) -> float:
    """0.0〜1.0 の類似度。片方が空なら中立の 0.5 を返す."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.5
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.92
    return difflib.SequenceMatcher(None, na, nb).ratio()


# --------------------------------------------------------------------------
# ファイル探索
# --------------------------------------------------------------------------
def iter_audio_files(roots: Sequence[str], recursive: bool = True) -> Iterator[str]:
    """指定パス(ファイル or フォルダ)から対象の音声ファイルを列挙する."""
    for root in roots:
        if os.path.isfile(root):
            if root.lower().endswith(AUDIO_EXTENSIONS):
                yield os.path.abspath(root)
            continue
        if not os.path.isdir(root):
            sys.stderr.write(f"警告: 見つかりません: {root}\n")
            continue
        if recursive:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for name in sorted(filenames):
                    if name.lower().endswith(AUDIO_EXTENSIONS):
                        yield os.path.abspath(os.path.join(dirpath, name))
        else:
            for name in sorted(os.listdir(root)):
                full = os.path.join(root, name)
                if os.path.isfile(full) and name.lower().endswith(AUDIO_EXTENSIONS):
                    yield os.path.abspath(full)


# --------------------------------------------------------------------------
# タグの読み取り
# --------------------------------------------------------------------------
def _first(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return str(value[0]).strip() if value else ""
    return str(value).strip()


def has_cover(path: str) -> bool:
    """既にカバーアートが埋め込まれているか."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        try:
            return bool(ID3(path).getall("APIC"))
        except ID3NoHeaderError:
            return False
    if ext == ".m4a":
        tags = MP4(path).tags
        return bool(tags and tags.get("covr"))
    return False


def read_track_info(path: str) -> TrackInfo:
    """タグを読み、足りない情報はファイル名/フォルダ名から補う."""
    info = TrackInfo(path=path)
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".mp3":
            audio = MP3(path)
            tags = audio.tags
            if tags:
                info.artist = _first(tags.get("TPE1"))
                info.album = _first(tags.get("TALB"))
                info.title = _first(tags.get("TIT2"))
                info.album_artist = _first(tags.get("TPE2"))
        elif ext == ".m4a":
            tags = MP4(path).tags
            if tags:
                info.artist = _first(tags.get("\xa9ART"))
                info.album = _first(tags.get("\xa9alb"))
                info.title = _first(tags.get("\xa9nam"))
                info.album_artist = _first(tags.get("aART"))
    except Exception as exc:  # 壊れたタグでも処理を続ける
        sys.stderr.write(f"警告: タグ読み取り失敗 ({os.path.basename(path)}): {exc}\n")

    info.from_tags = bool(info.artist or info.album or info.title)
    guess_artist, guess_title = parse_filename(path)
    if not info.artist:
        info.artist = guess_artist
    if not info.title:
        info.title = guess_title
    if not info.album:
        info.album = guess_album_from_folder(path)
    return info


_TRACK_NUMBER = re.compile(r"^\s*\d{1,3}\s*[-._)\]]\s*")
_DISC_TRACK = re.compile(r"^\s*\d{1,2}[-.]\d{1,3}\s*[-._)\]]\s*")


def parse_filename(path: str) -> tuple:
    """"01 - Artist - Title.mp3" のようなファイル名から (artist, title) を推定."""
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = _DISC_TRACK.sub("", stem)
    stem = _TRACK_NUMBER.sub("", stem)
    stem = stem.replace("_", " ").strip()
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", stem) if p.strip()]
    if len(parts) >= 2:
        return parts[0], " - ".join(parts[1:])
    return "", stem


def guess_album_from_folder(path: str) -> str:
    """親フォルダ名をアルバム名の候補にする ("Artist - Album (2001)" 等に対応)."""
    folder = os.path.basename(os.path.dirname(os.path.abspath(path)))
    if not folder or folder.lower() in {"music", "musics", "songs", "mp3", "m4a", "download", "downloads"}:
        return ""
    folder = re.sub(r"^\s*[\[(]?\d{4}[\])]?\s*[-._]?\s*", "", folder)
    folder = re.sub(r"\s*[\[(]\d{4}[\])]\s*$", "", folder)
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", folder) if p.strip()]
    return parts[-1] if parts else folder.strip()


# --------------------------------------------------------------------------
# ローカル画像の探索
# --------------------------------------------------------------------------
def find_local_cover(path: str) -> Optional[Cover]:
    """同じフォルダ(と親フォルダ)にある cover.jpg 等を探す."""
    directory = os.path.dirname(os.path.abspath(path))
    for folder in (directory, os.path.dirname(directory)):
        if not folder or not os.path.isdir(folder):
            continue
        try:
            entries = os.listdir(folder)
        except OSError:
            continue
        for entry in sorted(entries):
            stem, ext = os.path.splitext(entry)
            if ext.lower() not in LOCAL_COVER_EXTS:
                continue
            if normalize(stem).replace(" ", "") not in LOCAL_COVER_NAMES:
                continue
            full = os.path.join(folder, entry)
            try:
                with open(full, "rb") as handle:
                    data = handle.read(MAX_IMAGE_BYTES + 1)
            except OSError:
                continue
            mime = sniff_image(data)
            if mime and MIN_IMAGE_BYTES <= len(data) <= MAX_IMAGE_BYTES:
                return Cover(data=data, mime=mime, source="local", url=full, score=1.0)
    return None


def sniff_image(data: bytes) -> Optional[str]:
    """マジックバイトから MIME タイプを判定する(想定外のデータを弾く)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class RateLimiter:
    """ホストごとの最低リクエスト間隔を守るための簡易リミッタ."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


def http_get(url: str, timeout: float = 20.0, retries: int = 3) -> Optional[bytes]:
    """GET してボディを返す。失敗時は None(呼び出し側で握りつぶす)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    delay = 1.0
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(MAX_IMAGE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            return None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            return None
    return None


def http_get_json(url: str, timeout: float = 20.0) -> Optional[dict]:
    body = http_get(url, timeout=timeout)
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None


# --------------------------------------------------------------------------
# アートワーク検索
# --------------------------------------------------------------------------
def upscale_itunes_url(url: str, size: int) -> str:
    """artworkUrl100 を任意サイズ (例 600x600bb.jpg) に差し替える."""
    return re.sub(r"/\d+x\d+(bb)?\.(jpg|png)", f"/{size}x{size}bb.\\2", url)


def score_candidate(info: TrackInfo, artist: str, album: str, title: str) -> float:
    """検索結果がこのファイルに合致しそうかを 0.0〜1.0 で採点する."""
    artist_score = similarity(info.best_artist, artist)
    if info.album and album:
        name_score = similarity(info.album, album)
    elif info.title and title:
        name_score = similarity(info.title, title)
    elif info.album and title:
        name_score = similarity(info.album, title)
    else:
        name_score = 0.5
    return round(0.4 * artist_score + 0.6 * name_score, 3)


def itunes_queries(info: TrackInfo) -> list:
    """優先度順の (entity, 検索語) を組み立てる."""
    queries = []
    artist = info.best_artist
    if artist and info.album:
        queries.append(("album", f"{artist} {info.album}"))
    if artist and info.title:
        queries.append(("song", f"{artist} {info.title}"))
    if not artist and info.album:
        queries.append(("album", info.album))
    if not artist and info.title:
        queries.append(("song", info.title))
    seen = set()
    unique = []
    for entity, term in queries:
        key = (entity, normalize(term))
        if key in seen or not normalize(term):
            continue
        seen.add(key)
        unique.append((entity, term))
    return unique


def dedupe_candidates(candidates: list) -> list:
    """同じ画像 URL を除き、スコアの高い順に並べ替える."""
    seen = set()
    unique = []
    for candidate in sorted(candidates, key=lambda c: -c.score):
        if candidate.url in seen:
            continue
        seen.add(candidate.url)
        unique.append(candidate)
    return unique


def itunes_candidates(
    info: TrackInfo,
    size: int,
    country: str,
    limiter: "RateLimiter",
    term: Optional[str] = None,
    stop_at: Optional[float] = None,
    limit: int = 10,
) -> list:
    """iTunes Search API の検索結果を Cover の候補リストにして返す.

    term を渡すと、タグではなくそのキーワードで検索します(手動での再検索用)。
    stop_at を渡すと、それ以上のスコアの候補が出た時点で打ち切ります(自動処理用)。
    """
    queries = [("album", term), ("song", term)] if term else itunes_queries(info)
    found = []
    for entity, query in queries:
        params = urllib.parse.urlencode(
            {"term": query, "entity": entity, "limit": limit, "country": country, "media": "music"}
        )
        limiter.wait()
        payload = http_get_json(f"{ITUNES_ENDPOINT}?{params}")
        if not payload:
            continue
        for item in payload.get("results", []):
            art = item.get("artworkUrl100") or item.get("artworkUrl60")
            if not art:
                continue
            artist_name = item.get("artistName", "")
            collection = item.get("collectionName", "")
            track_name = item.get("trackName", "")
            found.append(
                Cover(
                    data=b"",
                    mime="",
                    source=f"itunes:{entity}",
                    url=upscale_itunes_url(art, size),
                    score=score_candidate(info, artist_name, collection, track_name),
                    label=" / ".join(x for x in (artist_name, collection or track_name) if x),
                )
            )
        if stop_at is not None and found and max(c.score for c in found) >= stop_at:
            break
    return dedupe_candidates(found)


def musicbrainz_candidates(
    info: TrackInfo,
    size: int,
    limiter: "RateLimiter",
    term: Optional[str] = None,
    limit: int = 5,
) -> list:
    """MusicBrainz のリリース検索から Cover Art Archive の候補を作る."""
    if term:
        query = term
    else:
        album = info.album or info.title
        if not album:
            return []
        clauses = [f'release:"{album}"']
        if info.best_artist:
            clauses.append(f'artist:"{info.best_artist}"')
        query = " AND ".join(clauses)

    params = urllib.parse.urlencode({"query": query, "fmt": "json", "limit": limit})
    limiter.wait()
    payload = http_get_json(f"{MUSICBRAINZ_ENDPOINT}?{params}")
    if not payload:
        return []

    caa_size = 500 if size <= 500 else 1200
    found = []
    for release in payload.get("releases", []):
        mbid = release.get("id")
        if not mbid:
            continue
        credits = release.get("artist-credit") or []
        release_artist = credits[0].get("name", "") if credits else ""
        title = release.get("title", "")
        found.append(
            Cover(
                data=b"",
                mime="",
                source="musicbrainz",
                url=COVERART_ARCHIVE.format(mbid=mbid, size=caa_size),
                score=score_candidate(info, release_artist, title, ""),
                label=" / ".join(x for x in (release_artist, title) if x),
            )
        )
    return dedupe_candidates(found)


def search_itunes(info: TrackInfo, size: int, country: str, limiter: "RateLimiter") -> Optional[Cover]:
    """自動処理用: iTunes の最有力候補を 1 件返す."""
    candidates = itunes_candidates(info, size, country, limiter, stop_at=0.9)
    return candidates[0] if candidates else None


def search_musicbrainz(info: TrackInfo, size: int, limiter: "RateLimiter") -> Optional[Cover]:
    """自動処理用: MusicBrainz の最有力候補を 1 件返す."""
    candidates = musicbrainz_candidates(info, size, limiter)
    return candidates[0] if candidates else None


def collect_candidates(
    info: TrackInfo, options: argparse.Namespace, caches: dict, term: Optional[str] = None
) -> list:
    """対話モード用: ローカル画像も含めた候補を優先度順に集める."""
    candidates = []
    if term is None and not options.no_local:
        local = find_local_cover(info.path)
        if local:
            local.label = f"フォルダ内の画像: {os.path.basename(local.url)}"
            candidates.append(local)
    if not options.offline:
        candidates.extend(
            itunes_candidates(
                info, options.size, options.country, caches["itunes_limiter"],
                term=term, limit=options.candidates,
            )
        )
        candidates.extend(
            musicbrainz_candidates(
                info, options.size, caches["mb_limiter"], term=term, limit=options.candidates,
            )
        )
    ranked = dedupe_candidates([c for c in candidates if c.source != "local"])
    local_first = [c for c in candidates if c.source == "local"]
    return local_first + ranked


def fetch_cover(candidate: Cover) -> Optional[Cover]:
    """候補 URL から画像を落として検証する(ローカル画像は取得済みなのでそのまま)."""
    if candidate.data:
        return candidate if sniff_image(candidate.data) else None
    data = http_get(candidate.url, timeout=30.0)
    if not data:
        return None
    mime = sniff_image(data)
    if not mime or not (MIN_IMAGE_BYTES <= len(data) <= MAX_IMAGE_BYTES):
        return None
    candidate.data = data
    candidate.mime = mime
    return candidate


# --------------------------------------------------------------------------
# 埋め込み
# --------------------------------------------------------------------------
def embed_cover(path: str, cover: Cover, replace: bool = False) -> None:
    """mp3 は ID3 の APIC、m4a は covr アトムとして画像を書き込む."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        if replace:
            tags.delall("APIC")
        tags.add(
            APIC(encoding=0, mime=cover.mime, type=3, desc="Cover", data=cover.data)
        )
        # v2.3 で保存すると Windows エクスプローラ等での表示互換性が高い
        tags.save(path, v2_version=3)
    elif ext == ".m4a":
        audio = MP4(path)
        if audio.tags is None:
            audio.add_tags()
        image_format = (
            MP4Cover.FORMAT_PNG if cover.mime == "image/png" else MP4Cover.FORMAT_JPEG
        )
        audio.tags["covr"] = [MP4Cover(cover.data, imageformat=image_format)]
        audio.save()
    else:
        raise ValueError(f"未対応の拡張子です: {ext}")


def backup_file(path: str) -> str:
    """上書き前のバックアップを作る (.bak を付与、重複時は連番)."""
    import shutil

    target = path + ".bak"
    index = 1
    while os.path.exists(target):
        target = f"{path}.bak{index}"
        index += 1
    shutil.copy2(path, target)
    return target


def save_folder_jpg(path: str, cover: Cover) -> Optional[str]:
    """フォルダに cover.jpg / cover.png も書き出す(既存があれば何もしない)."""
    ext = ".png" if cover.mime == "image/png" else ".jpg"
    target = os.path.join(os.path.dirname(os.path.abspath(path)), "cover" + ext)
    if os.path.exists(target):
        return None
    with open(target, "wb") as handle:
        handle.write(cover.data)
    return target


# --------------------------------------------------------------------------
# 対話モード(1 件ずつ画像を見て確定する)
# --------------------------------------------------------------------------
HELP_TEXT = """\
  [Enter] この画像で確定      [n] 次の候補を表示 / 再検索
  [k]     キーワードを入力    [s] このファイルは飛ばす
  [v]     OS の画像ビューアで開く(自分で閉じる必要があります)
  [q]     終了(ここまでの結果は保存されます)"""


@dataclass
class Decision:
    action: str  # accept / skip / quit
    cover: Optional[Cover] = None


class ConsoleUI:
    """対話の入出力をまとめたもの(テストではこれを差し替える)."""

    def __init__(self, previewer, ask=input, out=print) -> None:
        self.previewer = previewer
        self._ask = ask
        self.out = out

    def preview(self, cover: Cover, lines: Sequence[str]) -> None:
        self.previewer.show(cover.data, lines)

    def ask_key(self, prompt: str) -> str:
        try:
            return self._ask(prompt).strip().lower()
        except EOFError:
            return "q"

    def ask_text(self, prompt: str) -> str:
        try:
            return self._ask(prompt).strip()
        except EOFError:
            return ""

    def info(self, message: str) -> None:
        self.out(message)

    def close(self) -> None:
        self.previewer.close()


def describe_candidate(info: TrackInfo, cover: Cover, index: int, total: int) -> list:
    """プレビューに添える説明文."""
    size = preview.image_dimensions(cover.data)
    lines = [
        f"ファイル : {os.path.basename(info.path)}",
        f"タグ     : {info.best_artist or '(不明)'} / {info.album or info.title or '(不明)'}",
        f"候補     : {index + 1}/{total}  {cover.label or '(名称不明)'}",
        f"取得元   : {cover.source}  一致度 {cover.score}",
    ]
    if size:
        lines.append(f"画像     : {size[0]}x{size[1]} px")
    return lines


def interactive_select(
    info: TrackInfo, options: argparse.Namespace, caches: dict, ui: ConsoleUI
) -> Decision:
    """候補を 1 件ずつ見せて、Enter で確定 / n で次 / k でキーワード再検索."""
    term: Optional[str] = None
    candidates = collect_candidates(info, options, caches, term)
    index = 0

    while True:
        if index >= len(candidates):
            if candidates:
                ui.info("  候補が尽きました。")
            else:
                ui.info("  候補が見つかりませんでした。")
            keyword = ui.ask_text("  検索キーワード(空 Enter でこのファイルは飛ばす)> ")
            if not keyword:
                return Decision("skip")
            term = keyword
            candidates = collect_candidates(info, options, caches, term)
            index = 0
            continue

        candidate = candidates[index]
        cover = fetch_cover(candidate)
        if cover is None:
            index += 1
            continue

        ui.preview(cover, describe_candidate(info, cover, index, len(candidates)))
        answer = ui.ask_key("  [Enter]確定 / [n]次 / [k]キーワード / [s]飛ばす / [q]終了 > ")

        if answer == "":
            return Decision("accept", cover)
        if answer == "n":
            index += 1
            continue
        if answer == "k":
            keyword = ui.ask_text("  検索キーワード(空 Enter で候補一覧に戻る)> ")
            if keyword:
                term = keyword
                candidates = collect_candidates(info, options, caches, term)
                index = 0
            continue
        if answer == "s":
            return Decision("skip")
        if answer == "q":
            return Decision("quit")
        if answer == "v":
            path = preview.open_in_os_viewer(cover.data, cover.mime)
            ui.info(f"  ビューアで開きました: {path}" if path else "  ビューアを開けませんでした")
            continue
        ui.info(HELP_TEXT)


# --------------------------------------------------------------------------
# 1 ファイルの処理
# --------------------------------------------------------------------------
def process_file(
    path: str, options: argparse.Namespace, caches: dict, ui: Optional[ConsoleUI] = None
) -> Result:
    """1 ファイルを処理する。ui を渡すと 1 件ずつ確認する対話モードになる."""
    try:
        if not options.force and has_cover(path):
            return Result(path=path, status="skipped-has-cover")
    except Exception as exc:
        return Result(path=path, status="error", message=f"読み取り失敗: {exc}")

    info = read_track_info(path)
    if not (info.best_artist or info.album or info.title):
        return Result(path=path, status="no-metadata", message="検索の手がかりがありません")

    query = f"{info.best_artist} / {info.album or info.title}".strip(" /")
    cover: Optional[Cover] = None
    album_key = info.album_key()

    # 同じアルバムで一度決まった画像は、以降のトラックにそのまま使う
    # (対話モードで何十回も同じ確認をしないため)
    if album_key and album_key in caches["album"] and not options.ask_every_file:
        cover = caches["album"][album_key]

    if cover is None and ui is not None:
        ui.info(f"\n■ {os.path.basename(path)}  [{query or 'タグ情報なし'}]")
        decision = interactive_select(info, options, caches, ui)
        if decision.action == "quit":
            return Result(path=path, status="quit", query=query)
        if decision.action == "skip":
            return Result(path=path, status="skipped-by-user", query=query,
                          message="ユーザーが見送りました")
        cover = decision.cover

    if cover is None and ui is None and not options.no_local:
        cover = find_local_cover(path)

    if cover is None and ui is None and not options.offline:
        candidate = search_itunes(info, options.size, options.country, caches["itunes_limiter"])
        if candidate is None or candidate.score < options.min_score:
            fallback = search_musicbrainz(info, options.size, caches["mb_limiter"])
            if fallback and (candidate is None or fallback.score > candidate.score):
                candidate = fallback
        if candidate is not None and candidate.score >= options.min_score:
            cover = fetch_cover(candidate)
        elif candidate is not None:
            return Result(
                path=path,
                status="not-found",
                query=query,
                score=candidate.score,
                source=candidate.source,
                message=f"一致度が低いためスキップ (score={candidate.score} < {options.min_score})",
            )

    if cover is None:
        return Result(path=path, status="not-found", query=query, message="画像が見つかりませんでした")

    if album_key:
        caches["album"][album_key] = cover

    if options.dry_run:
        return Result(
            path=path, status="dry-run", source=cover.source, query=query,
            score=cover.score, url=cover.url,
        )

    try:
        if options.backup:
            backup_file(path)
        embed_cover(path, cover, replace=options.force)
        if options.save_folder_jpg:
            save_folder_jpg(path, cover)
    except Exception as exc:
        return Result(path=path, status="error", query=query, message=f"書き込み失敗: {exc}")

    return Result(
        path=path, status="embedded", source=cover.source, query=query,
        score=cover.score, url=cover.url,
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
STATUS_LABEL = {
    "embedded": "付与",
    "dry-run": "付与予定",
    "skipped-has-cover": "既にあり",
    "skipped-by-user": "見送り",
    "not-found": "見つからず",
    "no-metadata": "情報不足",
    "error": "エラー",
    "quit": "中断",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="add_cover_art.py",
        description="mp3 / m4a にカバーアートが無ければ Web から探して付与します。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例:\n"
            '  python add_cover_art.py "D:/Music" --dry-run       # 変更せず結果だけ確認\n'
            '  python add_cover_art.py "D:/Music" -i              # 画像を見ながら1件ずつ確定\n'
            '  python add_cover_art.py "D:/Music" -i --preview terminal  # ウィンドウを使わない\n'
            '  python add_cover_art.py "D:/Music" --backup        # 全自動で付与\n'
        ),
    )
    parser.add_argument("paths", nargs="+", help="対象のフォルダまたはファイル")
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="候補画像を見せて Enter で確定、n で次の候補、k でキーワード入力")
    parser.add_argument("--preview", choices=preview.PREVIEW_MODES, default="auto",
                        help="対話モードの表示方法: window(1枚のウィンドウを使い回す) / "
                             "terminal(ウィンドウ無し・端末内に描画) / none(文字のみ) / auto(既定)")
    parser.add_argument("--preview-width", type=int, default=44,
                        help="terminal 表示のときの横幅(文字数、既定: 44)")
    parser.add_argument("--candidates", type=int, default=8,
                        help="1 回の検索で集める候補数(既定: 8)")
    parser.add_argument("--ask-every-file", action="store_true",
                        help="同じアルバムでも 1 ファイルずつ確認する(既定はアルバム単位で 1 回)")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false",
                        help="サブフォルダを辿らない")
    parser.add_argument("--dry-run", action="store_true",
                        help="ファイルを変更せず、何をするかだけ表示する")
    parser.add_argument("--force", action="store_true",
                        help="既にカバーアートがあるファイルも上書きする")
    parser.add_argument("--backup", action="store_true",
                        help="書き込み前に .bak バックアップを作る")
    parser.add_argument("--size", type=int, default=600,
                        help="取得する画像サイズ(正方形ピクセル、既定: 600)")
    parser.add_argument("--country", default="JP",
                        help="iTunes ストアの国コード(既定: JP)")
    parser.add_argument("--min-score", type=float, default=0.6,
                        help="自動モードで採用する最低一致度 0.0〜1.0(既定: 0.6)")
    parser.add_argument("--no-local", action="store_true",
                        help="フォルダ内の cover.jpg などを使わず、必ず Web から取得する")
    parser.add_argument("--offline", action="store_true",
                        help="Web 検索をせず、フォルダ内の画像だけで付与する")
    parser.add_argument("--save-folder-jpg", action="store_true",
                        help="埋め込みに加えてフォルダに cover.jpg も保存する")
    parser.add_argument("--report", metavar="CSV",
                        help="処理結果を CSV に書き出す")
    parser.add_argument("--limit", type=int, default=0,
                        help="処理するファイル数の上限(動作確認用、0 で無制限)")
    parser.add_argument("--quiet", action="store_true", help="1件ごとの出力を抑える")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def write_report(results: Iterable[Result], destination: str) -> None:
    with open(destination, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["file", "status", "source", "query", "score", "url", "message"])
        for item in results:
            writer.writerow([
                item.path, item.status, item.source, item.query,
                item.score, item.url, item.message,
            ])


def print_summary(stats: Stats, options: argparse.Namespace) -> None:
    applied_label = "付与予定" if options.dry_run else "付与"
    applied_count = stats.dry_run if options.dry_run else stats.embedded
    print(
        "\n--- 結果 ---\n"
        f"{'対象':　<5}: {stats.total} 件\n"
        f"{applied_label:　<5}: {applied_count} 件\n"
        f"{'既にあり':　<5}: {stats.skipped} 件\n"
        f"{'見送り':　<5}: {stats.user_skipped} 件\n"
        f"{'見つからず':　<5}: {stats.not_found} 件\n"
        f"{'エラー':　<5}: {stats.errors} 件"
    )


def make_ui(options: argparse.Namespace) -> ConsoleUI:
    """対話モードの UI を用意する(プレビュー方式は自動フォールバック)."""
    previewer, note = preview.create_previewer(
        options.preview, width=options.preview_width
    )
    print(f"対話モード: プレビュー = {previewer.name}" + (f" ({note})" if note else ""))
    if previewer.name == "none" and not preview.pillow_available():
        print("画像を表示するには Pillow が必要です:  pip install pillow")
    if previewer.name == "window":
        print("ウィンドウは 1 枚だけを使い回します。終了時に自動で閉じます。")
    print(HELP_TEXT)
    return ConsoleUI(previewer)


def main(argv: Optional[Sequence[str]] = None) -> int:
    options = build_parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # Windows コンソールの文字化け対策
        except (AttributeError, ValueError):
            pass

    if options.interactive and not sys.stdin.isatty():
        sys.stderr.write("対話モード(-i)はキーボード入力が必要です。端末から実行してください。\n")
        return 2

    caches = {
        "album": {},
        "itunes_limiter": RateLimiter(0.4),
        "mb_limiter": RateLimiter(1.1),  # MusicBrainz は 1 req/sec が上限
    }
    stats = Stats()
    ui = make_ui(options) if options.interactive else None

    try:
        for path in iter_audio_files(options.paths, recursive=options.recursive):
            if options.limit and stats.total >= options.limit:
                break
            stats.total += 1
            result = process_file(path, options, caches, ui)
            stats.results.append(result)
            stats.count(result)

            if result.status == "quit":
                print("終了します。")
                break
            if not options.quiet and result.status != "skipped-has-cover":
                label = STATUS_LABEL.get(result.status, result.status)
                extra = result.message or f"{result.source} score={result.score}"
                print(f"[{label}] {os.path.basename(result.path)}  {extra}")
    except KeyboardInterrupt:
        print("\n中断しました。ここまでの結果を表示します。", file=sys.stderr)
    finally:
        if ui is not None:
            ui.close()  # プレビュー用ウィンドウを必ず閉じる

    print_summary(stats, options)
    if options.report:
        write_report(stats.results, options.report)
        print(f"レポート  : {options.report}")

    return 1 if stats.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
