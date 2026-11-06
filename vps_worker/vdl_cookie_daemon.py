#!/usr/bin/env python3
"""VDL Cookie 守护进程（VPS 常驻）。

职责：
  1) 后台线程每 VDL_COOKIE_PUSH_INTERVAL 秒（默认 120）循环推送 B站 Cookie 到网页版公共池；
  2) 提供轻量 HTTP 接口，供 Railway 在「容器重建/重启后公共池被清空」时主动召唤立即补推：
       POST /v1/push?token=<VDL_COOKIE_API_TOKEN>  -> 立即推送一次并返回结果
       GET  /healthz                               -> 200 健康检查
  3) 提供多平台 Playwright 解析接口（绕开已失效的 yt-dlp 提取器）：
       GET /v1/resolve?token=<token>&platform=<douyin|kuaishou>&url=<链接>
       （兼容旧路径 GET /v1/douyin/resolve、/v1/kuaishou/resolve）
  3.5) GET /v1/pull-cookie?token=<token> 返回本地缓存的 B站 Cookie，供 Railway 经
       反向隧道主动拉取（替代 VPS 主动推送，规避 VPS→Railway 跨境链路抖动）。

配置来自 .cookie_sync.env：VDL_COOKIE_SYNC_URL / VDL_COOKIE_SYNC_TOKEN / VDL_BILI_SESSDATA
  + VDL_COOKIE_API_TOKEN（Railway 调用 /v1/push、/v1/resolve 的令牌，务必与
    Railway 的 VDL_COOKIE_REFILL_TOKEN 一致）
  + VDL_COOKIE_API_PORT（默认 18731）/ VDL_COOKIE_PUSH_INTERVAL（默认 120）
"""
import os
import re
import signal
import subprocess
import sys
import time
import json
import threading
import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import logging
import atexit

logger = logging.getLogger("vdl-daemon")

# ── 2026-10-10 补日志捕获 ────────────────────────────────────────────────
# 背景：vdl-cookie 历史重启 12846 次，但风暴期查不到任何应用级 traceback。
# 实测三条原因：
#   ① 早期 unit 没有 StandardError=append（现已有，写 /opt/vdl-worker/daemon.log）；
#   ② 本模块从未 basicConfig —— logger 走 logging.lastResort，只放行 WARNING+，
#      INFO/DEBUG 全被丢弃，所以日志常年只有 3 行；
#   ③ 未捕获异常只在 stderr 打一次且无时间戳/线程名，daemon 线程内异常尤其难定位。
# 这里补：显式 handler 写 stderr（unit 已 append 落盘）+ 两个 excepthook，
# 保证任何未捕获异常都带完整栈、时间戳、线程名落盘。
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _pool_douyin_cookie() -> str | None:
    """从云端公共 Cookie 池取最新有效的抖音登录态（明文 header），供抖音解析注入。

    背景：抖音 Argus 风控上线后，游客态被确定性拦截。池里已有用户经浏览器扩展/手动
    贡献的有效抖音登录态（profile_self 真探针验证 status_code=0），但抖音解析（本 daemon）
    此前从没读取这个池——这是「自动获取 Cookie 能力」与抖音解析之间的断点。
    本函数补齐：优先消费池里有效登录态，登录态无效/缺失时返回 None，由调用方回落游客态。

    池目录与 vdl-web 同机同路径（~/.videodownloader/cookie_pool，无 ENC_KEY 时明文存储），
    此处直接读文件（零跨模块耦合）；仅取 30 天内、含 sessionid 的最新一条。
    """
    try:
        pool_dir = os.environ.get("VDL_COOKIE_POOL_DIR") or os.path.join(
            os.path.expanduser("~"), ".videodownloader", "cookie_pool")
        f = os.path.join(pool_dir, "douyin.com.json")
        if not os.path.isfile(f):
            return None
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        entries = data.get("cookies", []) if isinstance(data, dict) else []
        now = time.time()
        for c in sorted(entries, key=lambda x: x.get("ts", 0), reverse=True):
            if now - c.get("ts", 0) > 30 * 86400:
                continue
            h = c.get("header") or c.get("header_enc") or ""
            if h and "sessionid=" in h:
                return h
    except Exception as e:  # noqa: BLE001
        logger.warning("[cookie-pool] 读抖音登录态失败: %s", str(e)[:120])
    return None


