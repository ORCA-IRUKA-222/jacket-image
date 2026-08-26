# -*- coding: utf-8 -*-
"""カバーアート候補を確認するためのプレビュー表示.

3 つの方式を用意しています。

  window   … Tkinter のウィンドウを *1 枚だけ* 作り、候補ごとに中身を差し替える。
             曲数が多くてもウィンドウは増えず、処理の終わりに自動で閉じる。
  terminal … ウィンドウを一切開かず、ターミナル内に画像を文字(▀ + 24bit色)で描く。
  none     … 画像は表示せず、アーティスト名・アルバム名・URL などの文字情報だけ。

Pillow が入っていれば window / terminal が使えます。無い場合は none になります。
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
from typing import Optional, Sequence

try:  # 画像のデコード・リサイズに使用(任意)
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow 無しの環境
    Image = None

PREVIEW_MODES = ("auto", "window", "terminal", "none")


def pillow_available() -> bool:
    return Image is not None


def enable_ansi_colors() -> bool:
    """Windows のコンソールで ANSI エスケープを有効にする."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def render_ansi(data: bytes, width: int = 44) -> str:
    """画像を半角ブロック文字 + 24bit カラーの文字列に変換する.

    1 文字に上下 2 ピクセルを詰めるので、見た目の縦横比が崩れません。
    """
    if Image is None:
        raise RuntimeError("Pillow が必要です")
    width = max(8, min(int(width), 200))
    with Image.open(io.BytesIO(data)) as source:
        image = source.convert("RGB")
        height = max(2, round(width * image.height / image.width))
        if height % 2:
            height += 1
        image = image.resize((width, height), Image.LANCZOS)
        pixels = image.load()
        lines = []
        for y in range(0, height, 2):
            parts = []
            for x in range(width):
                top = pixels[x, y]
                bottom = pixels[x, y + 1]
                parts.append(
                    f"\x1b[38;2;{top[0]};{top[1]};{top[2]}m"
                    f"\x1b[48;2;{bottom[0]};{bottom[1]};{bottom[2]}m▀"
                )
            lines.append("".join(parts) + "\x1b[0m")
    return "\n".join(lines)


def image_dimensions(data: bytes) -> Optional[tuple]:
    """画像の (幅, 高さ)。Pillow が無い / 壊れている場合は None."""
    if Image is None:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            return image.size
    except Exception:
        return None


class NullPreviewer:
    """画像を表示しない(文字情報のみ)."""

    name = "none"

    def show(self, data: bytes, lines: Sequence[str]) -> None:
        for line in lines:
            print(line)

    def close(self) -> None:
        pass


class TerminalPreviewer:
    """ウィンドウを開かず、ターミナル内に画像を描く."""

    name = "terminal"

    def __init__(self, width: int = 44) -> None:
        if Image is None:
            raise RuntimeError("Pillow が必要です")
        self.width = width
        enable_ansi_colors()

    def show(self, data: bytes, lines: Sequence[str]) -> None:
        try:
            print(render_ansi(data, self.width))
        except Exception as exc:
            print(f"(画像を描画できませんでした: {exc})")
        for line in lines:
            print(line)

    def close(self) -> None:
        pass


class WindowPreviewer:
    """Tkinter のウィンドウを 1 枚だけ使い回すプレビュー.

    候補ごとに画像を差し替えるだけなのでウィンドウは増えず、
    close() で確実に閉じます(処理終了時・中断時とも呼ばれます)。
    """

    name = "window"

    def __init__(self, max_size: int = 520, title: str = "カバーアート確認") -> None:
        if Image is None:
            raise RuntimeError("Pillow が必要です")
        import tkinter  # 遅延 import(未インストール環境では RuntimeError にする)
        from PIL import ImageTk  # noqa: F401  利用可能か確認するだけ

        self._tkinter = tkinter
        self.max_size = max_size
        self.title = title
        self._root = None
        self._image_label = None
        self._text_label = None
        self._photo = None  # GC 防止のため参照を保持する

    def _alive(self) -> bool:
        if self._root is None:
            return False
        try:
            return bool(self._root.winfo_exists())
        except Exception:
            return False

    def _ensure_window(self) -> None:
        if self._alive():
            return
        tkinter = self._tkinter
        root = tkinter.Tk()
        root.title(self.title)
        root.configure(bg="#1e1e1e")
        root.resizable(False, False)
        try:
            root.attributes("-topmost", True)  # 端末を操作している間も前面に出す
        except Exception:
            pass
        root.protocol("WM_DELETE_WINDOW", lambda: None)  # ×では閉じない(終了時に自動で閉じる)
        self._image_label = tkinter.Label(root, bg="#1e1e1e")
        self._image_label.pack(padx=12, pady=(12, 6))
        self._text_label = tkinter.Label(
            root, bg="#1e1e1e", fg="#f0f0f0", justify="left", anchor="w", font=("", 10)
        )
        self._text_label.pack(padx=12, pady=(0, 12), fill="x")
        self._root = root

    def show(self, data: bytes, lines: Sequence[str]) -> None:
        from PIL import ImageTk

        self._ensure_window()
        with Image.open(io.BytesIO(data)) as source:
            image = source.convert("RGB")
            image.thumbnail((self.max_size, self.max_size), Image.LANCZOS)
            self._photo = ImageTk.PhotoImage(image)
        self._image_label.configure(image=self._photo)
        self._text_label.configure(text="\n".join(lines))
        self._root.update()

    def close(self) -> None:
        if self._alive():
            try:
                self._root.destroy()
            except Exception:
                pass
        self._root = None
        self._photo = None


def create_previewer(mode: str = "auto", width: int = 44, max_size: int = 520):
    """モード名から Previewer を作る。失敗したら段階的にフォールバックする.

    戻り値は (previewer, 補足メッセージ)。
    """
    if mode == "none":
        return NullPreviewer(), ""

    if mode in ("auto", "window"):
        try:
            return WindowPreviewer(max_size=max_size), ""
        except Exception as exc:
            if mode == "window":
                return _fallback(width, f"ウィンドウを使えません({exc})")

    if mode in ("auto", "terminal"):
        try:
            return TerminalPreviewer(width=width), ""
        except Exception as exc:
            return NullPreviewer(), f"ターミナル描画を使えません({exc}) → 文字情報のみ表示します"

    return NullPreviewer(), ""


def _fallback(width: int, reason: str):
    try:
        return TerminalPreviewer(width=width), f"{reason} → ターミナル内に表示します"
    except Exception:
        return NullPreviewer(), f"{reason} → 文字情報のみ表示します"


def open_in_os_viewer(data: bytes, mime: str) -> Optional[str]:
    """OS 標準の画像ビューアで開く(v キーで明示的に指定されたときだけ).

    このウィンドウは自動では閉じないので、通常のプレビューには使いません。
    """
    suffix = ".png" if mime == "image/png" else ".jpg"
    handle = tempfile.NamedTemporaryFile(prefix="cover_", suffix=suffix, delete=False)
    try:
        handle.write(data)
    finally:
        handle.close()
    path = handle.name
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            import subprocess

            subprocess.Popen(["open", path])
        else:
            import subprocess

            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return None
    return path
