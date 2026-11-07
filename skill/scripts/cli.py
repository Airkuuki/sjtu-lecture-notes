"""听课助手 CLI。

    python scripts/cli.py auth-check
    python scripts/cli.py list-courses [--json]
    python scripts/cli.py list-lectures --course-id 2001 [--json]
    python scripts/cli.py urls --course-id 2001 --lecture-id 1001 [--json]

退出码: 0 成功 / 1 运行失败 / 2 配置或凭据问题
"""

from __future__ import annotations

import argparse
import os
import json
import sys
import time
from pathlib import Path
from typing import Any

from common import (
    ConfigError,
    DiskError,
    PROJECT_ROOT,
    api_key,
    data_dir,
    data_root,
    find_ffmpeg,
    free_gb,
    load_config,
    load_env,
    log,
    notes_dir,
    notes_mode,
    read_json,
    redact_url,
    safe_name,
    resolve_project_path,
    setup_logging,
    workspace_dir,
)
from sjtu import AuthError, SjtuError, SjtuVideo, resolve_tecl_id

from merge import merge_dir, merge_lecture
from notes import (
    ensure_skeleton,
    is_skeleton,
    lint_note,
    lint_summary,
    note_path,
    write_course_index,
)
from pipeline import (
    fetch_platform_subtitles,
    frames_dir,
    grab_frames,
    lecture_dir,
    process_lecture,
    resegment_lecture,
    transcribe_lecture,
)

EXIT_OK, EXIT_FAIL, EXIT_CONFIG = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="听课助手", description="交大课堂视频/字幕/板书流水线")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("auth-check", help="验证令牌与整条认证链").add_argument(
        "--json", action="store_true"
    )

    p_courses = sub.add_parser("list-courses", help="列出课程")
    p_courses.add_argument("--json", action="store_true")

    p_lect = sub.add_parser("list-lectures", help="列出某课程的节次")
    p_lect.add_argument("--course-id", type=int, required=True, help="课程 id 或 tecl_id")
    p_lect.add_argument("--json", action="store_true")

    p_urls = sub.add_parser("urls", help="查看某节次的视频直链(默认脱敏)")
    p_urls.add_argument("--course-id", type=int, required=True)
    p_urls.add_argument("--lecture-id", type=int, required=True)
    p_urls.add_argument("--show-url", action="store_true", help="显示完整签名直链")
    p_urls.add_argument("--json", action="store_true")

    p_tr = sub.add_parser(
        "transcribe", help="拿一讲的文字稿: 有平台字幕用平台的, 没有才跑 bcut-asr"
    )
    p_tr.add_argument("--course-id", type=int, required=True)
    p_tr.add_argument("--lecture-id", type=int, required=True)
    p_tr.add_argument("--force", action="store_true", help="忽略已有产物重跑")
    p_tr.add_argument(
        "--provider",
        choices=("auto", "platform", "bcut"),
        default="auto",
        help="auto=平台字幕优先, 没有才 bcut(默认); platform=只用平台字幕; bcut=只用必剪",
    )
    p_tr.add_argument(
        "--no-keep-audio",
        dest="keep_audio",
        action="store_false",
        help="转写完成后删除音轨(默认保留, 约 19 MB/讲)",
    )
    p_tr.add_argument("--json", action="store_true")

    p_rs = sub.add_parser("resegment", help="只重切句(不重跑识别), 用于调 gap 参数")
    p_rs.add_argument("--course-id", type=int, required=True)
    p_rs.add_argument("--lecture-id", type=int, required=True)
    p_rs.add_argument("--gap-ms", type=int, default=None, help="断句停顿阈值(默认取配置)")
    p_rs.add_argument("--json", action="store_true")

    p_fr = sub.add_parser("frames", help="抓黑板帧(通道0,定时采样)与 PPT 帧(通道1,场景变化)")
    p_fr.add_argument("--course-id", type=int, required=True)
    p_fr.add_argument("--lecture-id", type=int, required=True)
    p_fr.add_argument("--mode", choices=("board", "ppt", "both"), default="both")
    p_fr.add_argument("--force", action="store_true", help="忽略已有帧重抓")
    p_fr.add_argument("--json", action="store_true")

    p_mg = sub.add_parser("merge", help="合成三路材料包(字幕 × PPT帧 × 板书帧)")
    p_mg.add_argument("--course-id", type=int, required=True)
    p_mg.add_argument("--lecture-id", type=int, required=True)
    p_mg.add_argument("--json", action="store_true")

    p_nt = sub.add_parser("notes", help="生成笔记骨架(不覆盖已写好的笔记)")
    p_nt.add_argument("--course-id", type=int, required=True)
    p_nt.add_argument("--lecture-id", type=int, required=True)
    p_nt.add_argument("--json", action="store_true")

    p_ck = sub.add_parser("check-note", help="笔记体检(纯本地: 美元符号配对/表格管道符/帧引用)")
    p_ck.add_argument("--path", help="只体检这一份; 省略则体检 04_产出/04_笔记/ 下全部")
    p_ck.add_argument("--json", action="store_true")

    p_sb = sub.add_parser("subtitles", help="扒平台 AI 字幕(有则存 SRT/TXT, 无则明确报不可用)")
    p_sb.add_argument("--course-id", type=int, required=True)
    p_sb.add_argument("--lecture-id", type=int, required=True)
    p_sb.add_argument("--force", action="store_true", help="重扒已有字幕")
    p_sb.add_argument("--json", action="store_true")

    p_ls = sub.add_parser("list-transcripts", help="离线列出各讲平台字幕可用情况(不联网)")
    p_ls.add_argument("--json", action="store_true")

    p_st = sub.add_parser("status", help="离线总览: 各讲走到哪一步(不联网)")
    p_st.add_argument("--json", action="store_true")

    p_bt = sub.add_parser("batch", help="批量跑完整流程(幂等, 可中断续跑)")
    p_bt.add_argument("--course-id", type=int, required=True)
    p_bt.add_argument("--limit", type=int, help="最多处理前 N 讲")
    p_bt.add_argument("--force", action="store_true", help="重跑已有产物")
    p_bt.add_argument("--no-notes", action="store_true", help="只到合成, 不生成笔记骨架")
    p_bt.add_argument(
        "--provider",
        choices=("auto", "platform", "bcut"),
        default="auto",
        help="转写来源: auto=平台字幕优先(默认); platform=只用平台字幕; bcut=只用必剪",
    )
    p_bt.add_argument("--json", action="store_true")

    return parser


