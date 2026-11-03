"""笔记骨架与课程索引。

笔记是**真正的交付物**(小文本), 所以放在项目里并进 git; 大文件仍在 D 盘。
骨架只填元信息与材料包路径, 正文由 Agent 读 material.md 后撰写。
已存在的笔记**永不覆盖**(只补骨架)。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from board import mmss
from common import atomic_write_text, log, note_file, notes_dir, notes_mode, read_json, safe_name
from merge import merge_dir
from pipeline import frames_dir, lecture_dir, lecture_ref


def note_filename(lecture: dict[str, Any]) -> str:
    begin = str(lecture.get("begin") or "")
    date = begin[:10].replace("-", "") or "unknown-date"
    return f"{date}_{lecture.get('id')}.md"


SKELETON_MARKER = "（待填"


def is_skeleton(path: Path | str) -> bool:
    """判断笔记是否仍是骨架(正文未写)。

    骨架由 ensure_skeleton 生成, 各处留有"（待填…"占位符;
    正文写完后这些占位符应当消失。
    """
    try:
        return SKELETON_MARKER in Path(path).read_text(encoding="utf-8")
    except OSError:
        return False


def note_path(cfg: dict[str, Any], course: dict[str, Any], lecture: dict[str, Any]) -> Path:
    return note_file(cfg, course.get("name") or "", note_filename(lecture), create_dir=True)


def frame_links(board: list[dict[str, Any]], board_dir: Path, limit: int = 6) -> str:
    """挑几帧板书直接嵌原图(绝对路径, 因为帧在 D 盘)。

    附录要给人"翻黑板照片"用, 所以给图片而不是帧号表格。
    """
    if not board:
        return "(无板书帧)"
    step = max(1, len(board) // limit)
    picked = board[::step][:limit]
    blocks = []
    for frame in picked:
        path = (board_dir / "frames" / frame["path"]).as_posix()
        blocks.append(f"**板书 {frame['id']}**（{mmss(frame['t_ms'])}）\n\n![]({path})")
    return "\n\n".join(blocks)


def render_skeleton(
    course: dict[str, Any],
    lecture: dict[str, Any],
    transcript: dict[str, Any],
    board_index: dict[str, Any] | None,
    ppt_index: dict[str, Any] | None,
    material_path: Path,
    board_dir: Path,
) -> str:
    board = (board_index or {}).get("frames") or []
    ppt = (ppt_index or {}).get("frames") or []
    quality = transcript.get("quality") or {}
    return f"""# {course.get('name')} · {lecture.get('begin')} 听课笔记

> 时长 {mmss(int(transcript.get('duration_ms') or 0))} ｜ 字幕 {quality.get('merged', 0)} 句 ｜ 板书帧 {len(board)} ｜ PPT 帧 {len(ppt)}
> 来源：{transcript.get('provider')} 转写 + 通道0 板书抓帧 + 通道1 PPT 抓帧

<!--
材料包（写笔记前先读它）:
  {material_path.as_posix()}
板书帧目录（可直接看图核对公式）:
  {board_dir.as_posix()}
生成时间: {datetime.now().astimezone().isoformat(timespec="seconds")}
-->

## 一、本讲主线

（待填：3~5 句，说明这一讲讲了什么、从哪到哪、与上一讲的衔接）

## 二、重点内容

### 2.1 （待填主题）　`[00:00]`

- （待填要点，每条带 `[mm:ss]` 或帧号）

## 三、例题与推导

（待填）

## 四、易错点与老师强调

（待填）

## 五、待办与通知

- [ ] （作业／测验／考试／下次课安排）

## 附录：板书帧速览