def _vdl_excepthook(exc_type, exc, tb):
    logger.critical("未捕获异常，进程即将退出", exc_info=(exc_type, exc, tb))
    try:
        sys.stderr.flush()
    except Exception:
        pass


def _vdl_thread_excepthook(args):
    logger.critical(
        "线程 %s 抛出未捕获异常，该线程退出（主进程仍在运行，功能可能已半残）"
        % getattr(getattr(args, "thread", None), "name", "?"),
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


sys.excepthook = _vdl_excepthook
threading.excepthook = _vdl_thread_excepthook

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bilibili_ecs_cookie import push_once, _collect_cookies, _push_to_cloud  # noqa: E402
from douyin_resolve import resolve as douyin_resolve  # noqa: E402
import douyin_direct  # noqa: E402  抖音 a_bogus 签名直连（快路，免 Chromium）
from kuaishou_resolve import resolve as kuaishou_resolve  # noqa: E402
from weibo_resolve import resolve as weibo_resolve  # noqa: E402
from ximalaya_album_resolve import resolve_album as ximalaya_album_resolve  # noqa: E402
from iqiyi_resolve import resolve as iqiyi_resolve  # noqa: E402
from douyu_resolve import resolve as douyu_resolve  # noqa: E402
from ysp_resolve import resolve as ysp_resolve  # noqa: E402
from m1905_resolve import resolve as m1905_resolve  # noqa: E402
from fun_resolve import resolve as fun_resolve  # noqa: E402
from bestv_resolve import resolve as bestv_resolve  # noqa: E402
from hongguo_resolve import resolve as hongguo_resolve  # noqa: E402
from inke_resolve import resolve as inke_resolve  # noqa: E402
from cc_resolve import resolve as cc_resolve  # noqa: E402
from weishi_resolve import resolve as weishi_resolve  # noqa: E402
from finder_resolve import resolve as finder_resolve  # noqa: E402
from xiaohongshu_resolve import resolve as xiaohongshu_resolve  # noqa: E402
from lespark_resolve import resolve as lespark_resolve  # noqa: E402
from liblib_resolve import resolve as liblib_resolve  # noqa: E402


def _load_env_file(path: str) -> None:
    """启动时自加载同目录 .cookie_sync.env（VAR=value 或 VAR='value' 格式）。

    不覆盖已存在的环境变量（显式注入的优先）。这样无论 daemon 由 systemd /
    nohup / 手动哪种方式启动，配置都可靠加载，不会因外部 source 失效而丢 token。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


_load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cookie_sync.env"))

API_TOKEN = os.environ.get("VDL_COOKIE_API_TOKEN", "")
API_PORT = int(os.environ.get("VDL_COOKIE_API_PORT", "18731"))
SYNC_URL = os.environ.get("VDL_COOKIE_SYNC_URL", "https://hanyuxz.top")
SYNC_TOKEN = os.environ.get("VDL_COOKIE_SYNC_TOKEN", "")
SESSDATA = os.environ.get("VDL_BILI_SESSDATA", "")
INTERVAL = int(os.environ.get("VDL_COOKIE_PUSH_INTERVAL", "120"))

_lock = threading.Lock()
# 推送锁：仅串行化「云端 Cookie 推送」自身（推送不启动 Chromium）。
_push_lock = threading.Lock()
# 解析锁：串行化各平台 Playwright 解析，确保任意时刻只有一个 Chromium 实例
# （VPS 内存吃紧，并发会卡死）。注意：解析与推送各用各的锁，互不阻塞——
# 否则推送因网络抖动（urlopen 无 timeout）长期占锁会把所有 /v1/resolve 饿死。
_resolve_lock = threading.Lock()
_state = {"ts": 0, "ok": None, "msg": ""}
# 主动推送开关：默认开启；设为 false 时仅收集并缓存 Cookie（供 Railway 经隧道拉取），
# 不再直连 Railway 推送——规避 VPS→Railway 跨境公网链路抖动导致的推送失败。
PUSH_ENABLED = os.environ.get("VDL_COOKIE_PUSH_ENABLED", "true").lower() in ("1", "true", "yes")
# 本地缓存有效期（秒）：/v1/pull-cookie 在有效期内直接返回缓存，避免每次起 Chromium。
COOKIE_TTL = int(os.environ.get("VDL_COOKIE_CACHE_TTL", "1800"))
# 最近一次收集到的 B站 Cookie 缓存（供 Railway 经隧道 /v1/pull-cookie 拉取）。
_cached = {"cookie": "", "cookies": [], "ts": 0, "passed": False, "hit_slider": False}

# 平台 → Playwright 解析函数映射（新增平台在此注册即可）
_RESOLVERS = {
    "douyin": douyin_resolve,
    "kuaishou": kuaishou_resolve,
    "weibo": weibo_resolve,
    "ximalaya_album": ximalaya_album_resolve,
    "iqiyi": iqiyi_resolve,
    "douyu": douyu_resolve,
    "yangshipin": ysp_resolve,
    "1905": m1905_resolve,
    "funshion": fun_resolve,
    "bestv": bestv_resolve,
    "hongguo": hongguo_resolve,
    "inke": inke_resolve,
    "cc": cc_resolve,
    "weishi": weishi_resolve,
    "finder": finder_resolve,
    "xiaohongshu": xiaohongshu_resolve,
    "lespark": lespark_resolve,
    "liblib": liblib_resolve,
}


PUSH_RETRIES = int(os.environ.get("VDL_COOKIE_PUSH_RETRIES", "2"))
PUSH_RETRY_BACKOFF = int(os.environ.get("VDL_COOKIE_PUSH_BACKOFF", "15"))
# 后台收集的硬上限。_collect_cookies 正常最坏约 88s（两次 goto 30s + 3s 等待 +
# 25s 轮询），留余量取 150s。超时必须放弃本轮并清理 chromium：该函数持有
# _resolve_lock，一旦 chromium 卡死（内存压力下渲染器 D 状态抖动），锁永不释放，
# 所有用户解析都会被判「解析服务正忙」——2026-11-05 实测卡死 12min 即此故。
COLLECT_TIMEOUT = int(os.environ.get("VDL_COOKIE_COLLECT_TIMEOUT", "150"))


def _collect_and_cache():
    """串行起 Chromium 收集一次 B站 Cookie 并写入 _cached（不推送）。

    用 _resolve_lock 串行化，避免与 /v1/resolve 并发起多个 Chromium 撑爆 VPS 内存。
    等待上限 8s：后台收集是低优先级，用户解析优先，等不到就跳过本轮（120s 后重试）。
    """
    if not _resolve_lock.acquire(blocking=False):
        if not _resolve_lock.acquire(timeout=8):
            logger.info("[collect] 解析占用中，跳过本轮收集（120s 后重试）")
            return None
    try:
        # 硬超时兜底：见 COLLECT_TIMEOUT 注释。线程无法强杀，但必须保证
        # ①锁一定释放（否则用户解析被饿死）②卡死的 chromium 被 SIGKILL。
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(_collect_cookies, SESSDATA)
        try:
            header, passed, hit_slider, cookies = fut.result(timeout=COLLECT_TIMEOUT)
        except concurrent.futures.TimeoutError:
            ex.shutdown(wait=False)
            _kill_all_chromium()
            logger.warning("[collect] 收集超时(>%ss)，已清理 chromium 并跳过本轮",
                           COLLECT_TIMEOUT)
            with _lock:
                _state.update(ts=int(time.time()), ok=False,
                              msg="collect timeout >%ss" % COLLECT_TIMEOUT)
            return None
        except Exception as e:  # noqa: BLE001
            ex.shutdown(wait=False)
            _kill_all_chromium()
            logger.warning("[collect] 收集失败，已清理 chromium: %s", str(e)[:150])
            raise
        ex.shutdown(wait=False)
        with _lock:
            _cached.update(cookie=header, cookies=cookies, ts=int(time.time()),
                           passed=passed, hit_slider=hit_slider)
        return header
    finally:
        _resolve_lock.release()


def _push_once_to_cloud(header):
    """推送一次到云端，返回 (ok, msg)。仅封装底层，不含重试。"""
    try:
        resp = _push_to_cloud(SYNC_URL, SYNC_TOKEN, header)
        return bool(resp and resp.get("ok")), str(resp)[:200]
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


def do_push(wait: bool = True):
    """收集并（可选）推送一次 B站 Cookie。

    - 收集始终执行并写入 _cached（Railway 可经隧道拉取，独立于推送成败）。
    - 推送受 VDL_COOKIE_PUSH_ENABLED 开关控制：默认开启，采用「重试 + 退避」，
      且推送与解析已拆锁（见 _resolve_lock），推送抖动不再饿死 /v1/resolve。
    - 推送失败仅更新状态、不刷屏（避免 VPS→Railway 跨境链路抖动时日志刷屏）。
    - 关闭主动推送（PUSH_ENABLED=false）时仅维持缓存供 Railway 拉取。
    """
    if not PUSH_ENABLED:
        try:
            _collect_and_cache()
        except Exception as e:  # noqa: BLE001
            with _lock:
                _state.update(ts=int(time.time()), ok=False,
                              msg="collect failed: %s" % str(e)[:120])
        return False, {"error": "push disabled (Railway pulls instead)"}
    if not _push_lock.acquire(blocking=False):
        if not wait:
            return False, {"error": "push already in progress"}
        _push_lock.acquire()  # 阻塞等待上一轮完成
    try:
        try:
            header = _collect_and_cache()
        except Exception as e:  # noqa: BLE001
            with _lock:
                _state.update(ts=int(time.time()), ok=False,
                              msg="collect failed: %s" % str(e)[:120])
            return False, {"error": "collect failed: %s" % str(e)[:120]}
        last_err = None
        for attempt in range(1, PUSH_RETRIES + 1):
            ok, msg = _push_once_to_cloud(header)
            if ok:
                with _lock:
                    _state.update(ts=int(time.time()), ok=True, msg="pushed ok")
                return True, {"ok": True, "msg": msg}
            last_err = msg
            if attempt < PUSH_RETRIES:
                time.sleep(PUSH_RETRY_BACKOFF)
        with _lock:
            _state.update(ts=int(time.time()), ok=False,
                          msg="push failed after %d tries: %s" % (PUSH_RETRIES, last_err))
        return False, {"error": "push failed after %d tries: %s" % (PUSH_RETRIES, last_err)}
    finally:
        _push_lock.release()


def _kill_all_chromium():
    """清掉所有残留 chrome-headless 进程（Playwright 的 browser.close() 偶发漏杀，
    1.6GB 小内存 VPS 上孤儿 chromium 会拖慢后续解析甚至导致 Railway 90s 超时）。
    do_resolve 串行化 + 每次解析前清理，保证同一时刻只有本次解析的 chromium 存活。

    🔴 关键：SIGKILL 对 D（disk sleep）状态进程**不生效**，要等 I/O 完成才退出。
    2026-11-05 事故就是孤儿渲染器卡在 D 状态反复读磁盘 shm（51MB/s）达 12min，
    杀不掉又不断拖垮整机。这里杀完必须复核，把「杀不死的」显式记 ERROR，
    否则下次同类问题只表现为「网站变慢」，无从定位。
    """
    def _chromium_pids():
        try:
            out = subprocess.run(
                ["ps", "-eo", "pid=,stat=,comm="],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except Exception:  # noqa: BLE001
            return []
        pids = []
        for line in out.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) != 3 or "chrom" not in parts[2].lower():
                continue
            # Z=zombie：已经死了、只等父进程回收，不是「杀不掉的活进程」，
            # 计进存活会造成假告警（2026-11-05 首版实测踩到）。
            if parts[1].startswith("Z"):
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            if pid > 0 and pid != os.getpid():
                pids.append(pid)
        return pids

    for pid in _chromium_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    survivors = _chromium_pids()
    if survivors:
        try:
            states = subprocess.run(
                ["ps", "-o", "pid=,state=,etime=,args=", "-p",
                 ",".join(str(p) for p in survivors)],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip().replace("\n", " | ")
        except Exception:  # noqa: BLE001
            states = str(survivors)
        logger.error("[chromium] SIGKILL 后仍有 %d 个存活（多半卡在 D 状态等 I/O，"
                     "会持续吃磁盘）：%s", len(survivors), states[:500])


_CACHE_SUBDIRS = (
    "Default/Cache", "Default/Code Cache", "Default/GPUCache",
    "Default/DawnWebGPUCache", "Default/DawnGraphiteCache",
    "Default/Service Worker/CacheStorage", "Default/Service Worker/ScriptCache",
)

# 单 profile 缓存上限 / 削峰水位（MB）。Chromium 纯缓存对解析零价值，
# 超限即削，防止 1.6GB 小机器被磁盘 I/O 拖成 11-05 那种 iowait 雪崩。
CACHE_CAP_MB = 60.0
CACHE_TRIM_TO_MB = 30.0
# 最近 10 分钟写入的文件不动：可能正被运行中的会话使用。
CACHE_FRESH_SEC = 600.0


def _prune_profile_caches(max_age_hours: float = 48.0,
                          cap_mb: float = CACHE_CAP_MB,
                          trim_to_mb: float = CACHE_TRIM_TO_MB,
                          fresh_sec: float = CACHE_FRESH_SEC,
                          root=None) -> int:
    """清理各 Playwright profile 目录下的 Chromium 缓存（保留登录态数据）。

    背景：bili_profile 等持久 profile 是「登录态持久化」设计（删了要重新扫码），
    但其中 Default/Cache、Code Cache、GPUCache 等纯缓存目录会无限膨胀
    （实测 bili_profile 1.1G 里 1004M 是 Cache）。这里只删缓存目录，
    不动 Cookies/IndexedDB/Local Storage 等登录态数据。

    两道闸（2026-11-07 补第二道）：
      1) 超龄：mtime 超过 ``max_age_hours`` 的文件直接删；
      2) 超量：单个 profile 缓存总量 > ``cap_mb`` 时，按 mtime 从旧到新删到
         ``trim_to_mb`` 水位。**只靠第一道不够** —— 短命会话一天就能写出
         上百 MB（实测 weibo_profile 103MB、bili_profile 曾 768MB/24h），
         这些文件都在 48h 内，等不到超龄就已经把磁盘 I/O 拖垮。
      两道都跳过最近 ``fresh_sec`` 秒内写入的文件，避免干扰正在跑的会话。
    """
    root = Path(root) if root is not None else Path(__file__).resolve().parent
    now = time.time()
    n = 0
    freed = 0
    try:
        profiles = [p for p in root.glob("*_profile") if p.is_dir()]
    except Exception:
        return 0
    for prof in profiles:
        # 先收集该 profile 全部缓存文件（size, mtime, path）
        entries = []
        for cache_dir in _CACHE_SUBDIRS:
            p = prof / cache_dir
            if not p.exists():
                continue
            try:
                for f in p.rglob("*"):
                    if not f.is_file():
                        continue
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    entries.append((st.st_size, st.st_mtime, f))
            except Exception:
                continue
        if not entries:
            continue
        # 闸 1：超龄
        keep = []
        for sz, mt, f in entries:
            if now - mt > max_age_hours * 3600:
                try:
                    f.unlink(missing_ok=True)
                    freed += sz
                    n += 1
                    continue
                except OSError:
                    pass
            keep.append((sz, mt, f))
        # 闸 2：超量（按 mtime 从旧到新削）
        total = sum(e[0] for e in keep)
        if total > cap_mb * 1024 * 1024:
            target = trim_to_mb * 1024 * 1024
            for sz, mt, f in sorted(keep, key=lambda e: e[1]):
                if total <= target:
                    break
                if now - mt <= fresh_sec:
                    continue
                try:
                    f.unlink(missing_ok=True)
                    total -= sz
                    freed += sz
                    n += 1
                except OSError:
                    pass
        # 清空后回收空的深层目录（目录本身保留，Chromium 会自建）
        for cache_dir in _CACHE_SUBDIRS:
            p = prof / cache_dir
            if not p.is_dir():
                continue
            try:
                for d in sorted(p.rglob("*"), key=lambda x: -len(str(x))):
                    if d.is_dir() and not any(d.iterdir()):
                        try:
                            d.rmdir()
                        except OSError:
                            pass
            except Exception:
                pass
    if n:
        logger.info("[prune] profile caches: removed %s files, freed %s MB",
                    n, int(freed / 1024 / 1024))
    return n


def _prune_loop():
    """每小时清一次 profile 缓存；启动先跑一次。

    12h 太久：短命会话一轮就能写出几十 MB，等下一轮前磁盘已在高位。
    每小时只是 walk 一遍缓存目录（毫秒级），成本可忽略。
    """
    while True:
        try:
            _prune_profile_caches()
        except Exception:
            pass
        time.sleep(3600)


def do_resolve(platform, url, cookie=""):
    """用 Playwright 解析指定平台视频。用 _resolve_lock 串行化 Chromium，
    与推送锁独立，避免被云端推送的网络抖动饿死。

    ``cookie``：可选登录态 Cookie，仅透传给支持该参数的平台（如 finder 微信
    视频号 worker 用其注入浏览器会话）；其余平台 resolver 不接受该参数，传空串。
    """
    resolver = _RESOLVERS.get(platform)
    if resolver is None:
        return False, "不支持的平台: %s" % platform
    # 抖音快路：a_bogus 签名纯 HTTP 直连（免 Chromium、不占 _resolve_lock，
    # 实测 ~0.7s/条 vs Playwright 20s+）。签名被抖音风控升级打掉或链接
    # 非视频页时才回落 Playwright 浏览器方案，快路失败对用户透明。
    if platform == "douyin":
        try:
            # 优先用云端池里的有效抖音登录态（突破 Argus 第一层：登录态缺失）；
            # 缺失/失效时传 None，回落游客态（原行为）。注意 Argus 还有第二层 UIFID
            # 设备指纹校验，仅靠登录态不足以完全突破，详见运维记录。
            cookie = _pool_douyin_cookie()
            return True, douyin_direct.resolve(url, cookie_header=cookie)
        except (douyin_direct.VideoNotFoundError, douyin_direct.ArgusBlockedError) as e:
            # 确定性结论（视频不存在 / Argus 风控拦截）：回落浏览器也是同样结果
            return False, str(e)
        except Exception as e:  # noqa: BLE001
            logger.warning("[douyin-direct] 直连失败，回落 Playwright: %s", str(e)[:150])
    if not _resolve_lock.acquire(blocking=False):
        # 上一轮 Chromium 还在跑：最多等 8s（避免并发请求无限排队 + 线程堆积
        # 撑爆小内存 VPS；等不到就返回「解析忙」让 Railway 端给用户可读提示）
        if not _resolve_lock.acquire(timeout=8):
            return False, "解析服务正忙（上一个解析任务未完成），请稍后重试"
    try:
        # 解析前清残留 chromium：上一轮若异常退出（客户端断连/超时杀线程），
        # browser.close() 的 finally 未必执行，孤儿会占内存拖慢本轮。
        _kill_all_chromium()
        # 硬超时兜底：worker 内部已有时间预算，但 VPS 内存压力下 Playwright 启动
        # 可能显著变慢（>90s），超出 Railway 限时。这里强制 85s 上限，超时杀
        # chromium 并返回干净错误，避免请求堆积在 _resolve_lock 后饿死后续请求。
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # 仅 finder 平台透传 cookie（其余 resolver 无 cookie 参数，避免 TypeError）
        if platform == "finder" and cookie:
            fut = ex.submit(resolver, url, cookie=cookie)
        else:
            fut = ex.submit(resolver, url)
        try:
            result = fut.result(timeout=85)
            ex.shutdown(wait=False)
            return True, result
        except concurrent.futures.TimeoutError:
            # shutdown(wait=False)：不阻塞等待卡死的 worker 线程（线程无法强杀，
            # 但 chromium 已被清掉，线程会在下次访问页面时自然抛错退出）
            ex.shutdown(wait=False)
            _kill_all_chromium()
            return False, "解析超时（>85s，VPS 资源紧张或平台变慢），请稍后重试"
    except Exception as e:
        _kill_all_chromium()
        return False, str(e)[:300]
    finally:
        _kill_all_chromium()
        _resolve_lock.release()


def _loop():
    while True:
        do_push()
        time.sleep(INTERVAL)


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_token(self, q):
        token = q.get("token", [""])[0]
        if not token:
            return False
        # 接受两种 token：本机 API token（VDL_COOKIE_API_TOKEN），或共享的同步
        # token（VDL_COOKIE_SYNC_TOKEN）。Railway 经隧道拉取时复用共享 sync token 即可，
        # 无需在两端另配独立密钥。
        if API_TOKEN and token == API_TOKEN:
            return True
        if SYNC_TOKEN and token == SYNC_TOKEN:
            return True
        return False

    def do_GET(self):
        p = urlparse(self.path)
        path = p.path
        q = parse_qs(p.query)
        if path == "/healthz":
            self._send(200, {"ok": True, "ts": int(time.time())})
            return
        # 供 Railway 经反向隧道主动拉取本地缓存的 B站 Cookie（替代 VPS 主动推送，
        # 规避 VPS→Railway 跨境公网链路抖动）。Railway 经本机隧道代理 127.0.0.1:18889
        # 转发到本机 127.0.0.1:18731 即达此端点。
        if path == "/v1/pull-cookie":
            if not self._check_token(q):
                self._send(403, {"error": "forbidden"})
                return
            with _lock:
                fresh = _cached.get("cookie") and (time.time() - _cached["ts"]) < COOKIE_TTL
            if not fresh:
                try:
                    _collect_and_cache()
                except Exception:  # noqa: BLE001
                    pass  # 收集失败也尽量返回已有缓存
            with _lock:
                c = dict(_cached)
            self._send(200, {
                "ok": True,
                "domain": "bilibili.com",
                "cookie": c.get("cookie", ""),
                "cookies": c.get("cookies", []),
                "ts": c.get("ts", 0),
                "passed": c.get("passed", False),
                "hit_slider": c.get("hit_slider", False),
            })
            return
        # 通用 resolve 端点：/v1/resolve?platform=xx&url=yy
        # 兼容旧路径 /v1/<platform>/resolve?url=yy
        platform = None
        if path == "/v1/resolve":
            platform = q.get("platform", [""])[0]
        elif path.startswith("/v1/") and path.endswith("/resolve"):
            platform = path[len("/v1/"):-len("/resolve")]
        if platform is not None:
            if not self._check_token(q):
                self._send(403, {"error": "forbidden"})
                return
            url = q.get("url", [""])[0]
            if not url:
                self._send(400, {"error": "missing url"})
                return
            # 可选登录态 Cookie（如微信视频号 finder 需要）；其余平台忽略
            cookie = q.get("cookie", [""])[0]
            ok, result = do_resolve(platform, url, cookie=cookie)
            if not ok:
                # 业务失败（链接无效/需登录/视频删除/未开播等）返回 200 + ok:false，
                # 让 Railway 端 _call_vps_worker 透传真实业务原因；只有解析器
                # 内部异常才返回 5xx。
                self._send(200, {"ok": False, "error": result})
                return
            # 成功：透传 worker 返回的 dict（worker 内部自带 ok:true）
            self._send(200, result)
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path)
        if p.path == "/v1/push":
            q = parse_qs(p.query)
            if not self._check_token(q):
                self._send(403, {"error": "forbidden"})
                return
            ok, resp = do_push(wait=False)
            with _lock:
                st = dict(_state)
            self._send(200, {"ok": ok, "result": resp, "last": st})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


def main():
    t = threading.Thread(target=_loop, daemon=True, name="cookie-loop")
    t.start()
    # profile 缓存清理（12h 周期）：防持久 profile 的 Chromium 缓存无限膨胀撑爆 VPS 磁盘
    tp = threading.Thread(target=_prune_loop, daemon=True, name="prune-loop")
    tp.start()
    srv = ThreadingHTTPServer(("0.0.0.0", API_PORT), _Handler)
    print(f"✅ cookie daemon up: loop={INTERVAL}s api=0.0.0.0:{API_PORT}", flush=True)
    logger.info("cookie daemon 启动: loop=%ss api=0.0.0.0:%s pid=%s", INTERVAL, API_PORT, os.getpid())
    atexit.register(lambda: logger.info("cookie daemon 正常退出 pid=%s", os.getpid()))
    try:
        srv.serve_forever()
    except BaseException:
        logger.critical("HTTP 服务循环异常终止", exc_info=True)
        raise


if __name__ == "__main__":
    main()
