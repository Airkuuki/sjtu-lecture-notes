"""流水线编排: 节次 -> 远程抽音频 -> bcut-asr -> 字幕产物。

产物落在 <data_root>/字幕/<课程>/<日期_节次id>/
    audio.m4a        远程抽出的音轨(约 19 MB/讲, 可 --no-keep-audio 丢弃)
    bcut_raw.json    bcut-asr 原始输出(留档, 重切句时不必重跑识别)
    transcript.json  segments=ASR 原始碎片段, merged=按停顿重切的句级行
    transcript.srt   由 merged 生成(句级, 不是 2 秒一条的碎片)
    transcript.txt   带 [mm:ss] 前缀, 供 Agent 阅读与引用
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from asr import (
    SCHEMA,
    bcut_json_to_segments,
    merge_segments,
    run_bcut,
    split_platform_blocks,
    to_srt,
    to_txt,
)
from board import grab
from common import (
    atomic_write_json,
    atomic_write_text,
    data_dir,
    find_ffmpeg,
    log,
    read_json,
    redact_url,
    require_disk,
    safe_name,
)
from media import extract_audio


def lecture_ref(lecture: dict[str, Any]) -> str:
    """稳定且可读的节次标识, 形如 20260101_1001。"""
    begin = str(lecture.get("begin") or "")
    date = begin[:10].replace("-", "") or "unknown-date"
    return f"{date}_{lecture.get('id')}"


def lecture_dir(cfg: dict[str, Any], course: dict[str, Any], lecture: dict[str, Any]) -> Path:
    return data_dir(
        cfg, "字幕", safe_name(course.get("name") or ""), lecture_ref(lecture),
        course_name=course.get("name") or "",
    )


def pick_channel(urls: list[dict[str, Any]], index: int) -> dict[str, Any]:
    """按配置取通道; 越界时退回最后一个。"""
    if not urls:
        raise ValueError("该节次没有任何视频通道")
    return urls[index] if 0 <= index < len(urls) else urls[-1]


def _estimate_mb(duration_s: float) -> float:
    """音轨 48kbps, 留 2 倍余量与 20MB 底。"""
    return max(20.0, duration_s * 48 / 8 / 1024 * 2 + 20)


def merge_params(cfg: dict[str, Any], gap_ms: int | None = None) -> dict[str, int]:
    asr_cfg = cfg.get("asr", {})
    return {
        "gap_ms": int(gap_ms if gap_ms is not None else asr_cfg.get("merge_gap_ms", 700)),
        "max_chars": int(asr_cfg.get("merge_max_chars", 80)),
        "max_dur_ms": int(asr_cfg.get("merge_max_dur_ms", 20000)),
    }


def build_payload(
    cfg: dict[str, Any],
    course: dict[str, Any],
    lecture: dict[str, Any],
    source: str,
    duration_ms: int,
    segments: list[dict[str, Any]],
    gap_ms: int | None = None,
    provider: str = "bcut-asr",
) -> dict[str, Any]:
    merged = merge_segments(segments, **merge_params(cfg, gap_ms))
    return {
        "schema": SCHEMA,
        "course": {
            "id": course.get("id"),
            "name": course.get("name"),
            "tecl_id": course.get("tecl_id"),
        },
        "lecture": {
            "id": lecture.get("id"),
            "begin": lecture.get("begin"),
            "end": lecture.get("end"),
        },
        "provider": provider,
        "source": source,
        "duration_ms": duration_ms,
        "segments": segments,
        "merged": merged,
        "merge_params": merge_params(cfg, gap_ms),
        "quality": {
            "segments": len(segments),
            "merged": len(merged),
            "chars": sum(len(s["text"]) for s in segments),
            "first_ms": segments[0]["start_ms"] if segments else None,
            "last_ms": segments[-1]["end_ms"] if segments else None,
        },
        "produced_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def write_artifacts(out: Path, payload: dict[str, Any]) -> None:
    merged = payload.get("merged") or payload.get("segments") or []
    atomic_write_json(out / "transcript.json", payload)
    atomic_write_text(out / "transcript.srt", to_srt(merged))
    atomic_write_text(out / "transcript.txt", to_txt(merged))


def fetch_platform_subtitles(
    cfg: dict[str, Any],
    client: Any,
    course: dict[str, Any],
    lecture: dict[str, Any],
    force: bool = False,
    gap_ms: int | None = None,
) -> dict[str, Any]:
    """扒平台 AI 字幕; 该讲尚未生成时返回 status=unavailable(不抛异常)。

    平台字幕是整块(15~100 秒)且无句级时间戳, 这里按句末标点切句并用字数比例插值。
    """
    out = lecture_dir(cfg, course, lecture)
    json_path, srt_path, txt_path = out / "platform.json", out / "platform.srt", out / "platform.txt"
    if json_path.is_file() and not force:
        cached = read_json(json_path, default={}) or {}
        log.info("已有平台字幕, 跳过: %s", json_path)
        return {
            "status": "cached",
            "lecture_id": lecture.get("id"),
            "json": str(json_path),
            **(cached.get("quality") or {}),
        }

    blocks = client.subtitles(lecture["id"])
    if blocks is None:
        log.info("节次 %s: 平台尚未生成字幕", lecture.get("id"))
        return {
            "status": "unavailable",
            "lecture_id": lecture.get("id"),
            "detail": "平台未生成该讲字幕(courTransferFlag=false); 可改用 transcribe(bcut-asr)",
        }

    fine = split_platform_blocks(blocks)
    payload = build_payload(
        cfg,
        course,
        lecture,
        f"platform-ai-subtitle:{lecture.get('id')}",
        blocks[-1]["end_ms"] if blocks else 0,
        fine,
        gap_ms=gap_ms,
        provider="platform-subtitle",
    )
    payload["raw_blocks"] = len(blocks)
    out.mkdir(parents=True, exist_ok=True)
    atomic_write_json(json_path, payload)
    atomic_write_text(srt_path, to_srt(payload["merged"]))
    atomic_write_text(txt_path, to_txt(payload["merged"]))
    log.info(
        "平台字幕: %d 块 -> %d 句 / %d 字 -> %s",
        len(blocks),
        payload["quality"]["merged"],
        payload["quality"]["chars"],
        json_path,
    )
    return {
        "status": "ok",
        "lecture_id": lecture.get("id"),
        "json": str(json_path),
        "srt": str(srt_path),
        "txt": str(txt_path),
        "blocks": len(blocks),
        **(payload.get("quality") or {}),
    }


def transcribe_lecture(
    cfg: dict[str, Any],
    client: Any,
    course: dict[str, Any],
    lecture: dict[str, Any],
    force: bool = False,
    keep_audio: bool = True,
) -> dict[str, Any]:
    """跑完一讲的转写; 已存在则直接复用(幂等)。"""
    out = lecture_dir(cfg, course, lecture)
    transcript_path = out / "transcript.json"

    if transcript_path.is_file() and not force:
        log.info("已有转写, 跳过: %s", transcript_path)
        cached = read_json(transcript_path, default={}) or {}
        return {
            "status": "cached",
            "lecture_id": lecture.get("id"),
            "workdir": str(out),
            "transcript": str(transcript_path),
            "segments": cached.get("quality", {}).get("segments"),
            "merged": cached.get("quality", {}).get("merged"),
            "chars": cached.get("quality", {}).get("chars"),
        }

    urls = client.video_urls(lecture["id"])
    channel = pick_channel(urls, int(cfg["video"]["audio_channel"]))
    duration_s = float(channel.get("vod_time") or 0)

    require_disk(out, _estimate_mb(duration_s), float(cfg["disk"]["min_free_gb"]))

    audio_path = out / "audio.m4a"
    if force or not audio_path.is_file() or audio_path.stat().st_size == 0:
        log.info(
            "节次 %s: 从 %s 抽音频(时长 %.0fs)",
            lecture.get("id"),
            urlsplit(channel["url"]).netloc,
            duration_s,
        )
        extract_audio(cfg, channel["url"], audio_path)
        log.info("音频 %.1f MB", audio_path.stat().st_size / 1048576)

    raw = run_bcut(cfg, audio_path, out / "bcut_raw.json")
    segments = bcut_json_to_segments(raw)
    duration_ms = (
        int(duration_s * 1000) if duration_s else (segments[-1]["end_ms"] if segments else 0)
    )
    payload = build_payload(
        cfg, course, lecture, redact_url(channel["url"]), duration_ms, segments
    )
    write_artifacts(out, payload)
    if not keep_audio:
        audio_path.unlink(missing_ok=True)

    log.info(
        "完成: ASR %d 段 -> 重切 %d 句 / %d 字 -> %s",
        len(segments),
        payload["quality"]["merged"],
        payload["quality"]["chars"],
        transcript_path,
    )
    return {
        "status": "ok",
        "lecture_id": lecture.get("id"),
        "workdir": str(out),
        "transcript": str(transcript_path),
        "srt": str(out / "transcript.srt"),
        "txt": str(out / "transcript.txt"),
        "segments": len(segments),
        "merged": payload["quality"]["merged"],
        "chars": payload["quality"]["chars"],
        "audio_kept": keep_audio,
    }


FRAME_MODES = ("board", "ppt")


def frames_dir(
    cfg: dict[str, Any], course: dict[str, Any], lecture: dict[str, Any], mode: str
) -> Path:
    return data_dir(
        cfg, "帧", safe_name(course.get("name") or ""), lecture_ref(lecture), mode,
        course_name=course.get("name") or "",
    )


def _grab_one(
    cfg: dict[str, Any],
    ffmpeg: str,
    urls: list[dict[str, Any]],
    channel_index: dict[str, int],
    mode: str,
    course: dict[str, Any],
    lecture: dict[str, Any],
    force: bool,
) -> tuple[str, dict[str, Any]]:
    out = frames_dir(cfg, course, lecture, mode)
    if (out / "index.json").is_file() and not force:
        cached = read_json(out / "index.json", default={}) or {}
        log.info("%s 帧已存在, 跳过: %s", mode, out)
        return mode, {"status": "cached", "dir": str(out), **(cached.get("stats") or {})}

    index = channel_index[mode]
    channel = pick_channel(urls, index)
    require_disk(out, 400.0, float(cfg["disk"]["min_free_gb"]))
    log.info("%s: 用通道 %d(时长 %ss)", mode, index, channel.get("vod_time"))
    info = grab(cfg, ffmpeg, channel["url"], out, mode)
    return mode, {"status": "ok", **info}


def grab_frames(
    cfg: dict[str, Any],
    client: Any,
    course: dict[str, Any],
    lecture: dict[str, Any],
    modes: tuple[str, ...] = FRAME_MODES,
    force: bool = False,
    parallel: bool = True,
) -> dict[str, Any]:
    """抓黑板帧(通道0)与 PPT 帧(通道1), 各自去重并建索引。

    两路互相独立(不同通道、不同目录), 默认并行 —— 实测一路受限于解码而非带宽,
    两路同时跑可把一讲的抓帧时间明显压下来。
    """
    ffmpeg = find_ffmpeg(cfg)
    urls = client.video_urls(lecture["id"])
    channel_index = {
        "board": int(cfg["video"]["board_channel"]),
        "ppt": int(cfg["video"]["ppt_channel"]),
    }
    results: dict[str, Any] = {}

    if len(modes) == 1 or not parallel:
        for mode in modes:
            name, info = _grab_one(
                cfg, ffmpeg, urls, channel_index, mode, course, lecture, force
            )
            results[name] = info
        return results

    with ThreadPoolExecutor(max_workers=len(modes)) as pool:
        futures = [
            pool.submit(
                _grab_one, cfg, ffmpeg, urls, channel_index, mode, course, lecture, force
            )
            for mode in modes
        ]
        for future in as_completed(futures):
            name, info = future.result()
            results[name] = info
    return {m: results[m] for m in modes if m in results}


def resegment_lecture(
    cfg: dict[str, Any],
    course: dict[str, Any],
    lecture: dict[str, Any],
    gap_ms: int | None = None,
) -> dict[str, Any]:
    """只重切句, 不重跑识别(用已有的 bcut_raw.json 或 transcript.json)。"""
    out = lecture_dir(cfg, course, lecture)
    payload = read_json(out / "transcript.json", default=None)
    segments: list[dict[str, Any]] = []

    raw_path = out / "bcut_raw.json"
    if raw_path.is_file():
        segments = bcut_json_to_segments(read_json(raw_path, default={}) or {})
    if not segments and isinstance(payload, dict):
        segments = list(payload.get("segments") or [])
    if not segments:
        raise FileNotFoundError(f"没有可重切的转写数据: {out}")

    base = payload if isinstance(payload, dict) else {}
    rebuilt = build_payload(
        cfg,
        base.get("course") or course,
        base.get("lecture") or lecture,
        base.get("source") or "",
        int(base.get("duration_ms") or 0),
        segments,
        gap_ms=gap_ms,
    )
    write_artifacts(out, rebuilt)
    return {
        "status": "resegmented",
        "workdir": str(out),
        "segments": len(segments),
        "merged": rebuilt["quality"]["merged"],
        "merge_params": rebuilt["merge_params"],
    }


# ── 全流程编排(批量用) ────────────────────────────────

STEPS = ("transcribe", "frames", "merge", "note")


def lecture_progress(
    cfg: dict[str, Any], course: dict[str, Any], lecture: dict[str, Any]
) -> dict[str, Any]:
    """离线判断一讲各步骤的完成情况(纯查文件, 不联网、不需要凭据)。"""
    from notes import note_filename

    name = safe_name(course.get("name") or "")
    ref = lecture_ref(lecture)
    tdir = data_dir(cfg, "字幕", name, ref, course_name=name)
    froot = data_dir(cfg, "帧", name, ref, course_name=name)
    mdir = data_dir(cfg, "合成", name, ref, course_name=name)

    return {
        "lecture_id": lecture.get("id"),
        "begin": lecture.get("begin"),
        "ref": ref,
        "transcribe": (tdir / "transcript.json").is_file(),
        "frames": all((froot / mode / "index.json").is_file() for mode in FRAME_MODES),
        "merge": (mdir / "material.md").is_file(),
        "note": notes_exist(cfg, name, note_filename(lecture)),
    }


def notes_exist(cfg: dict[str, Any], course_name: str, filename: str) -> bool:
    from common import note_file

    return note_file(cfg, course_name, filename).is_file()


def process_lecture(
    cfg: dict[str, Any],
    client: Any,
    course: dict[str, Any],
    lecture: dict[str, Any],
    force: bool = False,
    with_note: bool = True,
) -> dict[str, Any]:
    """跑完一讲的全流程: 转写 -> 两路抓帧 -> 合成 -> 笔记骨架。

    每一步都幂等(已有产物则跳过); 任何一步失败即记录原因并返回, 不抛异常,
    以便批量循环能继续处理后面的节次。
    """
    from merge import merge_lecture
    from notes import ensure_skeleton

    result: dict[str, Any] = {
        "lecture_id": lecture.get("id"),
        "begin": lecture.get("begin"),
        "steps": {},
        "ok": True,
    }

    try:
        result["steps"]["transcribe"] = transcribe_lecture(
            cfg, client, course, lecture, force=force
        ).get("status")
    except Exception as exc:  # noqa: BLE001 — 批量场景要把失败原因带回去
        result["steps"]["transcribe"] = f"error: {exc}"
        result["ok"] = False
        return result

    try:
        frames = grab_frames(cfg, client, course, lecture, force=force)
        result["steps"]["frames"] = {m: v.get("status") for m, v in frames.items()}
        result["kept"] = {m: v.get("kept") for m, v in frames.items()}
    except Exception as exc:  # noqa: BLE001
        result["steps"]["frames"] = f"error: {exc}"
        result["ok"] = False
        return result

    try:
        summary = merge_lecture(cfg, course, lecture)
        result["steps"]["merge"] = "ok"
        result["material"] = summary["material"]
    except Exception as exc:  # noqa: BLE001
        result["steps"]["merge"] = f"error: {exc}"
        result["ok"] = False
        return result

    if with_note:
        try:
            transcript = read_json(lecture_dir(cfg, course, lecture) / "transcript.json", default={}) or {}
            board_index = read_json(frames_dir(cfg, course, lecture, "board") / "index.json")
            ppt_index = read_json(frames_dir(cfg, course, lecture, "ppt") / "index.json")
            info = ensure_skeleton(cfg, course, lecture, transcript, board_index, ppt_index)
            result["steps"]["note"] = info["status"]
            result["note"] = info["note"]
        except Exception as exc:  # noqa: BLE001
            result["steps"]["note"] = f"error: {exc}"

    return result
