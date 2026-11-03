"""公共设施: 配置、日志、ffmpeg 定位、磁盘守卫、原子写。

设计约束(见 README「磁盘纪律」):
  * 大文件一律写 D 盘 data_root, 不碰 C 盘;
  * 不复制 ffmpeg, 原地引用 imageio-ffmpeg 自带的那份;
  * 任何写入前先过磁盘守卫。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.toml"
ENV_PATH = PROJECT_ROOT / ".env"

log = logging.getLogger("tingke")


class ConfigError(RuntimeError):
    """配置/环境问题(退出码 2)。"""


class DiskError(RuntimeError):
    """磁盘空间不足。"""


# ── 配置 ──────────────────────────────────────────────


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """读取 config.toml(标准库 tomllib, 不需要 PyYAML)。"""
    p = Path(path) if path else CONFIG_PATH
    if not p.is_file():
        raise ConfigError(f"找不到配置文件: {p}")
    with open(p, "rb") as f:
        return tomllib.load(f)


def load_env(path: Path | str | None = None) -> None:
    """把 .env 的键值读进 os.environ; 已存在的环境变量优先。"""
    p = Path(path) if path else ENV_PATH
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def api_key() -> str:
    """Canvas 个人访问令牌; 只从环境变量取, 不落日志。"""
    key = (os.environ.get("CANVAS_API_KEY") or "").strip()
    if not key:
        raise ConfigError(
            "缺少 CANVAS_API_KEY: 请复制 .env.example 为 .env 并填入令牌"
        )
    return key


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


# ── 路径 ──────────────────────────────────────────────


def _env_path(name: str) -> Path | None:
    """读一个"路径型"环境变量; 未设置或为空则返回 None。"""
    raw = os.environ.get(name, "").strip()
    return resolve_project_path(raw) if raw else None


# 记录项目真实的父目录(common.py 位于 <项目>/scripts/common.py)。
# 从 skill 目录运行时 PROJECT_ROOT 会指向 skill, 所以不能只靠它判断工作区;
# 但这份代码最初是从某个工作区同步过去的, 这里记下那个位置作为兜底线索。
_PROJECT_PARENT = PROJECT_ROOT.parent

# 同步 skill 时写入的"原始工作区"提示文件(skill 根下的 .workspace_hint)
_WORKSPACE_HINT = PROJECT_ROOT / ".workspace_hint"


def workspace_dir() -> Path | None:
    """会话工作区目录。

    判定顺序:
      1. 环境变量 TINGKE_WORKSPACE(用户/会话显式指定);
      2. 当前工作目录, 若其下有 听课助手/;
      3. 项目根的父目录(项目名就叫 听课助手 时);
      4. skill 目录里的 .workspace_hint(同步时写入的原始工作区)。
    都找不到返回 None, 调用方回落到配置里的 workspace_fallback。
    """
    explicit = _env_path("TINGKE_WORKSPACE")
    if explicit:
        return explicit
    cwd = Path.cwd().resolve()
    if (cwd / "听课助手").is_dir():
        return cwd
    if PROJECT_ROOT.name == "听课助手":
        return _PROJECT_PARENT
    if _WORKSPACE_HINT.is_file():
        raw = _WORKSPACE_HINT.read_text(encoding="utf-8").strip()
        if raw:
            hint = Path(raw).expanduser()
            if hint.is_dir():
                return hint
    return None


def course_transcript_dir(cfg: dict[str, Any], course_name: str) -> Path:
    """某门课的默认落点: <工作区>/<课程名>/课程转写/。

    用户没有明确指定保存位置时, 转写、帧、合成、笔记都放这里 ——
    产物跟着课程走, 换机器/换会话都能在原处找到, 也不往系统盘乱塞。
    """
    ws = workspace_dir()
    if ws is None:
        ws = resolve_project_path((cfg.get("paths") or {}).get("workspace_fallback", ".."))
    return ws / safe_name(course_name) / "课程转写"


def data_root(cfg: dict[str, Any], course_name: str | None = None) -> Path:
    """切片数据(音频/帧/合成)的根目录。

    优先级: TINGKE_DATA > 配置 paths.data_root > <工作区>/<课程>/课程转写。
    未显式配置且给了课程名时, 数据按课程就近存放。
    """
    override = _env_path("TINGKE_DATA")
    if override:
        override.mkdir(parents=True, exist_ok=True)
        return override
    configured = str((cfg.get("paths") or {}).get("data_root", "") or "").strip()
    if configured:
        root = resolve_project_path(configured)
        root.mkdir(parents=True, exist_ok=True)
        return root
    if course_name:
        root = course_transcript_dir(cfg, course_name)
        root.mkdir(parents=True, exist_ok=True)
        return root
    raise ConfigError(
        "无法确定数据目录: 请在 config.toml 设置 paths.data_root, 或设环境变量 TINGKE_DATA"
    )


def data_dir(cfg: dict[str, Any], *parts: str, course_name: str | None = None) -> Path:
    """数据子目录。

    注意: 传 course_name 时, 默认落点(<工作区>/<课程>/课程转写)已经包含了课程层级,
    所以调用方拼进来的课程名要**去掉**, 否则会变成 课程转写/字幕/某门课程/<节次>。
    显式配置了 data_root 时, 那个根是"全局根", 课程层级必须保留。
    """
    root = data_root(cfg, course_name)
    parts_list = list(parts)
    if course_name and root.name == "课程转写":
        course_seg = safe_name(course_name)
        parts_list = [p for p in parts_list if p != course_seg]
    d = root.joinpath(*parts_list)
    d.mkdir(parents=True, exist_ok=True)
    return d


def notes_dir(cfg: dict[str, Any], course_name: str | None = None) -> Path:
    """笔记(交付物)的目录。

    优先级: TINGKE_NOTES > 配置 paths.notes > <工作区>/<课程>/课程转写。
    走 resolve_project_path 而不是直接拼 PROJECT_ROOT, 这样也能配绝对路径 ——
    skill 目录里跑的那份代码就用绝对路径指回同一处, 避免同一门课出现两份笔记。
    """
    override = _env_path("TINGKE_NOTES")
    if override:
        override.mkdir(parents=True, exist_ok=True)
        return override
    configured = str((cfg.get("paths") or {}).get("notes", "") or "").strip()
    if configured:
        d = resolve_project_path(configured)
    elif course_name:
        d = course_transcript_dir(cfg, course_name)
    else:
        raise ConfigError(
            "无法确定笔记目录: 请在 config.toml 设置 paths.notes, 或设环境变量 TINGKE_NOTES"
        )
    d.mkdir(parents=True, exist_ok=True)
    return d


def notes_mode(cfg: dict[str, Any]) -> str:
    """笔记落点模式。

    * course(默认): 笔记放在 <notes>/<课程名>/ 下, 一份配置可管多门课;
    * flat:         笔记直接平铺在 <notes>/ 下, 用于"工作区/课程/"这种单课目录约定。
    """
    mode = str((cfg.get("paths") or {}).get("notes_mode", "course")).strip().lower()
    return mode if mode in ("course", "flat") else "course"


def notes_base(cfg: dict[str, Any], course_name: str) -> Path:
    """某门课笔记所在目录(已按 notes_mode 决定是否再套一层课程名)。

    统一入口: 所有"要放/要找某门课笔记"的地方都走它,
    避免出现 status 认为在 A、notes 却写到 B 的不一致。
    """
    root = notes_dir(cfg, course_name)
    if notes_mode(cfg) == "flat":
        return root
    return root / safe_name(course_name)


def note_file(
    cfg: dict[str, Any], course_name: str, filename: str, create_dir: bool = False
) -> Path:
    """某门课某个笔记文件的完整路径。"""
    base = notes_base(cfg, course_name)
    if create_dir:
        base.mkdir(parents=True, exist_ok=True)
    return base / filename


def resolve_project_path(value: str | Path) -> Path:
    """把配置里的路径解析成绝对路径(相对路径按项目根目录算)。"""
    p = Path(os.path.expandvars(str(value))).expanduser()
    if not p.is_absolute():
        p = (PROJECT_ROOT / p).resolve()
    return p


def safe_name(text: str, max_len: int = 60) -> str:
    """把课程名/讲次标题变成安全的目录名。"""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(text))
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")
    return (cleaned[:max_len].rstrip("._ ") or "untitled")


# ── ffmpeg ────────────────────────────────────────────


def find_ffmpeg(cfg: dict[str, Any]) -> str:
    """定位 ffmpeg: 配置 -> PATH -> imageio-ffmpeg 自带。绝不复制它。"""
    configured = str((cfg.get("ffmpeg") or {}).get("path") or "")
    if configured and Path(configured).is_file():
        return configured

    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path

    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return exe
    except Exception as exc:  # noqa: BLE001
        log.debug("imageio-ffmpeg 不可用: %s", exc)

    raise ConfigError(
        "找不到 ffmpeg: 请装到 PATH, 或在 config.toml 设置 ffmpeg.path"
    )


# ── 磁盘守卫 ──────────────────────────────────────────


def free_gb(path: Path | str) -> float:
    return shutil.disk_usage(str(path)).free / (1024**3)


def require_disk(path: Path | str, need_mb: float, min_free_gb: float) -> float:
    """确认剩余空间够用, 返回空闲 GB。不够直接抛 DiskError。"""
    free = free_gb(path)
    need = need_mb / 1024.0 + float(min_free_gb)
    if free < need:
        raise DiskError(
            f"磁盘不足: {path} 仅剩 {free:.1f} GB, 本次需要约 {need_mb:.0f} MB "
            f"+ 保留 {min_free_gb:.0f} GB"
        )
    return free


# ── 原子写 ────────────────────────────────────────────


def atomic_write_text(path: Path | str, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path | str, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path | str, default: Any = None) -> Any:
    p = Path(path)
    if not p.is_file():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return default


# ── 日志辅助 ──────────────────────────────────────────


def redact_url(url: str) -> str:
    """日志里只保留主机与路径, 丢掉签名参数(直链是敏感信息)。"""
    if not url:
        return ""
    head, _, _ = url.partition("?")
    return head


def human_mb(num_bytes: float) -> str:
    return f"{num_bytes / 1048576:.1f} MB"
