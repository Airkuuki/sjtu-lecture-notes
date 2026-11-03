"""媒体处理: 远程抽音频 / 探测时长。

关键设计: **不把视频落到磁盘**。对签名直链做远程 Range 拉流, ffmpeg 只把音轨写下来,
一讲约 19 MB 而不是 408 MB / 794 MB(实测)。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from common import find_ffmpeg, log
from sjtu import DEFAULT_UA

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


class MediaError(RuntimeError):
    """媒体处理失败。"""


def extract_audio(
    cfg: dict,
    url: str,
    out_path: Path | str,
    timeout: float = 3600.0,
) -> Path:
    """把远程音轨抽成 m4a(-c:a copy, 不重编码、不落视频)。"""
    ffmpeg = find_ffmpeg(cfg)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    log.info("远程抽音频 -> %s", out.name)
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-user_agent",
        DEFAULT_UA,
        "-i",
        url,
        "-vn",
        "-c:a",
        "copy",
        "-y",
        str(out),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"抽音频超时({timeout:.0f}s)") from exc
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        tail = (proc.stderr or "")[-400:].strip()
        raise MediaError(f"抽音频失败: {tail or 'ffmpeg 返回非零且无输出'}")
    return out


def probe_duration(ffmpeg: str, target: str | Path) -> float:
    """用 ffmpeg 的 stderr 解析时长(秒); 失败返回 0。"""
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(target)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return 0.0
    match = _DURATION_RE.search(proc.stderr or "")
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
