import logging
import sys
import time
from os import PathLike
from pathlib import Path
from typing import Literal, Optional, Union, get_args

import ffmpeg
import requests

from .orm import (
    ASRData,
    ResourceCompleteRspSchema,
    ResourceCreateRspSchema,
    ResultRspSchema,
    ResultStateEnum,
    TaskCreateRspSchema,
)

__version__ = "0.0.4"

logger = logging.getLogger(__name__)

API_BASE_URL = "https://member.bilibili.com/x/bcut/rubick-interface"

# 申请上传
API_REQ_UPLOAD = API_BASE_URL + "/resource/create"

# 提交上传
API_COMMIT_UPLOAD = API_BASE_URL + "/resource/create/complete"

# 创建任务
API_CREATE_TASK = API_BASE_URL + "/task"

# 查询结果
API_QUERY_RESULT = API_BASE_URL + "/task/result"

# 必剪语音识别模型 id
MODEL_ID = 7

# 接口会被 WAF 拦截: 默认的 python-requests User-Agent 会直接返回 412,
# 必须伪装成浏览器并带上必剪站点的 Referer
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}

SUPPORT_SOUND_FORMAT = Literal["flac", "aac", "m4a", "mp3", "wav"]

# 可直接上传的音频格式
INFILE_FMT = list(get_args(SUPPORT_SOUND_FORMAT))
# 字幕输出格式
OUTFILE_FMT = ["srt", "json", "lrc", "txt"]

# 单次 http 请求超时(秒)
DEFAULT_REQUEST_TIMEOUT = 30.0
# 任务状态轮询间隔(秒); 轮询过于频繁会被必剪 WAF 以 412 拦截
DEFAULT_POLL_INTERVAL = 3.0
# 单个识别任务最长等待时间(秒)
DEFAULT_TASK_TIMEOUT = 3600.0

# 必剪 WAF 的风控状态码
HTTP_RISK_STATUS = 412
RISK_MESSAGE = (
    "请求被必剪风控拦截(HTTP 412): 通常是调用过于频繁或出口 IP 被限流, "
    "请稍后重试或增大轮询间隔(-i)"
)


def ffmpeg_render(media_file: Union[str, PathLike]) -> bytes:
    "提取视频伴音并转码为aac格式"
    ret = (
        ffmpeg.input(str(media_file), v="warning")
        .output("pipe:", ac=1, format="adts")
        .run(capture_stdout=True)
    )
    # ffmpeg-python 0.2 的 run() 返回 (stdout, stderr), 这里兼容仅返回 stdout 的实现
    return ret[0] if isinstance(ret, tuple) else ret


class APIError(Exception):
    "接口调用错误"

    def __init__(self, code, msg) -> None:
        self.code = code
        self.msg = msg
        super().__init__(f"{code}:{msg}")

    def __str__(self) -> str:
        return f"{self.code}:{self.msg}"


def _api_data(resp: requests.Response) -> dict:
    "校验接口响应并取出 data 段"
    if resp.status_code == HTTP_RISK_STATUS:
        # 412 返回的是 html 风控页, 先于 raise_for_status 拦下来给出可操作提示
        raise APIError(-HTTP_RISK_STATUS, RISK_MESSAGE)
    resp.raise_for_status()
    try:
        payload = resp.json()
    except ValueError as err:
        raise APIError(-1, f"接口返回的不是 json: {resp.text[:200]!r}") from err
    if not isinstance(payload, dict):
        raise APIError(-1, f"接口返回格式异常: {payload!r}")
    # code 缺失时按成功处理, 但必须有 data
    code = payload.get("code", 0)
    if code:
        raise APIError(code, payload.get("message") or payload.get("msg") or "")
    if "data" not in payload:
        raise APIError(-1, f"接口返回缺少 data 字段: {str(payload)[:200]}")
    return payload["data"]