def make_client(cfg: dict[str, Any]) -> SjtuVideo:
    return SjtuVideo(
        api_key=api_key(),
        canvas_base=cfg["canvas"]["base"],
        launch_url=cfg["canvas"]["launch_url"],
        video_base=cfg["video"]["base"],
        video_ui=cfg["video"]["ui"],
        timeout=float(cfg["net"]["request_timeout"]),
    )


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ── 命令 ──────────────────────────────────────────────


def cmd_auth_check(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    client = make_client(cfg)
    # 数据根可能还没确定(配置留空 + 没有课程上下文), 这不该阻断认证自检:
    # 能算出就报, 算不出就说明"将按课程就近落盘"。
    info: dict[str, Any] = {
        "canvas_base": cfg["canvas"]["base"],
        "video_base": cfg["video"]["base"],
    }
    try:
        root = data_root(cfg)
        info["data_root"] = str(root)
        info["data_root_free_gb"] = round(free_gb(root), 1)
    except ConfigError:
        ws = workspace_dir()
        info["data_root"] = f"(未固定) 将按课程就近落到 {ws or '<工作区>'}/<课程>/课程转写/"
        if ws is not None:
            info["data_root_free_gb"] = round(free_gb(ws), 1)
    try:
        info["ffmpeg"] = find_ffmpeg(cfg)
    except ConfigError as exc:
        info["ffmpeg"] = f"缺失: {exc}"

    try:
        token = client.bootstrap()
        courses = client.list_courses()
        info["auth"] = "ok"
        info["jwt_len"] = len(token)
        info["hops"] = client.hops
        info["courses"] = len(courses)
    except (AuthError, SjtuError) as exc:
        info["auth"] = "failed"
        info["hops"] = client.hops
        info["error"] = str(exc)
        if args.json:
            _print_json(info)
        else:
            print(f"认证失败: {exc}", file=sys.stderr)
            for hop in client.hops:
                print(f"  {hop}", file=sys.stderr)
        return EXIT_FAIL

    if args.json:
        _print_json(info)
        return EXIT_OK

    print("认证链: 通过")
    for hop in info["hops"]:
        print(f"  {hop}")
    print(f"课程数: {info['courses']}")
    print(f"数据目录: {info['data_root']}  (剩余 {info['data_root_free_gb']} GB)")
    print(f"ffmpeg: {info['ffmpeg']}")
    return EXIT_OK


def cmd_list_courses(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    courses = make_client(cfg).list_courses()
    if args.json:
        _print_json(courses)
        return EXIT_OK
    print(f"{'课程 id':>8}  {'tecl_id':>8}  {'节次':>4}  {'PPT':>4}  {'字幕':>4}  课程")
    for c in courses:
        print(
            f"{str(c['id']):>8}  {str(c['tecl_id']):>8}  {str(c['lessons']):>4}  "
            f"{str(c['ppt_slices']):>4}  {'有' if c['has_transcript'] else '无':>4}  "
            f"{c['name']}  ({c['teacher']}, {c['classroom']})"
        )
    return EXIT_OK


def cmd_list_lectures(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    client = make_client(cfg)
    tecl_id = resolve_tecl_id(client.list_courses(), args.course_id)
    lectures = client.list_lectures(tecl_id)
    if args.json:
        _print_json({"tecl_id": tecl_id, "lectures": lectures})
        return EXIT_OK
    ready = sum(1 for x in lectures if x["playable"])
    print(f"tecl_id={tecl_id}  共 {len(lectures)} 讲, 其中可播放 {ready} 讲")
    for lec in lectures:
        flag = "可播放" if lec["playable"] else f"vodStatus={lec['vod_status']}"
        print(f"  {str(lec['id']):>9}  {lec['begin']}  {flag}  {lec['name']}")
    return EXIT_OK


def cmd_urls(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    client = make_client(cfg)
    items = client.video_urls(args.lecture_id)
    for item in items:
        if not args.show_url:
            item["url"] = redact_url(item["url"]) + "?<签名已隐藏>"
    if args.json:
        _print_json(items)
        return EXIT_OK
    print(f"节次 {args.lecture_id}: {len(items)} 个通道")
    board = int(cfg["video"]["board_channel"])
    ppt = int(cfg["video"]["ppt_channel"])
    for index, item in enumerate(items):
        role = "黑板/摄像头" if index == board else ("PPT/录屏" if index == ppt else "未知")
        print(f"  通道 {index} [{role}]  时长 {item['vod_time']}s  {item['message']}")
        print(f"    {item['url']}")
    return EXIT_OK


def cmd_transcribe(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    client = make_client(cfg)
    courses = client.list_courses()
    tecl_id = resolve_tecl_id(courses, args.course_id)
    course = next(c for c in courses if c["tecl_id"] == tecl_id)
    lecture = next(
        (x for x in client.list_lectures(tecl_id) if x["id"] == args.lecture_id), None
    )
    if lecture is None:
        print(f"课程 {tecl_id} 下找不到节次 {args.lecture_id}", file=sys.stderr)
        return EXIT_FAIL

    result = transcribe_lecture(
        cfg, client, course, lecture,
        force=args.force, keep_audio=args.keep_audio, provider=args.provider,
    )
    if args.json:
        _print_json(result)
        return EXIT_OK
    print(f"状态: {result['status']}")
    print(f"来源: {result.get('provider') or '未知'}")
    print(f"工作目录: {result['workdir']}")
    print(f"转写: {result['transcript']}")
    if result.get("segments") is not None:
        print(f"分段: {result['segments']} 段 / {result.get('chars', 0)} 字")
    return EXIT_OK


def cmd_resegment(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    client = make_client(cfg)
    courses = client.list_courses()
    tecl_id = resolve_tecl_id(courses, args.course_id)
    course = next(c for c in courses if c["tecl_id"] == tecl_id)
    lecture = next(
        (x for x in client.list_lectures(tecl_id) if x["id"] == args.lecture_id), None
    )
    if lecture is None:
        print(f"课程 {tecl_id} 下找不到节次 {args.lecture_id}", file=sys.stderr)
        return EXIT_FAIL
    result = resegment_lecture(cfg, course, lecture, gap_ms=args.gap_ms)
    if args.json:
        _print_json(result)
        return EXIT_OK
    print(f"重切完成: {result['segments']} 个碎片段 -> {result['merged']} 句")
    print(f"参数: {result['merge_params']}")
    print(f"工作目录: {result['workdir']}")
    return EXIT_OK


def _resolve_lecture(client: SjtuVideo, course_id: int, lecture_id: int):
    courses = client.list_courses()
    tecl_id = resolve_tecl_id(courses, course_id)
    course = next(c for c in courses if c["tecl_id"] == tecl_id)
    lecture = next((x for x in client.list_lectures(tecl_id) if x["id"] == lecture_id), None)
    return course, lecture


def cmd_frames(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    client = make_client(cfg)
    course, lecture = _resolve_lecture(client, args.course_id, args.lecture_id)
    if lecture is None:
        print(f"找不到节次 {args.lecture_id}", file=sys.stderr)
        return EXIT_FAIL
    modes = ("board", "ppt") if args.mode == "both" else (args.mode,)
    result = grab_frames(cfg, client, course, lecture, modes=modes, force=args.force)
    if args.json:
        _print_json(result)
        return EXIT_OK
    for mode, info in result.items():
        print(
            f"{mode:>5}: 候选 {info.get('raw', '-')} 帧 -> 保留 {info.get('kept', '-')} 帧"
            f" (去重 {info.get('dedup_ratio', 0) * 100:.0f}%)  {info.get('dir', '')}"
        )
    return EXIT_OK


def cmd_merge(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    client = make_client(cfg)
    course, lecture = _resolve_lecture(client, args.course_id, args.lecture_id)
    if lecture is None:
        print(f"找不到节次 {args.lecture_id}", file=sys.stderr)
        return EXIT_FAIL
    result = merge_lecture(cfg, course, lecture)
    if args.json:
        _print_json(result)
        return EXIT_OK
    print(f"材料包: {result['material']}")
    print(f"时间轴: {result['timeline']}")
    print(
        f"字幕 {result['segments']} 句 | 板书 {result['board_frames']} 帧 | PPT {result['ppt_frames']} 帧"
    )
    return EXIT_OK


def cmd_notes(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    client = make_client(cfg)
    course, lecture = _resolve_lecture(client, args.course_id, args.lecture_id)
    if lecture is None:
        print(f"找不到节次 {args.lecture_id}", file=sys.stderr)
        return EXIT_FAIL

    tdir = lecture_dir(cfg, course, lecture)
    transcript = read_json(tdir / "transcript.json", default=None)
    if not isinstance(transcript, dict):
        print(f"缺少转写, 先跑 transcribe: {tdir}", file=sys.stderr)
        return EXIT_FAIL
    board_index = read_json(frames_dir(cfg, course, lecture, "board") / "index.json")
    ppt_index = read_json(frames_dir(cfg, course, lecture, "ppt") / "index.json")
    material = merge_dir(cfg, course, lecture) / "material.md"
    if not material.is_file():
        print(f"缺少材料包, 先跑 merge: {material}", file=sys.stderr)
        return EXIT_FAIL

    result = ensure_skeleton(cfg, course, lecture, transcript, board_index, ppt_index)

    # 顺手刷新课程索引(列出各讲笔记有无)
    rows = []
    for item in client.list_lectures(course["tecl_id"]):
        path = note_path(cfg, course, item)
        rows.append(
            {
                "id": item["id"],
                "begin": item["begin"],
                "has_note": path.is_file(),
                "note": path.name if path.is_file() else "",
            }
        )
    index_path = write_course_index(cfg, course, rows)

    if args.json:
        _print_json({**result, "course_index": str(index_path)})
        return EXIT_OK
    print(f"笔记: {result['note']}")
    print(f"材料包: {result['material']}")
    print(f"课程索引: {index_path}")
    print("接下来读材料包写正文（硬规则见 SKILL.md）")
    return EXIT_OK


def cmd_check_note(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    """纯本地体检, 不联网、不需要凭据。"""
    if args.path:
        targets = [Path(args.path)]
    else:
        targets = sorted(_all_note_files(cfg))

    if not targets:
        print("没有找到任何笔记(04_产出/04_笔记/)", file=sys.stderr)
        return EXIT_OK

    results = []
    failed = 0
    for target in targets:
        if not target.is_file():
            results.append({"note": str(target), "problems": [{"kind": "missing-file"}]})
            failed += 1
            continue
        # 帧索引可能在不同数据根下, 逐个尝试, 命中哪个用哪个
        problems = None
        for froot in _frame_roots_for(cfg, target):
            problems = lint_note(target, frames_root=froot)
            if problems and any(p["kind"] == "unknown-frame" for p in problems):
                continue          # 这个根里没有对应帧, 换下一个
            break
        if problems is None:
            problems = lint_note(target)
        failed += 1 if problems else 0
        results.append({"note": str(target), "problems": problems})

    if args.json:
        _print_json(results)
    else:
        for item in results:
            print(f"{item['note']}: {lint_summary(item['problems'])}")
        print(f"\n共体检 {len(targets)} 份, 通过 {len(targets) - failed} 份")
    return EXIT_FAIL if failed else EXIT_OK


def _data_roots(cfg: dict[str, Any]) -> list[tuple[Path, str | None]]:
    """列出所有可能的数据根: [(根目录, 该根所属课程名 or None)]。

    两种布局都要支持:
      1) **按课程就近**(新约定): <工作区>/<课程>/课程转写/
         这种根的课程名已知, <根>/帧/<节次>/ 直接就是节次目录;
      2) **全局数据根**(显式 data_root 或历史位置): <根>/帧/<课程>/<节次>/
         这种根不知道课程名, 需要再扫一层。
    """
    result: list[tuple[Path, str | None]] = []
    configured = str((cfg.get("paths") or {}).get("data_root", "") or "").strip()
    if configured:
        result.append((resolve_project_path(configured), None))
    ws = workspace_dir()
    if ws is not None and ws.is_dir():
        for course_dir in sorted(p for p in ws.iterdir() if p.is_dir()):
            candidate = course_dir / "课程转写"
            if candidate.is_dir():
                result.append((candidate, course_dir.name))
    legacy_env = os.environ.get("TINGKE_LEGACY_DATA", "").strip()
    if legacy_env:
        legacy = Path(legacy_env)
        if legacy.is_dir():
            result.append((legacy, None))

    seen, out = set(), []
    for root, course in result:
        key = (str(root).lower(), course)
        if key not in seen and root.is_dir():
            seen.add(key)
            out.append((root, course))
    return out


def _scan_transcripts(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """扫描所有数据根, 汇总成 (课程, 节次) 行。"""
    rows: list[dict[str, Any]] = []

    def _has_content(d: Path) -> bool:
        return d.is_dir() and any(d.iterdir())

    for root, known_course in _data_roots(cfg):
        frames_root = root / "帧"
        subs_root = root / "字幕"
        merge_root = root / "合成"
        if not (frames_root.is_dir() or subs_root.is_dir()):
            continue

        if known_course:
            # 新布局: 根就是这门课的 课程转写, 底下直接是 <节次>
            courses = [known_course]
            refs_of = lambda course: {  # noqa: E731
                p.name for p in (frames_root).glob("*") if _has_content(p)
            } | {
                p.name for p in (subs_root).glob("*") if _has_content(p)
            }
            sub_dir = lambda course: subs_root          # noqa: E731
            frame_dir = lambda course: frames_root      # noqa: E731
            merge_dir_ = lambda course: merge_root      # noqa: E731
        else:
            # 全局布局: <根>/帧/<课程>/<节次>
            courses = sorted(
                {p.name for p in frames_root.glob("*") if p.is_dir()}
                | {p.name for p in subs_root.glob("*") if p.is_dir()}
            )
            refs_of = lambda course: {  # noqa: E731
                p.name for p in (frames_root / course).glob("*") if _has_content(p)
            } | {
                p.name for p in (subs_root / course).glob("*") if _has_content(p)
            }
            sub_dir = lambda course: subs_root / course       # noqa: E731
            frame_dir = lambda course: frames_root / course   # noqa: E731
            merge_dir_ = lambda course: merge_root / course   # noqa: E731

        for course in courses:
            for ref in sorted(refs_of(course)):
                fd, sd, md = frame_dir(course), sub_dir(course), merge_dir_(course)
                modes = [m for m in ("board", "ppt")
                         if (fd / ref / m / "index.json").is_file()]
                kept = {}
                for mode in modes:
                    data = read_json(fd / ref / mode / "index.json", default={}) or {}
                    kept[mode] = (data.get("stats") or {}).get("kept")
                rows.append({
                    "root": str(root),
                    "course": course,
                    "ref": ref,
                    # 有平台字幕也算"转写就绪"(transcribe 会把它转成标准产物)
                    "transcribe": (sd / ref / "transcript.json").is_file()
                    or (sd / ref / "platform.json").is_file(),
                    "board": "board" in modes,
                    "ppt": "ppt" in modes,
                    "board_frames": kept.get("board"),
                    "ppt_frames": kept.get("ppt"),
                    "merge": (md / ref / "material.md").is_file(),
                })
    return rows


def _all_note_files(cfg: dict[str, Any]) -> list[Path]:
    """收集所有笔记文件(排除 帧/字幕/合成 里的索引 md)。"""
    roots: list[Path] = []
    configured = str((cfg.get("paths") or {}).get("notes", "") or "").strip()
    if configured:
        roots.append(resolve_project_path(configured))
    ws = workspace_dir()
    if ws is not None and ws.is_dir():
        for course_dir in sorted(p for p in ws.iterdir() if p.is_dir()):
            candidate = course_dir / "课程转写"
            if candidate.is_dir():
                roots.append(candidate)
    legacy = PROJECT_ROOT / "04_产出" / "04_笔记"
    if legacy.is_dir():
        roots.append(legacy)

    out: list[Path] = []
    seen = set()
    skip_dirs = {"帧", "字幕", "合成"}
    for root in roots:
        for f in sorted(root.rglob("*.md")):
            if skip_dirs & set(f.relative_to(root).parts[:-1]):
                continue
            key = str(f).lower()
            if key not in seen:
                seen.add(key)
                out.append(f)
    return out


def _frame_roots_for(cfg: dict[str, Any], note: Path) -> list[Path]:
    """为一份笔记找出可能的"帧根目录"。"""
    roots: list[Path] = []
    # 新布局: 笔记就在 <课程>/课程转写/ 下, 帧在同目录的 帧/
    if note.parent.name == "课程转写":
        roots.append(note.parent)
    for root, _ in _data_roots(cfg):
        roots.append(root)
    return roots


def cmd_status(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    """离线总览: 扫描所有数据根与笔记目录, 不联网、不需要凭据。"""
    rows = _scan_transcripts(cfg)
    for row in rows:
        course, ref = row["course"], row["ref"]
        base = notes_dir(cfg, course)
        note_file = base / f"{ref}.md" if notes_mode(cfg) == "flat" else base / course / f"{ref}.md"
        row.update({
            # 骨架与正文要分开算: 骨架里仍有"（待填"占位符
            "note": note_file.is_file() and not is_skeleton(note_file),
            "skeleton": note_file.is_file() and is_skeleton(note_file),
        })

    if args.json:
        _print_json(rows)
        return EXIT_OK

    if not rows:
        print("还没有处理过任何节次。先跑: batch --course-id <tecl_id>")
        return EXIT_OK

    print(f"{'课程':<12}{'节次':<18}{'转写':<6}{'板书':<6}{'PPT':<6}{'合成':<6}{'笔记':<6}")
    print("-" * 60)
    for r in rows:
        note_mark = "✅" if r["note"] else ("骨架" if r["skeleton"] else "—")
        print(
            f"{r['course'][:11]:<12}{r['ref']:<18}{('✅' if r['transcribe'] else '—'):<6}"
            f"{(str(r['board_frames']) + ' 帧' if r['board'] else '—'):<6}"
            f"{(str(r['ppt_frames']) + ' 帧' if r['ppt'] else '—'):<6}"
            f"{('✅' if r['merge'] else '—'):<6}{note_mark:<6}"
        )
    written = sum(1 for r in rows if r["note"])
    skeletons = sum(1 for r in rows if r["skeleton"])
    print(
        f"\n共 {len(rows)} 讲: 已抓帧 {sum(1 for r in rows if r['board'] and r['ppt'])} 讲, "
        f"已合成 {sum(1 for r in rows if r['merge'])} 讲, "
        f"笔记正文 {written} 讲" + (f", 仅骨架 {skeletons} 讲" if skeletons else "")
    )
    return EXIT_OK


def cmd_batch(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    """批量跑完整流程; 每步幂等, 可随时中断再续。"""
    client = make_client(cfg)
    courses = client.list_courses()
    tecl_id = resolve_tecl_id(courses, args.course_id)
    course = next(c for c in courses if c["tecl_id"] == tecl_id)

    lectures = client.list_lectures(tecl_id)
    playable = [x for x in lectures if x.get("playable", True)]
    if args.limit:
        playable = playable[: args.limit]

    log.info(
        "课程 %s: %d 讲(可播放 %d), 开始批量处理", course.get("name"), len(lectures), len(playable)
    )

    results = []
    for position, lecture in enumerate(playable, 1):
        log.info("── [%d/%d] %s ──", position, len(playable), lecture.get("begin"))
        started = time.time()
        item = process_lecture(
            cfg,
            client,
            course,
            lecture,
            force=args.force,
            with_note=not args.no_notes,
                provider=args.provider,
        )
        item["seconds"] = round(time.time() - started, 1)
        results.append(item)
        log.info(
            "[%d/%d] %s -> %s (%.0fs)",
            position,
            len(playable),
            lecture.get("id"),
            "成功" if item["ok"] else "失败",
            item["seconds"],
        )

    ok = sum(1 for r in results if r["ok"])
    total = round(sum(r["seconds"] for r in results), 1)
    summary = {
        "course": course.get("name"),
        "tecl_id": tecl_id,
        "lectures": len(results),
        "ok": ok,
        "failed": len(results) - ok,
        "seconds": total,
        "results": results,
    }
    if args.json:
        _print_json(summary)
    else:
        print(f"课程 {course.get('name')}: {ok}/{len(results)} 讲完成, 共 {total:.0f}s")
        for r in results:
            if not r["ok"]:
                print(f"  失败 {r['lecture_id']}: {r['steps']}")
    return EXIT_OK if ok else EXIT_FAIL


def cmd_subtitles(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    client = make_client(cfg)
    course, lecture = _resolve_lecture(client, args.course_id, args.lecture_id)
    if lecture is None:
        print(f"找不到节次 {args.lecture_id}", file=sys.stderr)
        return EXIT_FAIL
    result = fetch_platform_subtitles(cfg, client, course, lecture, force=args.force)
    if args.json:
        _print_json(result)
        return EXIT_OK
    if result["status"] == "unavailable":
        print(f"节次 {args.lecture_id}: 平台未生成字幕")
        print(f"  {result['detail']}")
        return EXIT_FAIL
    print(f"平台字幕: {result['json']}")
    print(f"  {result.get('blocks')} 个原始块 -> {result.get('merged')} 句 / {result.get('chars')} 字")
    return EXIT_OK


def cmd_list_transcripts(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    """离线看各讲已有的字幕产物(支持两种数据布局)。"""
    rows = []
    for root, known_course in _data_roots(cfg):
        subs_root = root / "字幕"
        if not subs_root.is_dir():
            continue
        if known_course:
            # 新布局: <课程>/课程转写/字幕/<节次>/
            course_dirs = [(known_course, subs_root)]
        else:
            # 全局布局: <根>/字幕/<课程>/<节次>/
            course_dirs = [(p.name, p) for p in sorted(subs_root.glob("*")) if p.is_dir()]
        for course_name, base in course_dirs:
            for lec_dir in sorted(p for p in base.glob("*") if p.is_dir()):
                pj = lec_dir / "platform.json"
                tj = lec_dir / "transcript.json"
                rows.append(
                    {
                        "course": course_name,
                        "ref": lec_dir.name,
                        "platform": pj.is_file(),
                        "bcut": tj.is_file(),
                    }
                )

    if args.json:
        _print_json(rows)
        return EXIT_OK
    if not rows:
        print("数据目录里还没有任何字幕产物")
        return EXIT_OK
    print(f"{'课程':<14}{'节次':<20}{'平台字幕':<10}{'bcut转写':<10}")
    print("-" * 56)
    for r in rows:
        print(f"{r['course'][:13]:<14}{r['ref']:<20}"
              f"{'✅' if r['platform'] else '—':<10}{'✅' if r['bcut'] else '—':<10}")
    print(f"\n共 {len(rows)} 讲: 平台字幕 {sum(1 for r in rows if r['platform'])} 讲, "
          f"bcut 转写 {sum(1 for r in rows if r['bcut'])} 讲")
    return EXIT_OK


COMMANDS = {
    "auth-check": cmd_auth_check,
    "list-courses": cmd_list_courses,
    "list-lectures": cmd_list_lectures,
    "urls": cmd_urls,
    "transcribe": cmd_transcribe,
    "resegment": cmd_resegment,
    "frames": cmd_frames,
    "merge": cmd_merge,
    "notes": cmd_notes,
    "check-note": cmd_check_note,
    "subtitles": cmd_subtitles,
    "list-transcripts": cmd_list_transcripts,
    "status": cmd_status,
    "batch": cmd_batch,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        load_env()
        cfg = load_config()
        return COMMANDS[args.command](cfg, args)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except DiskError as exc:
        print(f"磁盘不足: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except SjtuError as exc:
        print(f"平台错误: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
