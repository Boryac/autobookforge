"""模块 7：MOBI / AZW3 转换。

通过 subprocess 调用 Calibre 的 ``ebook-convert``。未安装时优雅降级，
EPUB 输出不受影响。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

CALIBRE_CANDIDATES = (
    "ebook-convert",
    "ebook-convert.exe",
    r"C:\Program Files\Calibre2\ebook-convert.exe",
    r"C:\Program Files (x86)\Calibre2\ebook-convert.exe",
    "/Applications/calibre.app/Contents/MacOS/ebook-convert",
    "/usr/bin/ebook-convert",
)


class CalibreNotFoundError(Exception):
    """系统未安装 Calibre。"""


@dataclass
class ConvertResult:
    success: bool
    output: str
    message: str = ""


def find_calibre() -> str | None:
    """定位 ebook-convert 可执行文件。"""
    for cand in CALIBRE_CANDIDATES:
        if os.path.isabs(cand):
            if os.path.exists(cand):
                return cand
        else:
            found = shutil.which(cand)
            if found:
                return found
    return None


def is_available() -> bool:
    return find_calibre() is not None


def convert_to_mobi(
    epub_path: str,
    out_path: str,
    *,
    kindle_profile: bool = True,
    mobi_file_type: str = "new",
) -> ConvertResult:
    """EPUB -> MOBI/AZW3。"""
    exe = find_calibre()
    if not exe:
        return ConvertResult(False, out_path, "未检测到 Calibre（ebook-convert），已跳过转换")

    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)

    cmd = [exe, epub_path, out_path, "--no-inline-toc"]
    if kindle_profile:
        cmd += ["--output-profile", "kindle"]
    if out_path.lower().endswith((".mobi", ".azw3")):
        cmd += ["--mobi-file-type", mobi_file_type]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except Exception as exc:  # pragma: no cover
        return ConvertResult(False, out_path, f"转换异常: {exc}")

    if proc.returncode != 0:
        return ConvertResult(False, out_path, f"Calibre 返回码 {proc.returncode}: {proc.stderr.strip()[:300]}")
    return ConvertResult(True, out_path, "转换成功")


def convert_formats(epub_path: str, out_dir: str, base_name: str, formats: list[str]) -> dict[str, ConvertResult]:
    """批量转换到多个格式。"""
    results: dict[str, ConvertResult] = {}
    for fmt in formats:
        fmt = fmt.lower().strip()
        if fmt == "epub":
            results["epub"] = ConvertResult(True, epub_path, "已生成")
            continue
        if fmt not in ("mobi", "azw3"):
            results[fmt] = ConvertResult(False, "", f"不支持的格式: {fmt}")
            continue
        out = os.path.join(out_dir, f"{base_name}.{fmt}")
        results[fmt] = convert_to_mobi(epub_path, out)
    return results
