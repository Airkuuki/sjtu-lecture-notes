"""离线单元测试(不联网): 只测纯函数与本地设施。

    python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import sys
import tempfile
import os
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from common import (  # noqa: E402
    DiskError,
    atomic_write_json,
    free_gb,
    read_json,
    redact_url,
    require_disk,
)
from sjtu import (  # noqa: E402
    ApiError,
    SjtuError,
    extract_form,
    extract_iframe_jwt,
    jwt_from_url,
    resolve_tecl_id,
    unwrap,
)

CANVAS_LAUNCH_FORM = """
<html><body>
<form action="https://v.sjtu.edu.cn/jy-lti-adapter/lti/canvas/oidc/login-initiation/canvas-record"
      method="POST" id="tool_form" data-tool-id="v.sjtu.edu.cn">
  <input type="hidden" name="iss" value="https://oc.sjtu.edu.cn">
  <input type="hidden" name="login_hint" value="551411">
  <input type="hidden" name="client_id" value="10000000000001">
  <input type="hidden" name="target_link_uri" value="https://v.sjtu.edu.cn/lti/app">
  <input type="hidden" name="lti_message_hint" value="eyJhbGciOiJIUzI1NiJ9.abc">
  <input type="hidden" name="lti_storage_target" value="">
</form>
</body></html>
"""


class TestJwtParsing(unittest.TestCase):
    def test_query_form(self):
        self.assertEqual(
            jwt_from_url("https://v.sjtu.edu.cn/x?jwt_token=abc.def-123&other=1"),
            "abc.def-123",
        )

    def test_fragment_form(self):
        self.assertEqual(
            jwt_from_url("https://v.sjtu.edu.cn/ui/#/index?jwt_token=AAA.bbb-CCC"),
            "AAA.bbb-CCC",
        )

    def test_url_encoded(self):
        self.assertEqual(jwt_from_url("https://x/?jwt_token=a%2Bb%3Dc"), "a+b=c")

    def test_absent(self):
        self.assertIsNone(jwt_from_url("https://x/#/ivsModules/index?tokenId=zz"))
        self.assertIsNone(jwt_from_url(""))
        self.assertIsNone(jwt_from_url("https://x/?jwt_token="))


class TestFormParsing(unittest.TestCase):
    def test_launch_form(self):
        form = extract_form(CANVAS_LAUNCH_FORM)
        self.assertIsNotNone(form)
        action, fields = form
        self.assertTrue(action.endswith("/canvas-record"))
        self.assertEqual(fields["iss"], "https://oc.sjtu.edu.cn")
        self.assertEqual(fields["login_hint"], "551411")
        self.assertEqual(fields["lti_storage_target"], "")
        self.assertEqual(len(fields), 6)

    def test_no_form(self):
        self.assertIsNone(extract_form("<html><body>nothing</body></html>"))

    def test_form_without_named_inputs_is_skipped(self):
        html = '<form action="/a"><input type="text"></form><form action="/b"><input name="k" value="v"></form>'
        action, fields = extract_form(html)
        self.assertEqual(action, "/b")
        self.assertEqual(fields, {"k": "v"})

    def test_iframe_jwt(self):
        html = '<div><iframe src="https://v.sjtu.edu.cn/app?jwt_token=XYZ.123"></iframe></div>'
        self.assertEqual(extract_iframe_jwt(html), "XYZ.123")
        self.assertIsNone(extract_iframe_jwt("<div>no iframe</div>"))


class TestEnvelope(unittest.TestCase):
    def test_success(self):
        self.assertEqual(
            unwrap({"code": None, "data": {"records": [1]}, "dataOk": True, "ok": True, "status": 200}),
            {"records": [1]},
        )

    def test_unauthorized_http_200(self):
        """实测: 未授权时 HTTP 仍是 200, 靠 code 字符串判断。"""
        with self.assertRaises(ApiError) as ctx:
            unwrap({"code": "401 UNAUTHORIZED", "message": "/jy-application-resourcemanage", "status": 200})
        self.assertIn("401", str(ctx.exception))

    def test_no_data_is_soft(self):
        self.assertIsNone(unwrap({"code": None, "data": None, "dataOk": False, "ok": True}))

    def test_result_fallback(self):
        self.assertEqual(unwrap({"code": 0, "result": [1, 2]}), [1, 2])

    def test_non_object(self):
        with self.assertRaises(ApiError):
            unwrap([1, 2, 3])


class TestResolveCourse(unittest.TestCase):
    COURSES = [
        {"id": 1001, "tecl_id": 2001, "name": "课程A"},
        {"id": 1002, "tecl_id": 2002, "name": "课程B"},
    ]

    def test_by_tecl_id(self):
        self.assertEqual(resolve_tecl_id(self.COURSES, 2001), 2001)

    def test_by_course_id(self):
        self.assertEqual(resolve_tecl_id(self.COURSES, 1002), 2002)

    def test_missing(self):
        with self.assertRaises(SjtuError):
            resolve_tecl_id(self.COURSES, 999)


class TestLocalFacilities(unittest.TestCase):
    def test_redact_url(self):
        self.assertEqual(
            redact_url("https://live.sjtu.edu.cn/a/b.mp4?key=secret"),
            "https://live.sjtu.edu.cn/a/b.mp4",
        )
        self.assertEqual(redact_url(""), "")

    def test_atomic_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "state.json"
            atomic_write_json(path, {"a": "中文", "n": 1})
            self.assertEqual(read_json(path), {"a": "中文", "n": 1})
            self.assertEqual(read_json(Path(d) / "missing.json", default={}), {})

    def test_disk_guard_passes_for_small_need(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertGreater(free_gb(d), 0)
            require_disk(d, need_mb=1, min_free_gb=0)

    def test_disk_guard_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(DiskError):
                require_disk(d, need_mb=1, min_free_gb=10**9)


class TestAsrNormalization(unittest.TestCase):
    BCUT_RAW = {
        "version": "0.0.4",
        "utterances": [
            {"start_time": 0, "end_time": 1200, "transcript": "大家好", "words": []},
            {"start_time": 1200, "end_time": 3500, "transcript": "  ", "words": []},
            {"start_time": 3500, "end_time": 6100, "transcript": "这是一段测试", "words": []},
        ],
    }

    def test_segments_filter_empty_and_renumber(self):
        from asr import bcut_json_to_segments

        segs = bcut_json_to_segments(self.BCUT_RAW)
        self.assertEqual([s["i"] for s in segs], [1, 2])
        self.assertEqual(segs[1]["text"], "这是一段测试")
        self.assertEqual(segs[1]["start_ms"], 3500)

    def test_srt_shape(self):
        from asr import bcut_json_to_segments, to_srt

        srt = to_srt(bcut_json_to_segments(self.BCUT_RAW))
        self.assertTrue(srt.startswith("1\n00:00:00,000 --> 00:00:01,200\n大家好\n"))
        self.assertIn("00:00:03,500 --> 00:00:06,100", srt)
        self.assertTrue(srt.endswith("\n"))

    def test_srt_time_rollover(self):
        from asr import to_srt

        srt = to_srt([{"i": 1, "start_ms": 3_723_456, "end_ms": 3_725_000, "text": "x"}])
        self.assertIn("01:02:03,456 --> 01:02:05,000", srt)

    def test_txt_has_timestamps(self):
        from asr import bcut_json_to_segments, to_txt

        txt = to_txt(bcut_json_to_segments(self.BCUT_RAW))
        self.assertEqual(txt.splitlines()[0], "[00:00] 大家好")
        self.assertEqual(txt.splitlines()[1], "[00:03] 这是一段测试")

    def test_empty_inputs(self):
        from asr import to_srt, to_txt

        self.assertEqual(to_srt([]), "")
        self.assertEqual(to_txt([]), "")


class TestPipelineHelpers(unittest.TestCase):
    LECTURE = {"id": 1001, "begin": "2026-01-01 09:00:00"}

    def test_lecture_ref(self):
        from pipeline import lecture_ref

        self.assertEqual(lecture_ref(self.LECTURE), "20260101_1001")

    def test_lecture_ref_handles_missing_begin(self):
        from pipeline import lecture_ref

        self.assertEqual(lecture_ref({"id": 7}), "unknown-date_7")

    def test_pick_channel(self):
        from pipeline import pick_channel

        urls = [{"url": "a"}, {"url": "b"}]
        self.assertEqual(pick_channel(urls, 1)["url"], "b")
        self.assertEqual(pick_channel(urls, 9)["url"], "b")  # 越界退回最后一个
        with self.assertRaises(ValueError):
            pick_channel([], 0)

    def test_estimate_mb(self):
        from pipeline import _estimate_mb

        self.assertGreaterEqual(_estimate_mb(0), 20.0)
        self.assertGreater(_estimate_mb(3300), 20.0)


class TestBoardFrames(unittest.TestCase):
    SHOWINFO = (
        "[Parsed_showinfo_2 @ 0x1] n:   0 pts:      0 pts_time:0       duration:1\n"
        "[Parsed_showinfo_2 @ 0x1] n:   1 pts:  30720 pts_time:30.72   duration:1\n"
        "[Parsed_showinfo_2 @ 0x1] n:   2 pts:  61440 pts_time:61.44   duration:1\n"
    )

    def test_parse_pts_times(self):
        from board import parse_pts_times

        self.assertEqual(parse_pts_times(self.SHOWINFO), [0.0, 30.72, 61.44])
        self.assertEqual(parse_pts_times(""), [])

    def test_dhash_row_increasing_is_zero(self):
        from board import HASH_BYTES, HASH_W, dhash_from_gray

        block = bytes([c for _ in range(8) for c in range(HASH_W)])
        self.assertEqual(len(block), HASH_BYTES)
        self.assertEqual(dhash_from_gray(block), 0)

    def test_dhash_row_decreasing_is_all_ones(self):
        from board import HASH_W, dhash_from_gray

        block = bytes([HASH_W - c for _ in range(8) for c in range(HASH_W)])
        self.assertEqual(dhash_from_gray(block), (1 << 64) - 1)

    def test_dhash_rejects_short_block(self):
        from board import dhash_from_gray

        with self.assertRaises(ValueError):
            dhash_from_gray(b"\x00" * 10)

    def test_hamming(self):
        from board import hamming

        self.assertEqual(hamming(0b1011, 0b1001), 1)
        self.assertEqual(hamming(0, 0), 0)

    def test_dedup_drops_identical(self):
        from board import dedup

        entries = [{"id": "1", "t_ms": 0}, {"id": "2", "t_ms": 30000}, {"id": "3", "t_ms": 60000}]
        kept = dedup(entries, [0xAAAA, 0xAAAA, 0xAAAA], max_distance=6, max_gap_ms=60000)
        self.assertEqual([e["id"] for e in kept], ["1"])
        self.assertIn("phash", kept[0])

    def test_dedup_keeps_changed_frame(self):
        from board import dedup

        entries = [{"id": "1", "t_ms": 0}, {"id": "2", "t_ms": 30000}]
        kept = dedup(entries, [0x0000000000000000, 0xFFFFFFFFFFFFFFFF], max_distance=6, max_gap_ms=999999)
        self.assertEqual([e["id"] for e in kept], ["1", "2"])

    def test_dedup_forces_keep_on_large_gap(self):
        from board import dedup

        entries = [{"id": "1", "t_ms": 0}, {"id": "2", "t_ms": 120000}]
        kept = dedup(entries, [0, 0], max_distance=6, max_gap_ms=60000)
        self.assertEqual([e["id"] for e in kept], ["1", "2"])

    def test_cap_evenly_keeps_first_and_last(self):
        from board import cap_evenly

        entries = [{"id": str(i)} for i in range(100)]
        capped = cap_evenly(entries, 10)
        self.assertEqual(len(capped), 10)
        self.assertEqual(capped[0]["id"], "0")
        self.assertEqual(capped[-1]["id"], "99")
        self.assertEqual(cap_evenly(entries, 0), entries)

    def test_mmss(self):
        from board import mmss

        self.assertEqual(mmss(0), "00:00")
        self.assertEqual(mmss(95_000), "01:35")


class TestMergeSegments(unittest.TestCase):
    def _seg(self, i, start, end, text):
        return {"i": i, "start_ms": start, "end_ms": end, "text": text}

    def test_merges_small_gaps(self):
        from asr import merge_segments

        segs = [self._seg(1, 0, 1000, "你好"), self._seg(2, 1100, 2000, "世界")]
        merged = merge_segments(segs, gap_ms=700)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["text"], "你好世界")
        self.assertEqual(merged[0]["start_ms"], 0)
        self.assertEqual(merged[0]["end_ms"], 2000)

    def test_breaks_on_gap(self):
        from asr import merge_segments

        segs = [self._seg(1, 0, 1000, "你好"), self._seg(2, 3000, 4000, "世界")]
        merged = merge_segments(segs, gap_ms=700)
        self.assertEqual([m["text"] for m in merged], ["你好", "世界"])
        self.assertEqual([m["i"] for m in merged], [1, 2])

    def test_breaks_on_sentence_end(self):
        from asr import merge_segments

        segs = [self._seg(1, 0, 1000, "第一句。"), self._seg(2, 1100, 2000, "第二句")]
        merged = merge_segments(segs, gap_ms=99999)
        self.assertEqual([m["text"] for m in merged], ["第一句。", "第二句"])

    def test_breaks_on_max_chars(self):
        from asr import merge_segments

        segs = [self._seg(1, 0, 1000, "啊" * 10), self._seg(2, 1100, 2000, "呀" * 10)]
        merged = merge_segments(segs, gap_ms=99999, max_chars=15)
        self.assertEqual(len(merged), 2)

    def test_breaks_on_max_duration(self):
        from asr import merge_segments

        segs = [self._seg(1, 0, 1000, "a"), self._seg(2, 1100, 30000, "b")]
        merged = merge_segments(segs, gap_ms=99999, max_dur_ms=5000)
        self.assertEqual(len(merged), 2)

    def test_empty(self):
        from asr import merge_segments

        self.assertEqual(merge_segments([]), [])


class TestMergeTimeline(unittest.TestCase):
    FRAMES = [
        {"id": "0001", "t_ms": 0, "path": "f_00001.jpg"},
        {"id": "0002", "t_ms": 60000, "path": "f_00002.jpg"},
        {"id": "0003", "t_ms": 600000, "path": "f_00003.jpg"},
    ]

    def test_nearest_frame_picks_previous(self):
        from merge import nearest_frame

        self.assertEqual(nearest_frame(self.FRAMES, 90000, 180000)["id"], "0002")

    def test_nearest_frame_within_lead(self):
        from merge import nearest_frame

        # 字幕略早于帧(1 秒内)也应挂上
        self.assertEqual(nearest_frame(self.FRAMES, 59500, 180000)["id"], "0002")

    def test_nearest_frame_too_old_is_none(self):
        from merge import nearest_frame

        # 最近的帧在 600000, 字幕在 900000, 滞后 300s > 180s -> 不挂
        self.assertIsNone(nearest_frame(self.FRAMES, 900000, 180000))

    def test_nearest_frame_before_first_is_none(self):
        from merge import nearest_frame

        self.assertIsNone(nearest_frame([{"id": "x", "t_ms": 100000}], 0, 180000))

    def test_attach_frames(self):
        from merge import attach_frames

        segs = [
            {"start_ms": 0, "text": "a"},
            {"start_ms": 61000, "text": "b"},
            {"start_ms": 900000, "text": "c"},
        ]
        rows = attach_frames(segs, self.FRAMES, "ppt", max_lag_ms=180000)
        self.assertEqual(rows[0]["ppt"]["id"], "0001")
        self.assertEqual(rows[1]["ppt"]["id"], "0002")
        self.assertIsNone(rows[2]["ppt"])

    def test_render_timeline(self):
        from merge import render_timeline

        rows = [
            {"start_ms": 65000, "text": "你好", "ppt": {"id": "0002"}, "board": None},
            {"start_ms": 70000, "text": "含|竖线", "ppt": None, "board": {"id": "0007"}},
        ]
        md = render_timeline(rows)
        self.assertIn("| 01:05 | 你好 | 0002 |  |", md)
        self.assertIn("含\\|竖线", md)
        self.assertIn("| 0007 |", md)

    def test_render_material_counts(self):
        from merge import render_material

        transcript = {
            "provider": "bcut-asr",
            "duration_ms": 60000,
            "merged": [{"start_ms": 0, "text": "甲"}],
            "quality": {"chars": 1},
        }
        md = render_material(
            {"name": "课程A"},
            {"id": 1, "begin": "2026-01-01"},
            transcript,
            {"frames": self.FRAMES},
            {"frames": self.FRAMES[:2]},
        )
        self.assertIn("板书帧: 3 帧", md)
        self.assertIn("PPT 帧: 2 帧", md)
        self.assertIn("[00:00] 甲", md)


    def test_dedup_compare_previous_rejects_accumulated_drift(self):
        """回归: 静态画面下每个采样点漂 1 bit, 与"上一保留帧"比较会累积成假变化。"""
        from board import dedup

        # 10 个采样点, 每次只变 1 bit(总漂移 9 bit)
        hashes = [((1 << i) - 1) for i in range(10)]
        entries = [{"id": str(i), "t_ms": i * 15000} for i in range(10)]

        drifted = dedup(entries, hashes, max_distance=6, compare="last_kept", max_gap_ms=10**9)
        self.assertGreater(len(drifted), 1, "last_kept 本应被漂移骗出多次保留")

        edges = dedup(entries, hashes, max_distance=6, compare="previous", max_gap_ms=10**9)
        self.assertEqual([e["id"] for e in edges], ["0"], "previous 应只保留首帧")

    def test_dedup_compare_previous_keeps_real_change(self):
        from board import dedup

        entries = [{"id": "1", "t_ms": 0}, {"id": "2", "t_ms": 15000}, {"id": "3", "t_ms": 30000}]
        hashes = [0x0000000000000000, 0x0000000000000000, 0xFFFFFFFFFFFFFFFF]
        kept = dedup(entries, hashes, max_distance=6, compare="previous", max_gap_ms=10**9)
        self.assertEqual([e["id"] for e in kept], ["1", "3"])

    def test_dedup_rejects_bad_compare(self):
        from board import dedup

        with self.assertRaises(ValueError):
            dedup([], [], compare="nonsense")


class TestNotes(unittest.TestCase):
    COURSE = {"id": 1001, "tecl_id": 2001, "name": "课程A", "teacher": "教师A"}
    LECTURE = {"id": 1001, "begin": "2026-01-01 09:00:00", "end": "2026-01-01 10:00:00"}
    TRANSCRIPT = {
        "provider": "bcut-asr",
        "duration_ms": 3288000,
        "merged": [{"start_ms": 0, "text": "甲"}],
        "quality": {"merged": 343, "chars": 10863},
    }

    def test_note_filename(self):
        from notes import note_filename

        self.assertEqual(note_filename(self.LECTURE), "20260101_1001.md")

    def test_render_skeleton_has_metadata_and_sections(self):
        from pathlib import Path

        from notes import render_skeleton

        board = {
            "frames": [
                {"id": f"{i:04d}", "t_ms": i * 30000, "path": f"f_{i:05d}.jpg"} for i in range(1, 21)
            ]
        }
        md = render_skeleton(
            self.COURSE,
            self.LECTURE,
            self.TRANSCRIPT,
            board,
            {"frames": board["frames"][:3]},
            Path("D:/x/material.md"),
            Path("D:/x/board"),
        )
        self.assertIn("# 课程A · 2026-01-01 09:00:00 听课笔记", md)
        self.assertIn("字幕 343 句", md)
        self.assertIn("板书帧 20", md)
        self.assertIn("PPT 帧 3", md)
        self.assertIn("D:/x/material.md", md)
        for section in ("本讲主线", "重点内容", "例题与推导", "易错点", "待办与通知", "板书帧速览"):
            self.assertIn(section, md)
        # 用户要求: 不再生成时间轴索引与识别修正记录两个附录
        self.assertNotIn("时间轴索引", md)
        self.assertNotIn("识别修正记录", md)

    def test_render_skeleton_without_frames(self):
        from pathlib import Path

        from notes import render_skeleton

        md = render_skeleton(
            self.COURSE, self.LECTURE, self.TRANSCRIPT, None, None,
            Path("D:/x/material.md"), Path("D:/x/board"),
        )
        self.assertIn("板书帧 0", md)
        self.assertIn("(无板书帧)", md)

    def test_frame_links_uses_absolute_paths(self):
        from pathlib import Path

        from notes import frame_links

        frames = [{"id": f"{i:04d}", "t_ms": i * 60000, "path": f"f_{i:05d}.jpg"} for i in range(1, 13)]
        text = frame_links(frames, Path("D:/data/board"), limit=3)
        self.assertEqual(text.count("![]("), 3)
        self.assertIn("D:/data/board/frames/f_00001.jpg", text)


class TestNoteLint(unittest.TestCase):
    def _lint(self, text: str):
        import tempfile
        from pathlib import Path

        from notes import lint_note

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "n.md"
            p.write_text(text, encoding="utf-8")
            return lint_note(p)

    def test_clean_note_passes(self):
        problems = self._lint("# 标题\n\n公式 $f_{Y\\mid X}=f(x,y)/f_X(x)$ 没问题\n")
        self.assertEqual(problems, [])

    def test_catches_pipe_in_math(self):
        """回归: 表格里 $f_{Y\\|X}$ 会被切坏单元格, 产生落单的 $。"""
        problems = self._lint("| a | b |\n|---|---|\n| 47:44 | $f_{Y\\|X}=1$ |\n")
        self.assertTrue(any(p["kind"] == "pipe-in-math" for p in problems))

    def test_catches_unbalanced_dollar(self):
        problems = self._lint("正文 $F(x,y)\n继续\n")
        self.assertTrue(any(p["kind"] == "unbalanced-dollar" for p in problems))

    def test_catches_unbalanced_brace(self):
        problems = self._lint("公式 $f_{X(x$ 结束\n")
        self.assertTrue(any(p["kind"] == "unbalanced-brace" for p in problems))

    def test_catches_non_ascii_in_math(self):
        problems = self._lint("公式 $X_{变量}$ 结束\n")
        self.assertTrue(any(p["kind"] == "non-ascii-in-math" for p in problems))

    def test_catches_indented_math_block(self):
        problems = self._lint("1. 列表项\n\n    $$a=b$$\n")
        self.assertTrue(any(p["kind"] == "indented-math-block" for p in problems))

    def test_catches_blank_line_in_math_block(self):
        problems = self._lint("$$\na=b\n\nc=d\n$$\n")
        self.assertTrue(any(p["kind"] == "blank-line-in-math" for p in problems))

    def test_catches_missing_frame(self):
        problems = self._lint("![帧](D:/不存在的目录/f_00001.jpg)\n")
        self.assertTrue(any(p["kind"] == "missing-frame" for p in problems))

    def test_ignores_http_and_md_links(self):
        problems = self._lint("![x](https://example.com/a.jpg) [y](other.md)\n")
        self.assertEqual(problems, [])

    def test_catches_multiline_math_block(self):
        """回归: 用户侧渲染器只认单行 $$, 多行块会让后续 markdown 全部失效。"""
        problems = self._lint("$$a=b\n=c+d$$\n\n后面正常\n")
        self.assertTrue(any(p["kind"] == "multiline-math-block" for p in problems))

    def test_single_line_math_blocks_pass(self):
        problems = self._lint("$$a=b$$\n\n$$c=d$$\n")
        self.assertEqual(problems, [])

    def test_half_open_interval_is_not_a_paren_error(self):
        """回归: (x, x+h] 是合法的半开区间, 不该被当成括号不配对。"""
        problems = self._lint("条件 $X\\in(x,\\,x+h]$ 以及 $P\\left(Y\\le y\\right)$ 都合法\n")
        self.assertFalse(any(p["kind"] == "unbalanced-paren" for p in problems), problems)

    def test_catches_unbalanced_paren(self):
        problems = self._lint("公式 $P(X\\le x$ 少了右括号\n")
        self.assertTrue(any(p["kind"] == "unbalanced-paren" for p in problems))

    def test_catches_unknown_frame_id_in_tables(self):
        """回归: 手工写的帧号可能已被去重掉, 必须对照真实索引核验。"""
        import json
        import tempfile
        from pathlib import Path

        from notes import lint_note

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "frames" / "课程A" / "20260101_1"
            (root / "board").mkdir(parents=True)
            (root / "ppt").mkdir(parents=True)
            (root / "board" / "index.json").write_text(
                json.dumps({"frames": [{"id": "0007"}, {"id": "0009"}]}), encoding="utf-8"
            )
            (root / "ppt" / "index.json").write_text(
                json.dumps({"frames": [{"id": "0003"}]}), encoding="utf-8"
            )
            note = Path(d) / "课程A" / "20260101_1.md"
            note.parent.mkdir(parents=True)
            note.write_text(
                "| 时间 | 内容 | 板书帧 | PPT帧 |\n"
                "|---|---|---|---|\n"
                "| 10:31 | 好引用 | 0007 | 0003 |\n"
                "| 11:07 | 坏引用 | 0021 | 0003 |\n",
                encoding="utf-8",
            )
            problems = lint_note(note, frames_root=Path(d) / "frames")
            self.assertEqual(len(problems), 1, problems)
            self.assertEqual(problems[0]["kind"], "unknown-frame")
            self.assertIn("0021", problems[0]["detail"])

    def test_frame_check_skipped_without_index(self):
        problems = self._lint("| 时间 | 内容 | 板书帧 | PPT帧 |\n|---|---|---|---|\n| 10:31 | x | 9999 | 9999 |\n")
        self.assertEqual(problems, [])

    def test_lint_summary_text(self):
        from notes import lint_summary

        self.assertEqual(lint_summary([]), "笔记体检: 通过")
        self.assertIn("1 处问题", lint_summary([{"line": 3, "kind": "k", "detail": "d"}]))


class TestBatchAndStatus(unittest.TestCase):
    COURSE = {"id": 1001, "tecl_id": 2001, "name": "课程A"}
    LECTURE = {"id": 1001, "begin": "2026-01-01 09:00:00", "end": "2026-01-01 10:00:00"}

    def _cfg(self, root):
        return {
            "paths": {"data_root": str(root / "data"), "notes": str(root / "notes")},
            "video": {"board_channel": 0, "ppt_channel": 1},
            "disk": {"min_free_gb": 0},
        }

    def test_lecture_progress_all_false_when_empty(self):
        import tempfile
        from pathlib import Path

        from pipeline import lecture_progress

        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(Path(d))
            progress = lecture_progress(cfg, self.COURSE, self.LECTURE)
            self.assertEqual(progress["ref"], "20260101_1001")
            for step in ("transcribe", "frames", "merge", "note"):
                self.assertFalse(progress[step], step)

    def test_lecture_progress_detects_artifacts(self):
        import tempfile
        from pathlib import Path

        from pipeline import lecture_progress

        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(Path(d))
            ref = "20260101_1001"
            name = "课程A"
            (Path(d) / "data" / "字幕" / name / ref).mkdir(parents=True)
            (Path(d) / "data" / "字幕" / name / ref / "transcript.json").write_text("{}", encoding="utf-8")
            for mode in ("board", "ppt"):
                d2 = Path(d) / "data" / "帧" / name / ref / mode
                d2.mkdir(parents=True)
                (d2 / "index.json").write_text("{}", encoding="utf-8")
            (Path(d) / "data" / "合成" / name / ref).mkdir(parents=True)
            (Path(d) / "data" / "合成" / name / ref / "material.md").write_text("x", encoding="utf-8")
            (Path(d) / "notes" / name).mkdir(parents=True)
            (Path(d) / "notes" / name / f"{ref}.md").write_text("x", encoding="utf-8")

            progress = lecture_progress(cfg, self.COURSE, self.LECTURE)
            for step in ("transcribe", "frames", "merge", "note"):
                self.assertTrue(progress[step], step)

    def test_grab_one_uses_cache_without_ffmpeg(self):
        """有 index.json 时必须直接返回缓存, 不调用 ffmpeg。"""
        import json
        import tempfile
        from pathlib import Path

        from pipeline import _grab_one

        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(Path(d))
            out = Path(d) / "data" / "帧" / "课程A" / "20260101_1001" / "board"
            out.mkdir(parents=True)
            (out / "index.json").write_text(
                json.dumps({"stats": {"raw": 110, "kept": 78}}), encoding="utf-8"
            )
            mode, info = _grab_one(
                cfg, "不存在的-ffmpeg", [], {}, "board", self.COURSE, self.LECTURE, False
            )
            self.assertEqual(mode, "board")
            self.assertEqual(info["status"], "cached")
            self.assertEqual(info["kept"], 78)

    def test_is_skeleton_distinguishes_draft_from_written(self):
        import tempfile
        from pathlib import Path

        from notes import is_skeleton

        with tempfile.TemporaryDirectory() as d:
            draft = Path(d) / "draft.md"
            draft.write_text("# 标题\n\n（待填：3~5 句）\n", encoding="utf-8")
            written = Path(d) / "written.md"
            written.write_text("# 标题\n\n## 一、概念的引入\n\n正文…\n", encoding="utf-8")
            self.assertTrue(is_skeleton(draft))
            self.assertFalse(is_skeleton(written))
            self.assertFalse(is_skeleton(Path(d) / "不存在.md"))

    def test_process_lecture_records_transcribe_failure(self):
        """转写失败时要记录原因并返回 ok=False, 不能抛异常(批量要能继续)。"""
        import tempfile
        from pathlib import Path

        import pipeline

        class Boom:
            def video_urls(self, lecture_id):
                raise RuntimeError("模拟网络故障")

        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(Path(d))
            result = pipeline.process_lecture(cfg, Boom(), self.COURSE, self.LECTURE)
            self.assertFalse(result["ok"])
            self.assertIn("模拟网络故障", result["steps"]["transcribe"])
            self.assertNotIn("frames", result["steps"])


class TestPlatformSubtitles(unittest.TestCase):
    def test_split_blocks_by_punctuation(self):
        from asr import split_platform_blocks

        out = split_platform_blocks(
            [{"start_ms": 0, "end_ms": 9000, "text": "第一句话说完了。第二句话也说完。第三句收尾。"}]
        )
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["start_ms"], 0)
        self.assertEqual(out[-1]["end_ms"], 9000)
        for a, b in zip(out, out[1:]):
            self.assertLessEqual(a["end_ms"], b["start_ms"] + 1)
        self.assertTrue(all(s["end_ms"] >= s["start_ms"] for s in out))

    def test_split_keeps_block_without_punctuation(self):
        from asr import split_platform_blocks

        out = split_platform_blocks([{"start_ms": 100, "end_ms": 2000, "text": "没有标点的一整块"}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["text"], "没有标点的一整块")

    def test_split_merges_tiny_fragments(self):
        from asr import split_platform_blocks

        out = split_platform_blocks(
            [{"start_ms": 0, "end_ms": 8000, "text": "这是一句比较长的话。好。这是最后一句比较长的话。"}]
        )
        # "好。" 太短, 应并入前一句而不是单独成行
        self.assertEqual(len(out), 2)
        self.assertTrue(out[0]["text"].endswith("好。"))

    def test_split_empty_input(self):
        from asr import split_platform_blocks

        self.assertEqual(split_platform_blocks([]), [])


class TestSubtitleParsing(unittest.TestCase):
    def test_parse_platform_payload(self):
        """平台原始结构 afterAssemblyList[{bg,ed,res}] -> 归一化片段。"""
        import sjtu

        class FakeClient(sjtu.SjtuVideo):
            def __init__(self):
                pass

            def _get(self, path, params=None):
                self.path = path
                return {
                    "afterAssemblyList": [
                        {"bg": 1000, "ed": 5000, "res": "  第一段  "},
                        {"bg": 5000, "ed": 9000, "res": ""},
                        {"bg": 9000, "ed": 9000, "res": "零长度"},
                        {"bg": 10000, "ed": 12000, "res": "第二段"},
                    ]
                }

        out = FakeClient().subtitles(12345)
        self.assertEqual(len(out), 3)  # 空文本被丢掉
        self.assertEqual(out[0]["text"], "第一段")
        self.assertEqual(out[0]["start_ms"], 1000)
        self.assertEqual(out[1]["end_ms"], 10000)  # 零长度段自动补 1s

    def test_parse_returns_none_when_absent(self):
        import sjtu

        class FakeClient(sjtu.SjtuVideo):
            def __init__(self):
                pass

            def _get(self, path, params=None):
                return None

        self.assertIsNone(FakeClient().subtitles(999))


class TestPathConvention(unittest.TestCase):
    """保存位置规则: 未明确指定 -> <工作区>/<课程>/课程转写/; 指定了 -> 用指定的。"""

    def _cfg(self, **paths):
        return {"paths": {"data_root": "", "notes": "", "notes_mode": "flat", **paths}}

    def test_default_goes_under_workspace_course_dir(self):
        import tempfile
        from pathlib import Path as P

        from common import course_transcript_dir

        with tempfile.TemporaryDirectory() as d:
            ws = P(d) / "工作区"
            (ws / "听课助手").mkdir(parents=True)
            old = os.environ.get("TINGKE_WORKSPACE")
            os.environ["TINGKE_WORKSPACE"] = str(ws)
            try:
                target = course_transcript_dir(self._cfg(), "某门课程")
            finally:
                if old is None:
                    os.environ.pop("TINGKE_WORKSPACE", None)
                else:
                    os.environ["TINGKE_WORKSPACE"] = old
            self.assertEqual(target, ws / "某门课程" / "课程转写")

    def test_explicit_data_root_wins(self):
        import tempfile
        from pathlib import Path as P

        from common import data_root

        with tempfile.TemporaryDirectory() as d:
            explicit = P(d) / "固定盘"
            root = data_root(self._cfg(data_root=str(explicit)), "某门课程")
            self.assertEqual(root, explicit)

    def test_explicit_notes_wins(self):
        import tempfile
        from pathlib import Path as P

        from common import notes_dir

        with tempfile.TemporaryDirectory() as d:
            explicit = P(d) / "用户指定笔记"
            self.assertEqual(notes_dir(self._cfg(notes=str(explicit)), "某门课程"), explicit)

    def test_env_override_beats_config(self):
        import tempfile
        from pathlib import Path as P

        from common import data_root

        with tempfile.TemporaryDirectory() as d:
            cfg_root = P(d) / "配置目录"
            env_root = P(d) / "环境变量目录"
            old = os.environ.get("TINGKE_DATA")
            os.environ["TINGKE_DATA"] = str(env_root)
            try:
                got = data_root(self._cfg(data_root=str(cfg_root)), "某门课程")
            finally:
                if old is None:
                    os.environ.pop("TINGKE_DATA", None)
                else:
                    os.environ["TINGKE_DATA"] = old
            self.assertEqual(got, env_root)

    def test_data_root_without_course_or_config_raises(self):
        from common import ConfigError, data_root

        old = os.environ.pop("TINGKE_DATA", None)
        try:
            with self.assertRaises(ConfigError):
                data_root(self._cfg(), None)
        finally:
            if old is not None:
                os.environ["TINGKE_DATA"] = old


class TestDefaultLayoutRegression(unittest.TestCase):
    """回归: data_root/notes 留空(按课程就近)时, 各命令不能报"无法确定目录"。"""

    def _cfg(self, root):
        return {
            "paths": {"data_root": "", "notes": "", "notes_mode": "flat",
                      "workspace_fallback": str(root / "工作区")},
            "asr": {}, "video": {}, "disk": {"min_free_gb": 0},
        }

    def test_note_path_works_without_explicit_notes(self):
        """notes.note_path 必须能在 notes 留空时工作(原来抛 ConfigError)。"""
        import tempfile
        from pathlib import Path as P

        import notes
        from common import note_file

        with tempfile.TemporaryDirectory() as d:
            ws = P(d) / "工作区"
            (ws / "听课助手").mkdir(parents=True)
            old = os.environ.get("TINGKE_WORKSPACE")
            os.environ["TINGKE_WORKSPACE"] = str(ws)
            try:
                cfg = self._cfg(P(d))
                course = {"name": "某门课程"}
                lecture = {"id": 42, "begin": "2026-01-01 09:00:00"}
                path = notes.note_path(cfg, course, lecture)
                self.assertEqual(path.parent, ws / "某门课程" / "课程转写")
                self.assertTrue(path.name.endswith("_42.md"))
                self.assertEqual(notes.course_index_path(cfg, course),
                                 ws / "某门课程" / "课程转写" / "_index.md")
            finally:
                if old is None:
                    os.environ.pop("TINGKE_WORKSPACE", None)
                else:
                    os.environ["TINGKE_WORKSPACE"] = old

    def test_flat_mode_does_not_add_course_subdir(self):
        """flat 模式下不能再套一层课程名(否则 status 与 notes 判断不一致)。"""
        import tempfile
        from pathlib import Path as P

        from common import note_file, notes_base

        with tempfile.TemporaryDirectory() as d:
            ws = P(d) / "工作区"
            (ws / "听课助手").mkdir(parents=True)
            old = os.environ.get("TINGKE_WORKSPACE")
            os.environ["TINGKE_WORKSPACE"] = str(ws)
            try:
                cfg = self._cfg(P(d))
                base = notes_base(cfg, "某门课程")
                self.assertEqual(base, ws / "某门课程" / "课程转写")
                self.assertNotIn("某门课程", base.name)   # 不再多一层
            finally:
                if old is None:
                    os.environ.pop("TINGKE_WORKSPACE", None)
                else:
                    os.environ["TINGKE_WORKSPACE"] = old

    def test_data_dir_does_not_duplicate_course_name(self):
        """默认落点已含课程层级, data_dir 不能再拼一次(否则 字幕/某门课程/...)。"""
        import tempfile
        from pathlib import Path as P

        from common import data_dir

        with tempfile.TemporaryDirectory() as d:
            ws = P(d) / "工作区"
            (ws / "听课助手").mkdir(parents=True)
            old = os.environ.get("TINGKE_WORKSPACE")
            os.environ["TINGKE_WORKSPACE"] = str(ws)
            try:
                cfg = self._cfg(P(d))
                got = data_dir(cfg, "字幕", "某门课程", "999", course_name="某门课程")
                self.assertEqual(got, ws / "某门课程" / "课程转写" / "字幕" / "999")
            finally:
                if old is None:
                    os.environ.pop("TINGKE_WORKSPACE", None)
                else:
                    os.environ["TINGKE_WORKSPACE"] = old

    def test_data_dir_keeps_course_when_root_explicit(self):
        """显式 data_root 是全局根, 课程层级必须保留。"""
        import tempfile
        from pathlib import Path as P

        from common import data_dir

        with tempfile.TemporaryDirectory() as d:
            explicit = P(d) / "固定盘"
            cfg = self._cfg(P(d))
            cfg["paths"]["data_root"] = str(explicit)
            got = data_dir(cfg, "字幕", "某门课程", "999", course_name="某门课程")
            self.assertEqual(got, explicit / "字幕" / "某门课程" / "999")

    def test_notes_exist_works_without_explicit_notes(self):
        """pipeline.notes_exist 同样不能因 notes 留空而挂。"""
        import tempfile
        from pathlib import Path as P

        import pipeline

        with tempfile.TemporaryDirectory() as d:
            ws = P(d) / "工作区"
            (ws / "听课助手").mkdir(parents=True)
            old = os.environ.get("TINGKE_WORKSPACE")
            os.environ["TINGKE_WORKSPACE"] = str(ws)
            try:
                cfg = self._cfg(P(d))
                self.assertFalse(pipeline.notes_exist(cfg, "某门课程", "x.md"))
                target = ws / "某门课程" / "课程转写" / "x.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("x", encoding="utf-8")
                self.assertTrue(pipeline.notes_exist(cfg, "某门课程", "x.md"))
            finally:
                if old is None:
                    os.environ.pop("TINGKE_WORKSPACE", None)
                else:
                    os.environ["TINGKE_WORKSPACE"] = old


class TestImagePathRules(unittest.TestCase):
    """回归: 图片路径必须是相对路径; 含空格要用尖括号, 否则网页/编辑器都显示不出来。"""

    def _lint(self, text: str):
        import tempfile
        from pathlib import Path as P

        from notes import lint_note

        with tempfile.TemporaryDirectory() as d:
            p = P(d) / "n.md"
            p.write_text(text, encoding="utf-8")
            return lint_note(p)

    def test_relative_image_ok(self):
        import tempfile
        from pathlib import Path as P

        from notes import lint_note

        with tempfile.TemporaryDirectory() as d:
            frame = P(d) / "帧" / "1001" / "board" / "frames" / "f_00001.jpg"
            frame.parent.mkdir(parents=True)
            frame.write_bytes(b"x")
            note = P(d) / "n.md"
            note.write_text("![](帧/1001/board/frames/f_00001.jpg)\n", encoding="utf-8")
            self.assertEqual(lint_note(note), [])

    def test_absolute_image_path_flagged(self):
        # 路径在运行时拼出来, 不写死本机盘符 —— 否则发布脱敏会把这行替换掉,
        # 导致测试在发布副本里失效。
        from pathlib import Path as P

        target = P.cwd().drive + "/some/dir/f_00001.jpg"
        problems = self._lint(f"![]({target})\n")
        self.assertTrue(any(p["kind"] == "absolute-image-path" for p in problems))

    def test_space_in_image_path_flagged(self):
        from pathlib import Path as P

        target = P.cwd().drive + "/dir with space/f_1.jpg"
        problems = self._lint(f"![]({target})\n")
        self.assertTrue(any(p["kind"] == "space-in-image-path" for p in problems))

    def test_relative_image_resolved_against_note_dir(self):
        """相对路径要按笔记所在目录解析, 不是按 cwd。"""
        import os
        import tempfile
        from pathlib import Path as P

        from notes import lint_note

        with tempfile.TemporaryDirectory() as d:
            sub = P(d) / "课程转写"
            (sub / "帧" / "1001").mkdir(parents=True)
            (sub / "帧" / "1001" / "f_1.jpg").write_bytes(b"x")
            note = sub / "n.md"
            note.write_text("![](帧/1001/f_1.jpg)\n", encoding="utf-8")
            old = os.getcwd()
            os.chdir(d)          # cwd 换成别处
            try:
                self.assertEqual(lint_note(note), [])
            finally:
                os.chdir(old)


class FakeClient:
    """最小假客户端: 只实现转写要用到的两个接口。"""

    def __init__(self, blocks=None):
        self.blocks = blocks
        self.calls = {"subtitles": 0, "video_urls": 0}

    def subtitles(self, video_id):
        self.calls["subtitles"] += 1
        return self.blocks

    def video_urls(self, video_id):
        self.calls["video_urls"] += 1
        raise RuntimeError("VIDEO-BOOM")   # 走到这里就说明要跑 bcut


class TestTranscriptProviderDispatch(unittest.TestCase):
    """核心约定: 有平台字幕就用平台字幕, 没有才跑 bcut-asr。"""

    BLOCKS = [
        {"start_ms": 0, "end_ms": 20000, "text": "第一句话在这里。第二句话也在这里。"},
    ]

    def _cfg(self, tmp):
        return {
            "paths": {"data_root": str(tmp / "数据"), "notes": "", "notes_mode": "flat"},
            "asr": {"bcut_asr_repo": ".", "prefer_platform": True, "merge_gap_ms": 1000},
            "video": {"audio_channel": 1},
            "disk": {"min_free_gb": 0},
        }

    def _course_lecture(self):
        return (
            {"id": 1, "name": "课程A"},
            {"id": 99, "begin": "2026-01-01 09:00:00", "end": "2026-01-01 09:45:00"},
        )

    def test_prefers_platform_subtitles(self):
        import tempfile
        from pathlib import Path as P

        from common import read_json
        from pipeline import transcribe_lecture

        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(P(d))
            course, lecture = self._course_lecture()
            client = FakeClient(self.BLOCKS)
            res = transcribe_lecture(cfg, client, course, lecture)
            self.assertEqual(res["provider"], "platform-subtitle")
            self.assertEqual(client.calls["video_urls"], 0)   # 没走 bcut
            saved = read_json(P(res["transcript"]), default={})
            self.assertEqual(saved.get("provider"), "platform-subtitle")
            self.assertTrue((P(d) / "数据" / "字幕" / "课程A" / "20260101_99" / "transcript.srt").is_file())

    def test_falls_back_to_bcut_when_platform_unavailable(self):
        import tempfile
        from pathlib import Path as P

        from pipeline import transcribe_lecture

        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(P(d))
            course, lecture = self._course_lecture()
            client = FakeClient(None)          # 平台没字幕
            with self.assertRaises(RuntimeError) as ctx:
                transcribe_lecture(cfg, client, course, lecture)
            self.assertIn("VIDEO-BOOM", str(ctx.exception))   # 已回退到 bcut 路径
            self.assertEqual(client.calls["subtitles"], 1)

    def test_platform_only_raises_instead_of_falling_back(self):
        import tempfile
        from pathlib import Path as P

        from asr import AsrError
        from pipeline import transcribe_lecture

        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(P(d))
            course, lecture = self._course_lecture()
            client = FakeClient(None)
            with self.assertRaises(AsrError):
                transcribe_lecture(cfg, client, course, lecture, provider="platform")
            self.assertEqual(client.calls["video_urls"], 0)

    def test_bcut_mode_skips_platform_entirely(self):
        import tempfile
        from pathlib import Path as P

        from pipeline import transcribe_lecture

        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(P(d))
            course, lecture = self._course_lecture()
            client = FakeClient(self.BLOCKS)
            with self.assertRaises(RuntimeError):
                transcribe_lecture(cfg, client, course, lecture, provider="bcut")
            self.assertEqual(client.calls["subtitles"], 0)    # 根本没问平台

    def test_materializes_cached_platform_json_without_network(self):
        import tempfile
        from pathlib import Path as P

        from pipeline import transcribe_lecture

        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(P(d))
            course, lecture = self._course_lecture()
            # 先跑一次, 生成 platform.json
            transcribe_lecture(cfg, FakeClient(self.BLOCKS), course, lecture)
            out = P(d) / "数据" / "字幕" / "课程A" / "20260101_99"
            (out / "transcript.json").unlink()
            (out / "transcript.srt").unlink()
            (out / "transcript.txt").unlink()
            # 再用一个"一问就炸"的客户端: 应该只读本地缓存
            client = FakeClient(None)
            client.subtitles = lambda v: (_ for _ in ()).throw(AssertionError("不该联网"))
            res = transcribe_lecture(cfg, client, course, lecture)
            self.assertEqual(res["provider"], "platform-subtitle")
            self.assertTrue((out / "transcript.json").is_file())

    def test_prefer_platform_false_means_bcut(self):
        import tempfile
        from pathlib import Path as P

        from pipeline import transcribe_lecture

        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(P(d))
            cfg["asr"]["prefer_platform"] = False
            course, lecture = self._course_lecture()
            client = FakeClient(self.BLOCKS)
            with self.assertRaises(RuntimeError):
                transcribe_lecture(cfg, client, course, lecture)
            self.assertEqual(client.calls["subtitles"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
