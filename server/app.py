"""视频工坊 · FastAPI 后端入口。

路由一览：
    GET    /                      前端页面
    GET    /api/platforms         支持的平台清单
    POST   /api/resolve           解析链接，返回视频信息与可选清晰度
    POST   /api/download          创建下载任务
    GET    /api/tasks/{id}        查询任务状态（轮询兜底）
    GET    /api/tasks/{id}/events SSE 实时进度
    GET    /api/tasks/{id}/file   下载已完成的文件
    DELETE /api/tasks/{id}        取消任务并清理临时文件
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import json
import socket
import logging
import os
import re
import sys
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
import atomic_io
import requests  # 解说 worker HTTP 模式客户端（VDL_COMMENTARY_MODE=http 时用到）
import time
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import platform_model as plat

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.concurrency import run_in_threadpool
from fastapi import File as _FastAPIFile, Form, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from urllib.parse import quote, urljoin, urlparse

# 在 import downloader（间接 import yt_dlp）之前，先把本机可能已下载的新版 yt-dlp
# 插入 sys.path 最前，实现「解析器自动更新」无需重新打包。
import ydlp_update
ydlp_update.bootstrap()

import downloader
import library as library_mod
import subtitles as subtitles_mod
import subscriptions as subs_mod
import ffmpeg_tools as fftools
import retention as retention_mod
import crypto_vault as crypto_mod
import process_queue as pq_mod
import torrent as torrent_mod
from batch import BatchScheduler
from platforms import CHINA_DOMAINS, LinkError, UnsupportedPlatformError, is_china_host, parse_source, platform_catalog
from tasks import TaskStore, TASK_ID_LENGTH
from llm_config import inject_llm_env, get_llm_config, save_llm_config, PROVIDER_PRESETS, DEFAULT_PROVIDER

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vdl")

if getattr(sys, "frozen", False):
    # PyInstaller 打包后资源位置：
    #   - 单文件模式：解压到 sys._MEIPASS
    #   - macOS .app（单文件夹）：数据文件在 Contents/Resources
    #   - 其它单文件夹模式：与可执行文件同目录
    _exe = sys.executable
    _macos_dir = os.path.dirname(_exe)
    _resources = os.path.normpath(os.path.join(_macos_dir, "..", "Resources"))
    if getattr(sys, "_MEIPASS", None):
        _frozen_base = Path(sys._MEIPASS)
    elif os.path.exists(os.path.join(_resources, "web")):
        _frozen_base = Path(_resources)
    else:
        _frozen_base = Path(_macos_dir)
    BASE_DIR = _frozen_base
    WEB_DIR = _frozen_base / "web"
    # 下载产物放在用户目录，避免写入只读的 .app 包内
    DOWNLOAD_DIR = Path.home() / "Downloads" / "VideoDownloader"
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
    WEB_DIR = BASE_DIR / "web"
    DOWNLOAD_DIR = BASE_DIR / "downloads"

# --------------------------------------------------------------------------- #
# 结构化事件日志（运维可观测性）
# 未捕获异常 / 前端上报的 JS 错误 / 关键错误统一落盘为 JSON 行，轮转保留；
# 运维接口 /api/admin/events 读取，桌面端「导出诊断信息」聚合打包。
# 任何写入失败都不影响主流程（record_event 内部已吞异常）。
# --------------------------------------------------------------------------- #
import logging.handlers as _logging_handlers
EVENT_LOG_PATH = DOWNLOAD_DIR / ".events.log"
_event_logger = logging.getLogger("vdl.events")
if not _event_logger.handlers:
    try:
        _event_handler = _logging_handlers.RotatingFileHandler(
            EVENT_LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        _event_handler.setFormatter(logging.Formatter("%(message)s"))
        _event_logger.addHandler(_event_handler)
        _event_logger.setLevel(logging.INFO)
        _event_logger.propagate = False
    except Exception:
        pass

def record_event(level, category, message, *, request=None, extra=None):
    """线程安全写一条结构化事件（JSON 行），供运维查询与诊断导出。"""
    try:
        rec = {
            "ts": int(time.time()),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "level": str(level),
            "category": str(category),
            "message": str(message)[:2000],
        }
        if request is not None:
            rec["method"] = getattr(request, "method", "")
            try:
                rec["path"] = request.url.path
            except Exception:
                rec["path"] = ""
            rec["ua"] = (request.headers.get("User-Agent") or "")[:200]
            try:
                rec["ip"] = request.client.host if request.client else ""
            except Exception:
                rec["ip"] = ""
        if extra:
            rec["extra"] = extra
        _event_logger.info(json.dumps(rec, ensure_ascii=False))
    except Exception:
        pass

# 字幕提取产物目录（faster-whisper ASR，随下载目录走）
SUBTITLE_DIR = DOWNLOAD_DIR / "subtitles"
SUBTITLE_DIR.mkdir(parents=True, exist_ok=True)

MAX_CONCURRENT_DOWNLOADS = 3
MAX_CONCURRENT_PROBES = 8
# 批量下载相关环境变量（桌面版万能下载器重点能力）：
#   VDL_BATCH_HARD_MAX   线程池硬上限（实际并发不超过它，默认 8）
#   VDL_BATCH_RETRIES    失败自动重试次数（默认 2，指数退避最多 30s）
#   VDL_BATCH_MAX_ITEMS  单次批量最多链接数（默认 50，防误粘贴巨量链接打爆）
VDL_BATCH_HARD_MAX = int(os.environ.get("VDL_BATCH_HARD_MAX", "8") or 8)
BATCH_RETRIES_DEFAULT = int(os.environ.get("VDL_BATCH_RETRIES", "2") or 2)
VDL_BATCH_MAX_ITEMS = int(os.environ.get("VDL_BATCH_MAX_ITEMS", "50") or 50)
SINGLE_DOWNLOAD_RETRIES = 0  # 单条下载默认不自动重试（失败让用户手动点重试）；批量才默认重试

# ---- 格式转换（增值能力）：对已下载文件做 ffmpeg 转码，异步 job ----
CONVERT_DIR = DOWNLOAD_DIR / "conversions"
CONVERT_DIR.mkdir(parents=True, exist_ok=True)
CONVERT_JOBS: dict[str, dict] = {}
CONVERT_LOCK = threading.Lock()
FFMPEG_BIN = os.environ.get("VDL_FFMPEG_BIN") or shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if sys.platform == "darwin" else "")
FFPROBE_BIN = os.environ.get("VDL_FFPROBE_BIN") or shutil.which("ffprobe") or ("/opt/homebrew/bin/ffprobe" if sys.platform == "darwin" else "")
# 允许的目标格式 -> ffmpeg 参数；resolution 可选 original/1080/720/480
# （2026-08-23 扩充：+avi/flv/ts/m4v/wmv/mpeg/3gp/ogv 视频 + aac/wav/flac/ogg/opus 音频）
# （2026-08-24 再扩充：+hevc(H.265/MP4) 视频 + wma/mp2 音频，共 23 种）
CONVERT_TARGETS = {
    "mp4":  ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-movflags", "+faststart"],
    "mov":  ["-c:v", "libx264", "-c:a", "aac"],
    "mkv":  ["-c:v", "libx264", "-c:a", "aac"],
    "webm": ["-c:v", "libvpx-vp9", "-c:a", "libopus", "-b:v", "1M"],
    "avi":  ["-c:v", "libx264", "-c:a", "aac"],
    "flv":  ["-c:v", "libx264", "-c:a", "aac", "-flvflags", "add_keyframe_index"],
    "ts":   ["-c:v", "libx264", "-c:a", "aac", "-preset", "veryfast"],
    "m4v":  ["-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart"],
    "wmv":  ["-c:v", "wmv2", "-b:v", "2M", "-c:a", "wmav2", "-b:a", "192k"],
    "mpeg": ["-c:v", "mpeg2video", "-q:v", "3", "-c:a", "mp2", "-b:a", "192k"],
    "3gp":  ["-c:v", "libx264", "-c:a", "aac", "-vf", "scale='min(320,iw)':-2"],
    "ogv":  ["-c:v", "libvpx", "-b:v", "1M", "-c:a", "libopus"],
    "hevc": ["-c:v", "libx265", "-preset", "veryfast", "-c:a", "aac", "-movflags", "+faststart", "-tag:v", "hvc1"],
    "mp3":  ["-vn", "-c:a", "libmp3lame", "-q:a", "4"],
    "m4a":  ["-vn", "-c:a", "aac"],
    "aac":  ["-vn", "-c:a", "aac", "-b:a", "192k"],
    "wav":  ["-vn", "-c:a", "pcm_s16le"],
    "flac": ["-vn", "-c:a", "flac"],
    "ogg":  ["-vn", "-c:a", "libopus", "-b:a", "128k"],
    "opus": ["-vn", "-c:a", "libopus", "-b:a", "128k"],
    "wma":  ["-vn", "-c:a", "wmav2", "-b:a", "192k"],
    "mp2":  ["-vn", "-c:a", "mp2", "-b:a", "192k"],
    "gif":  ["-t", "5", "-vf", "fps=10,scale=480:-1:flags=lanczos"],
    # ---- 图片（单帧；jpg/webp 质量档由 _image_encode_args 覆盖；webp 图片走 Pillow）----
    "png":   [],
    "jpg":   ["-q:v", "5"],
    "jpeg":  ["-q:v", "5"],
    "webp":  ["-q:v", "80"],
    "bmp":   [],
    "tiff":  [],
}
CONVERT_EXT = {"mp4": "mp4", "mov": "mov", "mkv": "mkv", "webm": "webm", "avi": "avi", "flv": "flv",
               "ts": "ts", "m4v": "m4v", "wmv": "wmv", "mpeg": "mpg", "3gp": "3gp", "ogv": "ogv",
               "hevc": "mp4", "mp3": "mp3", "m4a": "m4a", "aac": "aac", "wav": "wav", "flac": "flac",
               "ogg": "ogg", "opus": "opus", "wma": "wma", "mp2": "mp2", "gif": "gif",
               "png": "png", "jpg": "jpg", "jpeg": "jpg",
               "webp": "webp", "bmp": "bmp", "tiff": "tiff"}
# 纯音频目标（跳过视频滤镜/缩放/旋转分支）
CONVERT_AUDIO = {"mp3", "m4a", "wav", "flac", "aac", "opus", "wma", "mp2"}
# 纯图片目标（图片转换 tab；单帧输出，无音轨）
CONVERT_IMAGE = {"png", "jpg", "jpeg", "webp", "bmp", "tiff"}
# 不支持 alpha 透明的图片目标（转码时需白底合成，避免透明区变黑）
IMAGE_NO_ALPHA = {"jpg", "jpeg", "bmp"}

# 音频目标支持的音质档位（音乐转换用；无损格式忽略码率）
_AUDIO_BITRATE_PRESETS = {"320k", "256k", "192k", "128k", "96k"}

# 音频编码器的固定前缀（无损格式不在此列，直接用 CONVERT_TARGETS）
_AUDIO_ENC_PREFIX = {
    "mp3": ["-vn", "-c:a", "libmp3lame"],
    "m4a": ["-vn", "-c:a", "aac"],
    "aac": ["-vn", "-c:a", "aac"],
    "opus": ["-vn", "-c:a", "libopus"],
    "wma": ["-vn", "-c:a", "wmav2"],
    "mp2": ["-vn", "-c:a", "mp2"],
}


def _audio_encode_args(target: str, audio_bitrate: str) -> list:
    """构造音频目标的编码参数（支持音质档位，音乐转换用）。

    返回空列表表示无有效档位，调用方应回退到 CONVERT_TARGETS[target]。
    无损格式（wav/flac）忽略码率，直接返回固定表。
    """
    if target in ("wav", "flac"):
        return CONVERT_TARGETS[target]
    br = (audio_bitrate or "").strip().lower()
    if not br:
        return []
    enc = _AUDIO_ENC_PREFIX.get(target)
    if not enc:
        return []
    # 码率规范化：支持 "320k" / "320" / "2M"
    try:
        if br.endswith("m"):
            kbps = int(float(br[:-1]) * 1000)
        elif br.endswith("k"):
            kbps = int(br[:-1])
        else:
            kbps = int(br)
    except (ValueError, TypeError):
        return []
    kbps = max(32, min(512, kbps))
    return enc + ["-b:a", f"{kbps}k"]


def _image_encode_args(target: str, quality: int = 0, resize: int = 0,
                       flatten_alpha: bool = True) -> list:
    """构造图片目标的 ffmpeg 参数（图片转换 tab 用）。

    - quality：0-100 质量档，仅 jpg(mjpeg -q:v 2~31 反向映射) 生效；png/bmp/tiff 无损忽略。
    - resize：长边像素（>0 时较长边缩到该值、短边等比），0=保持原尺寸。
    - flatten_alpha：jpg/bmp 不支持透明，RGBA 白底合成，避免透明区默认变黑。
    返回插入到输出路径之前的参数列表（含 -vf / -filter_complex）。
    """
    target = "jpg" if target == "jpeg" else target
    try:
        quality = int(quality or 0)
    except (TypeError, ValueError):
        quality = 0
    try:
        resize = int(resize or 0)
    except (TypeError, ValueError):
        resize = 0
    args: list = []
    scale_expr = ""
    if resize > 0:
        # 长边缩放（只缩不放）：横图缩宽、竖图缩高，min() 保证原图更小时保持原尺寸
        scale_expr = (f"scale='if(gt(iw,ih),min({resize},iw),-1)':'if(gt(ih,iw),min({resize},ih),-1)'")
    if target in IMAGE_NO_ALPHA and flatten_alpha:
        # 白底合成（filter_complex 带 label，不能用 -vf 链）：scale 后 split 出
        # 背景 drawbox 填白，再把带 alpha 的前景 overlay 回去
        graph = ("[0:v]" + (scale_expr + "," if scale_expr else "")
                 + "format=rgba,split=2[fg][bg];"
                 "[bg]drawbox=t=fill:c=white[bgw];"
                 "[bgw][fg]overlay=format=auto[out]")
        args += ["-filter_complex", graph, "-map", "[out]"]
    elif scale_expr:
        args += ["-vf", scale_expr]
    if target == "jpg":
        if quality > 0:
            # UI 质量 100→最好，mjpeg -q:v 1(最好)~31(最差)：线性反向映射，钳到 [2,31]
            qscale = max(2, min(31, round(31 - (quality / 100.0) * 30)))
            args += ["-q:v", str(qscale)]
    # 图片目标必须只产出一帧：若源是多帧视频，缺此参数 ffmpeg 会把多帧写进同一文件而整体报错
    args += ["-frames:v", "1"]
    return args


def _convert_image_webp(src: str, out: "Path", quality: int = 0, resize: int = 0) -> None:
    """WebP 转码（图片转换 tab）：部署环境的 ffmpeg 多无 libwebp 编码器，
    用 Pillow（MIT-CMU 许可，可商用）直接编码。保留 alpha 透明。"""
    from PIL import Image
    img = Image.open(src)
    try:
        resize = int(resize or 0)
    except (TypeError, ValueError):
        resize = 0
    if resize > 0 and max(img.size) > resize:
        scale = resize / float(max(img.size))   # 只缩不放
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                         Image.LANCZOS)
    try:
        quality = int(quality or 0)
    except (TypeError, ValueError):
        quality = 0
    img.save(out, "WEBP", quality=max(1, min(100, quality or 80)), method=4)
# 各目标格式依赖的 ffmpeg 编码器（缺 libx265 等库的 ffmpeg 需自动隐藏对应格式）
_TARGET_ENCODER_DEPS = {
    "mp4": ["libx264"], "mov": ["libx264"], "mkv": ["libx264"], "avi": ["libx264"],
    "flv": ["libx264"], "ts": ["libx264"], "m4v": ["libx264"], "3gp": ["libx264"],
    "webm": ["libvpx-vp9", "libopus"], "ogv": ["libvpx", "libopus"],
    "wmv": ["wmav2"], "mpeg": ["mpeg2video"], "hevc": ["libx265"],
    "mp3": ["libmp3lame"], "ogg": ["libopus"], "opus": ["libopus"],
    "m4a": [], "aac": [], "wav": [], "flac": [], "wma": [], "mp2": [], "gif": [],
    "png": [], "jpg": [], "jpeg": [], "webp": [], "bmp": [], "tiff": [],
}


def _probe_ffmpeg_encoders() -> set:
    """探测当前 ffmpeg 支持的编码器集合（一次，模块加载时）。
    探测失败（无 ffmpeg / 异常）返回空集合 = 不做任何限制。
    """
    if not FFMPEG_BIN:
        return set()
    try:
        r = subprocess.run([FFMPEG_BIN, "-hide_banner", "-encoders"],
                           capture_output=True, text=True, timeout=15)
    except Exception:
        return set()
    encs = set()
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0][:1] in ("V", "A", "S") and parts[1] != "=":
            encs.add(parts[1])
    return encs


# 可用目标格式（按依赖编码器过滤；无 ffmpeg 环境视为全部可用）
_FFMPEG_ENCODERS = _probe_ffmpeg_encoders()
CONVERT_TARGETS_AVAILABLE: list = (
    list(CONVERT_TARGETS.keys()) if not _FFMPEG_ENCODERS
    else [k for k, deps in _TARGET_ENCODER_DEPS.items() if not deps or all(d in _FFMPEG_ENCODERS for d in deps)]
)

# ---- 本地视频上传转码（需求文档模块一）：接收上传文件直接转码，复用上面的 ffmpeg 管线 ----
UPLOAD_TMP = DOWNLOAD_DIR / "uploads"
UPLOAD_TMP.mkdir(parents=True, exist_ok=True)
# 上传文件大小上限（字节），默认 1GB（CF 免费方案对大文件并发上传限流严重，见 2026-08-28 诊断），可用 VDL_UPLOAD_MAX_BYTES 覆盖
UPLOAD_MAX_BYTES = int(os.environ.get("VDL_UPLOAD_MAX_BYTES") or 1 * 1024 * 1024 * 1024)
# 单次分片上传块大小上限（前端固定 32MB，留余量防超限）
UPLOAD_CHUNK_MAX = 64 * 1024 * 1024
# 允许上传的文件后缀白名单（2026-08-23：视频 + 音频都可上传转换）
UPLOAD_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".m4v", ".ts", ".wmv",
                     ".mpeg", ".mpg", ".3gp", ".ogv", ".hevc", ".h265",
                     ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma"}
# 允许上传的音频后缀白名单（音乐转换 tab 网页上传路径用）
UPLOAD_AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".oga", ".wma"}
# 允许上传的图片后缀白名单（图片转换 tab；.gif 同时可作图片输入）
UPLOAD_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}

# ---- PDF / 图片去水印（需求文档模块二）：接收上传图片/PDF 做去水印，依赖 cv2/fitz（缺则降级） ----
DW_DIR = DOWNLOAD_DIR / "dewatermark"
DW_DIR.mkdir(parents=True, exist_ok=True)
DW_JOBS: dict[str, dict] = {}
DW_LOCK = threading.Lock()

# ---- 双节点分流：国内节点直连国内站，海外节点直连海外站，前端按链接域名自动选 ---- #
# VDL_REGION: 本节点所在区域，"cn"=国内 / "global"=海外（默认海外）
# VDL_PEER_ENDPOINT: 对端节点的完整地址，如 https://cn.example.com（留空=单节点模式）
# VDL_ALLOW_ORIGINS: 允许跨域访问本节点 API 的来源，逗号分隔；"*" 表示全部
NODE_REGION = (os.environ.get("VDL_REGION", "global").strip().lower() or "global")
PEER_ENDPOINT = os.environ.get("VDL_PEER_ENDPOINT", "").strip().rstrip("/")
_allow_raw = os.environ.get("VDL_ALLOW_ORIGINS", "").strip()
ALLOW_ORIGINS = [o.strip().rstrip("/") for o in _allow_raw.split(",") if o.strip()] or ([PEER_ENDPOINT] if PEER_ENDPOINT else [])
RESOLVE_TIMEOUT_SECONDS = 40          # 海外站（走代理），留出代理延迟余量
RESOLVE_TIMEOUT_DOMESTIC = int(os.environ.get("VDL_RESOLVE_TIMEOUT_DOMESTIC", "60"))  # 国内站经反向隧道后需要更长时间
SSE_INTERVAL_SECONDS = 0.5
SSE_MAX_SECONDS = 60 * 30
CLEANUP_INTERVAL_SECONDS = 600

# ---- 自动解说（增值功能）：松耦合桥接用户现成的 commentary-pipeline ----
# 复用 commentary-pipeline/process.py 整条管线（转写→配音→出片），本服务只负责
# 把下载好的视频喂进它的 input/、等成片回传，绝不重写解说逻辑。
# 默认关闭：需显式开启 + 配置管线目录，且解说 worker 应在独立机器/进程跑，别和下载抢 CPU。
#   VDL_COMMENTARY_ENABLED=true                      启用
#   VDL_COMMENTARY_DIR=/path/to/commentary-pipeline  管线项目根目录（需有 process.py + input/ + output/）
#   VDL_COMMENTARY_PYTHON=/path/to/python            跑 process.py 的解释器（需装 faster_whisper 等依赖）
#   VDL_COMMENTARY_VOICE=zh-CN-XiaoxiaoNeural        默认配音嗓音
_COMMENTARY_EXPLICIT = "VDL_COMMENTARY_ENABLED" in os.environ
COMMENTARY_ENABLED = os.environ.get("VDL_COMMENTARY_ENABLED", "false").strip().lower() == "true"
# 桌面版自动探测：没显式设 COMMENTARY_ENABLED 时，若管线可用则自动开启。
# 仅 frozen(打包版)生效——dev/在线版须显式开启，避免 import 即改写默认行为。
# 统一走 commentary_locate.locate_commentary()（含包内捆绑候选，践行自包含铁律）。
if not _COMMENTARY_EXPLICIT and plat.is_desktop():
    from commentary_locate import locate_commentary
    _loc = locate_commentary()
    if _loc is not None:
        COMMENTARY_ENABLED = True
        if "VDL_COMMENTARY_DIR" not in os.environ:
            os.environ["VDL_COMMENTARY_DIR"] = str(_loc.root)
        if "VDL_COMMENTARY_MODE" not in os.environ:
            # 包内捆绑走 worker 重入(#198 实现)；外部/显式走 local 子进程
            os.environ["VDL_COMMENTARY_MODE"] = "bundled" if _loc.bundled else "local"


# 解说运行环境探测已集中到 _CommentaryRuntime（见下方 _commentary_work_dir 之前的定义），
# 模块加载时一次性解析解释器与工具链，并暴露清晰的诊断信息，避免打包后路径截断导致静默失败。


# 广告位开关：默认关闭。下载站属广告平台高风险类目，默认不挂广告，
# 待流量稳定、确定接入合规广告源后再开。前端据此决定是否渲染广告位容器。
ADS_ENABLED = os.environ.get("VDL_ADS_ENABLED", "false").strip().lower() == "true"
# ---- 格式转换订阅开关（增值能力变现，默认关闭以保持开源全免费体验）----
# 部署者设 VDL_CONVERT_REQUIRE_SUB=true 并填 VDL_CONVERT_SUB_KEY 后进入「订阅墙」模式：
#   免费用户每日限 VDL_CONVERT_FREE_DAILY 次（按客户端 IP 计），超出需订阅；
#   请求头携带正确 X-Subscription-Key 的用户不限次。
# 两个开关任一为空/未设 → 视为未启用订阅墙，所有人免费无限（开源默认）。
CONVERT_REQUIRE_SUB = os.environ.get("VDL_CONVERT_REQUIRE_SUB", "false").strip().lower() == "true"
CONVERT_SUB_KEY = os.environ.get("VDL_CONVERT_SUB_KEY", "").strip()
CONVERT_FREE_DAILY = int(os.environ.get("VDL_CONVERT_FREE_DAILY", "3") or 3)
CONVERT_SUB_ENABLED = CONVERT_REQUIRE_SUB and bool(CONVERT_SUB_KEY)
_convert_quota: dict[str, dict] = {}      # ip -> {"date": "YYYY-MM-DD", "count": int}
_convert_quota_lock = threading.Lock()
# ---- 下载订阅开关（整体 freemium：免费每日限次、订阅无限；默认关闭保持全免费）----
# 与格式转换共用同一把订阅密钥 VDL_CONVERT_SUB_KEY（一个订阅解锁全部增值能力）。
# 单独开关 VDL_DOWNLOAD_REQUIRE_SUB 控制「是否对下载也启用限次」；
# VDL_DOWNLOAD_FREE_DAILY 设置免费用户每日可创建的任务数（默认 10，核心功能给得比转换宽松）。
DOWNLOAD_REQUIRE_SUB = os.environ.get("VDL_DOWNLOAD_REQUIRE_SUB", "false").strip().lower() == "true"
DOWNLOAD_FREE_DAILY = int(os.environ.get("VDL_DOWNLOAD_FREE_DAILY", "10") or 10)
DOWNLOAD_SUB_ENABLED = DOWNLOAD_REQUIRE_SUB and bool(CONVERT_SUB_KEY)
_download_quota: dict[str, dict] = {}     # ip -> {"date": "YYYY-MM-DD", "count": int}
_download_quota_lock = threading.Lock()

# ---- 格式 / 片段增强（桌面版本地加工）：基于 lib_id 的 ffmpeg 任务 ----
# process_queue 在 executor 创建后初始化（见下方）
_commentary_dir_raw = os.environ.get("VDL_COMMENTARY_DIR", "").strip()
COMMENTARY_DIR = Path(_commentary_dir_raw) if _commentary_dir_raw else None
# 解说 Python 解释器与工具链(ffmpeg/ffprobe)由下方 _CommentaryRuntime 在模块加载时集中探测。
COMMENTARY_VOICE = os.environ.get("VDL_COMMENTARY_VOICE", "zh-CN-XiaoxiaoNeural").strip() or "zh-CN-XiaoxiaoNeural"
COMMENTARY_TIMEOUT_SECONDS = int(os.environ.get("VDL_COMMENTARY_TIMEOUT", "7200") or 7200)  # 长视频 + whisper 大模型首跑 + edge-tts 排队，默认 2 小时
# 解说 worker 调用模式：local=同机 subprocess(默认) / http=独立 HTTP worker 服务(强机独立部署)
COMMENTARY_MODE = os.environ.get("VDL_COMMENTARY_MODE", "local").strip().lower()
COMMENTARY_ENDPOINT = os.environ.get("VDL_COMMENTARY_ENDPOINT", "").strip().rstrip("/")
COMMENTARY_TOKEN = os.environ.get("VDL_COMMENTARY_TOKEN", "").strip()  # 与 worker 的 WORKER_TOKEN 对应
_HERE = Path(__file__).resolve().parent


def _commentary_is_bundled() -> bool:
    """是否走「单二进制双角色」包内捆绑模式（worker 重入自身，依赖随包内置）。"""
    return os.environ.get("VDL_COMMENTARY_BUNDLED") == "1" or COMMENTARY_MODE == "bundled"


def _commentary_root(sub: str) -> Path:
    """解说管线工作目录(input/output/work)；bundled 模式重定向到可写目录(COMMENTARY_WORK_ROOT)。"""
    wr = os.environ.get("COMMENTARY_WORK_ROOT") or ""
    root = Path(wr) if wr else (COMMENTARY_DIR or Path("."))
    return root / sub
_COMMENTARY_OUT_RAW = os.environ.get("VDL_COMMENTARY_LOCAL_OUTPUT", "").strip()
def _user_data_dir() -> Path:
    """跨平台用户数据目录：Windows 打包后用 %APPDATA%/VideoDownloader（符合 Windows 规范，
    避免凭据被同机其他用户读取）；macOS/Linux 维持 ~/.video-downloader 以兼容现有用户。"""
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
    return Path.home() / ".video-downloader"


if _COMMENTARY_OUT_RAW:
    COMMENTARY_LOCAL_OUTPUT = Path(_COMMENTARY_OUT_RAW)
elif getattr(sys, "frozen", False):
    # 打包后 _HERE 位于 .app 包内部，若沿用会把上传的大视频写进 /Applications/*.app，
    # 导致替换 app 即丢数据、包体积暴涨、破坏代码签名。一律落到用户目录。
    COMMENTARY_LOCAL_OUTPUT = _user_data_dir() / "commentary_out"
else:
    COMMENTARY_LOCAL_OUTPUT = _HERE.parent / "commentary_out"
COMMENTARY_LOCAL_OUTPUT.mkdir(parents=True, exist_ok=True)
COMMENTARY_WORK_DIR = COMMENTARY_LOCAL_OUTPUT / "work"
COMMENTARY_WORK_DIR.mkdir(parents=True, exist_ok=True)
commentary_jobs: dict[str, dict] = {}
_commentary_lock = threading.Lock()



# ---- AI 去水印（E2FGVI worker，桌面版可选）：local subprocess 或 http worker ----
AI_DEWATERMARK_ENABLED = bool(
    plat.is_desktop()
    or bool(os.environ.get("VDL_AI_DEWATERMARK_ENABLED"))
)
AI_DEWATERMARK_MODE = os.environ.get("VDL_AI_DEWATERMARK_MODE", "local").strip().lower()
AI_DEWATERMARK_DIR_RAW = os.environ.get("VDL_AI_DEWATERMARK_DIR", "").strip()
AI_DEWATERMARK_DIR = Path(AI_DEWATERMARK_DIR_RAW) if AI_DEWATERMARK_DIR_RAW else None
AI_DEWATERMARK_PYTHON = os.environ.get("VDL_AI_DEWATERMARK_PYTHON", sys.executable)
AI_DEWATERMARK_ENDPOINT = os.environ.get("VDL_AI_DEWATERMARK_ENDPOINT", "http://127.0.0.1:8101")
AI_DEWATERMARK_TOKEN = os.environ.get("VDL_AI_DEWATERMARK_TOKEN", "").strip()
AI_DEWATERMARK_TIMEOUT = int(os.environ.get("VDL_AI_DEWATERMARK_TIMEOUT", "3600") or 3600)

# 启动时检测 GPU（用于 /api/nodes 告知前端）
def _detect_gpu() -> bool:
    try:
        import torch  # noqa: F401
        return torch.cuda.is_available() or (
            hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
        )
    except ImportError:
        return False

AI_GPU_AVAILABLE = _detect_gpu() if AI_DEWATERMARK_ENABLED else False

# 图片 AI 去水印（LaMa ONNX）是否可用：需 cv2 + numpy + onnxruntime。
# 与视频 AI 去水印（E2FGVI worker）相互独立；图片走本地 onnxruntime 推理，免 torch。
try:
    import dewatermark_ai as _dwc_ai_mod
    dwc_ai_available = _dwc_ai_mod.available()
except Exception:  # noqa: BLE001
    dwc_ai_available = False

# ---- 公开部署护栏：防止实例被当免费下载器薅爆带宽 ---- #
# 设为 0 表示不限制（自托管、内部使用时可关掉）
RATE_LIMIT_PER_HOUR = int(os.environ.get("VDL_RATE_LIMIT_PER_HOUR", "30") or 30)
RATE_LIMIT_WINDOW = 3600
_rate_log: dict[str, list[float]] = {}
_rate_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    """取真实客户端 IP，避免伪造 X-Forwarded-For 头绕过限流。

    优先级：X-Real-IP（Cloudflare 等反代会覆盖用户自填、最可信）
            > X-Forwarded-For 最右段（平台/反代追加的最近一跳，用户无法伪造最右）
            > 直连地址。
    """
    real = request.headers.get("x-real-ip")
    if real:
        return real.split(",")[0].strip()
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _is_loopback_ip(ip: str) -> bool:
    """环回地址（桌面 App 自连 127.0.0.1 / ::1 / localhost）判定的唯一真源。

    桌面 App 是单用户本机程序：前端（pywebview/WKWebView）所有请求都来自
    127.0.0.1，与本机 FastAPI 后端共用同一个环回 IP。若对它套用「每 IP 限流」，
    一台机器的全部正常操作会挤进同一个桶，30 次/小时的上限极易打满，导致用户
    在做解析/下载/转码时频繁收到「操作太频繁」的 429（2026-09-22 用户实测反馈）。
    环回地址不存在被外部薅带宽的滥用场景，故一律豁免；只有真实远端 IP 才受限。
    注意：经 Cloudflare / nginx 反代时 _client_ip 会取 X-Real-IP / X-Forwarded-For
    里的真实远端地址，不会误判为环回，公开部署的护栏依然有效。
    """
    if ip in ("127.0.0.1", "::1", "localhost"):
        return True
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


# ---- SSRF 防护：拒绝指向内网 / 环回 / 链路本地 / 云元数据的链接 ----
# 视频站都是公网域名；攻击者若传入内网地址（如 169.254.169.254 云元数据），
# 服务器会去请求并可能泄露凭据，或被当成跳板。入口强制只允许公网可达地址。
_PRIVATE_NETS = [
    ipaddress.ip_network(n) for n in (
        "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
        "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15",
        "::1/128", "fc00::/7", "fe80::/10",
    )
]


def _assert_safe_url(url: str) -> None:
    """SSRF 护栏：解析主机名，拒绝落在私有 / 环回 / 链路本地 / 保留网段的地址。"""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise LinkError("链接缺少主机名", "请检查链接是否完整")
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        raise LinkError("该主机不在允许范围内", "请粘贴公开可访问的视频链接")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # 解析不到的域名交给 yt-dlp 统一报错，这里不拦（避免误杀偶发 DNS 抖动）
        return
    # DNS 污染 / 代理 / CDN 场景下，getaddrinfo 常同时返回「真实公网 IP」和
    # 「保留 / 链路本地假地址」(如本机 youtube.com 解析出 2001::1 这类 Teredo 保留地址)。
    # 只要存在任一公网可达地址即视为合法公开域名，避免被假地址一票否决（与 /api/download
    # 去掉护栏的考量一致）。仅当【所有】解析地址都落在私网 / 环回 / 链路本地 / 保留 /
    # 组播段时才拒绝——这才是真正的内部地址攻击（如 169.254.169.254 云元数据）。
    has_public = False
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        if not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast):
            has_public = True
            break
    if not has_public:
        raise LinkError(
            "该链接指向非公开网络，已拒绝",
            "只允许下载公开可访问的视频；内网 / 本地 / 云元数据地址不可用",
        )


def _check_rate_limit(request: Request) -> None:
    """滑动窗口限流。超限抛 429，并告知还要等多久。

    环回客户端（桌面 App 自连）豁免，详见 _is_loopback_ip。
    """
    if RATE_LIMIT_PER_HOUR <= 0:
        return
    ip = _client_ip(request)
    if _is_loopback_ip(ip):
        return
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate_log.get(ip, []) if now - t < RATE_LIMIT_WINDOW]
        if len(hits) >= RATE_LIMIT_PER_HOUR:
            wait = int((RATE_LIMIT_WINDOW - (now - hits[0])) / 60) + 1
            _rate_log[ip] = hits
            raise HTTPException(
                status_code=429,
                detail=f"操作太频繁了，本实例每小时限 {RATE_LIMIT_PER_HOUR} 次，请 {wait} 分钟后再试；"
                       "需要更高额度可以自己部署一份（见项目 README）",
            )
        hits.append(now)
        _rate_log[ip] = hits
        if len(_rate_log) > 10000:        # 兜底清理，避免长期运行内存堆积
            for key in [k for k, v in _rate_log.items() if not v or now - v[-1] > RATE_LIMIT_WINDOW]:
                _rate_log.pop(key, None)


def _today_str() -> str:
    """当前服务器本地日期 YYYY-MM-DD，用于按自然日重置免费额度。"""
    return time.strftime("%Y-%m-%d", time.localtime())


def _subscription_quota(request, *, enabled, sub_key, free_daily, quota_store, quota_lock, label):
    """通用订阅 / 限次校验（格式转换、下载等增值 / 受限能力共用此一处）。

    返回 (subscribed, free_used, free_daily)：
      - 未启用订阅墙（enabled=False）：subscribed=False，免费额度字段为 0（全免费无限）。
      - 启用墙且携带正确订阅密钥：subscribed=True，不受免费额度限制。
      - 启用墙但免费用户：按 IP 当日计数，超出抛 402（前端据此引导订阅）。
    label 仅用于 402 文案（如「转换」「下载」）。
    """
    if not enabled:
        return (False, 0, 0)
    key = (request.headers.get("x-subscription-key") or "").strip()
    if sub_key and key == sub_key:
        return (True, 0, free_daily)
    ip = _client_ip(request)
    today = _today_str()
    with quota_lock:
        rec = quota_store.get(ip)
        if not rec or rec["date"] != today:
            rec = {"date": today, "count": 0}
            quota_store[ip] = rec
        if rec["count"] >= free_daily:
            raise HTTPException(
                status_code=402,
                detail=f"今日免费{label}次数已用完，订阅可解锁无限{label}",
            )
        rec["count"] += 1
        return (False, rec["count"], free_daily)


def _check_convert_quota(request: Request) -> tuple[bool, int, int]:
    """格式转换订阅 / 限次校验（复用通用 _subscription_quota）。"""
    return _subscription_quota(
        request, enabled=CONVERT_SUB_ENABLED, sub_key=CONVERT_SUB_KEY,
        free_daily=CONVERT_FREE_DAILY, quota_store=_convert_quota,
        quota_lock=_convert_quota_lock, label="转换",
    )


def _check_download_quota(request: Request) -> tuple[bool, int, int]:
    """下载订阅 / 限次校验（freemium：免费每日限次，订阅无限）。"""
    return _subscription_quota(
        request, enabled=DOWNLOAD_SUB_ENABLED, sub_key=CONVERT_SUB_KEY,
        free_daily=DOWNLOAD_FREE_DAILY, quota_store=_download_quota,
        quota_lock=_download_quota_lock, label="下载",
    )


# ---- 云端算力账号级每日配额（2026-09-29）：免费 3 次/日，会员 200 次/日 ------------ #
# 覆盖：转码(含免重传重转)/视频拼接/图片·PDF 去水印/字幕识别(whisper)/字幕烧录/字幕翻译。
# 与下载配额同一套账号级引擎（resource="cloud"）：cn 本地记账 + 异步上云（授权中心按
# 账号累计，同一账号 App/网页共享），hk 回派 cn /api/member/quota/use（cn 为权威）。
# 会员引擎异常 fail-open（不挡已登录用户）。紧急停用：VDL_CLOUD_QUOTA_OFF=true。
# 注意：旧 IP 粒度墙 _check_convert_quota（VDL_CONVERT_REQUIRE_SUB）已被本机制取代，
# 该环境变量必须保持 false —— 否则会双重计数。
_CLOUD_QUOTA_OFF = os.environ.get("VDL_CLOUD_QUOTA_OFF", "false").strip().lower() == "true"
_CLOUD_LOGIN_MSG = "使用该功能前请先登录账号（免费注册即得，每个功能每日 3 次免费额度）"
# 🔴 2026-10-06 拆池：4 个云端功能各自独立配额键（与桌面端 quota.CLOUD_RESOURCES 同名）。
_CLOUD_RESOURCE_NAMES = {
    "cloud_commentary": "视频解说",
    "cloud_convert": "在线转码",
    "cloud_concat": "在线拼接",
    "cloud_dewatermark": "图片去水印",
    "cloud_dewatermark_pdf": "PDF 去水印",
    "cloud_subtitle": "字幕提取",
    "cloud_subtitle_burn": "字幕烧录",
    "cloud_subtitle_translate": "字幕翻译",
}


def _cloud_quota_relay(request, payload: dict):
    """账号级配额检查/计数回派 cn 权威（hk 节点用）。失败返回 None（fail-open）。"""
    base = (os.environ.get("VDL_QUOTA_RELAY_URL")
            or os.environ.get("VDL_WORKER_URL") or "").strip().rstrip("/")
    if not base:
        return None
    import urllib.request as _ureq
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    auth = request.headers.get("Authorization")
    if auth:  # 原样带上 cn 签发的 bearer，由 cn 验签并定位用户
        headers["Authorization"] = auth
    req = _ureq.Request(base + "/api/member/quota/use",
                        data=json.dumps(payload).encode("utf-8"),
                        headers=headers, method="POST")
    try:
        with _ureq.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8", "ignore") or "{}")
    except Exception as e:  # noqa: BLE001 — 配额服务异常绝不挡功能
        try:
            logger.warning("[cloud-quota] 回派 cn 失败（fail-open）: %s", str(e)[:160])
        except Exception:  # noqa: BLE001
            pass
        return None


def cloud_quota_gate(request, need: int = 1, resource: str = "cloud_commentary") -> dict:
    """云端算力配额预检（不计数）。**按功能独立**：每个云端功能（解说/转码/
    去水印/字幕）各自一份「终身 3 次」+ 独立每日配额（2026-10-06 拆池），
    超限 402；匿名 403；引擎异常 fail-open。

    need：本次打算创建的任务数。
    resource：配额键（cloud_commentary / cloud_convert / cloud_dewatermark /
    cloud_subtitle），**各功能独立计数**，与桌面端同名键共用一份。
    返回 gate 描述（供 cloud_quota_count 复用，resource 随 gate 传递）。
    hk 节点仍回派 cn 权威（中心在 cn）。
    """
    resource = str(resource or "cloud_commentary").strip() or "cloud_commentary"
    if _CLOUD_QUOTA_OFF:
        return {"mode": "off"}
    try:
        if NODE_REGION != "cn" and (os.environ.get("VDL_QUOTA_RELAY_URL")
                                    or os.environ.get("VDL_WORKER_URL") or "").strip():
            res = _cloud_quota_relay(request, {"resource": resource, "n": need,
                                               "check_only": True})
            if isinstance(res, dict) and res.get("ok") is False:
                if res.get("code") == "MEMBER_QUOTA":
                    raise HTTPException(status_code=402,
                                        detail=res.get("error") or "免费云端额度已用尽")
                if res.get("code") == "NO_AUTH":
                    raise HTTPException(status_code=403, detail=_CLOUD_LOGIN_MSG)
            return {"mode": "relay", "resource": resource}
        if not get_current_user_id(request):
            raise HTTPException(status_code=403, detail=_CLOUD_LOGIN_MSG)
        store = current_member_store(request)
        if _is_member_any(store):
            return {"mode": "local", "store": store, "remaining": None}   # 会员不限
        # 免费用户：向中心问「该功能还有没有额度」（只查不扣，扣在 cloud_quota_count）
        _got = _cloud_quota_remaining(store, resource)
        rem, center_limit = (_got if isinstance(_got, tuple) else (None, None))
        if rem is None:
            # 拿不到权威结果（未登录云端 / 断网）→ 退回每日配额口径（fail-open）
            q = store.quota_state(resource)
            if q.get("unlimited") or q.get("unknown") or q.get("allowed", True):
                return {"mode": "local", "store": store, "remaining": q.get("remaining"),
                        "legacy_daily": True, "resource": resource}
            _res_name = _CLOUD_RESOURCE_NAMES.get(resource, "该功能")
            detail = (f"「{_res_name}」今日免费额度已用完（每日 {q['limit']} 次），"
                      f"明天再来，或开通会员解锁无限使用。")
            raise HTTPException(status_code=402, detail=detail)
        if rem > 0:
            # 🔴 2026-10-22：每日额度与中心额度**双闸**。个人中心「使用统计」承诺的是
            # 「每日 N 次」（feature_usage_status 读本地 daily_usage），此前这里只拦
            # 中心额度 → 统计页数字永远不动（用户反馈「图片去水印为什么不扣次数」）。
            # 放行前先查本地每日账（管理员可在后台把每日额度改小），用尽同样 402，
            # 文案与 legacy 路径保持一致。
            _q = store.quota_state(resource)
            if not (_q.get("unlimited") or _q.get("unknown") or _q.get("allowed", True)):
                _res_name = _CLOUD_RESOURCE_NAMES.get(resource, "该功能")
                raise HTTPException(status_code=402, detail=(
                    f"「{_res_name}」今日免费额度已用完（每日 {_q['limit']} 次），"
                    f"明天再来，或开通会员解锁无限使用。"))
            return {"mode": "local", "store": store, "remaining": rem,
                    "resource": resource}
        _res_name = _CLOUD_RESOURCE_NAMES.get(resource, "该功能")
        # 🔴 2026-10-06 用中心给的生效上限（管理员可在桌面后台改），不写死常量；
        #   旧中心不返回该字段时退回本地常量。文案里的「每个功能」是错的 ——
        #   拆池后额度是**按功能各一份**，说的是本功能的次数。
        _lim_txt = center_limit if isinstance(center_limit, int) and center_limit > 0 \
            else _cloud_quota_limits()[0]
        raise HTTPException(
            status_code=402,
            detail=f"「{_res_name}」的免费额度（终身 {_lim_txt} 次）已用完。"
                   f"开通会员即可解锁无限使用。")
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — 会员引擎异常 fail-open
        return {"mode": "local", "store": None, "resource": resource}


def _cloud_quota_token(store) -> str:
    """取云端账号 token。

    🔴 用网页版既有的 `store.cloud_session()`（`cloud_link.py` 全程用它发云端
    请求），**不要**用 `status()["account"]`（那是给前端的公开视图，不含 token），
    也不要用桌面端的 `_cloud_token()`（网页版 MembershipStore 没那个方法）。
    """
    try:
        return str((store.cloud_session() or {}).get("token") or "").strip()
    except Exception:
        return ""


def _is_member_any(store) -> bool:
    """是否任一会员档活跃（下载或 AI）。网页版只有 status()，没有 is_*_active()。"""
    try:
        st = store.status()
        return bool((st.get("download_member") or {}).get("active")) or \
               bool((st.get("ai_member") or {}).get("active"))
    except Exception:
        return False


def _cloud_quota_limits() -> tuple[int, int]:
    """（终身, 每日 auto）上限，读 quota.py 常量做单一真源。"""
    try:
        from quota import LIFETIME_CLOUD_EVENTS, DAILY_AUTO_RUNS
        return int(LIFETIME_CLOUD_EVENTS), int(DAILY_AUTO_RUNS)
    except Exception:
        return 3, 1


def _cloud_quota_remaining(store, resource: str = "cloud_commentary"):
    """问中心「该 resource 的终身额度还剩几次」→ `(remaining, limit)`；None = 拿不到。

    `limit` 是中心的**生效上限**（后台可覆盖），用于 402 文案报真数字；
    旧中心不返回该字段时为 None，文案退回本地常量。

    🔴 兼容两种中心返回：新中心 `lifetime_remaining` 是 **per-resource dict**；
    旧中心是 **int**（4 个功能共用的总池）→ 直接当该 resource 的余量用
    （旧中心本来就只记一份总数，语义等价于旧总池口径，不放大也不缩小）。
    """
    _lim = None
    try:
        import license_client
        tok = _cloud_quota_token(store)
        if not tok:
            return None
        r = license_client.cloud_quota_remote(tok, resource=resource)
        if not isinstance(r, dict) or not r.get("ok"):
            return None
        cq = r.get("cloud_quota")
        if not isinstance(cq, dict):
            return None
        # 🔴 2026-10-06 取中心给的**生效上限**（管理员可覆盖）。写死 3 会在管理员
        #   改过次数后报错数字（用户看到「终身 3 次已用完」但实际配的是 5）。
        lims = cq.get("lifetime_limits")
        if isinstance(lims, dict):
            try:
                _lim = max(0, int(lims.get(resource) or 0))
            except (TypeError, ValueError):
                _lim = None
        rem = cq.get("lifetime_remaining")
        if isinstance(rem, dict):
            # 🔴 2026-10-21：字典里**没有这个键** ≠ 余量为 0。中心的终身池只有 8 个
            #   「云端算力」键（_CLOUD_RESOURCES），而 2026-10-19 起音乐转换 / 图片
            #   转换拆出了 convert_audio / convert_image —— 中心压根不管这两个键。
            #   旧写法 rem.get(resource) or 0 → 0 ⇒ 免费用户第一次点音乐/图片转换就被
            #   判「终身额度已用完」（402），而这两项按表本该是免费 5 次/日。
            #   缺失键必须返回 None（=「中心不管这个功能」），让调用方退回每日配额。
            if resource not in rem:
                return None
            try:
                return max(0, int(rem.get(resource) or 0)), _lim
            except (TypeError, ValueError):
                return None
        try:
            return max(0, int(rem or 0)), _lim
        except (TypeError, ValueError):
            return None
    except Exception:
        return None


def _record_daily(store, resource: str, n: int) -> None:
    """中心额度扣减成功后，同步记本地每日账（**只能在条件分支内调用**，见统一守卫）。

    个人中心「使用统计」与后台「使用详情」读的都是 daily_usage —— 此前正常路径
    只扣中心终身额度、不记每日账，统计页对云端功能永远显示没用过
    （2026-10-22 用户反馈「图片去水印为什么不扣次数」的根因）。
    记账失败只记日志，绝不影响已创建的任务。
    """
    try:
        store.use_daily(resource, n=n)
    except Exception as e:  # noqa: BLE001 — 记账失败绝不回滚已创建的任务
        try:
            logger.warning("[cloud-quota] 每日记账失败（忽略）: %s", str(e)[:160])
        except Exception:
            pass


def cloud_quota_count(request, gate: dict, n: int = 1) -> None:
    """任务成功创建后的计数（与下载墙同语义：失败只记日志，不回滚已建任务）。

    🔴 2026-10-06 拆池：按 gate 里携带的 resource 向授权中心**原子扣减**
    该功能独立的终身额度（cloud_commentary/convert/dewatermark/subtitle 各一份）。
    中心不可达时退回每日配额口径（fail-open），绝不因记账失败让已创建的任务失败。
    """
    if gate.get("mode") in ("off",) or n <= 0:
        return
    resource = str(gate.get("resource") or "cloud_commentary").strip() or "cloud_commentary"
    try:
        if gate.get("mode") == "relay":
            _cloud_quota_relay(request, {"resource": resource, "n": n})
            # 🔴 2026-10-22：relay（hk 回派 cn）也记本地每日账，否则 HK 恢复后
            # 会复发「云功能不扣次数」（与 cn 直连路径 2026-10-22 修复同族）。
            # hk 本机 store 非账号权威 ⇒ 只做 best-effort 展示记账，失败忽略。
            try:
                _record_daily(current_member_store(request), resource, n)
            except Exception:  # noqa: BLE001 — 记账失败绝不回滚已创建的任务
                pass
            return
        store = gate.get("store") or current_member_store(request)
        if gate.get("legacy_daily"):
            # 拿不到中心（未登录云端 / 断网）→ 退回每日配额口径（按功能独立计数）
            store.use_daily(resource, n=n)
            return
        if _is_member_any(store):
            _record_daily(store, resource, n)       # 会员不限次，但每日账要记（统计页显示用）
            return
        try:
            import license_client
            tok = _cloud_quota_token(store)
            if not tok:
                store.use_daily(resource, n=n)      # 无云端身份 → 退回每日
                return
            r = license_client.cloud_quota_remote(tok, lifetime=n, resource=resource)
            if isinstance(r, dict) and r.get("ok"):
                _record_daily(store, resource, n)   # 中心扣成功 ⇒ 同步记每日账（统计页/后台明细）
            else:
                store.use_daily(resource, n=n)      # 中心异常 → fail-open
        except Exception:
            store.use_daily(resource, n=n)
    except Exception as e:  # noqa: BLE001 — 计数失败绝不回滚已创建的任务
        try:
            logger.warning("[cloud-quota] 计数失败（忽略）: %s", str(e)[:160])
        except Exception:  # noqa: BLE001
            pass


def _host_of(url: str) -> str:
    """从链接取出主机名（去掉 www./m. 前缀），解析失败返回空串。"""
    try:
        host = (urlparse(url).hostname or "").lower()
        return host.removeprefix("www.").removeprefix("m.")
    except ValueError:
        return ""

store = TaskStore(DOWNLOAD_DIR)
# 线程池只设硬上限；真正的「同时下几个」由 BatchScheduler 的并发计数器软控（可动态调整）
executor = ThreadPoolExecutor(max_workers=VDL_BATCH_HARD_MAX, thread_name_prefix="vdl-dl")
scheduler = BatchScheduler(executor, default_concurrency=MAX_CONCURRENT_DOWNLOADS)
process_queue = pq_mod.ProcessQueue(executor, default_concurrency=2, hard_max=4)

# ---- 订阅监控（桌面版功能）：本地 JSON 持久化 + 后台定时探查新视频 ----
SUB_ENABLED = plat.is_desktop() or bool(os.environ.get("VDL_SUBSCRIPTIONS_ENABLED"))
SUBSCRIBE_PROBE_LIMIT = int(os.environ.get("VDL_SUBSCRIBE_PROBE_LIMIT", "100") or 100)
SUB_CHECK_INTERVAL = int(os.environ.get("VDL_SUB_CHECK_INTERVAL", "1800") or 1800)  # 默认 30 分钟
sub_store = subs_mod.SubscriptionStore(DOWNLOAD_DIR / ".subscriptions.json")
prober = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_PROBES, thread_name_prefix="vdl-probe")

# ---- 时效自动清理（桌面版功能）：按保留期/容量上限清理下载目录 ----
# 与媒体库同一开关：只有桌面版（或显式开 VDL_LIBRARY_ENABLED）才管理本地磁盘。
RETENTION_ENABLED = (
    plat.is_desktop()
    or bool(os.environ.get("VDL_LIBRARY_ENABLED"))
    or bool(os.environ.get("VDL_RETENTION_ENABLED"))
)
retention_store = retention_mod.RetentionStore(DOWNLOAD_DIR / ".retention.json")

# ---- 库内保险箱（桌面版功能）：选中文件就地 AES 加密为 .vdlenc，播放前临时解密 ----
# 与媒体库同一开关。内存密钥 VAULT_KEY 为 None 即「锁定」态；vault.json 只存 salt+verify，
# 绝不存明文密码或密钥（见 crypto_vault.new_vault / unlock_key）。
CRYPTO_ENABLED = (
    plat.is_desktop()
    or bool(os.environ.get("VDL_LIBRARY_ENABLED"))
    or bool(os.environ.get("VDL_CRYPTO_ENABLED"))
)
VAULT_PATH = Path(
    os.environ.get("VDL_VAULT_CONFIG") or (Path.home() / ".video-downloader" / "vault.json")
)
VAULT_LOCK = threading.Lock()
VAULT_KEY: bytes | None = None  # 解锁后驻留内存；锁定/重启即清空
CRYPTO_JOBS: dict[str, dict] = {}
CRYPTO_LOCK = threading.Lock()
# 解密播放临时目录（与下载目录同盘，避免跨卷复制大文件）
VAULT_TMP = DOWNLOAD_DIR / ".vault_tmp"
VAULT_TMP.mkdir(parents=True, exist_ok=True)
CRYPTO_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vdl-crypto")

# ---- 桌面版种子下载（libtorrent 集成）：把 magnet/.torrent 下载到本地媒体库 ----
# 与媒体库同一开关策略（桌面版/显式开启）；libtorrent 为可选依赖，未安装时功能整体禁用。
TORRENT_ENABLED = (
    plat.is_desktop()
    or bool(os.environ.get("VDL_LIBRARY_ENABLED"))
    or bool(os.environ.get("VDL_TORRENT_ENABLED"))
)
torrent_manager = torrent_mod.get_manager(DOWNLOAD_DIR)


def _vault_load() -> dict | None:
    try:
        if VAULT_PATH.exists():
            return json.loads(VAULT_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _chmod_600(path: Path) -> None:
    """跨平台收紧文件权限到仅当前用户可读写。Windows 的 os.chmod 只切只读位、不控 ACL，
    故改用 icacls 移除继承并仅授权当前用户（无 pywin32 也可工作）。"""
    if sys.platform == "win32":
        try:
            import subprocess as _sp
            _sp.run(["icacls", str(path), "/inheritance:r",
                     "/grant:r", f"{os.environ.get('USERNAME', '')}:(R,W)"],
                    check=False, capture_output=True)
        except Exception:
            pass
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _vault_save(vault: dict) -> None:
    with atomic_io.mutation(VAULT_PATH):
        atomic_io.atomic_write_text(
            VAULT_PATH, json.dumps(vault, ensure_ascii=False), mode=0o600, chmod=_chmod_600
        )


def _vault_tmp_for(lib_id: str) -> Path:
    VAULT_TMP.mkdir(parents=True, exist_ok=True)
    return VAULT_TMP / (lib_id + ".dec")


def _prune_vault_tmp(max_age_seconds: int = 1800) -> None:
    """清理解密播放的临时文件（超过 30 分钟），避免明文长期留盘。"""
    try:
        now = time.time()
        for f in VAULT_TMP.iterdir():
            if f.is_file() and now - f.stat().st_mtime > max_age_seconds:
                try:
                    f.unlink()
                except OSError:
                    pass
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 自动解说（松耦合桥接 commentary-pipeline/process.py，不重写解说逻辑）
# --------------------------------------------------------------------------- #


class _CommentaryRuntime:
    """模块加载时一次性探测解说运行环境，集中管理解释器与工具链。

    打包后 sys.executable 是 .app 主程序、子进程 PATH 可能被截断，
    这里把所有不确定性收敛到一处，并暴露清晰的诊断信息（issues/ready）。
    """

    def __init__(self) -> None:
        self.python = self._resolve_python()
        self.python_ok = self._check_python(self.python)
        self.deps_ok = self._check_deps(self.python) if self.python_ok else False
        self.ffmpeg_dir, self.ffprobe_ok = self._resolve_ffmpeg()
        self.issues = self._collect_issues()

    # ---- 解释器 ----
    def _resolve_python(self) -> str:
        """解析跑 process.py 的解释器，统一委托 commentary_locate。"""
        from commentary_locate import locate_commentary
        loc = locate_commentary()
        if loc is None:
            return sys.executable
        return loc.python

    def _check_python(self, py: str) -> bool:
        # 打包后 sys.executable 通常是 .app 主程序，不应作为解释器——
        # 但「单二进制双角色」bundled 模式下，worker 正是用自身(sys.executable)重入，
        # 依赖已随包内置，故该例外下允许 sys.executable 作为解释器。
        if plat.is_desktop() and py == sys.executable:
            if os.environ.get("VDL_COMMENTARY_BUNDLED") == "1" or COMMENTARY_MODE == "bundled":
                return True
            return False
        # 开发/测试模式（非 frozen 且未显式设 VDL_COMMENTARY_PYTHON）信任当前解释器
        if "VDL_COMMENTARY_PYTHON" not in os.environ and not plat.is_desktop():
            return True
        try:
            r = subprocess.run(
                [py, "-c", "import sys; print(sys.executable)"],
                capture_output=True, text=True, timeout=5,
            )
            return r.returncode == 0 and "python" in (r.stdout or "").lower()
        except Exception:
            return False

    def _check_deps(self, py: str) -> bool:
        if not self.python_ok:
            return False
        try:
            r = subprocess.run(
                [py, "-c", "import faster_whisper, edge_tts"],
                capture_output=True, text=True, timeout=30,
            )
            return r.returncode == 0
        except Exception:
            return False

    # ---- 工具链 ffmpeg / ffprobe ----
    def _resolve_ffmpeg(self) -> tuple[str, bool]:
        """解析 ffmpeg/ffprobe 所在目录；打包后 PATH 可能被截断，这里显式收集所有可能目录。

        策略：
        1. 优先 VDL_FFPROBE_BIN / VDL_FFMPEG_BIN 的目录；
        2. 如果该目录没有 ffprobe，再补充系统常见目录；
        3. 返回所有可用目录（用 ; 拼接），并验证 ffprobe 确实能在合并后的 PATH 里找到。
        """
        dirs: list[str] = []
        seen: set[str] = set()

        def add(d: str | Path) -> None:
            s = str(d)
            if s and s not in seen:
                seen.add(s)
                dirs.append(s)

        # 1) 用户/启动器显式指定的 bin
        for key in ("VDL_FFPROBE_BIN", "VDL_FFMPEG_BIN"):
            v = os.environ.get(key, "").strip()
            if v:
                add(Path(v).parent)

        # 2) 当前进程 PATH 里能直接找到的工具
        for name in ("ffprobe", "ffmpeg"):
            p = shutil.which(name)
            if p:
                add(Path(p).parent)

        # 3) 系统常见目录兜底（按平台补充）
        if sys.platform == "win32":
            add(r"C:\ffmpeg\bin")
            add(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"))
        for fb in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
            add(fb)

        if not dirs:
            return "", False

        # 4) 验证：把这些目录前置到 PATH 后，ffprobe 是否真的能被发现
        probe_path = os.pathsep.join(dirs + ([os.environ.get("PATH", "")] if os.environ.get("PATH") else []))
        ok = bool(shutil.which("ffprobe", path=probe_path))
        return os.pathsep.join(dirs), ok

    def _collect_issues(self) -> list[str]:
        issues: list[str] = []
        if not self.python_ok:
            issues.append("解说 Python 解释器不可用（请设置 VDL_COMMENTARY_PYTHON 指向装了 faster_whisper/edge_tts 的解释器）")
        elif not self.deps_ok:
            issues.append("解说 Python 缺少依赖（faster_whisper / edge_tts 未安装）")
        if not self.ffprobe_ok:
            issues.append("找不到 ffprobe（请安装 ffmpeg 或设置 VDL_FFPROBE_BIN）")
        return issues

    def env(self) -> dict[str, str]:
        """为 process.py 子进程准备环境变量，关键是把 ffmpeg/ffprobe 目录前置到 PATH，
        并强制 PYTHONIOENCODING=utf-8（Windows 默认 cp936 会让 emoji 日志乱码/抛错）。
        同时注入统一 LLM 配置——若用户已在前端配置了 Key/提供商/模型。
        另外显式设置 COMMENTARY_BASE，确保打包后 config.py 里的 BASE 一定指向管线根目录
        （避免 PyInstaller 的 __file__ 解析差异导致找不到 assets/fonts 下捆绑的中文字体）。"""
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        if COMMENTARY_DIR:
            env["COMMENTARY_BASE"] = str(COMMENTARY_DIR)
        if self.ffmpeg_dir:
            env["PATH"] = self.ffmpeg_dir + os.pathsep + env.get("PATH", "")
        # 统一 LLM 配置：仅 Key 非空时注入，无 Key 不污染子进程环境
        inject_llm_env(env)
        return env

    def ready(self) -> bool:
        return not self.issues


COMMENTARY_RT = _CommentaryRuntime()


def _commentary_work_dir() -> Path:
    d = COMMENTARY_WORK_DIR / uuid.uuid4().hex[:12]
    d.mkdir(parents=True, exist_ok=True)
    return d





# 解说任务过程展示：阶段名 + 匹配日志关键词（不区分大小写）
_COMMENTARY_STEP_KEYWORDS = [
    ("准备输入文件", ["准备", "复制", "软链", "输入文件", "symlink", "copy file", "save upload"]),
    ("转写视频语音", ["[1/2] 转写", "whisper", "transcribe", "提取音轨", "转写完成", "transcript"]),
    ("生成 AI 解说词", ["[2/2] 自动解说词", "auto_script", "解说词", "script.json", "生成脚本", "script ready"]),
    ("合成 AI 配音", ["配音", "edge_tts", "tts", "语音合成", "synthesize", "narration"]),
    ("渲染成片", ["剪辑成片", "edit_ffmpeg", "edit.py", "ffmpeg", "渲染", "合并", "concat", "build", "成片"]),
    ("完成", ["全部完成", "成片在", "completed", "done"]),
]


def _ensure_commentary_steps(job: dict) -> list[dict]:
    """初始化或返回解说任务的步骤时间线。"""
    if "steps" not in job or not job["steps"]:
        now = time.time()
        job["steps"] = [
            {"name": name, "status": "pending", "detail": "", "created_at": now, "updated_at": now}
            for name, _ in _COMMENTARY_STEP_KEYWORDS
        ]
    return job["steps"]


def _commentary_log(job: dict, line: str) -> None:
    """给解说任务追加一行带时间戳的运行日志。"""
    if not line:
        return
    ts = time.strftime("%H:%M:%S", time.localtime())
    job.setdefault("logs", []).append(f"{ts}  {line.strip()}")
    if len(job.get("logs", [])) > 200:
        job["logs"][:] = job["logs"][-200:]


def _update_commentary_steps(job: dict, line: str) -> None:
    """根据 process.py 输出关键词自动推进步骤状态。"""
    steps = _ensure_commentary_steps(job)
    line_lower = line.lower()
    matched = -1
    for idx, (name, keywords) in enumerate(_COMMENTARY_STEP_KEYWORDS):
        if any(k.lower() in line_lower for k in keywords):
            matched = idx
            break
    if matched < 0:
        return
    now = time.time()
    for s in steps[:matched]:
        if s["status"] == "pending":
            s["status"] = "done"
            s["updated_at"] = now
    cur = steps[matched]
    cur["status"] = "running"
    cur["detail"] = line[:200]
    cur["updated_at"] = now


def _commentary_mark_error(job_id: str, detail: str) -> None:
    """把当前 running 的步骤标为 error。"""
    with _commentary_lock:
        job = commentary_jobs.get(job_id)
        if not job:
            return
        steps = _ensure_commentary_steps(job)
        now = time.time()
        for s in steps:
            if s["status"] == "running":
                s["status"] = "error"
                s["detail"] = detail[:200]
                s["updated_at"] = now
                break


def _apply_trim(src_path: str, in_dir: Path, start: float, end: float):
    """服务端预处理裁剪：同一源+起止始终产出同名文件，便于 script-only 与后续 render 复用。
    返回 (裁剪后路径, 裁剪后时长)；裁剪失败则回退原路径。"""
    key = hashlib.md5(f"{src_path}|{start:.3f}|{end:.3f}".encode("utf-8")).hexdigest()[:12]
    trim_out = in_dir / f"trim_{key}.mp4"
    if trim_out.exists() and trim_out.stat().st_size > 0:
        return str(trim_out), (end - start)
    dur = max(0.1, end - start)
    try:
        subprocess.run(
            [FFMPEG_BIN, "-y", "-ss", f"{start:.3f}", "-i", str(src_path),
             "-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "veryfast",
             "-c:a", "aac", "-movflags", "+faststart", str(trim_out)],
            capture_output=True, text=True, timeout=1800,
        )
    except Exception as exc:
        logger.warning("视频裁剪失败，回退使用原视频：%s", exc)
        return src_path, 0.0
    if not (trim_out.exists() and trim_out.stat().st_size > 0):
        return src_path, 0.0
    return str(trim_out), dur


def _commentary_eta(job: dict, line: str, src_dur: float) -> None:
    """从子进程进度行推算 ETA，写入 job['eta_remaining']（剩余秒）/ eta_done_at（绝对时间戳）。"""
    now = time.time()
    m = re.search(r"共\s*(\d+)\s*段", line)
    if m:
        job["eta_total_segs"] = int(m.group(1))
    m = re.search(r"开始渲染\s*(\d+)\s*段", line)
    if m:
        job["eta_render_total"] = int(m.group(1))
        job["eta_render_start"] = now
    m = re.search(r"\[(\d+)/(\d+)\]", line)
    if m:
        done = int(m.group(1))
        job["eta_render_done"] = done
        rs = job.get("eta_render_start")
        if rs:
            elapsed = now - rs
            if done > 0:
                per = elapsed / done
                total = job.get("eta_render_total") or done
                # 段是并发渲染的（min(cpu,8)），按并行度折算墙钟时间
                par = max(1, min(os.cpu_count() or 4, 8))
                remaining = per * max(0, total - done) / par
                job["eta_remaining"] = int(remaining)
                job["eta_done_at"] = int(now + remaining)
            return
    # 渲染前（转写/TTS）：用源时长做粗估，进入渲染段后由上面的实时进度收敛
    if job.get("eta_render_start") is None and src_dur:
        elapsed = now - job.get("started_at", now)
        coarse = src_dur * 1.2
        rem = max(0, int(coarse - elapsed))
        job["eta_remaining"] = rem
        job["eta_done_at"] = int(now + rem)


def _commentary_option_args(*, commentary_type: str = "deep_hl", highlight_source: str = "ai",
                             intro_highlight: bool = False, skip_intro_outro: bool = False,
                             no_narrate_intro_outro: bool = True, retain_pct: float | None = None,
                             web: bool = False, one_click: bool = False, mode: str | None = None,
                             style: str = "none") -> list:
    """把剪辑选项翻译成 process.py 的命令行参数（local / bundled 模式共用）。"""
    args = ["--commentary-type", commentary_type, "--highlight-source", highlight_source]
    if style and style != "none":
        args += ["--style", style]
    if intro_highlight:
        args.append("--intro-highlight")
    if skip_intro_outro:
        args.append("--skip-intro-outro")
    if not no_narrate_intro_outro:
        # 默认就是「保留片头片尾·不解说」，仅当用户显式要全片解说时才带 --narrate-all
        args.append("--narrate-all")
    if retain_pct is not None:
        args += ["--retain-pct", str(retain_pct)]
    if web:
        args.append("--web")
    if one_click:
        args.append("--one-click")
    if mode:  # 旧版兼容字段，仅当显式传了才带
        args += ["--mode", mode]
    return args


def _commentary_run(job_id: str, src_path: str, vertical: bool, voice: str, edit_only: str | None = None, script_only: bool = False, trim_start: float = 0.0, trim_end: float = 0.0, mode: str | None = None, commentary_type: str = "deep_hl", highlight_source: str = "ai", intro_highlight: bool = False, skip_intro_outro: bool = False, no_narrate_intro_outro: bool = True, retain_pct: float | None = None, web: bool = False, one_click: bool = False, title: str = "", style: str = "none") -> None:
    """后台线程：把下载好的视频喂给 commentary-pipeline，等成片回传。

    复用用户现成的 process.py 整条管线（whisper 转写 → edge-tts 配音 → ffmpeg 出片），
    本函数只负责文件桥接与成片定位。算力由解说 worker 独立承担，不影响下载服务。
    HTTP 模式(VDL_COMMENTARY_MODE=http)下转发给独立 worker 服务。
    """
    if COMMENTARY_MODE == "http":
        return _commentary_run_http(job_id, src_path, vertical, voice, mode,
                                    commentary_type=commentary_type,
                                    highlight_source=highlight_source,
                                    intro_highlight=intro_highlight,
                                    skip_intro_outro=skip_intro_outro,
                                    no_narrate_intro_outro=no_narrate_intro_outro,
                                    retain_pct=retain_pct, web=web,
                                    one_click=one_click, style=style)
    try:
        # 调用前先确认运行环境就绪，失败直接给清晰错误，避免盲目 subprocess 后误报「执行成功」
        if not COMMENTARY_RT.ready():
            raise RuntimeError("解说环境未就绪：" + "；".join(COMMENTARY_RT.issues))
        base = job_id  # 用 job_id 作安全 ascii 文件名，避开中文/空格对 process.py 路径处理的干扰
        # 解说锚点：优先用调用方传入的标题；否则从源文件名推断（下载任务文件名通常含剧集名）。
        # 目的是给 LLM 一个「当前剧集」的锚点，防止解说词凭记忆跑题到别的剧集。
        if not title:
            try:
                title = Path(src_path).stem or ""
            except Exception:
                title = ""
        in_dir = _commentary_root("input")
        out_dir = _commentary_root("output")
        in_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)

        # ---- 时长裁剪（服务端预处理）----
        # 用确定性命名（源路径+起止哈希）切出裁剪片段，script-only 与后续 render 复用同一文件，避免重复切。
        try:
            src_dur = fftools.probe_duration(Path(src_path), ffmpeg_bin=FFMPEG_BIN)
        except Exception:
            src_dur = 0.0
        ts, te = float(trim_start or 0), float(trim_end or 0)
        use_src = src_path
        if te > ts > 0 and (not src_dur or te <= src_dur + 1.0):
            use_src, _ = _apply_trim(src_path, in_dir, ts, te)

        in_file = in_dir / f"{base}.mp4"
        if in_file.exists() or in_file.is_symlink():
            in_file.unlink()
        try:
            os.symlink(use_src, in_file)
        except OSError:
            shutil.copyfile(use_src, in_file)  # 跨挂载点软链失败则退化为复制

        # 子进程参数：edit_only 真 → 走 --edit-only（吃已审核脚本，不重跑转录）；否则 --auto。
        # 加 -u 关闭 stdout 块缓冲，让进度行实时回流（ETA 才平滑）。
        # bundled 模式：用主程序自身(sys.executable)以 --vdl-commentary-worker 重入为 worker，
        # 依赖随包内置(已砍 torch)，无需外部 Python / pip，契合自包含铁律。
        _bundled = _commentary_is_bundled()
        extra = _commentary_option_args(commentary_type=commentary_type,
                                        highlight_source=highlight_source,
                                        intro_highlight=intro_highlight,
                                        skip_intro_outro=skip_intro_outro,
                                        no_narrate_intro_outro=no_narrate_intro_outro,
                                        retain_pct=retain_pct, web=web,
                                        one_click=one_click, mode=mode,
                                        style=style)
        if edit_only:
            if _bundled:
                args = [sys.executable, "--vdl-commentary-worker",
                        str(in_file), "--edit-only", edit_only]
            else:
                args = [COMMENTARY_RT.python, "-u", "process.py", str(in_file), "--edit-only", edit_only]
            if vertical:
                args.append("--vertical")
            if voice:
                args += ["--voice", voice]
            if title:
                # 精修出片：把 title 同时传给 --title（解说锚点）和 --out-name（成片原片名），
                # 避免成片名带 hash/安全文件名（ee6b7cf95736）
                args += ["--title", title, "--out-name", title]
            # 审核后出片：把用户在面板上改过的剪辑选项一并传下去（覆盖脚本自带选项）
            args += extra
        else:
            if _bundled:
                args = [sys.executable, "--vdl-commentary-worker", str(in_file), "--auto"]
            else:
                args = [COMMENTARY_RT.python, "-u", "process.py", str(in_file), "--auto"]
            if vertical:
                args.append("--vertical")
            if voice:
                args += ["--voice", voice]
            if title:
                # 同上：title → --title + --out-name，让成片用原片名而非 hash 名
                args += ["--title", title, "--out-name", title]
            if script_only:
                args.append("--script-only")
            args += extra

        # Popen 实时读取 stdout/stderr，按行追加到 commentary_jobs[job_id]['progress']，
        # 前端轮询时把进度条回显给用户，避免「30 分钟黑屏焦虑」。
        last_lines: list[str] = []

        def _append(line: str) -> None:
            line = line.rstrip("\n")
            if not line:
                return
            last_lines.append(line)
            if len(last_lines) > 80:  # 只保留最近 80 行，避免内存膨胀
                del last_lines[: len(last_lines) - 80]
            with _commentary_lock:
                job = commentary_jobs.setdefault(job_id, {})
                job["progress"] = list(last_lines)
                _commentary_log(job, line)
                _update_commentary_steps(job, line)
                _commentary_eta(job, line, src_dur)

        with _commentary_lock:
            job = commentary_jobs.setdefault(job_id, {})
            job["started_at"] = job.get("started_at") or time.time()
            job["source_duration"] = src_dur
            job["trim_start"] = ts
            job["trim_end"] = te
            job["trim_path"] = use_src if use_src != src_path else ""
            job["src_path"] = src_path
            steps = _ensure_commentary_steps(job)
            steps[0]["status"] = "running"
            steps[0]["detail"] = f"源视频: {Path(src_path).name}" + (
                f"（已裁剪 {ts:.0f}~{te:.0f}s）" if use_src != src_path else "")
            steps[0]["updated_at"] = time.time()

        try:
            proc = subprocess.Popen(
                args, cwd=str(COMMENTARY_DIR),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=COMMENTARY_RT.env(),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"解说管线无法启动：{exc}") from exc

        assert proc.stdout is not None
        for line in proc.stdout:
            _append(line)
        try:
            ret = proc.wait(timeout=COMMENTARY_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            tail = "\n".join(last_lines[-20:]) or "无输出"
            raise RuntimeError(
                f"解说管线超过 {COMMENTARY_TIMEOUT_SECONDS} 秒未完成，已终止。\n最近输出：\n{tail}"
            )
        if ret != 0:
            tail = "\n".join(last_lines[-20:]) or "无输出"
            raise RuntimeError(f"解说管线退出码 {ret}。\n最近输出：\n{tail}")

        if script_only:
            # --script-only 模式：只出了 script.json，不生成成片
            script_path = _commentary_root("work") / f"{base}.script.json"
            if not script_path.exists():
                tail = "\n".join(last_lines[-20:]) or "无输出"
                raise RuntimeError(f"解说管线执行成功但未找到脚本文件：{script_path}\n输出：\n{tail}")
            with _commentary_lock:
                job = commentary_jobs[job_id]
                _ensure_commentary_steps(job)
                now = time.time()
                # --script-only 只跑到「生成 AI 解说词」为止：配音/渲染尚未执行，
                # 绝不能标 done（否则 UI 全绿、用户误以为成片已生成）。
                _DONE_IN_SCRIPT_ONLY = ("准备输入文件", "转写视频语音", "生成 AI 解说词")
                for s in job["steps"]:
                    if s["name"] in _DONE_IN_SCRIPT_ONLY:
                        s["status"] = "done"
                        s["updated_at"] = now
                    elif s["name"] in ("合成 AI 配音", "渲染成片"):
                        s["status"] = "pending"
                        s["detail"] = "等待脚本审核通过后执行"
                        s["updated_at"] = now
                    elif s["name"] == "完成":
                        s["status"] = "pending"
                        s["detail"] = "解说词已生成，请先审核脚本再渲染成片"
                        s["updated_at"] = now
                _commentary_log(job, "解说词已生成，等待人工审核后再渲染成片")
                job.update(status="script_ready", script_path=str(script_path), output_path="")
        else:
            # 成片命名：<base>_成片.mp4 或 <base>_竖屏成片.mp4
            candidates = sorted(
                (p for p in out_dir.glob(f"{base}*.mp4") if p.name != in_file.name),
                key=lambda p: p.stat().st_mtime, reverse=True,
            )
            out = next(iter(candidates), None)
            if not out:
                tail = "\n".join(last_lines[-20:]) or "无输出"
                raise RuntimeError(f"解说管线执行成功但未找到成片。process.py 输出：\n{tail}")
            with _commentary_lock:
                job = commentary_jobs[job_id]
                _ensure_commentary_steps(job)
                now = time.time()
                for s in job["steps"]:
                    if s["status"] in ("pending", "running"):
                        s["status"] = "done"
                        s["updated_at"] = now
                job.update(status="completed", output_path=str(out))
    except Exception as exc:  # noqa: BLE001
        _commentary_mark_error(job_id, str(exc))
        with _commentary_lock:
            commentary_jobs.setdefault(job_id, {})["status"] = "failed"
            commentary_jobs[job_id]["error"] = str(exc)[:800]
        logger.exception("解说任务 %s 失败", job_id)


def _commentary_run_http(job_id: str, src_path: str, vertical: bool, voice: str, mode: str | None = None, commentary_type: str = "deep_hl", highlight_source: str = "ai", intro_highlight: bool = False, skip_intro_outro: bool = False, no_narrate_intro_outro: bool = True, retain_pct: float | None = None, web: bool = False, one_click: bool = False, style: str = "none") -> None:
    """HTTP 模式：把已下载视频 POST 给独立解说 worker，轮询取回成片到主站本地。"""
    endpoint = COMMENTARY_ENDPOINT
    headers = {"X-Worker-Token": COMMENTARY_TOKEN} if COMMENTARY_TOKEN else {}
    now = time.time()
    http_steps = [
        {"name": "上传视频到 worker", "status": "pending", "detail": "", "created_at": now, "updated_at": now},
        {"name": "worker 渲染中", "status": "pending", "detail": "", "created_at": now, "updated_at": now},
        {"name": "下载成片", "status": "pending", "detail": "", "created_at": now, "updated_at": now},
        {"name": "完成", "status": "pending", "detail": "", "created_at": now, "updated_at": now},
    ]
    with _commentary_lock:
        job = commentary_jobs.setdefault(job_id, {})
        job["steps"] = http_steps
        job["logs"] = []

    def _set_http_step(idx: int, status: str, detail: str = "") -> None:
        with _commentary_lock:
            steps = commentary_jobs.get(job_id, {}).get("steps", [])
            now2 = time.time()
            for i, s in enumerate(steps):
                if i < idx and s["status"] == "pending":
                    s["status"] = "done"
                    s["updated_at"] = now2
            if 0 <= idx < len(steps):
                steps[idx]["status"] = status
                steps[idx]["detail"] = detail[:200]
                steps[idx]["updated_at"] = now2

    def _http_log(line: str) -> None:
        with _commentary_lock:
            job = commentary_jobs.get(job_id)
            if not job:
                return
            ts = time.strftime("%H:%M:%S", time.localtime())
            job.setdefault("logs", []).append(f"{ts}  {line.strip()}")
            if len(job["logs"]) > 200:
                job["logs"][:] = job["logs"][-200:]

    try:
        _set_http_step(0, "running", f"上传 {Path(src_path).name}")
        with open(src_path, "rb") as fh:
            data = {
                "vertical": "true" if vertical else "false",
                "voice": voice,
                "commentary_type": commentary_type,
                "highlight_source": highlight_source,
                "intro_highlight": "true" if intro_highlight else "false",
                "skip_intro_outro": "true" if skip_intro_outro else "false",
                "no_narrate_intro_outro": "true" if no_narrate_intro_outro else "false",
                "web": "true" if web else "false",
                "one_click": "true" if one_click else "false",
                "style": style or "none",
            }
            if retain_pct is not None:
                data["retain_pct"] = str(retain_pct)
            if mode:
                data["mode"] = mode
            resp = requests.post(
                f"{endpoint}/render",
                files={"video": (f"{job_id}.mp4", fh, "video/mp4")},
                data=data,
                headers=headers,
                timeout=600,
            )
        if resp.status_code != 200:
            raise RuntimeError(f"解说 worker /render 返回 {resp.status_code}: {resp.text[:400]}")
        wjob = resp.json().get("job_id")
        if not wjob:
            raise RuntimeError("解说 worker 未返回 job_id")
        _set_http_step(0, "done", "已提交渲染任务")
        _http_log(f"worker job_id: {wjob}")

        _set_http_step(1, "running", f"worker job: {wjob}")
        deadline = time.time() + COMMENTARY_TIMEOUT_SECONDS
        while time.time() < deadline:
            time.sleep(5)
            st = requests.get(f"{endpoint}/status/{wjob}", headers=headers, timeout=30).json()
            status = st.get("status")
            _http_log(f"worker status: {status}")
            if status == "completed":
                break
            if status == "failed":
                raise RuntimeError("解说 worker 渲染失败: " + str(st.get("error", ""))[:600])
        else:
            raise RuntimeError("解说 worker 渲染超时（超过 VDL_COMMENTARY_TIMEOUT）")
        _set_http_step(1, "done", "worker 渲染完成")

        _set_http_step(2, "running", "正在下载成片")
        fr = requests.get(f"{endpoint}/file/{wjob}", headers=headers, stream=True, timeout=(10, 600))
        if fr.status_code != 200:
            raise RuntimeError(f"解说 worker /file 返回 {fr.status_code}")
        out_path = COMMENTARY_LOCAL_OUTPUT / f"{job_id}.mp4"
        downloaded = 0
        with open(out_path, "wb") as o:
            for chunk in fr.iter_content(1024 * 1024):
                if chunk:
                    o.write(chunk)
                    downloaded += len(chunk)
        _set_http_step(2, "done", f"已下载 {downloaded / 1024 / 1024:.1f} MB")

        _set_http_step(3, "done", "成片已就绪")
        with _commentary_lock:
            commentary_jobs[job_id].update(status="completed", output_path=str(out_path))
    except Exception as exc:  # noqa: BLE001
        _set_http_step(1 if http_steps[0]["status"] == "done" else 0, "error", str(exc)[:200])
        with _commentary_lock:
            commentary_jobs.setdefault(job_id, {})["status"] = "failed"
            commentary_jobs[job_id]["error"] = str(exc)[:800]
        logger.exception("解说任务 %s 失败(http 模式)", job_id)


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        demoted = store.purge_expired()
        if demoted:
            # B5：完成任务到期不再是整条删除，而是降级为历史条目（文件清掉、记录保留）
            logger.info("已将 %s 个到期完成任务转为历史条目（文件已清理）", demoted)
        try:
            store.flush()   # 兜底：节流窗口内没再被触碰的脏任务状态也落盘（B2）
        except Exception:
            logger.exception("任务状态落盘失败")
        _cleanup_orphan_upload_parts()
        _cleanup_merged_upload_sources()
        _purge_conversions_dir()


def _cleanup_orphan_upload_parts(max_age: float = 24 * 3600) -> int:
    """清理断传/取消遗留的孤儿分片（up_*.pNNNN），避免 UPLOAD_TMP 无限堆积。
    正常路径：finish 合并后分片即删；>24h 仍残留视为上传中断（断网/关页）。"""
    n = 0
    try:
        for p in UPLOAD_TMP.glob("up_*.p*"):
            try:
                if time.time() - p.stat().st_mtime > max_age:
                    p.unlink(missing_ok=True)
                    n += 1
            except OSError:
                pass
    except OSError:
        pass
    if n:
        logger.info("已清理 %s 个孤儿上传分片", n)
    return n


def _cleanup_merged_upload_sources(max_age: float = 2 * 3600) -> int:
    """清理为「重新编辑/重新转码免重传」保留的合并源文件（UPLOAD_TMP 下 up_<hex>.<ext>）。

    2026-09-29：/api/upload-chunk/finish 改为 src_is_temp=False 保留源文件 2h，
    期间 /api/convert/reconvert 可按新格式免重传重转；本函数是保留期的兜底清理，
    防止 UPLOAD_TMP 无限堆积。分片（up_*.pNNNN）不归这里管（见 _cleanup_orphan_upload_parts）。
    """
    n = 0
    freed = 0
    part_re = re.compile(r"\.p\d+$")
    try:
        for p in UPLOAD_TMP.iterdir():
            if not p.is_file() or part_re.search(p.name):
                continue
            try:
                st = p.stat()
                if time.time() - st.st_mtime > max_age:
                    freed += st.st_size
                    p.unlink(missing_ok=True)
                    n += 1
            except OSError:
                pass
    except OSError:
        pass
    if n:
        logger.info("已清理 %s 个过期上传源文件（释放 %s MB）", n, int(freed / 1024 / 1024))
    return n


def _purge_conversions_dir(max_age: float = 7 * 24 * 3600) -> int:
    """清理 CONVERT_DIR 下早于 max_age 秒的转换产物（用户下载后过期）。
    Railway ephemeral 磁盘有限、retention 默认关闭，此清理是「转码 ENOSPC」的主要防线。
    只清 conversions 子目录，不动 downloads 主目录（用户的下载资源）。"""
    n = 0
    freed = 0
    try:
        for p in CONVERT_DIR.iterdir():
            if not p.is_file():
                continue
            try:
                st = p.stat()
                if time.time() - st.st_mtime > max_age:
                    freed += st.st_size
                    p.unlink(missing_ok=True)
                    n += 1
            except OSError:
                pass
    except OSError:
        pass
    if n:
        logger.info("已清理 %s 个过期转换产物（释放 %s MB）", n, int(freed / 1024 / 1024))
    return n


async def _auto_update_ytdlp() -> None:
    """后台自动更新 yt-dlp 解析器（带 7 天冷却，避免每次部署都拉 PyPI）。

    平台反爬频繁改版，固定版本会逐步失效；此任务在启动后异步下载最新版
    yt-dlp 到 ~/.videodownloader/yt_dlp（bootstrap 下次启动优先使用），
    更新仅替换解析逻辑、不触碰登录态。失败静默（不影响主服务）。
    """
    import asyncio as _asyncio
    import random as _random
    try:
        # 冷却标记：~/.videodownloader/ytdlp_last_check 记录上次成功更新的日期
        _mark = Path.home() / ".videodownloader" / "ytdlp_last_check"
        try:
            if _mark.exists():
                age_days = (time.time() - _mark.stat().st_mtime) / 86400
                if age_days < 7:
                    return  # 7 天内已检查过
        except OSError:
            pass
        # 启动后延迟 60~180s（随机抖动，避开冷启动高峰）
        await _asyncio.sleep(_random.randint(60, 180))
        # 用线程跑阻塞的 PyPI 下载（requests 同步）
        result = await _asyncio.to_thread(ydlp_update.update)
        if result.get("updated"):
            # 2026-10-30 护栏：新 yt-dlp 必须能被**当前解释器**导入才算更新成功。
            # 今晚事故复盘：更新器拉到要求 Python≥3.11 的新版 yt-dlp 存进
            # ~/.videodownloader/yt_dlp 影子目录，而服务 venv 是 3.10 ⇒ 运行中进程
            # 无恙、下次重启必崩（ImportError 崩溃循环，2026-10-30 02:35 实际发生）。
            import subprocess as _sp
            import sys as _sys
            _chk = _sp.run(
                [_sys.executable, "-c",
                 "import sys; sys.path.insert(0, r'%s'); import yt_dlp"
                 % (Path.home() / ".videodownloader" / "yt_dlp")],
                capture_output=True, timeout=120)
            if _chk.returncode != 0:
                _bad = Path.home() / ".videodownloader" / "yt_dlp"
                _quar = Path.home() / ".videodownloader" / (
                    "yt_dlp.incompatible-quarantine-%s" % time.strftime("%Y%m%d-%H%M%S"))
                try:
                    _bad.rename(_quar)
                    logger.warning(
                        "[ytdlp] 新版与当前 Python %s 不兼容，已隔离到 %s（继续用 pip 侧旧版，本次不写成功标记）",
                        _sys.version.split()[0], _quar.name)
                except OSError:
                    logger.warning(
                        "[ytdlp] 新版与当前 Python 不兼容且隔离失败，需人工处理 ~/.videodownloader/yt_dlp")
                return
            logger.info("[ytdlp] 自动更新成功 -> %s（重启后生效）", result.get("version"))
            try:
                _mark.write_text(str(time.time()))
            except OSError:
                pass
        elif result.get("error"):
            logger.warning("[ytdlp] 自动更新失败: %s", result.get("error"))
        else:
            logger.info("[ytdlp] 已是最新版本 %s", result.get("version"))
    except Exception as e:  # noqa: BLE001
        logger.info("[ytdlp] 自动更新跳过: %s", str(e)[:100])


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    # 启动即打印运行实例的真实代码版本，便于在 Railway Deploy Logs 首行确认
    # 线上部署到哪次提交（Railway 的部署 ID 不是 git SHA，容易误判「没更新」）。
    try:
        import subprocess
        _root = str(Path(__file__).resolve().parent.parent)
        _sha = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=_root,
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        _sha = ''
    logger.info(
        "启动版本 git_sha=%s railway_commit=%s railway_deployment=%s railway_branch=%s",
        _sha,
        os.environ.get('RAILWAY_GIT_COMMIT_SHA', ''),
        os.environ.get('RAILWAY_DEPLOYMENT_ID', ''),
        os.environ.get('RAILWAY_GIT_BRANCH', ''),
    )
    orphans = store.purge_orphans()
    if orphans:
        logger.info("已清理 %s 个上次运行遗留的任务目录", orphans)
    # 启动时立即清理过期转换产物：Railway ephemeral 磁盘有限，retention 默认关闭，
    # 历史累积可能已撑满磁盘导致 ffmpeg 写输出 ENOSPC → rc=9 退出
    _purge_conversions_dir()
    # 桌面版种子下载：启动 libtorrent session（libtorrent 缺失时内部为空操作）
    if TORRENT_ENABLED and torrent_mod.available():
        try:
            torrent_manager.start()
        except Exception:
            logger.exception("启动种子下载管理器失败")
    cleaner = asyncio.create_task(_cleanup_loop())
    # 公共 Cookie 池后台探测（chrqj 等需登录态的站，避免公共池静默失效）
    try:
        _start_cookie_pool_watchdog()
    except Exception:
        logger.exception("启动公共 Cookie 池探测失败")
    # 启动预热：若公共池为空（容器重建/重启后持久卷被清空），异步召唤 VPS 守护进程
    # 立即补推一次 Cookie，使池在 ~30s 内补满，无需人工干预。
    try:
        _warm_cookie_pool()
    except Exception:
        logger.exception("启动公共 Cookie 池预热失败")
    # 反向 WebSocket 隧道本地代理（127.0.0.1:18889）：ECS 主动连入后，
    # Railway 上的 yt-dlp 经此代理的流量被透明转发到国内 cn_proxy，绕开跨境入站瓶颈
    try:
        asyncio.create_task(_cn_tunnel.start_cn_tunnel_proxy())
    except Exception:
        logger.exception("启动反向隧道本地代理失败")
    # 经反向隧道主动从 VPS 拉取 Cookie 写入公共池（替代 VPS 主动推，规避跨境链路抖动）
    try:
        _start_cookie_pull_from_vps()
    except Exception:
        logger.exception("启动 Cookie 隧道拉取任务失败")
    # yt-dlp 解析器自动更新（后台，7 天冷却）：平台反爬天天变，固定版本会逐步失效。
    # 更新后需重启才生效（bootstrap 已加载的旧版无法热替换），因此这里只下载新版
    # 到 ~/.videodownloader/yt_dlp，下次部署/重启时 bootstrap 自动优先使用。
    try:
        asyncio.create_task(_auto_update_ytdlp())
    except Exception:
        logger.exception("启动 yt-dlp 自动更新任务失败")
    # 图片 AI 去水印（LaMa ONNX）预热：后台线程提前下载权重，避免首个 AI 请求卡在
    # ~107MB 模型下载。失败仅记日志，不影响启动（运行时首次请求仍会按需下载）。
    try:
        if dwc_ai_available:
            import threading as _th

            def _prewarm_lama():
                try:
                    import dewatermark_ai as _dwai
                    _dwai._ensure_model()
                    logger.info("AI 去水印模型预热完成")
                except Exception:
                    logger.warning("AI 去水印模型预热失败（首次请求将按需下载）", exc_info=True)

            _th.Thread(target=_prewarm_lama, name="vdl-lama-prewarm", daemon=True).start()
    except Exception:
        logger.exception("启动 AI 去水印预热线程失败")
    # 启动即确保超级用户标记（后台管理面板授权：admin.json / VDL_ADMIN_IDENTIFIER / 首个注册账号）。
    # ⚠️ B 修复（2026-10-04）：网页端此前漏调此函数，导致 VDL_ADMIN_IDENTIFIER 设了也不生效，
    #    网页端永远显示"普通用户"。与桌面端对齐（app.py:2021）。幂等、绝不降级。
    try:
        from auth_store import ensure_superusers
        # 安全审计 2026-10-13（F-03）：启动钩子没有请求上下文 ⇒ 不许无名单兜底提权，
        # 公网超管只能来自 VDL_ADMIN_IDENTIFIER / admin.json 显式名单
        # （与桌面端 app 仓同口径）。
        ensure_superusers(allow_bootstrap=False)
    except Exception:
        logger.exception("确保超级用户失败")
    yield
    cleaner.cancel()
    if TORRENT_ENABLED:
        try:
            torrent_manager.stop()
        except Exception:
            pass
    executor.shutdown(wait=False, cancel_futures=True)
    prober.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="视频工坊", version="1.0.0", lifespan=lifespan)

# 跨域 CORS：公开站默认允许所有来源（allow_credentials=False，不携凭证，安全）。
# 部署者可通过 VDL_ALLOW_ORIGINS 或双节点 PEER_ENDPOINT 限定具体来源；否则回退 "*"。
# 注意：中间件必须始终注册，否则跨域自托管（含带 X-Subscription-Key 的订阅请求）
# 的浏览器预检会被 405 拒绝。
_cors_origins = ALLOW_ORIGINS if ALLOW_ORIGINS else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    # Range 必须放行：网页版分片下载（/api/media/proxy）靠 `Range: bytes=a-b` 切段，
    # 而解析落在对端节点时页面与 API 不同源 ⇒ 带 Range 的 fetch 会先发预检 OPTIONS，
    # 预检里没有 Range 就会被浏览器直接拦掉（症状：海外视频一律「加速下载不可用」）。
    # 虽然新版 Fetch 规范已把 Range 列为可豁免的自列表头，但 Safari/WKWebView 支持滞后，
    # 显式放行才是稳的。仅放宽 GET 上这一个头，不放宽来源与方法。
    # Authorization（2026-09-28）：下载配额墙上线后前端默认携带登录 bearer（V1 免费
    # 10 次/日按账号计），海外站请求跨域打 global 节点时预检必须放行该头，
    # 否则海外下载直接被浏览器拦死（比 Range 更致命）。
    allow_headers=["Content-Type", "X-Subscription-Key", "X-Api-Key", "X-Device-Id",
                   "Range", "Authorization"],
)


# ── 本机直连判定（安全审计 2026-10-13，F-06）──────────────────────────────── #
# 🔴 旧实现 `routers/core.py::_is_local_request` 把「取不到来源地址（host == \"\"）」
#    也算本机 ——「无法确定来源」被当成「可信」，是最不该有的默认；且 `routers/auth.py`
#    `_is_loopback` 只比 `request.client.host`，而 uvicorn 默认开 `--proxy-headers`，
#    `scope["client"]` 已被 X-Forwarded-For 重写 ⇒ 客户端自填 XFF 即可自称本机。
#
# 修复：抽出共用真源 `_is_local_direct` = socket 对端环回 **且** 未携带任何代理头。
#   线上经 nginx + CF 隧道时 nginx 的 $remote_addr 恰为 127.0.0.1，只比对端地址会把
#   公网请求误判成本机；带任何代理头 ⇒ 一律不按本机对待。判据不依赖反代语义、可离线验证。
#   （与桌面端 app 仓 `app._is_local_direct` 同源同语义。）
_PROXY_HEADER_NAMES = (
    "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "x-forwarded-port",
    "x-real-ip", "forwarded", "true-client-ip", "x-client-ip",
    "x-cluster-client-ip", "x-original-forwarded-for",
    "cf-connecting-ip", "cf-ray", "cf-ipcountry",
)


def _has_proxy_headers(request) -> bool:
    """请求是否携带任何反代 / 隧道头（带 → 说明不是本机直连）。"""
    try:
        for name in _PROXY_HEADER_NAMES:
            if (request.headers.get(name) or "").strip():
                return True
    except Exception:
        return True          # 取不到头就按「经过代理」从严处理
    return False


def _is_local_direct(request) -> bool:
    """是否为本机直连（socket 对端环回 **且** 未携带任何代理头）。

    这是全仓「本机免鉴权」判定的唯一真源，路由层不要再自己写 client.host 比较。
    """
    if request is None or _has_proxy_headers(request):
        return False
    try:
        host = (request.client.host if request.client else "") or ""
    except Exception:
        return False
    return host in ("127.0.0.1", "::1", "localhost")


# 安全审计 2026-10-13（F-11 / 移植自 app 仓 R-02）：跨源 CSRF 兜底校验的来源白名单。
# 全局 CORS 默认对任意 Origin 放行（浏览器扩展 content script 从任意 https 页面回传嗅探项
# 所必需，见 routers/sniffer.py），因此对**仅前端同源使用**的敏感端点（改配置 / 管理动作类），
# 必须额外做来源校验，防止恶意网页跨源触发写盘 / 改配置 / 外传用户内容：
#   - 无 Origin 头 → 放行（同源前端、本机直连 curl）；
#   - Origin 命中本机 / 扩展白名单 → 放行；
#   - 其余（含 "null" 的沙箱 iframe、任意外部站点）→ 拒绝。
_EXT_ORIGIN_RE = re.compile(r"^(chrome|moz|safari-web|ms-browser)-extension://[^\s]*$")
_LOCAL_ORIGIN_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$")
# 站点自身域名（含子域）视作同源，不误判为跨站。部署在 hanyuxz.top（含 share/download/vdl-check 等子域）。
_SITE_ORIGIN_RE = re.compile(r"^https?://([a-z0-9-]+\.)*hanyuxz\.top(:\d+)?$", re.IGNORECASE)
# VDL_ALLOW_ORIGINS 中显式声明的来源（如 hanyuxz.top）等同站点自身，一并放行。
_ALLOWED_SITE_ORIGINS = {o.rstrip("/").lower() for o in ALLOW_ORIGINS}


def _cross_site_rejected(request) -> bool:
    """判断是否应拒绝该跨源请求（CSRF 兜底）。配合全局 `allow_origins=["*"]` 使用。"""
    try:
        origin = (request.headers.get("origin") or "").strip()
    except Exception:
        return False
    if not origin:
        return False
    origin_n = origin.rstrip("/").lower()
    return not (_EXT_ORIGIN_RE.match(origin)
                or _LOCAL_ORIGIN_RE.match(origin)
                or _SITE_ORIGIN_RE.match(origin)
                or origin_n in _ALLOWED_SITE_ORIGINS)


# 前端缓存策略（2026-10-22 重写，修「网页版首屏白屏数秒」）：
# 根因链：ECS 上 index.html 的 `__BUILD_FP__` 占位符从未被替换 → 浏览器 isBuild=false
# → app.js/styles.css 带 `?v=dev+时间戳` ⇒ URL 每次都不同 → 浏览器与 CF 边缘永远
# 无法缓存（cf-cache-status: BYPASS）→ 每次访问都从 3Mbps 出口的源站全量拉
# （app.js br 后仍 161KB，实测 TTFB 6.5s）→ 长时间白屏。
# 修复：① 服务端出 HTML 时注入真实指纹（app.js/styles.css 的 mtime+size，
#   scp 热更后自动变化，无需重启）；② HTML 仍 no-store（指纹必须每次新鲜）；
#   ③ JS/CSS 改 public, max-age=30 天, immutable —— URL 指纹变化即缓存击穿，
#   2026-09-29「CF 边缘 2h 旧文件」事故的前提（URL 不变）已不复存在。
def _static_fp() -> str:
    """前端静态资源指纹：mtime+size 组合，scp 覆盖文件后必然改变。

    ⚠️ WEB_DIR 在测试/部分环境可能是 str ⇒ 必须 Path() 包一层；
    任何异常都回落 "dev"（占位符不替换 → 前端走 Date.now() URL，
    只是没缓存收益，绝不能让 HTML 500）。
    """
    try:
        base = Path(WEB_DIR)
        parts = []
        for name in ("app.js", "styles.css"):
            st = (base / name).stat()
            parts.append(f"{int(st.st_mtime):x}{st.st_size:x}")
        return "-".join(parts) or "dev"
    except Exception:  # noqa: BLE001 — 指纹失败绝不影响页面返回
        return "dev"


@app.middleware("http")
async def _no_cache_frontend(request: Request, call_next):
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".htm")):
        # HTML 注入真实指纹（替换 index.html 里的 __BUILD_FP__ 占位符）
        ctype = resp.headers.get("content-type", "")
        if "text/html" in ctype and not resp.headers.get("content-encoding"):
            body = b""
            async for chunk in resp.body_iterator:
                body += chunk
            body = body.replace(b"__BUILD_FP__", _static_fp().encode())
            headers = dict(resp.headers)
            headers["content-length"] = str(len(body))
            headers["cache-control"] = "no-store, max-age=0"   # HTML 必须每次新鲜，否则指纹不刷新
            return Response(content=body, status_code=resp.status_code,
                            headers=headers, media_type=ctype)
        resp.headers["Cache-Control"] = "no-store, max-age=0"
    elif path.endswith((".js", ".css")):
        # URL 指纹化（index.html 注入的 ?v=）⇒ 可安全长缓存；CF 边缘也会 HIT
        resp.headers["Cache-Control"] = "public, max-age=2592000, immutable"
    return resp
logger.info("CORS 已开启，允许来源：%s", ", ".join(_cors_origins))


# ---- 海外请求兜底转发（2026-09-27）--------------------------------------- #
# 背景：双节点模式下，海外站（YouTube 等）的解析/下载请求本应由**浏览器直连**
# 海外对端（见 web/app.js 的 baseFor()）。但页面一旦长期不关，内存里的
# node.peer 会停留在「当时」的认知 —— 服务器侧后来才配上对端时，老页面仍以为
# 自己是「本机直连」，于是把 YouTube 请求发给国内节点。国内节点到不了 YouTube
# （TLS SNI 被重置），用户只看到 `[Errno 104] Connection reset by peer`
# 这种既看不懂、也解决不了的错误（2026-09-27 用户报障的原始现象）。
#
# 前端修复（web-dev 30ef052：对端落盘 / 定期刷新 / 失败自愈）只能救
# **重新加载过**的页面；已经开着的旧标签页无论如何都救不了 —— 唯一的办法是
# 节点侧兜底：判定为海外站就把请求原样转发给对端，把对端响应原样回给浏览器。
# 这样旧页面无需知道对端存在也能正常工作。
#
# 只在国内节点（VDL_REGION=cn 且有 VDL_PEER_ENDPOINT）生效，单节点部署零影响。
_PEER_FWD_POST_PATHS = ("/api/resolve", "/api/download", "/api/batch")
_PEER_FWD_TASK_PREFIX = "/api/tasks/"
# 这两类响应不能缓冲：大文件（几十 MB）与 SSE 事件流。只回 307，让浏览器
# 直连对端取件 —— 顺带避免把视频流经国内节点中转（跨境带宽翻倍）。
_PEER_FWD_REDIRECT_SUFFIX = ("/file", "/events")


def _urls_in_body(raw: bytes) -> list[str]:
    """从请求体里取出待处理的链接（/api/resolve 的 url、/api/batch 的 urls）。"""
    try:
        data = json.loads(raw.decode("utf-8", "ignore") or "{}")
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    for key in ("url", "urls"):
        val = data.get(key)
        if isinstance(val, str):
            out.append(val)
        elif isinstance(val, list):
            out.extend([v for v in val if isinstance(v, str)])
    return out


def _has_overseas_url(raw: bytes) -> bool:
    """请求体里是否含「非国内站」链接（决定是否该转给海外对端）。

    判据复用 platforms.is_china_host（与前端 baseFor / 平台清单同一份
    china_domains，避免两套名单漂移）。任一条海外链接即转发：混合批次转给
    对端后，国内站由对端按 VDL_WORKER_URL 回派，不会丢任务。
    """
    for u in _urls_in_body(raw):
        try:
            host = (urlparse(u).hostname or "").lower()
        except Exception:
            continue
        if host and not is_china_host(host):
            return True
    return False


async def _peer_forward(request: Request, path: str, body: bytes) -> Response:
    """把当前请求原样转发给海外对端，并原样返回其响应。

    对端挂在 Cloudflare 后面，Python-urllib 默认 UA 会被 CF 规则 1010 拒，
    故必须伪装浏览器 UA（与 _relay_cookie_to_peer_if_needed 同一处理）。
    转发失败（对端不可达）返回 502 + 明确 category，绝不再抛「Connection reset」
    这种用户无法处置的原生异常文案。
    """
    target = PEER_ENDPOINT + path
    if request.url.query:
        target += "?" + request.url.query
    if path.endswith(_PEER_FWD_REDIRECT_SUFFIX):
        # 307 保留方法与查询串；浏览器直连对端取件/订阅事件流
        return Response(status_code=307, headers={"Location": target,
                                                  "Cache-Control": "no-store"})

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
    ctype = request.headers.get("content-type")
    if ctype:
        headers["Content-Type"] = ctype

    def _do() -> tuple[int, bytes, str]:
        req = urllib.request.Request(target, data=(body or None), headers=headers,
                                     method=request.method)
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, r.read(), (r.headers.get("Content-Type") or "application/json")
        except urllib.error.HTTPError as e:      # 解析失败等业务错误：照原样透传
            return e.code, e.read(), (e.headers.get("Content-Type") or "application/json")

    try:
        status, payload, media = await run_in_threadpool(_do)
    except Exception as e:  # noqa: BLE001
        logger.warning("[peer-fwd] %s 转发对端失败: %s", path, str(e)[:160])
        return JSONResponse(
            status_code=502,
            content={
                "error": "海外节点暂时不可达",
                "hint": "海外线路（香港节点）连接失败，请稍后重试；国内平台不受影响。",
                "category": "peer_unreachable",
            },
        )
    logger.info("[peer-fwd] %s -> %s status=%s", path, PEER_ENDPOINT, status)
    return Response(content=payload, status_code=status, media_type=media.split(";")[0].strip())


@app.middleware("http")
async def _peer_overseas_fallback(request: Request, call_next):
    """国内节点兜底：把海外站请求（含旧页面发来的）转给海外对端。

    仅当「本节点是国内节点 + 配了对端」时启用：
    - POST /api/resolve|/api/download|/api/batch：请求体含非国内站链接 → 转发；
    - /api/tasks/*：先走本节点，本节点没有该任务（404）则转发 —— 旧页面把对端
      的 task_id 打到本节点时，进度轮询/删除/暂停照样能用。
    """
    if not PEER_ENDPOINT or NODE_REGION != "cn" or request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    if request.method == "POST" and path in _PEER_FWD_POST_PATHS:
        raw = await request.body()          # 读一次即缓存，call_next 仍拿得到
        if _has_overseas_url(raw):
            return await _peer_forward(request, path, raw)
        return await call_next(request)
    if path.startswith(_PEER_FWD_TASK_PREFIX):
        resp = await call_next(request)
        if resp.status_code == 404:
            return await _peer_forward(request, path, b"")
        return resp
    return await call_next(request)


# --------------------------------------------------------------------------- #
# 可选 API 鉴权（对外给用户使用时建议开启）
# 设置 VDL_API_TOKEN 后，除 /api/nodes（前端要先拿到 authRequired 才能引导输入 token）
# 与静态资源外，所有 /api/* 请求必须携带正确 token，否则 401。
# token 取自 Authorization: Bearer <t> 或 X-Api-Key: <t>。
# 未设置则维持当前无鉴权行为（个人本机/私用）。
# --------------------------------------------------------------------------- #
API_TOKEN = (os.environ.get("VDL_API_TOKEN") or "").strip()
AUTH_REQUIRED = bool(API_TOKEN)


class _ApiTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not AUTH_REQUIRED:
            return await call_next(request)
        path = request.url.path
        # 静态资源与节点信息接口放行：前端需先读 authRequired 才能引导用户输入 token
        if not path.startswith("/api/") or path == "/api/nodes":
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        token = ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        else:
            token = (request.headers.get("X-Api-Key") or "").strip()
        if token and token == API_TOKEN:
            return await call_next(request)
        return JSONResponse(
            status_code=401,
            content={"error": "缺少或错误的 API Token", "hint": "服务端已启用 VDL_API_TOKEN，请输入访问令牌"},
        )


app.add_middleware(_ApiTokenMiddleware)


# --------------------------------------------------------------------------- #
# 请求模型 & 错误处理
# --------------------------------------------------------------------------- #

class ResolveRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    cookie: str = Field(default="", max_length=8192)
    proxy: str = Field(default="", max_length=256)


class DownloadRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    # 2026-10-19：前端解析后已知视频标题，随下载请求带上 ⇒ 用量明细记「真名」。
    # 不带（旧客户端 / 批量）时后端回落取 URL 尾段（如 BV 号），后台仍可定位。
    # 与桌面端同口径（max_length=256）；前端发送前自行截断，避免 422。
    title: str = Field(default="", max_length=256)
    quality: str = Field(default=downloader.BEST_KEY, max_length=16)
    cookie: str = Field(default="", max_length=8192)
    proxy: str = Field(default="", max_length=256)
    # 提取文案："" 不提取 / "spoken" 口播文案 / "description" 发布简介 / "both" 两者
    extract_script: str = Field(default="", max_length=16)
    # 精确指定 CDN 源（如腾讯 hd-1/shd-3），空则按清晰度自适应；由「测速选源」自动填入
    format_id: str = Field(default="", max_length=64)
    # 下载加速：m3u8 分片并行段数（0=用默认 32，范围 1-64）。腾讯等单连接限速站提高可线性提速
    concurrent_fragments: int = Field(default=0, ge=0, le=64)
    # 下载器：native（默认，yt-dlp 原生）/ aria2c（外部下载器，需本机已装，缺失自动回退）
    downloader: str = Field(default="native", max_length=16)
    # 在线观看：前端从解析结果传入，存入任务后任务面板可直接打开观看
    play_url: str = Field(default="", max_length=2048)
    # 分离轨（抖音）配套的纯音频轨直链：网页版据此在浏览器内双轨合并保存（零服务端带宽）
    audio_play_url: str = Field(default="", max_length=2048)
    watch_options: list[dict] = Field(default_factory=list)
    is_hls: bool = False


class ConvertRequest(BaseModel):
    """格式转换请求（与 routers/convert.py 共享，故保留在 app.py 公开契约层，不迁入 core）。"""
    task_id: str
    target: str
    resolution: str = "original"


def _require_task(task_id: str, device_id: str = ""):
    """取任务，可选设备归属校验（2026-08-22 设备隔离）。

    🔴 安全修复 2026-10-12（渗透自查 · 中危，与 tasks.py::list_all 同源）：
    旧规则「无归属任务（device_id 为空）对所有设备可见」使任何人都能按 task_id
    读取他人任务详情。现改为严格匹配：请求方与任务的 device_id **都非空且相等**
    才放行；其余一律 404（不返回 403，避免任务 id 可枚举探测）。
    """
    task = store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if not device_id or not task.device_id or task.device_id != device_id:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return task



# 各目标格式默认使用的视频/音频编码器（供自动无损直转判断：源编码一致时 -c copy 零重编码）
_TARGET_VIDEO_CODEC = {
    "mp4": "h264", "mov": "h264", "mkv": "h264", "avi": "h264", "flv": "h264",
    "ts": "h264", "m4v": "h264", "3gp": "h264", "hevc": "hevc",
    "webm": "vp9", "ogv": "vp8", "wmv": "wmv2", "mpeg": "mpeg2video",
}
_TARGET_AUDIO_CODEC = {
    "mp4": "aac", "mov": "aac", "mkv": "aac", "avi": "aac", "flv": "aac",
    "ts": "aac", "m4v": "aac", "3gp": "aac", "hevc": "aac",
    "mp3": "mp3", "m4a": "aac", "aac": "aac", "wma": "wmav2", "mp2": "mp2",
    "wav": "pcm_s16le", "flac": "flac", "ogg": "opus", "opus": "opus",
    "webm": "opus", "ogv": "opus",
}


def _probe_src_codecs(src: str) -> tuple:
    """ffprobe 探测源文件首个视频/音频流的 codec_name（失败返回 ('', '')）。"""
    try:
        fb = str(Path(FFMPEG_BIN).with_name("ffprobe")) if FFMPEG_BIN else ""
        if not fb or not Path(fb).exists():
            fb = shutil.which("ffprobe") or "ffprobe"
        r = subprocess.run([fb, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=codec_name", "-of", "csv=p=0", src],
                           capture_output=True, text=True, timeout=20)
        v = r.stdout.strip()
        r2 = subprocess.run([fb, "-v", "error", "-select_streams", "a:0",
                             "-show_entries", "stream=codec_name", "-of", "csv=p=0", src],
                            capture_output=True, text=True, timeout=20)
        a = r2.stdout.strip()
        return v, a
    except Exception:
        return "", ""


def _run_convert(job_id: str, src: str, target: str, resolution: str,
                bitrate: str = "", audio: bool = True, rotate: int = 0,
                remux: bool = False, src_is_temp: bool = False,
                audio_bitrate: str = "", image_quality: int = 0,
                resize: int = 0, flatten_alpha: bool = True,
                is_image: bool = False) -> None:
    """后台线程：ffmpeg 转码，更新 CONVERT_JOBS 状态。
    新增参数（上传转码用）：bitrate 视频码率、audio 是否保留音轨、
    rotate 竖屏旋转(0/90/180/270)、remux 仅换容器无损(-c copy)。
    audio_bitrate：音频目标的音质档位（如 "320k"），支持音乐转换选码率。
    image_quality/resize/flatten_alpha：图片目标（CONVERT_IMAGE）的质量档、
    长边像素缩放、不支持 alpha 的目标是否白底合成（图片转换 tab 用）。
    is_image：图片转换 tab 显式标记（gif 同名目标在视频 tab 是"视频→动图"、
    在图片 tab 是"图片→gif"，二者参数不同，必须靠此标志分流）。
    src_is_temp：src 是否为上传落盘的临时文件，True 时转码结束（成败都）
    清理，避免 UPLOAD_TMP 无限堆积；task 模式（已下载文件）必须为 False。
    """
    job = CONVERT_JOBS.get(job_id)
    if not job:
        return
    out = None
    try:
        out = Path(job["out_path"])
        audio_only = target in CONVERT_AUDIO
        if target == "webp" and (is_image or target in CONVERT_IMAGE):
            # WebP 特例：部署环境 ffmpeg 多无 libwebp 编码器，改用 Pillow（MIT，可商用）
            # 直接出图（保留 alpha、等比只缩不放），不走 ffmpeg。
            job["stage"] = "转码中"
            job["progress"] = 60
            _convert_image_webp(src, out, image_quality, resize)
            if not out.exists() or out.stat().st_size == 0:
                raise RuntimeError("WebP 转换未产出有效文件")
        else:
            # 2026-08-23：用户 YouTube 1080p H.264+AAC MP4 触发 ffmpeg frame=0/0 立即退出（疑似容器资源受限
            # 让 libx264 init 异常），加 err_detect/fflags 容错（跳过坏帧）+ analyzeduration 充分探测
            cmd = [FFMPEG_BIN, "-y", "-err_detect", "ignore_err", "-fflags", "+discardcorrupt",
                   "-analyzeduration", "10M", "-probesize", "10M", "-i", src]
            # 自动无损直转：源视频/音频编码与目标格式默认编码一致 → -c copy 零重编码（毫秒级），
            # 规避 Railway 容器 libx264 1080p 重编码资源受限（rc=9 frame=0/0 立即退出）
            vcodec, acodec = _probe_src_codecs(src)
            want_v = _TARGET_VIDEO_CODEC.get(target, "")
            want_a = _TARGET_AUDIO_CODEC.get(target, "")
            auto_copy = bool(want_v and vcodec and vcodec == want_v
                             and want_a and acodec and acodec == want_a
                             and rotate == 0 and resolution == "original" and not bitrate and audio)
            if target in CONVERT_IMAGE or (is_image and target == "gif"):
                # 图片：单帧输出，无音轨；质量/缩放/白底合成见 _image_encode_args
                cmd += _image_encode_args(target, image_quality, resize, flatten_alpha)
            elif target == "gif":
                cmd += CONVERT_TARGETS["gif"]
            elif (remux or auto_copy) and rotate == 0:
                # 仅换容器无损复制 / 编码一致自动直转：忽略码率/分辨率/旋转（旋转需滤镜，与 -c copy 不兼容）
                cmd += ["-c", "copy"]
            else:
                # 音频目标 + 指定音质档位 → 动态参数覆盖固定表（128k/192k/256k/320k）
                _aargs = _audio_encode_args(target, audio_bitrate) if audio_only else None
                cmd += _aargs if _aargs else CONVERT_TARGETS[target]
                if not audio_only:
                    vf = []
                    if resolution != "original":
                        h = {"1080": "1080", "720": "720", "480": "480"}.get(resolution)
                        if h:
                            vf.append(f"scale=-2:{h}")
                    if rotate in (90, 180, 270):
                        tf = {90: "transpose=1", 180: "transpose=3", 270: "transpose=2"}[rotate]
                        vf.append(tf)
                    if vf:
                        cmd += ["-vf", ",".join(vf)]
                    if bitrate:
                        cmd += ["-b:v", str(bitrate)]
                    if not audio:
                        cmd += ["-an"]
            cmd.append(str(out))
            job["stage"] = "无损直转" if auto_copy else "转码中"
            job["progress"] = 0

            # 2026-08-23 进度实时修复：原方案依赖 stderr 的 `Duration:` 行解析 total_dur，
            # 但 ffmpeg 在 Railway 容器资源紧张/异常退出时可能未打 Duration 就被卡住，
            # 导致 progress 永远 0、前端卡 30% 给用户「卡死」感。
            # 改为：①ffprobe 独立预探测总时长（稳定可靠，独立于 ffmpeg 转码进程）；
            #      ②ffmpeg 加 -nostats -progress pipe:1 让 stdout 持续输出 `out_time_us=`
            #        （结构化 key=value，默认每 ~0.5s 一组，比 stderr 的 `time=` 更可靠），
            #        主线程读 stdout 算百分比；独立 daemon 线程读 stderr 保留诊断 tail。
            total_dur = 0.0
            try:
                pp = subprocess.run(
                    [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
                    capture_output=True, text=True, timeout=30,
                )
                if pp.returncode == 0 and pp.stdout.strip():
                    total_dur = float(pp.stdout.strip())
            except Exception:
                pass

            stderr_tail: list = []     # 保留尾部 80 行 stderr；转码失败时拼进 error 便于排查
            proc = subprocess.Popen(
                cmd + ["-nostats", "-progress", "pipe:1"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0, text=True,
            )
            # 独立线程读 stderr：保留诊断 tail；主线程专注解析 -progress，避免 stdout/stderr I/O 互相阻塞
            def _drain_stderr():
                try:
                    for line in proc.stderr:
                        stderr_tail.append(line.rstrip())
                        if len(stderr_tail) > 80:
                            stderr_tail.pop(0)
                except Exception:
                    pass
            threading.Thread(target=_drain_stderr, daemon=True).start()

            # 主线程读 stdout 解析 -progress 输出（默认每 ~0.5s 一组 key=value：frame/fps/out_time_us/progress=continue|end）
            _re_out_time_us = re.compile(r"^out_time_us=(-?\d+)")
            for line in proc.stdout:
                line = line.strip()
                m = _re_out_time_us.match(line)
                if m and total_dur > 0:
                    try:
                        cur_s = int(m.group(1)) / 1_000_000
                        pct = max(0, min(99, int(cur_s / total_dur * 100)))
                        if pct > job.get("progress", 0):
                            job["progress"] = pct   # 进度只升不降，避免 ffmpeg 估算波动导致回弹
                    except (ValueError, OverflowError):
                        pass
            proc.wait(timeout=1800)
            if proc.returncode != 0:
                # 过滤 ffmpeg 头部无诊断价值的版本/configuration 行，保留真正的错误/输入/输出信息
                meaningful = [l for l in stderr_tail
                              if l and not l.startswith(('ffmpeg version', '  built with', '  configuration:',
                                                          '  libav', '  libsw', '  libpostproc', '  libavutil'))]
                tail = "\n".join(meaningful[-30:]).strip() or "\n".join(stderr_tail[-10:]).strip() or "(无 stderr)"
                raise RuntimeError(f"ffmpeg 执行失败（rc={proc.returncode}）：\n{tail}")
            if not out.exists() or out.stat().st_size == 0:
                raise RuntimeError("ffmpeg 未产出有效文件")
        # 可选：存入媒体库（DOWNLOAD_DIR 磁盘目录，scan_library 会自动收录）
        if job.get("to_library"):
            # 媒体库文件名带参数前缀：`[格式]原名.目标扩展名`，方便识别来源与参数
            src_stem = Path(job.get("src_name") or out.name).stem or "converted"
            dest_name = f"[{target.upper()}]{src_stem}{out.suffix}"
            dest = DOWNLOAD_DIR / dest_name
            if dest.exists() and dest.resolve() != out.resolve():
                dest = DOWNLOAD_DIR / f"{dest.stem}_{uuid.uuid4().hex[:6]}{out.suffix}"
            shutil.copy2(out, dest)
            try:
                job["library_id"] = library_mod.encode_id(
                    dest.resolve().relative_to(DOWNLOAD_DIR.resolve()).as_posix())
            except Exception:
                job["library_id"] = ""
        job["status"] = "completed"
        logger.info("convert %s done -> %s", job_id, out.name)
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)[:4000]   # 转码错误信息（ffmpeg stderr 尾部 30 行，含真正诊断价值），加大上限便于排查
        logger.warning("convert %s failed: %s", job_id, e)
    finally:
        # 上传临时源文件：转码结束（无论成败）后清理，避免 UPLOAD_TMP 无限堆积
        if src_is_temp and src:
            try:
                _src = Path(src)
                if _src.exists() and (out is None or _src.resolve() != out.resolve()):
                    _src.unlink(missing_ok=True)
            except Exception:
                logger.warning("convert %s cleanup temp src failed: %s", job_id, src)



@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """兜底：任何未被具体 handler 覆盖的异常，向前端返回可读的错误原因。

    默认 FastAPI（debug=False）只会返回笼统的 "Internal Server Error"，
    排查时拿不到真实堆栈。这里把异常类型+消息透传给前端，并写服务端日志，
    让用户（双击启动 App 也能）直接看到 500 的真实成因。
    """
    from fastapi import HTTPException as _HTTPException

    # 先判断，避免把 FastAPI 自身的 HTTPException（402 订阅提示 / 400 参数错误等）当未捕获异常记录
    if isinstance(exc, _HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    logger.exception("未捕获异常 %s %s: %s", request.method, request.url.path, exc)
    # 落入结构化事件日志，闭环「用户报问题我们看不到服务端错误」
    record_event("error", "server_exception", f"{type(exc).__name__}: {exc}", request=request)
    msg = f"{type(exc).__name__}: {str(exc)[:200]}"
    return JSONResponse(
        status_code=500,
        content={
            "error": msg,
            "hint": "服务端未预期错误，请把上方错误信息反馈，或查看服务端日志",
            "detail": msg,
        },
    )


@app.exception_handler(LinkError)
async def handle_link_error(_: Request, exc: LinkError) -> JSONResponse:
    status = 415 if isinstance(exc, UnsupportedPlatformError) else 400
    content = {"error": exc.message, "hint": exc.hint}
    # 诊断增强：把错误分类与脱敏后的上下文透传给前端，便于精准提示与线上排查
    category = getattr(exc, "category", None) or "unknown"
    content["category"] = category
    ctx = getattr(exc, "context", None) or {}
    if ctx:
        content["diag"] = {
            "host": ctx.get("host"),
            "is_china": ctx.get("is_china"),
            "is_hardened": ctx.get("is_hardened"),
            "cookie_source": ctx.get("cookie_source"),
            "proxy_used": ctx.get("proxy_used"),
            "is_cloud": ctx.get("is_cloud"),
        }
    return JSONResponse(status_code=status, content=content)


@app.exception_handler(downloader.ResolveRestricted)
async def handle_restricted(_: Request, exc: "downloader.ResolveRestricted") -> JSONResponse:
    # 受限内容属于"确认无解"，用 422 与网络/解析异常区分开
    content = {"error": exc.message, "hint": exc.hint}
    content["category"] = getattr(exc, "category", None) or "restricted"
    return JSONResponse(status_code=422, content=content)



# --------------------------------------------------------------------------- #
# 文件系统辅助（桌面版便捷入口）
# --------------------------------------------------------------------------- #

class OpenPathRequest(BaseModel):
    """打开本地目录/文件。仅允许白名单路径（下载目录及其子项）。"""
    path: str = Field(default="", max_length=4096)




# --------------------------------------------------------------------------- #
# 自动解说（增值功能）：下载完 → 一键生成解说成片。壳，逻辑全在 commentary-pipeline
# --------------------------------------------------------------------------- #

class CommentaryRequest(BaseModel):
    task_id: str = Field(default="", max_length=64)
    file_id: str = Field(default="", max_length=2048)
    vertical: bool = False
    voice: str = Field(default="", max_length=64)
    trim_start: float = Field(default=0.0, ge=0.0)
    trim_end: float = Field(default=0.0, ge=0.0)
    # 剪辑选项（与 commentary-pipeline 的 commentary_options 模型一致）
    commentary_type: str = Field(default="deep_hl",
                                 description="解说类型: deep_hl=高光处叠加深度解说; normal_hl=高光部分普通解说; "
                                             "full_normal=全片普通解说; full_deep=全片深入解说")
    highlight_source: str = Field(default="ai", description="高光来源: ai=AI自动挑; manual=人工在审核面板挑")
    intro_highlight: bool = Field(default=False, description="片头插入最精彩片段当钩子")
    skip_intro_outro: bool = Field(default=False, description="去片头片尾(自动检测边界后剪掉)")
    no_narrate_intro_outro: bool = Field(default=True,
                                         description="保留片头片尾但不解说(默认开启；与 skip_intro_outro 互斥，后者优先)")
    retain_pct: float | None = Field(default=None, description="保留全片时长百分比(10~100, 不填=不裁剪)")
    web: bool = Field(default=False, description="联网搜索资料辅助发挥")
    one_click: bool = Field(default=False, description="一键生成: 全片深入解说+AI联网+片头插精彩片段")
    style: str = Field(default="none", description="解说口吻风格: none=默认; funny=搞笑; serious=严肃; domineering=霸道; angry=愤青; suspense=悬疑; healing=治愈; sarcastic=毒舌")
    mode: str | None = Field(default=None,
                             description="(旧版兼容) 三选一解说模式，会被上面的新选项覆盖")


class ScriptUpdateRequest(BaseModel):
    """PUT /api/commentary/script/{job_id}：提交人工修改后的解说词与全局配音。"""
    title: str = Field(default="", max_length=256)
    voice: str = Field(default="", max_length=64)
    segments: list = Field(default_factory=list)  # [{start, end, narration, note, voice?}, ...]








# 独立「解说成片」标签页：列出所有已生成成片，并支持按 id 直接下载/播放。
# id 为成片绝对路径的 urlsafe base64，便于无状态回查且防止路径穿越。
def _commentary_roots() -> list[Path]:
    """扫描两个可能的输出目录：HTTP 模式落地到 COMMENTARY_LOCAL_OUTPUT，
    本地模式落地到 COMMENTARY_DIR/output。"""
    roots = []
    if COMMENTARY_LOCAL_OUTPUT and COMMENTARY_LOCAL_OUTPUT.exists() and COMMENTARY_LOCAL_OUTPUT.is_dir():
        roots.append(COMMENTARY_LOCAL_OUTPUT)
    if COMMENTARY_DIR:
        d = _commentary_root("output")
        if d.exists() and d.is_dir():
            roots.append(d)
    return roots


def _decode_commentary_id(cid: str) -> Path:
    try:
        raw = base64.urlsafe_b64decode(cid.encode("ascii")).decode("utf-8")
    except Exception:
        raise HTTPException(status_code=400, detail="非法的成片标识")
    p = Path(raw).resolve()
    allowed = {r.resolve() for r in _commentary_roots()}
    if not any(p == root or str(p).startswith(str(root) + os.sep) for root in allowed):
        raise HTTPException(status_code=403, detail="越权访问被拒绝")
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="成片不存在或已被清理")
    return p










class CommentaryRenameReq(BaseModel):
    name: str










# ---- 脚本审核专用路由 ----

def _resolve_source(payload: CommentaryRequest) -> str:
    """解析视频来源（task_id 或 file_id），返回绝对路径。"""
    if payload.file_id:
        p = library_mod._resolve_safe(DOWNLOAD_DIR, payload.file_id)
        if not p:
            raise HTTPException(status_code=404, detail="媒体库文件不存在")
        suffix = p.suffix.lower()
        if suffix == library_mod.ENCRYPTED_EXT:
            raise HTTPException(status_code=409, detail="加密文件不支持生成解说，请先在保险箱解锁")
        if suffix not in library_mod.VIDEO_EXTS:
            raise HTTPException(status_code=409, detail="该文件不是视频，无法生成解说成片")
        return str(p)
    elif payload.task_id:
        task = _require_task(payload.task_id)
        if task.status != "completed" or not task.filepath or not task.filepath.exists():
            raise HTTPException(status_code=409, detail="下载任务尚未完成，无法生成解说")
        return str(task.filepath)
    else:
        raise HTTPException(status_code=400, detail="请提供 task_id 或 file_id")


def _commentary_title(payload: "CommentaryRequest", src_path: str) -> str:
    """为解说任务推导剧名锚点：优先用下载任务的标题，否则退回源文件名（下载文件名通常含剧集名）。"""
    if getattr(payload, "task_id", ""):
        try:
            t = _require_task(payload.task_id)
            tt = getattr(t, "title", "") or ""
            if tt:
                return tt
        except Exception:
            pass
    return Path(src_path).stem or ""










# ---- 配音试听 / 预览全部 ----

def _run_voice_preview(text: str, voice: str, output_mp3: Path, timeout: int = 60) -> None:
    """用 edge-tts 把一段文本转成指定音色的 mp3。

    bundled 模式：edge_tts 已随包冻结，直接 in-process 调用，避开「subprocess 跑
    .py 脚本 vs PyInstaller frozen exe」的兼容性问题。
    dev 模式：edge_tts 装在 commentary-pipeline .venv 里，subprocess 到 COMMENTARY_RT.python。
    """
    if not COMMENTARY_RT.ready():
        raise RuntimeError("解说环境未就绪：" + "；".join(COMMENTARY_RT.issues))

    # bundled 模式：桌面打包态，edge_tts 已冻结进 exe，直接 in-process 调用
    if plat.is_desktop():
        _run_voice_preview_inprocess(text, voice, output_mp3, timeout=timeout)
        return

    # dev/外部：subprocess 走 commentary-pipeline 的 venv
    if not COMMENTARY_DIR or not (COMMENTARY_DIR / "scripts" / "voice_preview.py").exists():
        raise RuntimeError("voice_preview.py 不存在（请在 commentary-pipeline/scripts/ 下创建）")
    script = COMMENTARY_DIR / "scripts" / "voice_preview.py"
    cmd = [COMMENTARY_RT.python, str(script), text, voice, str(output_mp3)]
    try:
        proc = subprocess.run(cmd, cwd=str(COMMENTARY_DIR), capture_output=True,
                              text=True, timeout=timeout, env=COMMENTARY_RT.env())
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"edge-tts 生成超时（>{timeout}s）") from exc
    except Exception as exc:
        raise RuntimeError(f"edge-tts 调用失败：{exc}") from exc
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "无输出").strip()[:400]
        raise RuntimeError(f"edge-tts 退出码 {proc.returncode}: {msg}")
    if not output_mp3.exists() or output_mp3.stat().st_size < 100:
        raise RuntimeError("edge-tts 未产出有效音频文件")


def _run_voice_preview_inprocess(text: str, voice: str, output_mp3: Path, timeout: int = 60) -> None:
    """bundled 模式：in-process 调 edge_tts 合成 mp3，省去 PyInstaller frozen exe 跑 .py 脚本的兼容坑。"""
    import asyncio
    import edge_tts

    FALLBACK_VOICES = [
        "zh-CN-YunjianNeural",
        "zh-CN-YunyangNeural",
        "zh-CN-YunxiaNeural",
        "zh-CN-XiaoxiaoNeural",
    ]

    async def _try(text: str, v: str, timeout_s: float) -> bool:
        try:
            await asyncio.wait_for(edge_tts.Communicate(text, v).save(str(output_mp3)), timeout=timeout_s)
            return output_mp3.exists() and output_mp3.stat().st_size > 100
        except Exception:
            return False

    async def _main():
        for _ in range(2):
            if await _try(text, voice, 20):
                return
        tried = [voice]
        for fb in FALLBACK_VOICES:
            if fb == voice:
                continue
            tried.append(fb)
            if await _try(text, fb, 20):
                return
        raise RuntimeError(f"edge-tts 生成失败: 已尝试 {', '.join(tried)}，均无法合成该文本")

    try:
        asyncio.run(_main())
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"edge-tts 调用失败：{exc}") from exc
    if not output_mp3.exists() or output_mp3.stat().st_size < 100:
        raise RuntimeError("edge-tts 未产出有效音频文件")






# --------------------------------------------------------------------------- #
# ---- 本地媒体库（桌面版功能）：浏览 / 播放 / 删除已下载的媒体文件 ----








# ---- 时效自动清理：按保留期/容量上限清理下载目录（预览 → 执行，媒体走回收站） ----

class RetentionConfigRequest(BaseModel):
    auto_enabled: bool | None = None
    interval_hours: float | None = Field(default=None, ge=0.25, le=168)
    temp_enabled: bool | None = None
    temp_days: float | None = Field(default=None, ge=0, le=3650)
    frames_enabled: bool | None = None
    frames_days: float | None = Field(default=None, ge=0, le=3650)
    thumbs_enabled: bool | None = None
    thumbs_days: float | None = Field(default=None, ge=0, le=3650)
    media_enabled: bool | None = None
    media_days: float | None = Field(default=None, ge=1, le=3650)
    quota_enabled: bool | None = None
    quota_gb: float | None = Field(default=None, ge=1, le=100000)
    media_use_trash: bool | None = None


class RetentionRunRequest(BaseModel):
    # 只清指定档位；留空=按当前配置全清。前端「预览后执行」会带上用户勾选的档位。
    categories: list[str] | None = None


def _require_retention() -> None:
    if not RETENTION_ENABLED:
        raise HTTPException(status_code=404, detail="自动清理仅桌面版可用")










# --------------------------------------------------------------------------- #
# 库内保险箱：选中文件就地 AES 加密 / 解密 + 解密播放
# --------------------------------------------------------------------------- #
class CryptoSetPassRequest(BaseModel):
    passwd: str = Field(min_length=1, max_length=512)
    confirm: str = Field(default="", max_length=512)
    old: str = Field(default="", max_length=512)


class CryptoUnlockRequest(BaseModel):
    passwd: str = Field(min_length=1, max_length=512)


class CryptoIdsRequest(BaseModel):
    lib_ids: list[str] = Field(default_factory=list)


def _require_crypto() -> None:
    if not CRYPTO_ENABLED:
        raise HTTPException(status_code=404, detail="保险箱功能未启用")


def _require_unlocked() -> None:
    if VAULT_KEY is None:
        raise HTTPException(status_code=423, detail="保险箱已锁定，请先解锁")


def _crypto_job_status(job_id: str) -> dict:
    with CRYPTO_LOCK:
        job = CRYPTO_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


def _kind_of(path: Path) -> str:
    suf = path.suffix.lower()
    if suf in library_mod.AUDIO_EXTS:
        return "audio"
    if suf in library_mod.IMAGE_EXTS:
        return "image"
    return "video"


def _run_crypto_job(job_id: str, lib_ids: list[str], mode: str) -> None:
    """mode = 'encrypt' | 'decrypt'。加密把原件移回收站保底；解密还原原名并删除 .vdlenc。"""
    total = len(lib_ids)
    done = 0
    errors: list[str] = []
    with CRYPTO_LOCK:
        job = CRYPTO_JOBS.get(job_id)
        if job:
            job["status"] = "running"
            job["total"] = total

    for lid in lib_ids:
        with CRYPTO_LOCK:
            job = CRYPTO_JOBS.get(job_id)
            if not job or job.get("cancel"):
                with CRYPTO_LOCK:
                    if job:
                        job["status"] = "canceled"
                return
        src = library_mod._resolve_safe(DOWNLOAD_DIR, lid)
        if not src:
            errors.append(lid + ": 文件不存在")
            done += 1
            with CRYPTO_LOCK:
                if job:
                    job["done"] = done
            continue
        try:
            if mode == "encrypt":
                if src.suffix.lower() == library_mod.ENCRYPTED_EXT:
                    errors.append(src.name + ": 已是加密文件")
                else:
                    dst = src.parent / (src.name + library_mod.ENCRYPTED_EXT)
                    crypto_mod.encrypt_file(src, dst, VAULT_KEY, src.name, _kind_of(src))
                    # 原件移回收站保底，绝不静默硬删
                    retention_mod.move_to_trash(src)
            else:  # decrypt
                if src.suffix.lower() != library_mod.ENCRYPTED_EXT:
                    errors.append(src.name + ": 不是加密文件")
                else:
                    orig_name, _kind, _ext = crypto_mod.read_header(src)
                    out = src.parent / orig_name
                    if out.exists():
                        errors.append(src.name + ": 还原目标已存在，跳过")
                    else:
                        tmp_dec = src.parent / "tmp_dec"
                        crypto_mod.decrypt_file(src, tmp_dec, VAULT_KEY)
                        tmp_dec.replace(out)
                        src.unlink()  # 解密成功才删 .vdlenc
        except Exception as exc:  # 单文件失败不中断整批
            errors.append(src.name + ": " + type(exc).__name__ + " " + str(exc)[:120])
        done += 1
        with CRYPTO_LOCK:
            if job:
                job["done"] = done
                job["errors"] = errors

    with CRYPTO_LOCK:
        job = CRYPTO_JOBS.get(job_id)
        if job and job.get("status") != "canceled":
            job["status"] = "completed"
            job["errors"] = errors


def _prune_crypto_jobs() -> None:
    if len(CRYPTO_JOBS) <= 200:
        return
    with CRYPTO_LOCK:
        done = [jid for jid, j in CRYPTO_JOBS.items()
                if j.get("status") in ("completed", "failed", "canceled")]
        for jid in (done[:-50] if len(done) > 50 else []):
            CRYPTO_JOBS.pop(jid, None)




# ---- 桌面版种子下载（libtorrent 集成）：magnet/.torrent → 本地媒体库 ----
class TorrentAddRequest(BaseModel):
    uri: str = Field(min_length=1, max_length=8192)
    name: str = Field(default="", max_length=512)
    paused: bool = False
    save_path: str = Field(default="", max_length=4096)  # 相对 DOWNLOAD_DIR 的子目录；留空=根
    file_priorities: dict[int, int] = Field(default_factory=dict)  # {文件下标: 优先级(0=跳过)}


class TorrentRemoveRequest(BaseModel):
    delete_files: bool = False


class TorrentFilesRequest(BaseModel):
    priorities: dict[int, int] = Field(default_factory=dict)  # {文件下标: 优先级(0=跳过)}


def _require_torrent() -> None:
    if not (TORRENT_ENABLED and torrent_mod.available() and torrent_manager is not None):
        raise HTTPException(status_code=404, detail="种子下载功能未启用（需桌面版并安装 libtorrent 或 aria2）")


















# ---- 字幕处理：在线字幕提取 / 内嵌字幕抽取 / 硬字幕烧录 / 可选 LLM 翻译 ----

class SubListRequest(BaseModel):
    lib_id: str = Field(min_length=1)
    cookie: str = Field(default="", max_length=8192)


class SubExtractRequest(BaseModel):
    lib_id: str = Field(min_length=1)
    lang: str = Field(min_length=1, max_length=16)
    cookie: str = Field(default="", max_length=8192)


class SubBurnRequest(BaseModel):
    lib_id: str = Field(min_length=1)
    sub_rel: str = Field(min_length=1, max_length=256)  # 相对视频目录的字幕文件名


class SubTranslateRequest(BaseModel):
    lib_id: str = Field(min_length=1)
    sub_rel: str = Field(min_length=1, max_length=256)
    api_key: str = Field(default="", max_length=512)
    base_url: str = Field(default="", max_length=512)
    model: str = Field(default="", max_length=128)
    target: str = Field(default="简体中文", max_length=32)


def _resolve_lib_video(lib_id: str) -> Path:
    p = library_mod._resolve_safe(DOWNLOAD_DIR, lib_id)
    if not p or p.suffix.lower() not in library_mod.VIDEO_EXTS:
        raise HTTPException(status_code=404, detail="视频文件不存在")
    return p










# ---- LLM 服务商选择器 API（统一配置，前端面板持久化）----

class LLMConfigRequest(BaseModel):
    provider: str = Field(default="openai", max_length=32)
    api_key: str = Field(default="", max_length=256)
    base_url: str = Field(default="", max_length=512)
    model: str = Field(default="", max_length=128)








# ---- 格式 / 片段增强：对已下载媒体做本地 ffmpeg 加工（转音频 / GIF / 裁剪 / 压缩 / 放大）----
# 与字幕处理同源（基于 lib_id）；产物落源目录并写侧车 → 媒体库自动可见。

class ProcessRequest(BaseModel):
    # 单个文件（向后兼容）
    lib_id: str = Field(default="", max_length=2048)
    # 批量文件
    lib_ids: list[str] = Field(default_factory=list)
    op: str = Field(min_length=1, max_length=16)
    params: dict = Field(default_factory=dict)










def _run_ai_dewatermark(job_id: str, src: str, params: dict) -> None:
    """AI 去水印：调 watermark-removal worker（HTTP 或本地 subprocess），轮询完成。"""
    import json, time as _time
    import requests as _requests

    x = int(params.get("x", 0) or 0)
    y = int(params.get("y", 0) or 0)
    w = int(params.get("w", 100) or 100)
    h = int(params.get("h", 50) or 50)
    band = int(params.get("band", 5) or 5)
    mode = str(params.get("mode", "auto") or "auto")

    try:
        if AI_DEWATERMARK_MODE == "http":
            resp = _requests.post(
                f"{AI_DEWATERMARK_ENDPOINT}/render",
                json={"video": src, "x": x, "y": y, "w": w, "h": h, "band": band, "mode": mode},
                headers={"X-Worker-Token": AI_DEWATERMARK_TOKEN} if AI_DEWATERMARK_TOKEN else {},
                timeout=30,
            )
            resp.raise_for_status()
            worker_job = resp.json()["job_id"]
        else:
            # local 模式：subprocess 调 process.py
            proc_dir = AI_DEWATERMARK_DIR or (Path(__file__).resolve().parent.parent / "watermark-removal")
            proc = proc_dir / "process.py"
            if not proc.exists():
                raise RuntimeError(f"AI 去水印管线未找到：{proc}")
            import subprocess as _sp
            result = _sp.run(
                [AI_DEWATERMARK_PYTHON, str(proc), src,
                 "--x", str(x), "--y", str(y), "--w", str(w), "--h", str(h),
                 "--band", str(band), "--mode", mode],
                capture_output=True, text=True, timeout=AI_DEWATERMARK_TIMEOUT,
                env=dict(os.environ, E2FGVI_BASE=str(proc_dir / "E2FGVI")),
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "AI 去水印失败")
            # local 模式同步完成，直接走收尾
            out = Path(src).parent / f"{Path(src).stem}_AI去水印.mp4"
            if not out.exists():
                raise RuntimeError("AI 去水印未生成输出文件")
            meta = library_mod._load_sidecar(Path(src))
            fftools._write_sidecar(out, meta, "AI去水印")
            new_id = library_mod.encode_id(out.resolve().relative_to(DOWNLOAD_DIR.resolve()).as_posix())
            with process_queue.lock:
                process_queue.jobs[job_id].update(status="completed", out_path=str(out), lib_id=new_id, name=out.name)
            logger.info("ai_dewatermark %s done -> %s", job_id, out.name)
            return

        # HTTP 模式：轮询 worker 直到完成
        deadline = _time.time() + AI_DEWATERMARK_TIMEOUT
        while _time.time() < deadline:
            _time.sleep(5)
            st = _requests.get(
                f"{AI_DEWATERMARK_ENDPOINT}/status/{worker_job}",
                headers={"X-Worker-Token": AI_DEWATERMARK_TOKEN} if AI_DEWATERMARK_TOKEN else {},
                timeout=10,
            ).json()
            if st["status"] == "completed":
                break
            if st["status"] == "failed":
                raise RuntimeError(st.get("error", "AI 去水印 worker 失败"))
        else:
            raise RuntimeError("AI 去水印超时")

        # 下载成片
        dl = _requests.get(
            f"{AI_DEWATERMARK_ENDPOINT}/file/{worker_job}",
            headers={"X-Worker-Token": AI_DEWATERMARK_TOKEN} if AI_DEWATERMARK_TOKEN else {},
            timeout=60,
        )
        dl.raise_for_status()
        out = Path(src).parent / f"{Path(src).stem}_AI去水印.mp4"
        out.write_bytes(dl.content)

        meta = library_mod._load_sidecar(Path(src))
        fftools._write_sidecar(out, meta, "AI去水印")
        new_id = library_mod.encode_id(out.resolve().relative_to(DOWNLOAD_DIR.resolve()).as_posix())
        with process_queue.lock:
            process_queue.jobs[job_id].update(status="completed", out_path=str(out), lib_id=new_id, name=out.name)
        logger.info("ai_dewatermark %s (http) done -> %s", job_id, out.name)

    except Exception as e:
        with process_queue.lock:
            process_queue.jobs[job_id]["status"] = "failed"
            process_queue.jobs[job_id]["error"] = str(e)[:400]
        logger.warning("ai_dewatermark %s failed: %s", job_id, e)


def _process_log(job_id: str, line: str) -> None:
    if not line:
        return
    with process_queue.lock:
        job = process_queue.jobs.get(job_id)
        if not job:
            return
        ts = time.strftime("%H:%M:%S", time.localtime())
        job.setdefault("logs", []).append(f"{ts}  {line.strip()}")
        if len(job.get("logs", [])) > 200:
            job["logs"][:] = job["logs"][-200:]


def _process_set_step(job_id: str, idx: int, status: str, detail: str = "") -> None:
    with process_queue.lock:
        job = process_queue.jobs.get(job_id)
        if not job:
            return
        steps = job.get("steps") or []
        now = time.time()
        for s in steps[:idx]:
            if s["status"] == "pending":
                s["status"] = "done"
                s["updated_at"] = now
        if 0 <= idx < len(steps):
            steps[idx]["status"] = status
            steps[idx]["detail"] = detail[:200]
            steps[idx]["updated_at"] = now


def _run_process(job_id: str, src: str, op: str, params: dict) -> None:
    """后台线程：按 op 调用 ffmpeg_tools，产物落源目录并写侧车，更新 process_queue.jobs。"""
    with process_queue.lock:
        job = process_queue.jobs.get(job_id)
    if not job:
        return
    _process_set_step(job_id, 1, "done", f"源文件: {Path(src).name}")
    _process_set_step(job_id, 2, "running", f"操作: {op}")
    _process_log(job_id, f"开始处理: {op} -> {src}")
    try:
        src_path = Path(src)
        out_dir = src_path.parent
        meta = library_mod._load_sidecar(src_path)
        p = params or {}
        out = None
        suffix = ""
        if op == "audio":
            out = fftools.extract_audio(src_path, out_dir,
                                       fmt=str(p.get("fmt", "mp3")),
                                       bitrate=str(p.get("bitrate", "192k")),
                                       ffmpeg_bin=FFMPEG_BIN)
            suffix = "音频"
        elif op == "gif":
            out = fftools.make_gif(src_path, out_dir,
                                  start=float(p.get("start", 0) or 0),
                                  duration=float(p.get("duration", 5) or 5),
                                  fps=int(p.get("fps", 12) or 12),
                                  width=int(p.get("width", 480) or 480),
                                  ffmpeg_bin=FFMPEG_BIN)
            suffix = "动图"
        elif op == "trim":
            out = fftools.trim_video(src_path, out_dir,
                                    start=float(p.get("start", 0) or 0),
                                    end=float(p.get("end", 0) or 0),
                                    reencode=bool(p.get("reencode", True)),
                                    ffmpeg_bin=FFMPEG_BIN)
            suffix = "片段"
        elif op == "crop":
            out = fftools.crop_video(src_path, out_dir,
                                    crop_expr=str(p.get("crop_expr", "")),
                                    ffmpeg_bin=FFMPEG_BIN)
            suffix = "裁剪"
        elif op == "compress":
            sh = int(p.get("scale_h", 720) or 720)
            out = fftools.compress_video(src_path, out_dir, scale_h=sh,
                                        crf=int(p.get("crf", 28) or 28),
                                        ffmpeg_bin=FFMPEG_BIN)
            suffix = f"压缩{sh}p"
        elif op == "upscale":
            fac = float(p.get("factor", 2) or 2)
            out = fftools.upscale_video(src_path, out_dir, factor=fac,
                                       sharpen=bool(p.get("sharpen", True)),
                                       ffmpeg_bin=FFMPEG_BIN)
            suffix = f"放大{fac}x"
        elif op == "frame":
            out = fftools.snapshot(src_path, out_dir,
                                  at=float(p.get("at", 1) or 0),
                                  fmt=str(p.get("fmt", "jpg")),
                                  width=int(p.get("width", 0) or 0),
                                  ffmpeg_bin=FFMPEG_BIN)
            suffix = "封面"
        elif op == "sheet":
            out = fftools.contact_sheet(src_path, out_dir,
                                       rows=int(p.get("rows", 3) or 3),
                                       cols=int(p.get("cols", 4) or 4),
                                       width=int(p.get("width", 1280) or 1280),
                                       duration=float(meta.get("duration") or 0),
                                       ffmpeg_bin=FFMPEG_BIN)
            suffix = "预览图"
        elif op == "ringtone":
            out = fftools.make_ringtone(src_path, out_dir,
                                       start=float(p.get("start", 0) or 0),
                                       duration=float(p.get("duration", 30) or 30),
                                       fmt=str(p.get("fmt", "m4r")),
                                       fade=float(p.get("fade", 1) or 0),
                                       ffmpeg_bin=FFMPEG_BIN)
            suffix = "铃声"
        elif op == "dewatermark":
            out = fftools.remove_watermark(src_path, out_dir,
                                          x=int(p.get("x", 0) or 0),
                                          y=int(p.get("y", 0) or 0),
                                          w=int(p.get("w", 100) or 100),
                                          h=int(p.get("h", 50) or 50),
                                          show=bool(p.get("show", False)),
                                          band=int(p.get("band", 10) or 10),
                                          ffmpeg_bin=FFMPEG_BIN)
            if out and bool(p.get("show", False)):
                # show 模式：仅画框不做处理，不写侧车，提示用户再指定位置提交
                _process_set_step(job_id, 3, "done", f"预览框: {out.name}")
                _process_log(job_id, f"dewatermark-show 完成: {out.name}")
                with process_queue.lock:
                    job.update(status="completed", out_path=str(out), lib_id="", name=out.name)
                logger.info("process %s (dewatermark-show) done", job_id)
                return
            suffix = "去水印"
        elif op == "ai_dewatermark":
            _run_ai_dewatermark(job_id, str(src_path), p)
            return  # 异步 worker，不在此处收尾
        elif op == "frames":
            # 批量抽帧：产物是一个子目录（不是单文件），单独收尾
            res = fftools.extract_frames(src_path, out_dir,
                                        start=float(p.get("start", 0) or 0),
                                        end=float(p.get("end", 0) or 0),
                                        interval=float(p.get("interval", 1) or 1),
                                        limit=int(p.get("limit", 200) or 200),
                                        fmt=str(p.get("fmt", "jpg")),
                                        width=int(p.get("width", 0) or 0),
                                        ffmpeg_bin=FFMPEG_BIN)
            if not res:
                raise RuntimeError("未抽到任何帧（检查起止时间是否超出视频时长）")
            frames_dir, count = res
            _process_set_step(job_id, 3, "done", f"{count} 帧 -> {frames_dir.name}")
            _process_log(job_id, f"抽帧完成: {count} 帧")
            with process_queue.lock:
                job.update(status="completed", out_path=str(frames_dir), lib_id="",
                           name=frames_dir.name, count=count, is_dir=True)
            logger.info("process %s (frames) done -> %s (%d 帧)", job_id, frames_dir.name, count)
            return
        else:
            raise ValueError("不支持的处理类型")
        if not out or not out.exists() or out.stat().st_size == 0:
            raise RuntimeError("处理未产出有效文件")
        fftools._write_sidecar(out, meta, suffix)
        new_id = library_mod.encode_id(out.resolve().relative_to(DOWNLOAD_DIR.resolve()).as_posix())
        _process_set_step(job_id, 3, "done", f"输出: {out.name}")
        _process_log(job_id, f"处理完成: {out.name}")
        with process_queue.lock:
            job.update(status="completed", out_path=str(out), lib_id=new_id, name=out.name)
        logger.info("process %s (%s) done -> %s", job_id, op, out.name)
    except Exception as e:  # noqa: BLE001
        _process_set_step(job_id, 2, "error", str(e)[:200])
        _process_log(job_id, f"处理失败: {e}")
        with process_queue.lock:
            job["status"] = "failed"
            job["error"] = str(e)[:400]
        logger.warning("process %s (%s) failed: %s", job_id, op, e)


# ---- 订阅监控：关注频道/UP 主，自动下载新发布的视频 ----

def _run_subscription_check(sub: "subs_mod.Subscription") -> dict:
    """探查频道新视频并加入下载队列；更新已知 id 基线。返回结果摘要。

    基线策略：仅把"成功创建下载任务"的视频 id 计入 last_video_ids，
    解析失败的 id 不计入（下次 check 仍可重试）；任务最终失败由用户在下载面板手动重试。
    """
    items = subs_mod.probe_channel(sub.url, sub.cookie, sub.proxy, limit=SUBSCRIBE_PROBE_LIMIT)
    known = set(sub.last_video_ids)
    fresh = [it for it in items if it["id"] not in known]
    task_ids: list[str] = []
    failed: list[str] = []
    submitted_ids: set[str] = set()
    for it in fresh:
        if not it.get("url"):
            continue
        try:
            url, platform = parse_source(it["url"])
        except (UnsupportedPlatformError, LinkError):
            failed.append(it["title"])
            continue
        task = store.create(
            url=url, title=it["title"], platform=platform.name,
            quality=downloader.quality_label(sub.quality_key), quality_key=sub.quality_key,
        )
        scheduler.submit(downloader.run_download, task, store, sub.quality_key, sub.cookie, sub.proxy, SINGLE_DOWNLOAD_RETRIES)
        task_ids.append(task.id)
        submitted_ids.add(it["id"])
    # 合并基线（保留最近最多 200 条），仅计入成功提交任务的 id
    new_baseline = list(sub.last_video_ids)
    for vid in submitted_ids:
        if vid not in new_baseline:
            new_baseline.append(vid)
    sub_store.update(sub.id, last_video_ids=new_baseline[:200], last_checked=time.time())
    return {"sub_id": sub.id, "checked": len(items), "new_videos": fresh, "task_ids": task_ids, "failed": failed}


class SubscribeRequest(BaseModel):
    url: str = Field(min_length=4, max_length=8192)
    name: str = Field(default="", max_length=128)
    quality: str = Field(default=downloader.BEST_KEY, max_length=16)
    cookie: str = Field(default="", max_length=8192)
    proxy: str = Field(default="", max_length=256)
    auto_check: bool = True










def _subscription_watchdog() -> None:
    """后台常驻：周期性对开启 auto_check 的订阅探查新视频并下载。"""
    while True:
        time.sleep(SUB_CHECK_INTERVAL)
        try:
            for sub in sub_store.list_all():
                if not sub.auto_check:
                    continue
                try:
                    _run_subscription_check(sub)
                except Exception:
                    logger.exception("订阅 %s 自动检查失败", sub.id)
        except Exception:
            logger.exception("订阅 watchpod 异常")


if SUB_ENABLED:
    _sub_watchdog = threading.Thread(target=_subscription_watchdog, name="vdl-sub-watchdog", daemon=True)
    _sub_watchdog.start()


def _retention_watchdog() -> None:
    """后台常驻：按 interval_hours 周期执行时效清理。

    启动后先静置 120 秒再首跑 —— 避免「刚开 App 就开始删东西」，也给用户留出
    改配置的窗口。总开关 auto_enabled 关闭时只空转，不碰磁盘。
    """
    time.sleep(120)
    while True:
        cfg = retention_store.get()
        interval = max(0.25, float(cfg.interval_hours or 6.0)) * 3600
        if not cfg.auto_enabled:
            time.sleep(min(interval, 1800))
            continue
        try:
            result = retention_mod.run(DOWNLOAD_DIR, cfg)
            if result["removed"] or result["failed"]:
                logger.info("自动清理：移除 %s 项，释放 %s，失败 %s 项",
                            result["removed"], retention_mod.human_size(result["freed"]),
                            result["failed"])
                retention_store.update(last_run=result["ran_at"], last_freed=result["freed"],
                                       last_removed=result["removed"])
        except Exception:
            logger.exception("自动清理执行异常")
        time.sleep(interval)


if RETENTION_ENABLED:
    _ret_watchdog = threading.Thread(target=_retention_watchdog, name="vdl-retention", daemon=True)
    _ret_watchdog.start()






























# --------------------------------------------------------------------------- #
# Web 精简版路由（web-dev）：仅挂载网页基础功能所需路由
#   保留：core(解析/下载) / crypto / fs / cloud(WebDAV 云盘) /
#     convert(视频转换) / dewatermark(PDF/图片去水印)
#   公共 Cookie 池：代码内联于本文件后部（非 cloud.py），含接收端 /api/cookie/sync、
#     本机 from-local、查询 status、清理 cache/clear 及后台探测 watchdog，网页版复用。
#   剥离(App 专属，仅在 main 完整版挂载)：commentary / library /
#     retention / archive / torrents / subtitles / llm / process / subscriptions
# 必须在 app.mount("/", StaticFiles) 之前 include，否则 "/" 挂载会前缀匹配吞掉 /api/* 路由
from routers import crypto as _crypto_rtr
app.include_router(_crypto_rtr.router)
from routers import fs as _fs_rtr
app.include_router(_fs_rtr.router)
from routers import convert as _convert_rtr
app.include_router(_convert_rtr.router)
from routers import dewatermark as _dewatermark_rtr
app.include_router(_dewatermark_rtr.router)
from routers import core as _core_rtr
app.include_router(_core_rtr.router)
# 反向 WebSocket 隧道：ECS 主动连入，把国内 cn_proxy 经隧道暴露给本机代理
import cn_tunnel as _cn_tunnel
app.include_router(_cn_tunnel.router)

# —— 账号 / 会员 / 客服 / 后台 / AI 字幕（自 app-dev 移植，2026-09-11）——
# A1 自建轻量账号(auth_store) + B2 per-user-ID 文件存储(membership)，
# 与桌面端同一套引擎；subtitle 为 faster-whisper CPU 推理（本地/上传文件路径均可用）。
from membership import MembershipStore as _MembershipStore
member_store = _MembershipStore()  # 会员引擎共享单例（/api/member + 功能配额判定共用同一状态文件）
from routers import membership as _membership_rtr
app.include_router(_membership_rtr.router)
from routers import payment as _payment_rtr
app.include_router(_payment_rtr.router)
from routers import extension as _extension_rtr
app.include_router(_extension_rtr.router)
from routers import auth as _auth_rtr
app.include_router(_auth_rtr.router)
from routers import admin as _admin_rtr
app.include_router(_admin_rtr.router)
# 云端侧的 Cookie 池管理入口（`POST /api/cookie/pool-admin`）：桌面 App 的后台
# 「Cookie 池」页靠它代理读「网页版真正在用的那个池」。admin_cookies 的 docstring
# 写明「同一份代码、两个部署角色（云端节点直接读写 / 桌面 App 代理）」，但 web 端
# **一直没挂这份路由** ⇒ 桌面后台报「云端不可达 HTTP 405」（2026-10-21 实事故）。
from routers import admin_cookies as _admin_cookies_rtr
app.include_router(_admin_cookies_rtr.router)
from routers import support as _support_rtr
app.include_router(_support_rtr.router)
from routers import subtitle as _subtitle_rtr
app.include_router(_subtitle_rtr.router)
from routers import share_token as _share_token_rtr
app.include_router(_share_token_rtr.router)
# 百度网盘下载（2026-10-25 用户拍板：网页版也做，登录态存服务器）。
# 与桌面端那套的区别在 routers/cloud_drive.py 里有说明：网页版由服务器带登录态
# 取文件再流式中转，dlink 用服务端加密令牌承载、百度 cookie 不出服务器。
from routers import cloud_drive as _cloud_drive_rtr
app.include_router(_cloud_drive_rtr.router)
# 网页访客统计（PV/UV/事件/来源，仅落哈希不落 IP 明文；上架/变现决策的数据依据）
from routers import webstats as _webstats_rtr
app.include_router(_webstats_rtr.router)
# 用户信息类数据云备份 · 网页版通道（2026-10-17）：网页版也能把偏好 + 浏览器登录态
# 随账号备份/恢复。分文件存（scope="web"），与桌面端那份互不覆盖；加解密全在
# 浏览器端做（WebCrypto），本服务只转发密文。
from routers import userdata as _userdata_rtr
app.include_router(_userdata_rtr.router)
# 账号 / per-user 会员解析：供各路由经 request 取当前用户态 store。
# 必须在模块顶层定义（与 member_store 同级），否则 routers 里 `import app` 取不到。
from user_membership import get_current_user_id, current_member_store

# —— 公共 Cookie 池 + 本机 Cookie 缓存（来自 main 分支，合并时保留）——
# 与「仅本机个人缓存」(cookie_cache.py) 严格隔离：独立存储目录、仅白名单域、入池前验真。
_SYNC_RL = {"ts": {}, "lock": threading.Lock()}


def _sync_rate_ok(ip: str, scope: str = "") -> bool:
    """防滥用限流。环回客户端（桌面 App 自连本机后端）豁免。

    - 不传 scope：按 **单 IP 30 秒至多一次**。用于面向网页访客、无需令牌的
      /api/cookie/contribute。
    - 传 scope：按 **(IP, scope) 各 30 秒一次**。用于令牌鉴权的 /api/cookie/sync
      ——「同步到云端」是按域批量推送（本机浏览器登录了十来个站就推十几次），
      按 IP 限流会让除第一个以外的域全部撞 429，末尾的 youtube.com / youku.com
      必然失败（2026-09-26 实测到的自愈链路阻断点）。按域计既能放行批量推送，
      又仍挡住对同一域的反复刷写。
    """
    if _is_loopback_ip(ip):
        return True
    key = f"{ip}|{scope}" if scope else ip
    now = time.time()
    with _SYNC_RL["lock"]:
        last = _SYNC_RL["ts"].get(key, 0)
        if now - last < 30:
            return False
        _SYNC_RL["ts"][key] = now
        return True


def _cookie_pool_alert(domain: str) -> None:
    msg = (f"[cookie_pool] 域名 {domain} 公共 Cookie 池已空/全部失效，"
           f"网页版 chrqj 将 403，请补充 Cookie（App 端『同步 Cookie 到云端』或设 CHRQJ_COOKIE）")
    logger.warning(msg)
    try:
        alert = Path.home() / ".videodownloader" / "cookie_pool_alert.json"
        alert.write_text(json.dumps({"domain": domain, "ts": int(time.time()), "empty": True}, ensure_ascii=False))
    except Exception:
        pass
    wh = os.environ.get("VDL_COOKIE_ALERT_WEBHOOK")
    if wh:
        try:
            requests.post(wh, json={"text": msg}, timeout=10)
        except Exception:
            pass


def _cookie_pool_watchdog() -> None:
    while True:
        try:
            from cookie_pool import verify_and_prune, all_domains
            for d in all_domains():
                try:
                    if verify_and_prune(d) == 0:
                        _cookie_pool_alert(d)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(1800)  # 30 分钟一轮


def _start_cookie_pool_watchdog() -> None:
    t = threading.Thread(target=_cookie_pool_watchdog, daemon=True)
    t.start()


def _cookie_pull_from_vps() -> None:
    """经反向隧道从 VPS 守护进程主动拉取 B站 Cookie 写入公共池。

    替代「VPS 主动直连 Railway 推送」：VPS→Railway 跨境公网链路抖动，直连推送
    频繁 TLS 握手超时失败（实测三次全挂）。改为 Railway 经本机隧道代理
    127.0.0.1:18889 主动拉取 VPS 本机 daemon 的 /v1/pull-cookie（入站，链路稳定），
    再把 Cookie 写入公共池。隧道未就绪时退避等待，不报错。
    """
    try:
        import requests
    except Exception:
        logger.warning("[cookie_pull] requests 不可用，跳过隧道拉取")
        return
    # 优先用显式配置的拉取 token；否则复用共享的同步 token（VDL_COOKIE_SYNC_TOKEN，
    # Railway 校验 /api/cookie/sync 时同款，两端天然一致，无需另配密钥）。
    token = os.environ.get("VDL_COOKIE_REFILL_TOKEN") or os.environ.get("VDL_COOKIE_SYNC_TOKEN", "")
    if not token:
        logger.warning("[cookie_pull] 未配置 VDL_COOKIE_REFILL_TOKEN / VDL_COOKIE_SYNC_TOKEN，跳过")
        return
    # 本节点若根本没有 daemon（例如香港只做网页/海外节点，解析 daemon 在国内 ECS），
    # 这条拉取链路就没有对象：直连模式会每 interval 秒刷一条「连接失败」日志。
    # 设 VDL_COOKIE_PULL_ENABLED=0 显式停用（公共池改由 App 直推解析入口节点）。
    if (os.environ.get("VDL_COOKIE_PULL_ENABLED", "1") or "1").strip().lower() in ("0", "false", "no", "off"):
        logger.info("[cookie_pull] 已由 VDL_COOKIE_PULL_ENABLED=0 停用（本节点无解析 daemon）")
        return
    interval = int(os.environ.get("VDL_COOKIE_PULL_INTERVAL", "1200"))  # 默认 20 分钟
    while True:
        try:
            import cn_tunnel as _cn
            # 直连模式（daemon 与后端同机，见 downloader.worker_proxy_url）无需隧道；
            # 仅当确实要走隧道代理时才等隧道就绪，否则直连部署会被这里永久卡住。
            proxy = downloader.worker_proxy_url()
            if proxy and not _cn._TUNNEL_READY.is_set():
                time.sleep(30)
                continue
            url = "http://127.0.0.1:18731/v1/pull-cookie?token=" + token
            # 显式同时覆盖 http/https 代理，避免环境变量 http_proxy/https_proxy 误伤本地请求；
            # 直连模式下传空串即显式禁用代理（None 反而会回落环境代理）。
            resp = requests.get(
                url,
                proxies={"http": proxy, "https": proxy} if proxy else {"http": "", "https": ""},
                timeout=90,
            )
            data = resp.json()
            cookie = (data or {}).get("cookie") or ""
            if data.get("ok") and cookie:
                from cookie_pool import add_cookie
                added = add_cookie("bilibili.com", cookie, source="vps-pull")
                logger.info("[cookie_pull] 已从 VPS 拉取并写入公共池 added=%s", added)
            else:
                logger.warning("[cookie_pull] VPS 返回空/失败: %s", str(data)[:160])
        except Exception as e:
            logger.warning("[cookie_pull] 拉取失败: %s", str(e)[:160])
        time.sleep(interval)


def _start_cookie_pull_from_vps() -> None:
    t = threading.Thread(target=_cookie_pull_from_vps, name="vdl-cookie-pull", daemon=True)
    t.start()


def _warm_cookie_pool() -> None:
    """启动时若公共池为空，主动召唤 VPS 守护进程补推（非阻塞，秒级填满）。

    覆盖「Railway 容器重建/重启导致持久卷被清空」的空窗：不再等 VPS 下次定时推送，
    启动即触发一次立即补推（request_refill 内部带 60s 冷却，避免重复召唤）。
    """
    try:
        from cookie_pool import get_cookie, request_refill, all_domains
        for d in all_domains():
            if get_cookie(d) is None:
                request_refill(d)
    except Exception:
        logger.exception("公共池启动预热异常")


# /v1/resolve 转发限流：按 IP 每 10 分钟最多 60 次（token 泄漏/滥用保护）
_VPS_RL: dict[str, list[float]] = {}
_VPS_RL_WINDOW = 600
_VPS_RL_MAX = 60
_VPS_RL_LOCK = threading.Lock()


def _vps_rate_ok(client_ip: str) -> bool:
    if _is_loopback_ip(client_ip):
        return True
    _now = time.time()
    with _VPS_RL_LOCK:
        q = _VPS_RL.setdefault(client_ip, [])
        while q and _now - q[0] > _VPS_RL_WINDOW:
            q.pop(0)
        if len(q) >= _VPS_RL_MAX:
            return False
        q.append(_now)
        return True


@app.get("/v1/resolve")
def vps_resolve_proxy(platform: str, url: str, token: str = "", cookie: str = "", request: Request = None) -> dict:
    """App 桌面端解析转发端点：/v1/resolve → 本机解析 daemon（18731）。

    桌面 App 的 downloader._call_vps_worker 会请求本端点（token 用 VDL_COOKIE_SYNC_TOKEN）。
    本端点校验 token 后复用与网页版相同的 `_call_vps_worker` 链路，返回与 daemon 一致的
    JSON（ok/video_url/title/error…），App 端零改动消费。

    两种部署形态：
      * 海外（Railway 时代）：经「18889 隧道代理 → WebSocket 反向隧道」到国内 ECS daemon；
      * 国内 ECS（2026-09-22 起，daemon 与 vdl-web 同机）：**直连** 127.0.0.1:18731，
        由 `VDL_WORKER_PROXY=direct` 显式关闭代理 —— 隧道上游（原 Railway 应用）已删除，
        走 18889 只会得到「视频解析服务不可达」。
    """
    expected = (
        os.environ.get("VDL_COOKIE_REFILL_TOKEN") or os.environ.get("VDL_COOKIE_SYNC_TOKEN", "")
    ).strip()
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail="无效令牌")
    _ip = (request.client.host if request and request.client else "") or ""
    if _ip and not _vps_rate_ok(_ip):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    try:
        return downloader._call_vps_worker(platform, url, cookie=cookie)
    except downloader.ResolveError as e:
        return {"ok": False, "error": str(e), "hint": getattr(e, "hint", "")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"转发失败: {type(e).__name__} {e}"}


@app.get("/api/cookie/pull-diag")
def cookie_pull_diag(mode: str = "full", token: str = "", request: Request = None) -> dict:
    """隧道健康/诊断接口（**已加令牌校验**，2026-10-12）。

    🔴 为什么忽然要令牌：本端点此前**没有任何鉴权**，而它经 nginx/CF 暴露在公网
    （2026-10-12 实测：`GET https://hanyuxz.top/api/cookie/pull-diag` 不带任何令牌
      返回 200），并且默认 `mode=full` 是**重操作**——会同步经隧道拉取 VPS Cookie
    并**写入公共池**。也就是「任何人都能空手触发一次跨隧道写池 + 占用出口带宽」，
    属未鉴权的状态变更端点。现与同文件 `/v1/resolve` 同口径校验令牌
    （`VDL_COOKIE_REFILL_TOKEN` / `VDL_COOKIE_SYNC_TOKEN`），令牌可经
    `Authorization: Bearer <t>`、`X-Api-Key: <t>` 或 `?token=<t>` 任一传入。
    自动化调用方：现有监控脚本明确不再用它当探针（`tunnel_monitor.sh` 注释），
    故收紧不影响任何在跑的定时任务；人工排查按上面三种方式带令牌即可。

    mode=quick：轻量健康探针——只返回本机状态字段（token_present / tunnel_ready /
            vps_ok / requests_available），秒回，不做任何跨隧道网络操作。
    mode=full（默认）：完整诊断——同步经隧道拉取 VPS Cookie 写公共池，返回逐环节
            结果与候选明细（重操作，响应可能数十秒，仅人工排查时使用）。

    两种模式均不含敏感内容（Cookie 只报长度不报明文）。
    """
    _expected = (
        os.environ.get("VDL_COOKIE_REFILL_TOKEN") or os.environ.get("VDL_COOKIE_SYNC_TOKEN", "")
    ).strip()
    _hdr = ""
    if request is not None:
        _auth = (request.headers.get("Authorization") or "").strip()
        if _auth.lower().startswith("bearer "):
            _hdr = _auth[7:].strip()
        else:
            _hdr = (request.headers.get("X-Api-Key") or "").strip()
    if not _expected or (_hdr or token or "").strip() != _expected:
        raise HTTPException(status_code=403, detail="无效令牌")

    import cn_tunnel as _cn
    out: dict = {
        "token_present": bool(os.environ.get("VDL_COOKIE_SYNC_TOKEN") or os.environ.get("VDL_COOKIE_REFILL_TOKEN")),
        "tunnel_ready": _cn._TUNNEL_READY.is_set(),
        "mode": mode,
    }
    if mode == "quick":
        # 轻量模式不做跨隧道网络操作：WS 连接存在即说明 VPS client 存活，
        # 以 tunnel_ready 近似 vps_ok；真实回源可用性由 /api/tunnel-test 验证。
        out["vps_ok"] = _cn._TUNNEL_READY.is_set()
        out["requests_available"] = True
        return out
    try:
        import requests
    except Exception as e:  # noqa: BLE001
        out["requests_available"] = False
        out["requests_error"] = str(e)[:160]
        return out
    out["requests_available"] = True
    tok = os.environ.get("VDL_COOKIE_REFILL_TOKEN") or os.environ.get("VDL_COOKIE_SYNC_TOKEN", "")
    proxy = downloader.worker_proxy_url()
    out["proxy_mode"] = proxy or "direct"
    url = "http://127.0.0.1:18731/v1/pull-cookie?token=" + tok
    try:
        # 显式同时覆盖 http/https 代理，避免环境变量代理误伤本地请求；直连模式传空串禁用代理
        resp = requests.get(
            url,
            proxies={"http": proxy, "https": proxy} if proxy else {"http": "", "https": ""},
            timeout=90,
        )
        out["http_status"] = resp.status_code
        try:
            data = resp.json()
            out["vps_ok"] = bool((data or {}).get("ok"))
            ck = (data or {}).get("cookie") or ""
            out["cookie_len"] = len(ck)
            if (data or {}).get("ok") and ck:
                from cookie_pool import (
                    add_cookie,
                    get_cookie,
                    _candidates,
                    _pool_file,
                    _decrypt_item,
                    _POOL_DIR,
                    _cipher,
                )
                out["added"] = add_cookie("bilibili.com", ck, source="vps-pull")
                # 细粒度诊断：看清 add 后文件落盘位置 / 读取候选 / 解密链路
                out["pool_dir"] = str(_POOL_DIR)
                out["mount"] = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or ""
                out["cipher_ok"] = _cipher() is not None
                out["candidates"] = []
                for d in _candidates("bilibili.com"):
                    f = _pool_file(d)
                    info = {"cand": d, "exists": f.exists()}
                    if f.exists():
                        try:
                            fdata = json.loads(f.read_text())
                            cookies = fdata.get("cookies", [])
                            info["n"] = len(cookies)
                            info["entries"] = []
                            for idx, c0 in enumerate(cookies):
                                ent = {
                                    "idx": idx,
                                    "ts": c0.get("ts"),
                                    "age": int(time.time() - (c0.get("ts") or 0)),
                                    "keys": sorted(c0.keys()),
                                    "header_len": len(c0.get("header") or ""),
                                    "enc_len": len(c0.get("header_enc") or ""),
                                    "decrypted_len": len(_decrypt_item(c0)),
                                }
                                info["entries"].append(ent)
                        except Exception as e:  # noqa: BLE001
                            info["read_err"] = str(e)[:120]
                    out["candidates"].append(info)
                out["pool_available_after"] = bool(get_cookie("bilibili.com"))
            else:
                out["vps_msg"] = str(data)[:160]
        except Exception as e:  # noqa: BLE001
            out["json_error"] = str(e)[:160]
            out["resp_head"] = resp.text[:160]
    except Exception as e:  # noqa: BLE001
        out["pull_error"] = str(e)[:200]
    return out


@app.get("/api/cookie/inject-diag")
def cookie_inject_diag(host: str = "") -> dict:
    """诊断：给定 host（如 v.youku.com / bilibili.com），返回 _base_options 解析时会注入的登录态来源。

    复刻 _base_options 的 Cookie 解析分支（本机缓存 → 公共池 → miss），但不真正下载。
    仅返回来源与脱敏长度，不含明文 Cookie。用于确认「共享池 Cookie 能否在解析时自动注入」
    （即 [5] 验证项）：贡献优酷 Cookie 后访问 /api/cookie/inject-diag?host=v.youku.com，
    resolved.source 应为 pool、len>0。
    """
    from urllib.parse import urlparse

    h = urlparse(host).netloc or host or ""
    if not h:
        return {"ok": False, "error": "missing host"}
    # 与 _base_options 同口径：b23.tv 回退 bilibili.com；其余用 host 本身
    cookie_host = "bilibili.com" if "b23.tv" in h else h
    out: dict = {"host": h, "cookie_host": cookie_host}
    # 1) 本机浏览器缓存（Railway 无浏览器，通常 miss）
    try:
        from cookie_cache import get_cached_cookie_header

        cached = get_cached_cookie_header(cookie_host)
    except Exception:
        cached = None
    out["cache"] = {"hit": bool(cached), "len": len(cached or "")} if cached else {"hit": False}
    # 2) 公共池（Railway 实际生效的兜底）
    try:
        from cookie_pool import get_cookie, _candidates, is_allowed

        pooled = get_cookie(cookie_host)
        out["pool"] = {
            "allowed": is_allowed(cookie_host),
            "candidates": _candidates(cookie_host),
            "hit": bool(pooled),
            "len": len(pooled or ""),
        }
    except Exception as e:  # noqa: BLE001
        out["pool"] = {"error": str(e)[:160]}
    # 3) 最终解析结论（与 _base_options 一致：cache 优先，否则 pool）
    resolved = cached or pooled
    out["resolved"] = {
        "source": "cache" if cached else ("pool" if pooled else "miss"),
        "len": len(resolved or ""),
    }
    out["ok"] = bool(resolved)
    return out


@app.post("/api/cookie/cache/clear")
def cookie_cache_clear() -> dict:
    """清除本机 Cookie 缓存（仅删 ~/.videodownloader/cookies，不影响浏览器本身）。"""
    from cookie_cache import clear_cookie_cache
    n = clear_cookie_cache()
    return {"ok": True, "cleared": n}


# 需要「转发给海外对端」的站点：网页版这些站的解析/下载由前端 baseFor() 直接
# 发到对端节点执行，Cookie 必须落在**对端**才生效；而桌面端 to-cloud 只认云端地址。
# 云端收下后转发一份即可让「桌面端 30min 自动推送」一路续到对端。
_PEER_RELAY_DOMAINS: tuple[str, ...] = ("youtube.com",)


def _relay_cookie_to_peer_if_needed(domain: str, header: str) -> dict | None:
    """把指定域的 Cookie 转发给海外对端节点（VDL_PEER_ENDPOINT）；非目标域返回 None。

    复用对端已有的 /api/cookie/sync（两端共用同一个 VDL_COOKIE_SYNC_TOKEN）。
    任何失败都只记日志、绝不影响本次同步的返回结果 —— 转发只是「锦上添花」，
    云端自身入池已成功。
    """
    try:
        from cookie_pool import _strip_sub
        if _strip_sub(domain) not in _PEER_RELAY_DOMAINS:
            return None
    except Exception:
        return None
    peer = (os.environ.get("VDL_PEER_ENDPOINT") or "").strip().rstrip("/")
    token = os.environ.get("VDL_COOKIE_SYNC_TOKEN", "")
    if not peer or not token or not peer.lower().startswith("http"):
        return {"relayed": False, "reason": "no_peer_or_token"}
    try:
        import time as _t
        import urllib.error
        import urllib.request

        body = json.dumps({"token": token, "domain": domain, "cookie": header}).encode()
        req = urllib.request.Request(
            peer + "/api/cookie/sync",
            data=body,
            headers={
                "Content-Type": "application/json",
                # 带浏览器 UA：对端可能挂在 Cloudflare 后面，Python-urllib 会被 1010 拦
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
            },
            method="POST",
        )
        # 对端也可能有同款限流 / 瞬时 5xx：对 429/5xx 做有限退避重试，
        # 保证「云端收到 → 续到对端」这一跳不因一次抖动而断（YouTube 自愈链路关键跳）。
        last: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    resp = json.loads(r.read().decode() or "{}")
                logger.info("[cookie_sync] relay domain=%s -> %s ok=%s", domain, peer, resp.get("ok"))
                return {"relayed": True, "resp": resp}
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                    _t.sleep(2 ** attempt)
                    continue
                raise
        raise last or RuntimeError("relay 失败")
    except Exception as e:  # noqa: BLE001
        logger.warning("[cookie_sync] relay domain=%s -> %s 失败: %s", domain, peer, str(e)[:160])
        return {"relayed": False, "reason": str(e)[:120]}


@app.post("/api/cookie/sync")
def cookie_sync(payload: dict, request: Request) -> dict:
    """App 端上报指定站点登录态到公共池（需用户知情同意 + 客户端令牌）。"""
    token = os.environ.get("VDL_COOKIE_SYNC_TOKEN", "")
    supplied = (payload or {}).get("token", "")
    if not token or supplied != token:
        raise HTTPException(status_code=403, detail="无效令牌")
    domain = (payload or {}).get("domain", "")
    cookie = (payload or {}).get("cookie", "")
    if not domain or not cookie:
        raise HTTPException(status_code=400, detail="缺少 domain/cookie")
    from cookie_pool import add_cookie, verify_cookie, is_allowed
    if not is_allowed(domain):
        raise HTTPException(status_code=400, detail="不支持的域名")
    ip = (request.client.host if request.client else "") or ""
    # 按「IP + 域」限流：桌面端一次「同步到云端」会按域批量推送十几次，
    # 按 IP 限流会把除第一个以外的域全部 429 掉（见 _sync_rate_ok 注释）。
    if not _sync_rate_ok(ip, domain):
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")
    ok = verify_cookie(domain, cookie)
    if ok is False:
        raise HTTPException(
            status_code=400,
            detail="Cookie 无效，未能通过目标站验真（请确认已登录且为完整 Cookie；"
                   "优酷/腾讯视频等站需含登录态字段如 P__yk__uck / vus_session）",
        )
    added = add_cookie(domain, cookie, source="sync")
    relay = _relay_cookie_to_peer_if_needed(domain, cookie)
    return {"ok": True, "added": added, "verified": (ok is True), "relay": relay}


@app.post("/api/cookie/contribute")
def cookie_contribute(payload: dict, request: Request) -> dict:
    """访客自愿把本次登录态贡献到公共池（需前端显式勾选 + 后端验真）。

    与 /api/cookie/sync（运维令牌写入）不同，本端点面向网页访客、无需令牌，
    但门槛更严格：① 仅白名单域名；② 单 IP 30s 一次限频；③ 必须 verify_cookie
    验真（明确无效才拒，网络不可达放行）；④ B站 裸 SESSDATA 自动补前缀。
    隐私保护：只有访客主动勾选才会入池，且其登录态将共享给所有访客。
    """
    from urllib.parse import urlparse
    url = (payload or {}).get("url", "")
    cookie = (payload or {}).get("cookie", "")
    ckey = (payload or {}).get("ckey", "")
    host = urlparse(url).netloc if url else ""
    from cookie_pool import add_cookie, add_ckey, is_allowed, verify_cookie, _norm_domain, _strip_sub
    domain = _strip_sub(_norm_domain(host))
    if not domain or not is_allowed(domain):
        raise HTTPException(status_code=400, detail="该平台暂不支持公共池贡献")
    cookie = (cookie or "").strip()
    if not cookie:
        raise HTTPException(status_code=400, detail="缺少 cookie")
    # B站 裸 SESSDATA 归一化，保证从池复用时不 403（与 _base_options 一致）
    if domain == "bilibili.com" and "=" not in cookie:
        cookie = f"SESSDATA={cookie}"
    ip = (request.client.host if request.client else "") or ""
    if not _sync_rate_ok(ip):
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")
    ok = verify_cookie(domain, cookie)
    if ok is False:
        raise HTTPException(
            status_code=400,
            detail="Cookie 无效，未能通过目标站验真（请确认已登录且为完整 Cookie；"
                   "优酷/腾讯视频等站需含登录态字段如 P__yk__uck / vus_session）",
        )
    added = add_cookie(domain, cookie, source="contrib")
    if ckey:
        add_ckey(domain, ckey, source="contrib")
        added = True
    logger.info("[cookie_pool] contrib domain=%s ip=%s added=%s ckey=%s", domain, ip, added, bool(ckey))
    relay = _relay_cookie_to_peer_if_needed(domain, cookie)
    return {"ok": True, "added": added, "verified": (ok is True), "ckey": bool(ckey), "relay": relay}


@app.post("/api/cookie/sync/from-local")
def cookie_sync_from_local(payload: dict, request: Request) -> dict:
    """仅本机(App 端)调用：读取本机浏览器指定站点登录态并上报到公共池。"""
    ip = (request.client.host if request.client else "") or ""
    if ip not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="仅允许本机调用")
    from cookie_pool import add_cookie, verify_cookie, is_allowed, _norm_domain
    domain = _norm_domain((payload or {}).get("domain") or "chrqj.com")
    if not is_allowed(domain):
        raise HTTPException(status_code=400, detail="不支持的域名")
    header = downloader.get_browser_cookie_header(domain, f"https://{domain}/")
    if not header:
        raise HTTPException(
            status_code=400,
            detail=f"本机浏览器未检测到 {domain} 登录态，请先在浏览器登录该站",
        )
    ok = verify_cookie(domain, header)
    if ok is False:
        raise HTTPException(status_code=400, detail="本机 Cookie 无效，未能通过目标站验真")
    added = add_cookie(domain, header, source="local")
    return {"ok": True, "added": added, "verified": (ok is True)}


def _cloud_sync_config() -> tuple[str, str]:
    """返回 (sync_url, token)。优先读 env，其次读本机配置文件。

    配置文件路径：~/.videodownloader/cloud_sync.json = {"url": "...", "token": "..."}
    桌面版用此配置把本机浏览器登录态推送到网页版(Railway)公共池。
    """
    url = (os.environ.get("VDL_COOKIE_SYNC_URL") or "").strip()
    token = (os.environ.get("VDL_COOKIE_SYNC_TOKEN") or "").strip()
    cfg = Path.home() / ".videodownloader" / "cloud_sync.json"
    if (not url or not token) and cfg.exists():
        try:
            d = json.loads(cfg.read_text(encoding="utf-8"))
            url = url or (d.get("url") or "").strip()
            token = token or (d.get("token") or "").strip()
        except Exception:
            pass
    return url, token


def _push_cookie_to_cloud(domain: str, header: str, url: str, token: str) -> dict:
    """把单站 Cookie 推送到云端公共池（POST /api/cookie/sync）。

    带浏览器 User-Agent 以绕过 Cloudflare 对 Python-urllib 的拦截（error 1010）。
    对 429/5xx 做有限指数退避重试，降低连发导致的限流（与 app-dev 同步加固）。
    """
    import time
    import urllib.request
    import urllib.error

    target = url.rstrip("/") + "/api/cookie/sync"
    body = json.dumps({"token": token, "domain": domain, "cookie": header}).encode()
    last_err: Exception | None = None
    for attempt in range(3):
        req = urllib.request.Request(
            target,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise
        except Exception as e:
            last_err = e
            raise
    raise last_err or RuntimeError("推送 Cookie 到云端失败")


@app.post("/api/cookie/sync/to-cloud")
def cookie_sync_to_cloud(request: Request) -> dict:
    """仅本机(App 端)调用：读取本机浏览器各强反爬站的登录态并推送到网页版公共池。

    这是「网页版由桌面版共享登录态」的核心：桌面版常驻时，自动把抖音/B站/快手等
    登录态推送到 Railway 公共池，网页版访客无需手动粘贴即可复用。
    """
    ip = (request.client.host if request.client else "") or ""
    if ip not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="仅允许本机调用")
    from cookie_pool import _root_domains, is_allowed, _norm_domain
    url, token = _cloud_sync_config()
    if not url or not token:
        raise HTTPException(
            status_code=400,
            detail="未配置云端同步地址/令牌。请在桌面版环境设置 VDL_COOKIE_SYNC_URL 与 "
                   "VDL_COOKIE_SYNC_TOKEN，或写入 ~/.videodownloader/cloud_sync.json",
        )
    results = []
    for domain in sorted(_root_domains()):
        if domain == "chrqj.com":
            # chrqj 不需要推到公共池（本身就是桌面专享），跳过
            continue
        try:
            header = downloader.get_browser_cookie_header(domain, f"https://{domain}/")
        except Exception:
            header = None
        if not header:
            results.append({"domain": domain, "pushed": False, "reason": "本机未登录"})
            continue
        try:
            resp = _push_cookie_to_cloud(domain, header, url, token)
            results.append({
                "domain": domain,
                "pushed": bool(resp.get("ok")),
                "verified": resp.get("verified"),
            })
        except Exception as e:
            results.append({"domain": domain, "pushed": False, "reason": str(e)[:120]})
    pushed = sum(1 for r in results if r.get("pushed"))
    return {"ok": True, "pushed": pushed, "total": len(results), "results": results}



def cookie_status(url: str = "") -> dict:
    """查询某链接是否需要 Cookie、本机浏览器是否已有可用登录态、以及公共池新鲜度。"""
    if not url:
        raise HTTPException(status_code=400, detail="请提供链接")
    _assert_safe_url(url)
    url, platform = parse_source(url)
    host = _host_of(url)
    info = downloader.detect_browser_cookie(host)
    pool_ts = None
    try:
        from cookie_pool import _candidates, _pool_file
        for d in _candidates(host):
            f = _pool_file(d)
            if f.exists():
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    for c in data.get("cookies", []):
                        ts = c.get("ts")
                        if ts and (pool_ts is None or ts > pool_ts):
                            pool_ts = ts
                except Exception:
                    pass
    except Exception:
        pass
    return {
        "host": host,
        "platform": platform.key,
        "needed": downloader.is_cookie_hardened_host(host),
        "available": info["available"],
        "browser": info["browser"],
        "profile": info["profile"],
        "pool_updated_at": pool_ts,
    }


# 首页：内联 styles.css，彻底消除刷新时无样式闪烁（FOUC）。
# 外部 styles.css 经 Cloudflare 回源美国主机往返约 1s，慢网络下浏览器可能先渲染无样式
# 内容。内联后首屏样式随 HTML 一同到达，零外部 CSS 请求。文件变更时按 mtime 自动重读。
#
# ⚠️ 失效判据必须同时看 index.html 与 styles.css 两个 mtime（2026-09-30 踩坑）：
# 原先只按 CSS mtime 失效，于是「只换 index.html、不动 CSS」的部署会**永远吐旧页**
# （进程内存里那份 HTML 不会被重读，连 CF 回源都是旧的，怎么刷新浏览器都没用）。
# 本次就是只改了 index.html + app.js，结果线上一直显示旧的「更多功能」页。
# 运维上还留着这个坑的放大效应：只要部署顺序里恰好也动了 CSS 就会被掩盖，很久才暴露一次。
import threading as _threading

_index_cache = {"html": None, "css_mtime": 0.0, "idx_mtime": 0.0}
_index_lock = _threading.Lock()


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    idx_path = os.path.join(WEB_DIR, "index.html")
    css_path = os.path.join(WEB_DIR, "styles.css")
    try:
        css_mtime = os.path.getmtime(css_path)
    except OSError:
        css_mtime = 0.0
    try:
        idx_mtime = os.path.getmtime(idx_path)
    except OSError:
        idx_mtime = 0.0
    with _index_lock:
        cache = _index_cache
        if (cache["html"] is None
                or cache["css_mtime"] != css_mtime
                or cache["idx_mtime"] != idx_mtime):
            try:
                html = open(idx_path, encoding="utf-8").read()
                css = open(css_path, encoding="utf-8").read()
                html = html.replace(
                    '<link rel="stylesheet" href="styles.css" />',
                    f'<style data-inline-css>{css}</style>',
                )
                cache["html"] = html
                cache["css_mtime"] = css_mtime
                cache["idx_mtime"] = idx_mtime
            except (OSError, FileNotFoundError):
                cache["html"] = cache["html"] or "<!doctype html><html><body>error</body></html>"
        return HTMLResponse(cache["html"])


# 2026-10-22：原 `_static_no_store` 中间件（对 js/css 一律 no-store）已删除——
# 它注册晚于 `_no_cache_frontend`，外层覆盖了后者的 5 分钟缓存策略，且与
# 「?v= 指纹」机制叠加后变成「URL 每次都不同 + 禁止缓存」双重浪费，是首屏
# 白屏数秒的根因之一。现策略统一在 `_no_cache_frontend`：HTML no-store +
# 注入指纹；JS/CSS 依赖 URL 指纹安全长缓存（immutable）。

app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