{frame_links(board, board_dir)}
"""


def course_index_path(cfg: dict[str, Any], course: dict[str, Any]) -> Path:
    return note_file(cfg, course.get("name") or "", "_index.md", create_dir=True)


def ensure_skeleton(
    cfg: dict[str, Any],
    course: dict[str, Any],
    lecture: dict[str, Any],
    transcript: dict[str, Any],
    board_index: dict[str, Any] | None,
    ppt_index: dict[str, Any] | None,
) -> dict[str, Any]:
    """生成笔记骨架; 已存在则原样保留(绝不覆盖已写好的笔记)。"""
    path = note_path(cfg, course, lecture)
    material = merge_dir(cfg, course, lecture) / "material.md"
    board_dir = frames_dir(cfg, course, lecture, "board")

    if path.is_file():
        log.info("笔记已存在, 不覆盖: %s", path)
        return {"status": "exists", "note": str(path), "material": str(material)}

    atomic_write_text(
        path,
        render_skeleton(
            course, lecture, transcript, board_index, ppt_index, material, board_dir
        ),
    )
    log.info("笔记骨架: %s", path)
    return {"status": "created", "note": str(path), "material": str(material)}


def write_course_index(
    cfg: dict[str, Any], course: dict[str, Any], rows: list[dict[str, Any]]
) -> Path:
    """课程级索引: 列出已有哪些讲的笔记。"""
    path = course_index_path(cfg, course)
    lines = [
        f"# {course.get('name')} · 听课笔记索引",
        "",
        f"- 课程 id: {course.get('id')} ｜ tecl_id: {course.get('tecl_id')}",
        f"- 教师: {course.get('teacher')} ｜ 教室: {course.get('classroom')}",
        f"- 更新: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        "",
        "| 节次 | 时间 | 笔记 |",
        "|---|---|---|",
    ]
    for row in rows:
        mark = "✅" if row.get("has_note") else "—"
        lines.append(f"| {row.get('id')} | {row.get('begin')} | {mark} {row.get('note', '')} |")
    atomic_write_text(path, "\n".join(lines) + "\n")
    return path


# ── 笔记体检 ──────────────────────────────────────────
# 为什么需要: Markdown 与 LaTeX 有两处天然冲突, 靠人眼检查必漏 ——
#   1) 表格行按 | 切单元格, 数学里一旦出现 | (哪怕写成 \|),
#      单元格会被切开, 产生落单的 $, 渲染器随后把整段文档当数学解析并报错;
#   2) 行内 $ 不配对会把后面的正文全部吞进数学模式。
# 所以交付前必须机械校验。

_MATH_RE = re.compile(r"\$\$(.+?)\$\$|\$([^$\n]+)\$", re.S)
_FRAME_REF_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_CMD_RE = re.compile(r"\\([a-zA-Z]+|.)")


def _math_snippets(text: str) -> list[tuple[int, str]]:
    """返回 [(行号, 片段)]。"""
    out = []
    for match in _MATH_RE.finditer(text):
        body = match.group(1) if match.group(1) is not None else match.group(2)
        out.append((text[: match.start()].count("\n") + 1, body))
    return out


def _frame_pools(frames_root: Path | None, note: Path) -> tuple[set[int], set[int]]:
    """从笔记路径推出帧索引位置, 取出真实存在的帧号。

    笔记在 <notes>/<课程>/<日期_节次id>.md, 帧在 <frames_root>/<课程>/<日期_节次id>/{board,ppt}/index.json
    """
    if frames_root is None:
        return set(), set()
    course, ref = note.parent.name, note.stem
    pools = []
    for mode in ("board", "ppt"):
        index = frames_root / course / ref / mode / "index.json"
        data = read_json(index, default={}) or {}
        pools.append(
            {
                int(f["id"])
                for f in (data.get("frames") or [])
                if str(f.get("id", "")).isdigit()
            }
        )
    return pools[0], pools[1]


_TIMELINE_ROW_RE = re.compile(
    r"^\|\s*\d{2}:\d{2}\s*\|.*?\|\s*([\d—\-]*)\s*\|\s*([\d—\-]*)\s*\|\s*$"
)
_SHORTHAND_ROW_RE = re.compile(r"^\|\s*(\d{4})\s*\|\s*\d{2}:\d{2}\s*\|")


def lint_note(path: Path | str, frames_root: Path | str | None = None) -> list[dict[str, Any]]:
    """机械体检一份笔记, 返回问题清单(空 = 通过)。

    给了 frames_root 时, 还会核对表格里引用的帧号是否真实存在。
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    problems: list[dict[str, Any]] = []

    def add(line: int, kind: str, detail: str) -> None:
        problems.append({"line": line, "kind": kind, "detail": detail})

    for index, line in enumerate(lines, 1):
        if line.count("$") % 2:
            add(index, "unbalanced-dollar", line.strip()[:100])

    for line_no, body in _math_snippets(text):
        if "|" in body:
            add(
                line_no,
                "pipe-in-math",
                f"数学片段里出现 | : {body.strip()[:80]} (表格行会被切坏, 请改用 \\mid)",
            )
        if body.count("{") != body.count("}"):
            add(line_no, "unbalanced-brace", body.strip()[:80])
        # 半开区间 (x, x+h] 是合法写法, 故左右括号只能合计比较, 不能按类型配对
        if body.count("(") + body.count("[") != body.count(")") + body.count("]"):
            add(line_no, "unbalanced-paren", body.strip()[:80])
        if "\n\n" in body:
            add(line_no, "blank-line-in-math", "块级公式里有空行, 部分渲染器会截断")
        for char in body:
            if ord(char) > 127:
                add(line_no, "non-ascii-in-math", f"{body.strip()[:60]} 含 {char!r}")
                break

    # 块级公式必须顶格: 缩进会被当成列表项延续/代码块, 各渲染器表现不一
    for index, line in enumerate(lines, 1):
        stripped = line.lstrip()
        if stripped.startswith("$$") and len(line) != len(stripped):
            add(
                index,
                "indented-math-block",
                f"块级公式缩进了 {len(line) - len(stripped)} 空格, 请顶格写",
            )

    # 跨多行的 $$ 块: 实测某些渲染器(用户侧)只认单行 $$,
    # 多行的第一行之后会原样输出, 且后续 markdown 解析整体失效。
    # 最稳的做法是每条公式压成单行(长推导就拆成多个单行块 + 中间用文字过渡)。
    index = 0
    while index < len(lines):
        if "$$" in lines[index] and lines[index].count("$$") < 2:
            start = index
            end = index
            while end + 1 < len(lines) and "$$" not in lines[end + 1]:
                end += 1
            add(
                start + 1,
                "multiline-math-block",
                f"块级公式跨了 {end - start + 2} 行; 请压成单行 $$...$$, 长推导拆成多个单行块",
            )
            index = end + 1
        else:
            index += 1

    for match in _FRAME_REF_RE.finditer(text):
        target = match.group(1)
        line_no = text[: match.start()].count("\n") + 1
        if target.startswith(("http://", "https://")) or target.endswith(".md"):
            continue
        # 绝对路径: 换机器/换落点就失效, 网页端也解析不了(网页只认 POSIX 绝对路径或相对路径)
        if re.match(r"^[A-Za-z]:[\\/]", target) or target.startswith("/"):
            add(line_no, "absolute-image-path",
                f"图片用了绝对路径, 请改成相对路径(相对笔记所在目录): {target[:70]}")
        # 裸空格: CommonMark 的 destination 不允许裸空格, 会整行退化成普通文字
        if " " in target and not target.startswith("<"):
            add(line_no, "space-in-image-path",
                f"图片路径含空格, 必须包在尖括号里 <...> 否则解析不成图片: {target[:70]}")
        # 相对路径按"笔记所在目录"解析, 而不是按当前工作目录 ——
        # 否则从任意位置跑 check-note 都会把正常的相对图片判成缺失。
        ref = Path(target).expanduser()
        resolved = ref if ref.is_absolute() else (path.parent / ref)
        if not resolved.is_file():
            add(
                text[: match.start()].count("\n") + 1,
                "missing-frame",
                f"引用的帧不存在: {target}",
            )

    # 表格里以帧号形式引用的, 核对是否真实存在(需 frames_root)
    board_ids, ppt_ids = _frame_pools(Path(frames_root) if frames_root else None, path)
    if board_ids or ppt_ids:
        for index, line in enumerate(lines, 1):
            row = _TIMELINE_ROW_RE.match(line)
            if row:
                for cell, pool, label in (
                    (row.group(1), board_ids, "板书"),
                    (row.group(2), ppt_ids, "PPT"),
                ):
                    if not pool:
                        continue
                    for token in re.findall(r"\d{4}", cell or ""):
                        if int(token) not in pool:
                            add(
                                index,
                                "unknown-frame",
                                f"{label}帧 {token} 在该讲已保留的帧里不存在",
                            )
            short = _SHORTHAND_ROW_RE.match(line)
            if short and board_ids and int(short.group(1)) not in board_ids:
                add(
                    index,
                    "unknown-frame",
                    f"板书帧 {short.group(1)} 在该讲已保留的帧里不存在",
                )

    return problems


def lint_summary(problems: list[dict[str, Any]]) -> str:
    if not problems:
        return "笔记体检: 通过"
    lines = [f"笔记体检: 发现 {len(problems)} 处问题"]
    for item in problems:
        lines.append(f"  L{item['line']:<5} [{item['kind']}] {item['detail']}")
    return "\n".join(lines)
