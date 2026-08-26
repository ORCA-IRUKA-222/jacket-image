# -*- coding: utf-8 -*-
"""テスト用に、最小構成の mp3 / m4a ファイルを生成するヘルパー."""

from __future__ import annotations

import struct

# MPEG-1 Layer III / 128kbps / 44.1kHz / stereo のフレーム (417 バイト)
_MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413


def write_mp3(path: str, frames: int = 20) -> str:
    with open(path, "wb") as handle:
        handle.write(_MP3_FRAME * frames)
    return path


def _box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _full(kind: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return _box(kind, bytes([version]) + flags.to_bytes(3, "big") + payload)


def write_m4a(path: str) -> str:
    """mutagen が解析できる最小の AAC(mp4a) コンテナを書き出す."""
    matrix = struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    mvhd = _full(
        b"mvhd", 0, 0,
        struct.pack(">IIII", 0, 0, 1000, 1000)
        + struct.pack(">IHH", 0x00010000, 0x0100, 0)
        + b"\x00" * 8 + matrix + b"\x00" * 24 + struct.pack(">I", 2),
    )
    tkhd = _full(
        b"tkhd", 0, 7,
        struct.pack(">IIIII", 0, 0, 1, 0, 1000) + b"\x00" * 8
        + struct.pack(">hhhh", 0, 0, 0x0100, 0) + matrix + struct.pack(">II", 0, 0),
    )
    mdhd = _full(b"mdhd", 0, 0, struct.pack(">IIIIHH", 0, 0, 44100, 44100, 0x55C4, 0))
    hdlr = _full(b"hdlr", 0, 0, b"\x00" * 4 + b"soun" + b"\x00" * 12 + b"SoundHandler\x00")
    smhd = _full(b"smhd", 0, 0, struct.pack(">hH", 0, 0))
    dinf = _box(b"dinf", _full(b"dref", 0, 0, struct.pack(">I", 1) + _full(b"url ", 0, 1, b"")))
    esds = _full(
        b"esds", 0, 0,
        b"\x03\x19\x00\x00\x00\x04\x11\x40\x15" + b"\x00" * 12
        + b"\x05\x02\x12\x10\x06\x01\x02",
    )
    mp4a = _box(
        b"mp4a",
        b"\x00" * 6 + struct.pack(">H", 1) + b"\x00" * 8
        + struct.pack(">HHHH", 2, 16, 0, 0) + struct.pack(">I", 44100 << 16) + esds,
    )
    stbl = _box(
        b"stbl",
        _full(b"stsd", 0, 0, struct.pack(">I", 1) + mp4a)
        + _full(b"stts", 0, 0, struct.pack(">I", 0))
        + _full(b"stsc", 0, 0, struct.pack(">I", 0))
        + _full(b"stsz", 0, 0, struct.pack(">II", 0, 0))
        + _full(b"stco", 0, 0, struct.pack(">I", 0)),
    )
    moov = _box(b"moov", mvhd + _box(b"trak", tkhd + _box(b"mdia", mdhd + hdlr + _box(b"minf", smhd + dinf + stbl))))
    ftyp = _box(b"ftyp", b"M4A " + struct.pack(">I", 0) + b"M4A mp42isom")
    with open(path, "wb") as handle:
        handle.write(ftyp + moov + _box(b"mdat", b"\x00" * 8))
    return path


def fake_jpeg(size: int = 8000) -> bytes:
    return b"\xff\xd8\xff\xe0" + b"\x00" * (size - 6) + b"\xff\xd9"


def fake_png(size: int = 8000) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * (size - 8)
