from enum import IntEnum
from typing import Optional

from pydantic import BaseModel, Field


class ASRDataSeg(BaseModel):
    """文字识别-断句"""

    class ASRDataWords(BaseModel):
        """文字识别-逐字"""

        label: str
        start_time: int
        end_time: int

    start_time: int
    end_time: int
    transcript: str
    # 部分接口返回不含逐字信息, 缺省为空列表而不是直接校验失败
    words: list[ASRDataWords] = Field(default_factory=list)

    def to_srt_ts(self) -> str:
        """转换为srt时间戳"""

        def _conv(ms: int) -> tuple[int, int, int, int]:
            return ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000

        s_h, s_m, s_s, s_ms = _conv(self.start_time)
        e_h, e_m, e_s, e_ms = _conv(self.end_time)
        return f"{s_h:02d}:{s_m:02d}:{s_s:02d},{s_ms:03d} --> {e_h:02d}:{e_m:02d}:{e_s:02d},{e_ms:03d}"

    def to_lrc_ts(self) -> str:
        """转换为lrc时间戳"""

        def _conv(ms: int) -> tuple[int, int, int]:
            return ms // 60000, ms // 1000 % 60, ms % 1000 // 10

        s_m, s_s, s_ms = _conv(self.start_time)
        return f"[{s_m:02d}:{s_s:02d}.{s_ms:02d}]"


class ASRData(BaseModel):
    """语音识别结果"""

    utterances: list[ASRDataSeg] = Field(default_factory=list)
    version: str = ""

    def __iter__(self):
        return iter(self.utterances)

    def has_data(self) -> bool:
        """是否识别到数据"""
        return len(self.utterances) > 0

    def to_txt(self) -> str:
        """转成 txt 格式字幕 (无时间标记)"""
        return "\n".join(seg.transcript for seg in self.utterances)

    def to_srt(self) -> str:
        """转成 srt 格式字幕"""
        return "\n".join(
            f"{n}\n{seg.to_srt_ts()}\n{seg.transcript}\n"
            for n, seg in enumerate(self.utterances, 1)
        )

    def to_lrc(self) -> str:
        """转成 lrc 格式字幕"""
        return "\n".join(
            f"{seg.to_lrc_ts()}{seg.transcript}" for seg in self.utterances
        )

    def to_ass(self) -> str:
        """转换为 ass 格式"""
        # TODO: ass 序列化实现
        raise NotImplementedError("ass 格式尚未实现")


class ResourceCreateRspSchema(BaseModel):
    """上传申请响应"""

    resource_id: str
    title: str
    type: int
    in_boss_key: str
    size: int
    upload_urls: list[str]
    upload_id: str
    per_size: int


class ResourceCompleteRspSchema(BaseModel):
    """上传提交响应"""

    resource_id: str
    download_url: str


class TaskCreateRspSchema(BaseModel):
    """任务创建响应"""

    resource: str
    result: str
    task_id: str  # 任务id


class ResultStateEnum(IntEnum):
    """任务状态枚举

    继承 IntEnum, 便于与 ResultRspSchema.state(int) 直接比较。
    """

    STOP = 0  # 未开始
    RUNING = 1  # 运行中
    ERROR = 3  # 错误
    COMPLETE = 4  # 完成


class ResultRspSchema(BaseModel):
    """任务结果查询响应"""

    task_id: str  # 任务id
    result: Optional[str] = None  # 结果数据-json 在 state 1 的情况为 None
    remark: str = ""  # 任务状态详情
    # 保持 int: 接口新增状态值时不至于让解析直接失败
    state: int  # 任务状态, 见 ResultStateEnum

    @property
    def state_name(self) -> str:
        """任务状态名, 未知状态返回 UNKNOWN(n)"""
        try:
            return ResultStateEnum(self.state).name
        except ValueError:
            return f"UNKNOWN({self.state})"

    def parse(self) -> ASRData:
        "解析结果数据"
        if not self.result:
            raise ValueError(
                f"任务尚无结果数据(task_id={self.task_id}, state={self.state_name})"
            )
        return ASRData.model_validate_json(self.result)
