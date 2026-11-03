"""上海交大课堂视频平台客户端。

认证链(全部已实测验证, 无需浏览器 / 无需扫码 / 无需粘 tokenId):

    Canvas 个人访问令牌
      -> GET /login/session_token?return_to=<账号级工具 3136 launch>   拿到一次性 session_url
      -> GET session_url                                              建立 Canvas web 会话
      -> GET launch_url                                               拿到 LTI 1.3 launch 表单
      -> POST 表单 -> v.sjtu /jy-lti-adapter/.../login-initiation      302
      -> Canvas /api/lti/authorize_redirect -> /api/lti/authorize      拿到 id_token 表单
      -> POST 表单 -> v.sjtu /jy-lti-adapter/.../launch/space-home      302, Location 带 jwt_token
    之后所有数据接口: 头 `jwt-token` + `Referer: .../resourcemanage-ui/`

接口(前缀 = video.base = /jy-application-resourcemanage):
    GET /v1/group_subject_vod_list/t-1     课程列表
    GET /v1/subject_vod_list_new           某教学班(teclId)的节次
    GET /v1/course_vod_urls_new            节次的视频直链(多个通道)

响应信封(三种都要认):
    成功      {"code":null,"data":{...},"dataOk":true,"ok":true,"status":200}
    未授权    {"code":"401 UNAUTHORIZED","status":200}                 HTTP 仍是 200
    无数据    {"code":null,"data":null,"dataOk":false,"ok":true}
"""

from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import unquote, urljoin, urlsplit

import requests

from common import ConfigError

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

MAX_HOPS = 16
VOD_READY = 5  # vodStatus: 5=可播放 4=修复中 6=暂无回放

_FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.I | re.S)
_ACTION_RE = re.compile(r"\baction\s*=\s*[\"']([^\"']*)[\"']", re.I)
_FIELD_RE = re.compile(r"<(?:input|button)\b([^>]*)>", re.I)
_NAME_RE = re.compile(r"\bname\s*=\s*[\"']([^\"']+)[\"']", re.I)
_VALUE_RE = re.compile(r"\bvalue\s*=\s*[\"']([^\"']*)[\"']", re.I)
_IFRAME_RE = re.compile(r"<iframe\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)
_JWT_IN_URL_RE = re.compile(r"https?://[^\"'\s<>]*jwt_token=[^\"'\s<>&]+", re.I)


class SjtuError(RuntimeError):
    """平台相关错误。"""


class AuthError(SjtuError):
    """认证链失败(附走到第几跳)。"""


class ApiError(SjtuError):
    """接口返回错误。"""


# ── 纯函数(可离线单测) ────────────────────────────────


def jwt_from_url(url: str) -> str | None:
    """从 URL 的 fragment 或 query 里取 jwt_token。"""
    if not url:
        return None
    frag = url.split("#", 1)[1] if "#" in url else url
    query = frag.split("?", 1)[1] if "?" in frag else frag
    for pair in query.split("&"):
        key, sep, value = pair.partition("=")
        if sep and key == "jwt_token" and value:
            return unquote(value)
    return None


def extract_form(html: str) -> tuple[str, dict[str, str]] | None:
    """取第一个带命名字段的 <form>, 返回 (action, 字段字典)。"""
    for attrs, body in _FORM_RE.findall(html or ""):
        fields: dict[str, str] = {}
        for field_attrs in _FIELD_RE.findall(body):
            name = _NAME_RE.search(field_attrs)
            if not name:
                continue
            value = _VALUE_RE.search(field_attrs)
            fields[name.group(1)] = value.group(1) if value else ""
        if fields:
            action = _ACTION_RE.search(attrs)
            return (action.group(1) if action else ""), fields
    return None


def extract_iframe_jwt(html: str) -> str | None:
    """页面里内嵌的 iframe src 若带 jwt_token 则取出。"""
    for src in _IFRAME_RE.findall(html or ""):
        token = jwt_from_url(src)
        if token:
            return token
    match = _JWT_IN_URL_RE.search(html or "")
    return jwt_from_url(match.group(0)) if match else None


def unwrap(payload: Any) -> Any:
    """拆信封: 返回 data / result; 无数据返回 None; 出错抛 ApiError。"""
    if not isinstance(payload, dict):
        raise ApiError(f"接口返回非对象: {type(payload).__name__}")
    code = payload.get("code")
    if code not in (None, 0, "0"):
        message = payload.get("message") or payload.get("msg") or ""
        raise ApiError(f"接口返回错误 code={code} {message}".strip())
    if payload.get("dataOk") is False or payload.get("ok") is False:
        return None
    if "data" in payload:
        return payload.get("data")
    return payload.get("result")


def _records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        recs = data.get("records")
        if isinstance(recs, list):
            return recs
    if isinstance(data, list):
        return data
    return []


# ── 客户端 ────────────────────────────────────────────


class SjtuVideo:
    """认证 + 数据接口。构造时不发任何网络请求。"""

    def __init__(
        self,
        api_key: str,
        canvas_base: str,
        launch_url: str,
        video_base: str,
        video_ui: str,
        timeout: float = 30.0,
        user_agent: str = DEFAULT_UA,
    ) -> None:
        if not api_key:
            raise ConfigError("缺少 CANVAS_API_KEY")
        self.api_key = api_key
        self.canvas_base = canvas_base.rstrip("/")
        self.launch_url = launch_url
        self.video_base = video_base.rstrip("/")
        self.video_ui = video_ui
        self.timeout = timeout
        self.ua = user_agent

        self._jwt: str | None = None
        self.hops: list[str] = []  # 诊断用: 认证链走过的主机+路径

        self._api = requests.Session()
        self._api.headers.update(
            {
                "User-Agent": self.ua,
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
            }
        )
        self._web = requests.Session()
        self._web.headers.update(
            {
                "User-Agent": self.ua,
                "Accept-Language": "zh-CN",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )

    # ── 认证 ──────────────────────────────────────────

    def _hop_label(self, url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.netloc}{parts.path[:60]}"

    def bootstrap(self, force: bool = False) -> str:
        """走完整认证链拿到 jwt_token; 结果缓存在实例上。"""
        if self._jwt and not force:
            return self._jwt

        self.hops = []
        session_url = self._get_session_url()
        self._web.get(session_url, timeout=self.timeout, allow_redirects=True)
        self.hops.append("session_token -> Canvas web 会话已建立")

        url, method, data = self.launch_url, "GET", None
        for index in range(1, MAX_HOPS + 1):
            if method == "GET":
                resp = self._web.get(url, timeout=self.timeout, allow_redirects=False)
            else:
                resp = self._web.post(
                    url, data=data, timeout=self.timeout, allow_redirects=False
                )
            self.hops.append(f"{index:2d}. {method} {self._hop_label(resp.url)} -> {resp.status_code}")

            location = resp.headers.get("Location")
            if location:
                nxt = urljoin(resp.url, location)
                token = jwt_from_url(nxt)
                if token:
                    self.hops.append(f"{index:2d}. Location 中发现 jwt_token ✔")
                    self._jwt = token
                    return token
                url, method, data = nxt, "GET", None
                continue

            token = extract_iframe_jwt(resp.text)
            if token:
                self.hops.append(f"{index:2d}. 页面内嵌 jwt_token ✔")
                self._jwt = token
                return token

            form = extract_form(resp.text)
            if not form:
                raise AuthError(
                    "认证链断了: 既无 Location/jwt_token, 也没有可提交的表单。\n"
                    + "\n".join(self.hops)
                )
            action, fields = form
            url = urljoin(resp.url, action) if action else resp.url
            method, data = "POST", fields

        raise AuthError(
            f"认证链超过 {MAX_HOPS} 跳仍未拿到 jwt_token。\n" + "\n".join(self.hops)
        )

    def _get_session_url(self) -> str:
        resp = self._api.get(
            f"{self.canvas_base}/login/session_token",
            params={"return_to": self.launch_url},
            timeout=self.timeout,
            allow_redirects=False,
        )
        if resp.status_code == 401:
            raise AuthError("Canvas 鉴权失败(401): 令牌无效/已过期, 或请求缺少 Authorization 头")
        resp.raise_for_status()
        try:
            payload = resp.json()
        except ValueError as exc:
            raise AuthError(f"session_token 返回非 JSON: {resp.text[:120]!r}") from exc
        session_url = payload.get("session_url")
        if not session_url:
            raise AuthError(f"session_token 未返回 session_url: {payload}")
        return session_url

    # ── 数据接口 ──────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.ua,
            "Accept": "application/json, text/plain, */*",
            "Referer": self.video_ui,
            "jwt-token": self.bootstrap(),
        }

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.video_base}{path}"
        resp = requests.get(
            url, params=params, headers=self._headers(), timeout=self.timeout
        )
        resp.raise_for_status()
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ApiError(
                f"{path} 返回非 JSON(HTTP {resp.status_code}): {resp.text[:120]!r}"
            ) from exc
        return unwrap(payload)

    def list_courses(self, page_size: int = 100) -> list[dict[str, Any]]:
        """课程列表(带能力标记: 有无字幕 / PPT 切片数)。"""
        out: list[dict[str, Any]] = []
        for page in range(1, 51):
            data = self._get(
                "/v1/group_subject_vod_list/t-1",
                {
                    "page.pageIndex": str(page),
                    "page.pageSize": str(page_size),
                    "page.orders[0].asc": "false",
                    "page.orders[0].field": "updateTime",
                },
            )
            batch = _records(data)
            if not batch:
                break
            out.extend(batch)
            page_count = 0
            if isinstance(data, dict):
                try:
                    page_count = int(data.get("pageCount") or 0)
                except (TypeError, ValueError):
                    page_count = 0
            if page_count and page >= page_count:
                break
        return [self._course_view(r) for r in out]

    @staticmethod
    def _course_view(record: dict[str, Any]) -> dict[str, Any]:
        teachers = record.get("teacNames") or []
        return {
            "id": record.get("id"),
            "tecl_id": record.get("teclId"),
            "name": record.get("subjName") or record.get("teclName") or "",
            "code": record.get("subjCode") or "",
            "teacher": "/".join(str(t) for t in teachers) if isinstance(teachers, list) else str(teachers or ""),
            "classroom": record.get("clroName") or "",
            "lessons": record.get("courTimes"),
            "ppt_slices": record.get("coursePptSliceCount"),
            "has_transcript": bool(record.get("courTransferFlag")),
        }

    def list_lectures(self, tecl_id: int) -> list[dict[str, Any]]:
        """某教学班的节次列表。"""
        data = self._get(
            "/v1/subject_vod_list_new",
            {
                "page.pageIndex": "1",
                "page.pageSize": "1000",
                "teclIds": str(tecl_id),
                "page.orders[0].asc": "false",
                "page.orders[0].field": "courBeginTime",
                "schoolOpenStatusFlag": "false",
            },
        )
        return [self._lecture_view(r) for r in _records(data)]

    @staticmethod
    def _lecture_view(record: dict[str, Any]) -> dict[str, Any]:
        status = record.get("vodStatus")
        try:
            status_int = int(status)
        except (TypeError, ValueError):
            status_int = -1
        return {
            "id": record.get("id"),
            "name": record.get("subjName") or record.get("courName") or "",
            "begin": record.get("courBeginTime") or record.get("beginTime") or "",
            "end": record.get("courEndTime") or record.get("endTime") or "",
            "vod_status": status_int,
            "playable": status_int == VOD_READY,
            # 平台是否已生成 AI 字幕, 以及生成任务状态(0=未开始, 6=完成)
            "has_transcript": bool(record.get("courTransferFlag")),
            "transcript_status": record.get("transcriptOptimizeTaskStatus"),
        }

    def subtitles(self, video_id: int | str) -> list[dict[str, Any]] | None:
        """平台 AI 字幕; 该讲尚未生成时返回 None。

        返回结构: [{"start_ms", "end_ms", "text"}]
        平台原始结构为 afterAssemblyList=[{bg, ed, res, zh, en, ...}], bg/ed 为毫秒,
        每段通常 15~100 秒(整块), 需再切句。
        """
        data = self._get(f"/v1/course/ai/translate/{video_id}")
        if not isinstance(data, dict):
            return None
        segments: list[dict[str, Any]] = []
        for item in data.get("afterAssemblyList") or []:
            text = str(item.get("res") or "").strip()
            if not text:
                continue
            try:
                start = int(item.get("bg") or 0)
                end = int(item.get("ed") or 0)
            except (TypeError, ValueError):
                continue
            if end <= start:
                end = start + 1000
            segments.append({"start_ms": start, "end_ms": end, "text": text})
        return segments

    def video_urls(self, video_id: int | str) -> list[dict[str, Any]]:
        """节次的视频直链(每个通道一条)。直链自带签名, 不要再打印到日志。"""
        data = self._get("/v1/course_vod_urls_new", {"courseId": str(video_id)})
        if not isinstance(data, dict):
            return []
        items = data.get("courseVodViewList") or []
        out = []
        for item in items:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            out.append(
                {
                    "cour_id": item.get("courId"),
                    "message": item.get("message") or "",
                    "url": item["url"],
                    "vod_time": item.get("vodTime"),
                }
            )
        return out


def resolve_tecl_id(courses: Iterable[dict[str, Any]], course_id: int) -> int:
    """允许用课程 id 或 tecl_id 指定课程, 统一解析成 tecl_id。"""
    for course in courses:
        if course.get("tecl_id") == course_id or course.get("id") == course_id:
            if course.get("tecl_id") is None:
                break
            return int(course["tecl_id"])
    raise SjtuError(f"找不到课程: {course_id}(可用 list-courses 查看)")
