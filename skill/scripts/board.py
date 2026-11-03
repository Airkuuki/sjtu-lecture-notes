"""板书 / PPT 抓帧: 远程抽帧, 不落视频。

两路的取帧策略不同(实测依据):
  * 通道 0 = 教室摄像头 -> 黑板**手写**过程是渐变的, 纯场景检测会漏,
    所以用**定时采样**(fps=1/interval)兜底。
  * 通道 1 = 屏幕录屏   -> PPT 是整页切换, 用**场景变化检测**(select scene)。

去重用 dHash(64 位, 差分哈希)。为了不引入图像库, 哈希由 ffmpeg 把整段
图像序列一次性转成 9x8 灰度 rawvideo(每帧 72 字节), 再在 Python 里比较相邻像素。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from common import atomic_write_json, atomic_write_text, log
from sjtu import DEFAULT_UA

_PTS_RE = re.compile(r"pts_time:\s*([0-9.]+)")
_FRAME_NAME = "f_%05d.jpg"
HASH_W, HASH_H = 9, 8
HASH_BYTES = HASH_W * HASH_H


class FrameError(RuntimeError):
    """抽帧失败。"""


# ── 纯函数(可离线单测) ────────────────────────────────


def parse_pts_times(stderr: str) -> list[float]:
    """从 showinfo 日志里取每个输出帧的时间点(秒)。"""
    return [float(m) for m in _PTS_RE.findall(stderr or "")]


def dhash_from_gray(block: bytes) -> int:
    """9x8 灰度 -> 64 位差分哈希。"""
    if len(block) < HASH_BYTES:
        raise ValueError(f"灰度块太小: {len(block)}")
    bits = 0
    for row in range(HASH_H):
        base = row * HASH_W
        for col in range(HASH_W - 1):
            bits = (bits << 1) | (1 if block[base + col] > block[base + col + 1] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def dedup(
    entries: list[dict[str, Any]],
    hashes: list[int],
    max_distance: int = 6,
    min_gap_ms: int = 1500,
    max_gap_ms: int = 60000,
    compare: str = "last_kept",
) -> list[dict[str, Any]]:
    """保留"画面确实变了"的帧; 间隔过大时强制保留一张以防漂移。

    compare:
      * "last_kept" —— 与上一次**保留**的帧比较。适合画面一直在动的场景(摄像头),
        起到抑制近似重复的作用;
      * "previous"  —— 与**上一个采样点**比较, 即真正的边沿检测。适合静态画面
        (PPT 录屏): 实测与上一保留帧比较时, 静态段每秒 1 bit 的噪声会累积,
        55 分钟能累积出 45 次假变化, 而真实画面只有 4 个。
    """
    if compare not in ("last_kept", "previous"):
        raise ValueError(f"compare 只能是 last_kept / previous, 收到 {compare}")

    kept: list[dict[str, Any]] = []
    prev_digest: int | None = None
    kept_digest: int | None = None
    last_ms: int | None = None

    for entry, digest in zip(entries, hashes):
        t_ms = int(entry["t_ms"])
        if kept_digest is None:
            keep = True
        else:
            base = kept_digest if compare == "last_kept" else prev_digest
            gap = t_ms - int(last_ms or 0)
            keep = (base is not None and hamming(digest, base) > max_distance and gap >= min_gap_ms) or (
                gap > max_gap_ms
            )
        if keep:
            entry = dict(entry)
            entry["phash"] = f"{digest:016x}"
            kept.append(entry)
            kept_digest = digest
            last_ms = t_ms
        prev_digest = digest
    return kept


def cap_evenly(entries: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    """超出上限时按时间均匀抽稀, 保留首尾。"""
    if cap <= 0 or len(entries) <= cap:
        return entries
    step = len(entries) / cap
    picked = [entries[min(len(entries) - 1, int(i * step))] for i in range(cap)]
    if picked[-1] is not entries[-1]:
        picked[-1] = entries[-1]
    return picked


def mmss(ms: int) -> str:
    total = max(0, int(ms)) // 1000
    return f"{total // 60:02d}:{total % 60:02d}"


# ── ffmpeg 交互 ───────────────────────────────────────


def _run(cmd: list[str], timeout: float, binary: bool = False):
    try:
        return subprocess.run(cmd, capture_output=True, text=not binary, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FrameError(f"ffmpeg 超时({timeout:.0f}s)") from exc


def extract_candidates(
    cfg: dict[str, Any],
    ffmpeg: str,
    url: str,
    out_dir: Path,
    mode: str = "board",
    timeout: float = 3600.0,
) -> list[dict[str, Any]]:
    """定时采样抽候选帧, 返回带时间点的清单。

    board 用 board.interval_sec(黑板手写是渐变, 必须靠定时采样兜底);
    ppt   用 ppt.interval_sec(幻灯片停留时间远大于间隔即可覆盖)。
    """
    section = cfg["board"] if mode == "board" else cfg["ppt"]
    interval = float(section["interval_sec"])
    max_width = int(section["max_width"])
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    vf = f"fps=1/{interval},scale={max_width}:-2,showinfo"
    log.info("定时采样抽帧(%s: 每 %.0fs 一帧, 宽 %d)", mode, interval, max_width)

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "info",
        "-user_agent",
        DEFAULT_UA,
        "-i",
        url,
        "-vf",
        vf,
        "-q:v",
        "4",
        "-y",
        str(frames_dir / _FRAME_NAME),
    ]
    proc = _run(cmd, timeout)
    if proc.returncode != 0:
        raise FrameError(f"抽帧失败: {(proc.stderr or '')[-400:].strip()}")

    times = parse_pts_times(proc.stderr or "")
    files = sorted(frames_dir.glob("f_*.jpg"))
    if len(files) != len(times):
        log.warning("帧数与时间点数不一致(%d vs %d), 按较短者对齐", len(files), len(times))
    count = min(len(files), len(times))
    return [
        {"id": f"{i + 1:04d}", "t_ms": int(times[i] * 1000), "path": files[i].name}
        for i in range(count)
    ]


def hash_sequence(ffmpeg: str, frames_dir: Path, expected: int) -> list[int]:
    """一次 ffmpeg 调用把整段图像序列转成 9x8 灰度, 再算 dHash。"""
    if expected <= 0:
        return []
    first = frames_dir / (_FRAME_NAME % 1)
    if not first.is_file():
        raise FrameError(f"找不到起始帧: {first}")
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-start_number",
        "1",
        "-i",
        str(frames_dir / _FRAME_NAME),
        "-vf",
        f"scale={HASH_W}:{HASH_H}:flags=area,format=gray",
        "-f",
        "rawvideo",
        "-",
    ]
    proc = _run(cmd, 600.0, binary=True)
    raw = proc.stdout or b""
    blocks = len(raw) // HASH_BYTES
    if blocks < expected:
        log.warning("哈希帧数不足(%d < %d), 按实际数量处理", blocks, expected)
    return [dhash_from_gray(raw[i * HASH_BYTES : (i + 1) * HASH_BYTES]) for i in range(blocks)]


def write_index(out_dir: Path, mode: str, source: str, entries: list[dict[str, Any]], stats: dict) -> None:
    """写 index.json 与 index.md(人/Agent 都能读)。

    注意: 条目的 `id` 是该帧在原始采样序列中的序号(去重后不连续), 4 位;
    而文件名是 `f_%05d.jpg`, 5 位 —— 两者**不同号**, 定位文件请一律用 `path` 字段。
    """
    atomic_write_json(
        out_dir / "index.json",
        {
            "schema": "kc.frames/1",
            "mode": mode,
            "source": source,
            "frames": entries,
            "stats": stats,
        },
    )
    lines = [f"# {mode} 帧索引", "", f"共 {len(entries)} 帧", "", "| 帧号 | 时间 | 图 |", "|---|---|---|"]
    for entry in entries:
        lines.append(
            f"| {entry['id']} | {mmss(entry['t_ms'])} | [jpg](frames/{entry['path']}) |"
        )
    atomic_write_text(out_dir / "index.md", "\n".join(lines) + "\n")


def grab(
    cfg: dict[str, Any],
    ffmpeg: str,
    url: str,
    out_dir: Path,
    mode: str,
    timeout: float = 3600.0,
) -> dict[str, Any]:
    """完整流程: 定时采样抽候选 -> 对 JPEG 算 dHash 去重 -> 上限抽稀 -> 索引。

    为什么两路都用这个, 而不是场景检测/流式哈希(实测教训):
      * `select` 的 scene 比较的是相邻帧(25fps), PPT 切换动画把变化摊到十几帧,
        每帧差值 <0.01, 55 分钟只检出 2 处;
      * 一度改成"一条流里按 1fps 算 dHash", 结果 3288 个采样点判出 31 个"变化点",
        但把抽出的 31 帧再哈希比对, 相邻帧汉明距离大量为 0, 聚类后只有 5 个真实画面
        —— 流式哈希在静态画面上不稳定, 且"与上一保留帧比较"会让 1 bit/秒 的噪声
        累积成假变化;
      * 对**抽出来的 JPEG** 算 dHash 则很稳定(板书那路 110 -> 78, 且帧确实不同)。
    所以统一走"定时采样 + JPEG 去重"。
    """
    return _grab_interval(cfg, ffmpeg, url, out_dir, mode, timeout)


def hash_stream(
    ffmpeg: str, url: str, fps: float = 1.0, timeout: float = 3600.0
) -> list[dict[str, Any]]:
    """[已弃用 · 实测无效] 一条流里按 fps 采样算 dHash。

    保留仅为记录这个尝试: 在同一段 PPT 录屏上它把 3288 个采样点判成 31 个"变化点",
    但把抽出的 31 帧重新哈希比对, 相邻帧汉明距离大量为 0, 聚类后只有 5 个真实画面
    —— 静态画面上流式哈希不稳定, 且与"上一保留帧"比较会让噪声累积成假变化。
    现在统一走 grab() 里的"定时采样 + 对 JPEG 算 dHash"。
    """
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "info",
        "-user_agent",
        DEFAULT_UA,
        "-i",
        url,
        "-vf",
        f"fps={fps},scale={HASH_W}:{HASH_H}:flags=area,format=gray,showinfo",
        "-f",
        "rawvideo",
        "-",
    ]
    proc = _run(cmd, timeout, binary=True)
    stderr = (proc.stderr or b"").decode("utf8", "ignore")
    times = parse_pts_times(stderr)
    raw = proc.stdout or b""
    blocks = len(raw) // HASH_BYTES
    count = min(len(times), blocks)
    if count == 0:
        raise FrameError("哈希采样为空(ffmpeg 未产出帧)")
    return [
        {"t_ms": int(times[i] * 1000), "digest": dhash_from_gray(raw[i * HASH_BYTES : (i + 1) * HASH_BYTES])}
        for i in range(count)
    ]


def extract_one(
    ffmpeg: str, url: str, t_ms: int, dest: Path, timeout: float = 300.0
) -> bool:
    """在指定时间点定向抽一帧(ffmpeg 会自己按 Range 只取需要的数据)。"""
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-user_agent",
        DEFAULT_UA,
        "-ss",
        f"{t_ms / 1000:.3f}",
        "-i",
        url,
        "-frames:v",
        "1",
        "-q:v",
        "4",
        "-y",
        str(dest),
    ]
    proc = _run(cmd, timeout)
    return dest.is_file() and dest.stat().st_size > 0


def _grab_ppt(
    cfg: dict[str, Any], ffmpeg: str, url: str, out_dir: Path, timeout: float
) -> dict[str, Any]:
    """[已弃用 · 实测无效] 基于 hash_stream 的 PPT 抓帧, 见 hash_stream 说明。"""
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    fps = float(cfg.get("ppt", {}).get("hash_fps", 1.0))
    log.info("PPT: 按 %.1ffps 采样算 dHash(一条流, 不落视频)", fps)
    samples = hash_stream(ffmpeg, url, fps, timeout)

    entries = [{"id": f"{i + 1:04d}", "t_ms": s["t_ms"]} for i, s in enumerate(samples)]
    kept = dedup(
        entries,
        [s["digest"] for s in samples],
        max_distance=int(cfg.get("ppt", {}).get("hash_distance", 6)),
        min_gap_ms=int(cfg.get("ppt", {}).get("min_gap_ms", 1500)),
        max_gap_ms=int(cfg.get("ppt", {}).get("max_gap_ms", 120000)),
    )
    kept = cap_evenly(kept, int(cfg.get("frames", {}).get("max_per_lecture", 400)))

    log.info("PPT: %d 个采样点 -> %d 个变化点, 开始定向抽帧", len(samples), len(kept))
    final: list[dict[str, Any]] = []
    for index, entry in enumerate(kept, 1):
        name = f"{index:04d}_{mmss(entry['t_ms']).replace(':', '-')}.jpg"
        dest = frames_dir / name
        if extract_one(ffmpeg, url, entry["t_ms"], dest):
            final.append(
                {
                    "id": f"{index:04d}",
                    "t_ms": entry["t_ms"],
                    "path": name,
                    "phash": entry.get("phash", ""),
                }
            )
        else:
            log.warning("抽帧失败, 跳过: t=%dms", entry["t_ms"])

    stats = {
        "raw": len(samples),
        "kept": len(final),
        "dropped": len(samples) - len(final),
        "dedup_ratio": round(1 - len(final) / len(samples), 3) if samples else 0.0,
    }
    write_index(out_dir, "ppt", redact_source(url), final, stats)
    log.info("ppt: 采样 %d -> 保留 %d", len(samples), len(final))
    return {"mode": "ppt", "dir": str(out_dir), **stats}


def redact_source(url: str) -> str:
    return url.split("?")[0]


def _grab_interval(
    cfg: dict[str, Any],
    ffmpeg: str,
    url: str,
    out_dir: Path,
    mode: str,
    timeout: float,
) -> dict[str, Any]:
    """定时采样 + dHash 去重(用于黑板手写这种渐变画面)。"""
    entries = extract_candidates(cfg, ffmpeg, url, out_dir, mode, timeout)
    raw_count = len(entries)
    if not entries:
        write_index(out_dir, mode, redact_source(url), [], {"raw": 0, "kept": 0})
        return {"mode": mode, "raw": 0, "kept": 0, "dir": str(out_dir)}

    hashes = hash_sequence(ffmpeg, out_dir / "frames", raw_count)
    entries = entries[: len(hashes)]
    section = cfg.get("board") if mode == "board" else cfg.get("ppt", {})
    kept = dedup(
        entries,
        hashes,
        max_distance=int(section.get("hash_distance", 6)),
        min_gap_ms=int(section.get("min_gap_ms", 1500)),
        max_gap_ms=int(section.get("max_gap_ms", 120000)),
        compare=str(section.get("compare", "last_kept")),
    )
    cap = int(cfg.get("frames", {}).get("max_per_lecture", 400))
    kept = cap_evenly(kept, cap)

    keep_names = {e["path"] for e in kept}
    removed = 0
    for path in (out_dir / "frames").glob("f_*.jpg"):
        if path.name not in keep_names:
            path.unlink(missing_ok=True)
            removed += 1

    stats = {
        "raw": raw_count,
        "kept": len(kept),
        "dropped": removed,
        "dedup_ratio": round(removed / raw_count, 3) if raw_count else 0.0,
    }
    write_index(out_dir, mode, redact_source(url), kept, stats)
    log.info(
        "%s: 候选 %d -> 保留 %d(去重 %.0f%%)",
        mode,
        raw_count,
        len(kept),
        stats["dedup_ratio"] * 100,
    )
    return {"mode": mode, "dir": str(out_dir), **stats}
