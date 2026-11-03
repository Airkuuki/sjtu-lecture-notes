"""合成: 把字幕、PPT 帧、板书帧按时间轴对齐成一份材料包。

产物(<data_root>/合成/<课程>/<节次>/):
    timeline.md  三路对照表: | 时间 | 字幕 | PPT帧 | 板书帧 |
    material.md  给 Agent 读的单文件材料包(元信息 + 帧索引 + 全程字幕)

对齐规则: 每条字幕行挂上"时间不晚于它"的最近一帧(允许超前 2 秒),
超过 max_lag_ms 就不再挂(说明那段没有新画面)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from board import mmss
from common import atomic_write_text, data_dir, log, read_json, safe_name
from pipeline import frames_dir, lecture_dir, lecture_ref


def nearest_frame(
    frames: list[dict[str, Any]], t_ms: int, max_lag_ms: int, lead_ms: int = 2000
) -> dict[str, Any] | None:
    """取时间上不晚于 t_ms(允许超前 lead_ms)的最近一帧; 太旧则不挂。

    frames 必须已按 t_ms 升序。
    """
    chosen: dict[str, Any] | None = None
    for frame in frames:
        if int(frame["t_ms"]) <= t_ms + lead_ms:
            chosen = frame
        else:
            break
    if chosen is None or t_ms - int(chosen["t_ms"]) > max_lag_ms:
        return None
    return chosen


def attach_frames(
    segments: list[dict[str, Any]],
    frames: list[dict[str, Any]],
    key: str,
    max_lag_ms: int = 180000,
) -> list[dict[str, Any]]:
    """给每条字幕行挂上某一路的帧。"""
    ordered = sorted(frames, key=lambda f: int(f["t_ms"]))
    rows = []
    for seg in segments:
        start_ms = int(seg["start_ms"])
        rows.append(
            {
                "start_ms": start_ms,
                "text": str(seg["text"]),
                key: nearest_frame(ordered, start_ms, max_lag_ms),
            }
        )
    return rows


def _cell(frame: dict[str, Any] | None) -> str:
    return frame["id"] if frame else ""


def render_timeline(rows: list[dict[str, Any]]) -> str:
    """渲染三路对照表(rows 需同时带 ppt 与 board 键)。"""
    lines = ["| 时间 | 字幕 | PPT 帧 | 板书帧 |", "|---|---|---|---|"]
    for row in rows:
        lines.append(
            "| {t} | {text} | {ppt} | {board} |".format(
                t=mmss(row["start_ms"]),
                text=row["text"].replace("|", "\\|"),
                ppt=_cell(row.get("ppt")),
                board=_cell(row.get("board")),
            )
        )
    return "\n".join(lines) + "\n"


def _frame_table(index: dict[str, Any] | None) -> str:
    frames = (index or {}).get("frames") or []
    if not frames:
        return "(无)"
    lines = ["| 帧号 | 时间 | 文件 |", "|---|---|---|"]
    for frame in frames:
        lines.append(f"| {frame['id']} | {mmss(frame['t_ms'])} | {frame['path']} |")
    return "\n".join(lines)


def render_material(
    course: dict[str, Any],
    lecture: dict[str, Any],
    transcript: dict[str, Any],
    board_index: dict[str, Any] | None,
    ppt_index: dict[str, Any] | None,
    board_dir: Path | None = None,
    ppt_dir: Path | None = None,
) -> str:
    """给 Agent 的单文件材料包。"""
    merged = transcript.get("merged") or transcript.get("segments") or []
    quality = transcript.get("quality") or {}
    lines = [
        f"# 材料包: {course.get('name')} / {lecture.get('begin')}",
        "",
        f"- 节次 id: {lecture.get('id')}",
        f"- 转写来源: {transcript.get('provider')}",
        f"- 时长: {mmss(int(transcript.get('duration_ms') or 0))}",
        f"- 字幕: {len(merged)} 句 / {quality.get('chars', 0)} 字",
        f"- 板书帧: {len((board_index or {}).get('frames') or [])} 帧(通道 0 教室摄像头)",
        f"- PPT 帧: {len((ppt_index or {}).get('frames') or [])} 帧(通道 1 屏幕录屏)",
        f"- 板书帧目录: {board_dir or '(无)'}",
        f"- PPT 帧目录: {ppt_dir or '(无)'}",
        "",
        "> 读法: 下面依次是全程字幕(带 [mm:ss])、板书帧索引、PPT 帧索引。",
        "> 字幕由 ASR 产生, 数学术语可能有错(例如\"年号\"应为\"二维\"), 写笔记时按上下文修正。",
        "",
        "## 全程字幕",
        "",
    ]
    lines += [f"[{mmss(seg['start_ms'])}] {seg['text']}" for seg in merged]
    lines += ["", "## 板书帧(通道 0: 黑板手写)", "", _frame_table(board_index)]
    lines += ["", "## PPT 帧(通道 1: 屏幕录屏)", "", _frame_table(ppt_index)]
    return "\n".join(lines) + "\n"


def merge_dir(cfg: dict[str, Any], course: dict[str, Any], lecture: dict[str, Any]) -> Path:
    return data_dir(
        cfg, "合成", safe_name(course.get("name") or ""), lecture_ref(lecture),
        course_name=course.get("name") or "",
    )


def build_material(
    cfg: dict[str, Any],
    course: dict[str, Any],
    lecture: dict[str, Any],
    transcript: dict[str, Any],
    board_index: dict[str, Any] | None,
    ppt_index: dict[str, Any] | None,
) -> dict[str, Any]:
    """生成 timeline.md 与 material.md。"""
    out = merge_dir(cfg, course, lecture)
    segments = transcript.get("merged") or transcript.get("segments") or []
    board_frames = (board_index or {}).get("frames") or []
    ppt_frames = (ppt_index or {}).get("frames") or []

    rows = attach_frames(segments, ppt_frames, "ppt")
    board_rows = attach_frames(segments, board_frames, "board")
    for row, board_row in zip(rows, board_rows):
        row["board"] = board_row.get("board")

    board_dir = frames_dir(cfg, course, lecture, "board")
    ppt_dir = frames_dir(cfg, course, lecture, "ppt")
    atomic_write_text(out / "timeline.md", render_timeline(rows))
    atomic_write_text(
        out / "material.md",
        render_material(
            course, lecture, transcript, board_index, ppt_index, board_dir, ppt_dir
        ),
    )

    summary = {
        "dir": str(out),
        "timeline": str(out / "timeline.md"),
        "material": str(out / "material.md"),
        "segments": len(segments),
        "board_frames": len(board_frames),
        "ppt_frames": len(ppt_frames),
    }
    log.info("合成完成: %s", out / "material.md")
    return summary


def merge_lecture(
    cfg: dict[str, Any],
    course: dict[str, Any],
    lecture: dict[str, Any],
    require_frames: bool = True,
) -> dict[str, Any]:
    """读取已有转写与帧索引, 合成材料包。"""
    tdir = lecture_dir(cfg, course, lecture)
    transcript = read_json(tdir / "transcript.json", default=None)
    if not isinstance(transcript, dict):
        raise FileNotFoundError(f"缺少转写: {tdir / 'transcript.json'}(先跑 transcribe)")

    board_index = read_json(frames_dir(cfg, course, lecture, "board") / "index.json")
    ppt_index = read_json(frames_dir(cfg, course, lecture, "ppt") / "index.json")
    if require_frames and not (board_index or ppt_index):
        raise FileNotFoundError("缺少帧索引: 先跑 frames")

    return build_material(cfg, course, lecture, transcript, board_index, ppt_index)
