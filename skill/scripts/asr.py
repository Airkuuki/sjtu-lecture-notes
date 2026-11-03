"""转录来源: 平台 AI 字幕 与 bcut-asr(必剪云端), 两条路各有用处。

* 平台字幕(`/v1/course/ai/translate/{id}`): 无额外成本、无标点的整块 ASR。
  实测同样有识别错误(如"量体问题"应为"二体问题"), 但胜在是平台官方产出。
  缺点: 新讲的字幕有延迟(实测第 7 周的两讲还没生成, 第 6 周及更早已就绪)。
* bcut-asr: 输出带标点与句级时间戳, 对新讲立即可用, 但需上传音频到必剪云端。

默认走 bcut-asr; 想要"扒字幕"时用 `subtitles` 命令拉平台字幕, 有则用、无则明确报不可用。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from common import log, resolve_project_path

SCHEMA = "kc.transcript/1"
TIMEOUT_PAD = 300.0


class AsrError(RuntimeError):
    """识别失败。"""


# ── 纯函数(可离线单测) ────────────────────────────────


def bcut_json_to_segments(data: dict[str, Any]) -> list[dict[str, Any]]:
    """把 bcut-asr 的 ASRData 归一化成我们的时间段结构。"""
    segments: list[dict[str, Any]] = []
    for item in data.get("utterances") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("transcript") or "").strip()
        if not text:
            continue
        segments.append(
            {
                "i": len(segments) + 1,
                "start_ms": int(item.get("start_time") or 0),
                "end_ms": int(item.get("end_time") or 0),
                "text": text,
            }
        )
    return segments


def _ts(ms: int) -> str:
    ms = max(0, int(ms))
    hours, rest = divmod(ms, 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _mmss(ms: int) -> str:
    total = max(0, int(ms)) // 1000
    return f"{total // 60:02d}:{total % 60:02d}"


def split_platform_blocks(
    segments: list[dict[str, Any]], min_chars: int = 8
) -> list[dict[str, Any]]:
    """把平台字幕的整块(15~100 秒)按句末标点切成句级。

    平台不给句级时间戳, 只能按字数比例在块内插值 —— 用于"定位到大概位置"够用,
    不保证每句起止精确。切不出标点时原样保留。
    """
    out: list[dict[str, Any]] = []
    for seg in segments:
        text = str(seg.get("text") or "").strip()
        start = int(seg.get("start_ms") or 0)
        end = int(seg.get("end_ms") or start + 1000)
        if not text:
            continue
        pieces = [p.strip() for p in re.split(r"(?<=[。！？!?；;])", text) if p.strip()]
        if len(pieces) <= 1:
            out.append({"start_ms": start, "end_ms": end, "text": text})
            continue
        total = sum(len(p) for p in pieces)
        cursor = start
        elapsed = 0
        for index, piece in enumerate(pieces):
            elapsed += len(piece)
            piece_end = end if index == len(pieces) - 1 else start + int((end - start) * elapsed / total)
            if out and len(piece) < min_chars and index != len(pieces) - 1:
                out[-1]["text"] += piece
                out[-1]["end_ms"] = piece_end
            else:
                out.append({"start_ms": cursor, "end_ms": piece_end, "text": piece})
            cursor = piece_end
    return out


def to_srt(segments: list[dict[str, Any]]) -> str:
    """标准 SRT: 段间空行, 结尾留一个换行。"""
    blocks = [
        f"{seg['i']}\n{_ts(seg['start_ms'])} --> {_ts(seg['end_ms'])}\n{seg['text']}\n"
        for seg in segments
    ]
    return "\n".join(blocks)


def to_txt(segments: list[dict[str, Any]]) -> str:
    """带 [mm:ss] 前缀的纯文本, 方便人和 Agent 引用时间点。"""
    return "\n".join(f"[{_mmss(seg['start_ms'])}] {seg['text']}" for seg in segments) + (
        "\n" if segments else ""
    )


SENTENCE_END = ("。", "！", "？", "…", "!", "?", "；", ";")


def merge_segments(
    segments: list[dict[str, Any]],
    gap_ms: int = 700,
    max_chars: int = 80,
    max_dur_ms: int = 20000,
) -> list[dict[str, Any]]:
    """把 ASR 的碎片段按停顿/长度重切成句级行。

    实测: bcut-asr 平均每段只有 9 个字、且几乎不以句末标点结尾,
    直接当字幕看很碎; 段间停顿(中位 120ms, 句间常 >700ms)是最可靠的切分信号。
    """
    merged: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for seg in segments:
        if cur is None:
            cur = {
                "start_ms": int(seg["start_ms"]),
                "end_ms": int(seg["end_ms"]),
                "text": str(seg["text"]),
            }
            continue
        gap = int(seg["start_ms"]) - cur["end_ms"]
        would_chars = len(cur["text"]) + len(seg["text"])
        would_dur = int(seg["end_ms"]) - cur["start_ms"]
        boundary = (
            gap >= gap_ms
            or would_chars > max_chars
            or would_dur > max_dur_ms
            or cur["text"].rstrip().endswith(SENTENCE_END)
        )
        if boundary:
            merged.append(cur)
            cur = {
                "start_ms": int(seg["start_ms"]),
                "end_ms": int(seg["end_ms"]),
                "text": str(seg["text"]),
            }
        else:
            cur["text"] += str(seg["text"])
            cur["end_ms"] = int(seg["end_ms"])
    if cur is not None:
        merged.append(cur)
    for index, item in enumerate(merged, 1):
        item["i"] = index
    return merged


# ── 调用 bcut-asr ─────────────────────────────────────


def run_bcut(cfg: dict[str, Any], audio: Path, out_json: Path) -> dict[str, Any]:
    """以子进程方式调用修复版 bcut-asr, 返回它写出的 JSON。"""
    repo = resolve_project_path(cfg["asr"]["bcut_asr_repo"])
    if not (repo / "bcut_asr" / "__main__.py").is_file():
        raise AsrError(f"找不到 bcut-asr 仓库: {repo}(检查 config.toml 的 asr.bcut_asr_repo)")

    env = os.environ.copy()
    path_parts = [str(repo)]
    pylibs = cfg.get("paths", {}).get("pylibs")
    if pylibs:
        path_parts.append(str(resolve_project_path(pylibs)))
    if env.get("PYTHONPATH"):
        path_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(path_parts)

    out_json.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "bcut_asr",
        str(audio),
        str(out_json),
        "-i",
        str(cfg["asr"].get("poll_interval", 5)),
        "-t",
        str(cfg["asr"].get("task_timeout", 3600)),
    ]
    log.info("调用 bcut-asr 识别(必剪云端)…")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
            timeout=float(cfg["asr"].get("task_timeout", 3600)) + TIMEOUT_PAD,
        )
    except subprocess.TimeoutExpired as exc:
        raise AsrError("bcut-asr 超时未返回") from exc

    if proc.returncode != 0 or not out_json.is_file():
        tail = ((proc.stderr or "") + (proc.stdout or ""))[-600:].strip()
        hint = ""
        # 解释器与预编译依赖不匹配: 典型是 PATH 里的 Python 是 3.14, 而 pylibs
        # 里的 pydantic_core 是 cp312 编译的, 导入时报 ArgsKwargs ImportError。
        if "ArgsKwargs" in tail or "pydantic_core" in tail:
            raise AsrError(
                "bcut-asr 导入失败: 当前 Python 与 pylibs 里的预编译包版本不匹配"
                f"(解释器 {sys.version.split()[0]}, 依赖目录 {pylibs})。\n"
                "  解决: 用 run.ps1 跑(它会读 .env.local.ps1 里的 TINGKE_PYTHON), "
                "或设 TINGKE_PYTHON 指向一个与 pylibs 匹配的解释器(如 3.12)。"
            ) from None
        if "风控" in tail or "412" in tail:
            hint = "(命中必剪风控 412, 稍后重跑即可)"
        raise AsrError(f"bcut-asr 失败(退出码 {proc.returncode}){hint}: {tail}")

    try:
        return json.loads(out_json.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise AsrError(f"bcut-asr 输出不是合法 JSON: {exc}") from exc