class BcutASR:
    "必剪 语音识别接口"

    sound_name: str
    sound_fmt: str
    sound_size: int
    sound_bin: Optional[bytes]
    sound_path: Optional[Path]
    task_id: Optional[str]

    def __init__(
        self,
        file: Optional[Union[str, PathLike]] = None,
        *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.session = session if session is not None else requests.Session()
        # 不设置会被 WAF 以 412 拦截; 自定义 session 可能没有 headers 属性
        headers = getattr(self.session, "headers", None)
        if headers is not None:
            headers.update(DEFAULT_HEADERS)
        self.timeout = timeout
        self.task_id = None
        self.sound_bin = None
        self.sound_path = None
        self.sound_size = 0
        self.__etags: list[str] = []
        if file:
            self.set_data(file)

    def set_data(
        self,
        file: Optional[Union[str, PathLike]] = None,
        raw_data: Optional[bytes] = None,
        data_fmt: Optional[str] = None,
    ) -> None:
        "设置欲识别的数据"
        if file is not None:
            if not isinstance(file, (str, PathLike)):
                raise TypeError("unknow file ptr")
            path = Path(file)
            if not path.is_file():
                raise FileNotFoundError(f"输入文件不存在: {path}")
            suffix = (data_fmt or path.suffix.lstrip(".")).lower()
            self.sound_name = path.name
            self.sound_path = path
            self.sound_bin = None
            self.sound_size = path.stat().st_size
        elif raw_data is not None:
            if not raw_data:
                raise ValueError("输入音频数据为空")
            suffix = (data_fmt or "").lower()
            self.sound_name = f"{int(time.time())}.{suffix}"
            self.sound_path = None
            self.sound_bin = raw_data
            self.sound_size = len(raw_data)
        else:
            raise ValueError("none set data")
        if suffix not in INFILE_FMT:
            raise TypeError(f"format is not support: {suffix!r}, 支持: {INFILE_FMT}")
        self.sound_fmt = suffix
        logger.info("加载文件成功: %s (%d bytes)", self.sound_name, self.sound_size)

    def _read_chunk(self, start: int, end: int) -> bytes:
        "按需读取分片数据, 文件输入时不整段载入内存"
        if self.sound_bin is not None:
            return self.sound_bin[start:end]
        if self.sound_path is None:
            raise ValueError("none set data")
        with open(self.sound_path, "rb") as f:
            f.seek(start)
            return f.read(end - start)

    def upload(self) -> None:
        "申请上传"
        if not self.sound_size or not self.sound_fmt:
            raise ValueError("none set data")
        self.__etags = []
        resp = self.session.post(
            API_REQ_UPLOAD,
            data={
                "type": 2,
                "name": self.sound_name,
                "size": self.sound_size,
                "resource_file_type": self.sound_fmt,
                "model_id": MODEL_ID,
            },
            timeout=self.timeout,
        )
        resp_data = ResourceCreateRspSchema.model_validate(_api_data(resp))
        self.__in_boss_key = resp_data.in_boss_key
        self.__resource_id = resp_data.resource_id
        self.__upload_id = resp_data.upload_id
        self.__upload_urls = resp_data.upload_urls
        self.__per_size = resp_data.per_size
        self.__clips = len(resp_data.upload_urls)
        if not self.__clips or self.__per_size <= 0:
            raise APIError(
                -1, f"上传参数异常: {self.__clips} 分片, 分片大小 {self.__per_size}"
            )
        logger.info(
            "申请上传成功, 总计大小%dKB, %d分片, 分片大小%dKB: %s",
            resp_data.size // 1024,
            self.__clips,
            resp_data.per_size // 1024,
            self.__in_boss_key,
        )
        self.__upload_part()
        self.__commit_upload()

    def __upload_part(self) -> None:
        "上传音频数据"
        for clip in range(self.__clips):
            start_range = clip * self.__per_size
            end_range = min((clip + 1) * self.__per_size, self.sound_size)
            logger.info("开始上传分片%d: %d-%d", clip, start_range, end_range)
            resp = self.session.put(
                self.__upload_urls[clip],
                data=self._read_chunk(start_range, end_range),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            etag = resp.headers.get("Etag")
            if not etag:
                raise APIError(-1, f"分片{clip}上传成功但响应缺少 Etag, 无法提交")
            self.__etags.append(etag)
            logger.info("分片%d上传成功: %s", clip, etag)

    def __commit_upload(self) -> None:
        "提交上传数据"
        resp = self.session.post(
            API_COMMIT_UPLOAD,
            data={
                "in_boss_key": self.__in_boss_key,
                "resource_id": self.__resource_id,
                "etags": ",".join(self.__etags),
                "upload_id": self.__upload_id,
                "model_id": MODEL_ID,
            },
            timeout=self.timeout,
        )
        resp_data = ResourceCompleteRspSchema.model_validate(_api_data(resp))
        self.__download_url = resp_data.download_url
        logger.info("提交成功")

    def create_task(self) -> str:
        "开始创建转换任务"
        resp = self.session.post(
            API_CREATE_TASK,
            json={"resource": self.__download_url, "model_id": str(MODEL_ID)},
            timeout=self.timeout,
        )
        resp_data = TaskCreateRspSchema.model_validate(_api_data(resp))
        self.task_id = resp_data.task_id
        logger.info("任务已创建: %s", self.task_id)
        return self.task_id

    def result(self, task_id: Optional[str] = None) -> ResultRspSchema:
        "查询转换结果"
        task_id = task_id or self.task_id
        if not task_id:
            raise ValueError("任务未创建, 无法查询结果")
        resp = self.session.get(
            API_QUERY_RESULT,
            params={"model_id": MODEL_ID, "task_id": task_id},
            timeout=self.timeout,
        )
        return ResultRspSchema.model_validate(_api_data(resp))

    def wait(
        self,
        interval: float = DEFAULT_POLL_INTERVAL,
        timeout: float = DEFAULT_TASK_TIMEOUT,
    ) -> ASRData:
        "轮询任务状态直到完成, 返回识别结果; 失败或超时抛异常"
        deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
        while True:
            task_resp = self.result()
            state = task_resp.state
            if state == ResultStateEnum.COMPLETE:
                logger.info("识别成功")
                return task_resp.parse()
            if state == ResultStateEnum.ERROR:
                raise APIError(-1, f"识别失败: {task_resp.remark}")
            if state == ResultStateEnum.STOP:
                logger.info("等待识别开始")
            elif state == ResultStateEnum.RUNING:
                logger.info("识别中 %s", task_resp.remark)
            else:
                logger.info(
                    "未知任务状态 %s: %s", task_resp.state_name, task_resp.remark
                )
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"识别任务超时({timeout}秒, task_id={self.task_id})")
            time.sleep(interval)


def _load_input(infile) -> tuple[Union[bytes, Path], str, str]:
    "读取/转码输入媒体, 返回 (数据源, 音频格式, 文件名)"
    stream = None
    if isinstance(infile, (str, PathLike)):
        name = str(infile)
    else:
        stream = infile
        name = getattr(infile, "name", None)
        if not isinstance(name, str):
            raise ValueError("无法获取输入文件名")
    if name in ("-", "<stdin>"):
        raise ValueError("输入文件错误: 不支持从 stdin 读取")
    path = Path(name)
    suffix = path.suffix.lstrip(".").lower()
    if suffix in INFILE_FMT:
        if stream is None:
            if not path.is_file():
                raise FileNotFoundError(f"输入文件不存在: {path}")
            # 文件输入惰性读取, 避免大文件整段载入内存
            return path, suffix, str(path)
        data = stream.read()
        if not data:
            raise ValueError(f"输入文件为空: {name}")
        return data, suffix, name
    # 非标准音频/视频: 调用 ffmpeg 分离伴音
    logger.info("非标准音频文件, 尝试调用ffmpeg转码")
    if not path.is_file():
        raise ValueError(f"ffmpeg 只能处理本地文件, 无法处理: {name}")
    try:
        data = ffmpeg_render(path)
    except ffmpeg.Error as err:
        stderr = getattr(err, "stderr", None) or b""
        detail = stderr.decode("utf8", "ignore").strip()[-500:]
        logger.error("ffmpeg转码失败: %s", detail or err)
        raise
    except FileNotFoundError as err:
        raise FileNotFoundError("未找到 ffmpeg, 请先安装并加入 PATH") from err
    logger.info("ffmpeg转码完成")
    if not data:
        raise ValueError("ffmpeg 未输出音频数据")
    return data, "aac", str(path)


def _resolve_outfile_fmt(format_, output) -> str:
    "确定输出格式: 显式 -f/format 优先, 其次输出文件名后缀, 最后 srt"
    fmt = format_.lower() if isinstance(format_, str) and format_ else None
    if fmt is not None and fmt not in OUTFILE_FMT:
        raise ValueError(f"不支持的输出格式: {format_!r}, 支持: {OUTFILE_FMT}")
    suffix = ""
    if isinstance(output, (str, PathLike)):
        suffix = Path(str(output)).suffix.lstrip(".").lower()
    elif output is not None:
        name = getattr(output, "name", None)
        if isinstance(name, str) and name not in ("-", "<stdout>"):
            suffix = Path(name).suffix.lstrip(".").lower()
    if fmt is None:
        if suffix in OUTFILE_FMT:
            fmt = suffix
        elif suffix:
            logger.warning("无法从输出文件名推断格式(%s), 回退为 srt", suffix)
            fmt = "srt"
        else:
            fmt = "srt"
    elif suffix in OUTFILE_FMT and suffix != fmt:
        logger.warning(
            "输出文件名后缀(%s)与指定格式(%s)不一致, 以指定格式为准", suffix, fmt
        )
    return fmt


def _resolve_output(output, outfile_fmt: str, infile_name: str):
    "返回 (输出流, 是否需要关闭, 目标描述)"
    if output is None:
        target = Path(infile_name).with_suffix(f".{outfile_fmt}")
        return open(target, "w", encoding="utf8"), True, str(target)
    if isinstance(output, (str, PathLike)):
        target = str(output)
        if target == "-":
            return sys.stdout, False, "<stdout>"
        return open(target, "w", encoding="utf8"), True, target
    name = getattr(output, "name", None)
    if output is sys.stdout or name in ("-", "<stdout>"):
        return sys.stdout, False, "<stdout>"
    return output, False, str(name) if name else "<stream>"


def _serialize(result: ASRData, outfile_fmt: str) -> str:
    "按格式序列化字幕"
    match outfile_fmt:
        case "srt":
            return result.to_srt()
        case "lrc":
            return result.to_lrc()
        case "json":
            return result.model_dump_json(indent=2)
        case "txt":
            return result.to_txt()
    raise ValueError(f"不支持的输出格式: {outfile_fmt!r}")


def run_everywhere(argg) -> int:
    "执行一次完整识别流程(CLI 与 module 共用), 返回进程退出码(0 成功)"
    logging.basicConfig(
        format="%(asctime)s - [%(levelname)s] %(message)s", level=logging.INFO
    )

    interval = getattr(argg, "interval", None) or DEFAULT_POLL_INTERVAL
    task_timeout = getattr(argg, "timeout", None)
    if task_timeout is None:
        task_timeout = DEFAULT_TASK_TIMEOUT

    try:
        source, infile_fmt, infile_name = _load_input(argg.input)
        outfile_fmt = _resolve_outfile_fmt(
            getattr(argg, "format", None), getattr(argg, "output", None)
        )
    except (ValueError, OSError, ffmpeg.Error) as err:
        logger.error("输入参数错误: %s", err)
        return 1

    asr = BcutASR()
    try:
        if isinstance(source, (str, PathLike)):
            asr.set_data(file=source)
        else:
            asr.set_data(raw_data=source, data_fmt=infile_fmt)
        # 上传文件
        asr.upload()
        # 创建任务并轮询结果
        asr.create_task()
        result = asr.wait(interval=interval, timeout=task_timeout)
    except APIError as err:
        logger.error("接口错误: %s", err)
        return 1
    except requests.RequestException as err:
        logger.error("网络错误: %s", err)
        return 1
    except TimeoutError as err:
        logger.error("%s", err)
        return 1
    except KeyboardInterrupt:
        logger.warning("已取消")
        return 130

    if not result.has_data():
        logger.error("未识别到语音")
        return 1

    try:
        stream, need_close, target = _resolve_output(
            getattr(argg, "output", None), outfile_fmt, infile_name
        )
        try:
            stream.write(_serialize(result, outfile_fmt))
            stream.flush()
        finally:
            if need_close:
                stream.close()
    except OSError as err:
        logger.error("写出字幕失败: %s", err)
        return 1
    logger.info("转换成功: %s", target)
    return 0
