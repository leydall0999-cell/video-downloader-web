#!/usr/bin/env python3
"""平台健康巡检（最小闭环）：样本实测 + 域名级成功遥测 + 需求榜，三源合成健康表。

三个证据源，互为补充：
  1. **样本实测**：对该平台样本链接真实打一次 /api/resolve（最准，但要维护样本库）；
  2. **成功遥测**（2026-10-31 新增，主力）：解析成功只落 host + 时间戳（**不记 URL**），
     谁被真实用户解析成功了就证明谁活着。**不需要样本库**，也就没有「链接烂掉」
     的维护成本，且天然只覆盖有真实流量的平台——那正是「坏了才值得修」的那些；
  3. **需求榜**（失败侧）：用户要但没拿到（pending_extractor / unsupported_platform）。

设计要点（均为实测修正，勿回退）：
  * **多样本多数判定**：单样本会误报。实测 B站 3 条样本中仅 1 条 412 失败
    （该视频**内容级**限制：地区/会员），若只测 1 条就会误判「B站挂了」。
  * **区分「平台失效」与「样本失效」**：解析失败时回查样本链接是否还活着
    （HTTP 4xx/5xx 或页面含「删除/失效/不存在」）→ 样本失效属**维护问题**，
    不算平台坏了，需换样本重测。
  * **无样本不装数据**：没有样本也没有遥测/需求证据的平台，显示 `unknown`
    （诚实的未知），而不是假装健康。

状态分档：
  healthy        样本全通，或（无样本但）近期有成功遥测
  degraded       样本部分通
  sample_invalid 样本全挂且样本自身也死了（维护问题，非平台坏）
  broken         样本全挂且样本仍活着 ⇒ 平台真的坏了（会触发自动修复）
  repaired_pending_restart  自动升 yt-dlp 后复测已通过，但线上进程仍在跑旧版，
                 需重启 vdl-web 才真正生效
  needs_manual   升版无效且根因明确（登录墙/付费/地区/缺提取器）⇒ 人力才可能解决
  demand_only    无样本、无成功遥测，但有用户需求 ⇒ 有人要却从没成功过，值得排查
  unknown        三源皆无证据
  no_sample      无样本（保留，供样本库补齐进度用）

自动修复（2026-11-01 接入，见 platform_autorepair）：
  仅对 `broken` 触发 —— 升 yt-dlp（`ydlp_update.update()`）→ **子进程**复测
  （新进程 bootstrap 才用得到新版；运行中进程装完仍是旧的）→ 有效则标
  `repaired_pending_restart`（**不自动重启生产服务**），无效则按根因落派单
  `data/repair_tickets.json`。升版是全局动作，故成功后会回归校验其它平台样本，
  一旦「修好 A 弄坏 B」判 `regressed_other` 并建议回滚。
  `degraded` 不触发：实测多为内容级限制（B站某视频 412），升版无用且白担风险。
  环境变量 VDL_HEALTH_AUTOREPAIR=0 可关闭自动修复。

  yt-dlp 主动更新（2026-11-10 新增）：除 broken 时自动修复层升版外，**每次巡检都主动检查并
  安装当前解释器可用的最新兼容版**（`ydlp_update.update()`，sha256 校验 + 影子目录原子替换）。
  装完后若本次确有新版本 → 巡检结束**自动重启 vdl-web** 使其生效（不再只写 PENDING_RESTART
  标记等人手动重启）。开关：VDL_HEALTH_YDLP_UPDATE=0 关闭主动更新；VDL_HEALTH_APPLY_RESTART=0
  关闭自动重启（退回「只装影子目录 + 标记」的旧行为）。重启命令 VDL_HEALTH_RESTART_CMD，默认
  `systemctl restart vdl-web`。
  另采集 ffmpeg / Playwright / Chromium / Node / Python 当前版本写入健康表 `tools` 字段，
  供后台「平台健康」面板展示（这些工具只报告、不自动升级；只有 yt-dlp 会主动更新）。

用法：
    python platform_health_check.py                 # 全量巡检
    python platform_health_check.py bilibili douyin # 只测指定平台 key
    VDL_HEALTH_TELEMETRY_DAYS=7 python platform_health_check.py
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
ENDPOINT = os.environ.get("VDL_HEALTH_ENDPOINT", "http://127.0.0.1:18890/api/resolve")
_HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES_PATH = os.environ.get(
    "VDL_PLATFORM_SAMPLES", os.path.join(_HERE, "platform_samples.json"))
OUT_PATH = os.environ.get("VDL_HEALTH_OUT", os.path.join(_HERE, "data", "platform_health.json"))
PER_URL_TIMEOUT = int(os.environ.get("VDL_HEALTH_TIMEOUT", "90"))
TELEMETRY_DAYS = int(os.environ.get("VDL_HEALTH_TELEMETRY_DAYS", "30"))
DEMAND_DAYS = int(os.environ.get("VDL_HEALTH_DEMAND_DAYS", "84"))
DATA_DIR = os.environ.get("VDL_DATA_DIR") or str(Path.home() / ".video-downloader")
_DEAD_MARKERS = ("删除", "失效", "不存在", "已下线", "404", "not found", "removed", "deleted")
# 稿件级不可播（≠ 平台故障）：判据与 downloader._bilibili_api_extract 的可读文案对齐
_CONTENT_UNAVAILABLE_MARKERS = (
    "不可播放", "稿件不可见", "已删除", "已下架", "设为私有",
    "大会员专享", "仅限特定地区", "粉丝可见",
)
# 平台级风控拦截（≠ 样本失效）：解析失败但样本链接本身没死，是平台整体被拦
# （如抖音 Argus 安全插件，hint 自带「风控」字样）。此类失败样本按存活计，
# 使全挂时正确判 broken → 触发自动修复层 → 根因归类 anti_bot → 落派单。
_PLATFORM_BLOCK_MARKERS = ("Argus", "风控")

# 复用 server 侧模块（拿遥测/需求聚合），找不到就降级为自读 JSONL。
for _p in (_HERE, os.path.join(_HERE, "server"), os.path.join(os.path.dirname(_HERE), "server")):
    if os.path.isfile(os.path.join(_p, "platform_telemetry.py")) and _p not in sys.path:
        sys.path.insert(0, _p)
        break
try:
    import platform_telemetry
    import resolve_demand
except Exception:  # noqa: BLE001
    platform_telemetry = None
    resolve_demand = None

# 修复层（2026-11-01）：判出 broken 后自动升 yt-dlp + 子进程复测。
# 导入失败只降级为「只检测不修复」，绝不让巡检整体挂掉。
try:
    for _p in (_HERE, os.path.join(_HERE, "server")):
        if os.path.isfile(os.path.join(_p, "platform_autorepair.py")) and _p not in sys.path:
            sys.path.insert(0, _p)
            break
    import platform_autorepair
except Exception:  # noqa: BLE001
    platform_autorepair = None

# 自动修复开关：默认开启（用户要求接上），可 VDL_HEALTH_AUTOREPAIR=0 关闭
AUTOREPAIR = os.environ.get("VDL_HEALTH_AUTOREPAIR", "1").strip() not in ("0", "false", "no")

# yt-dlp 主动更新（2026-11-10）：除 broken 时自动修复层升版外，每次巡检都主动检查并安装
# 当前解释器可用的最新兼容版；装完后若本次确有新版本，巡检结束自动重启 vdl-web 使其生效。
try:
    import ydlp_update
except Exception:  # noqa: BLE001
    ydlp_update = None
# 主动更新 yt-dlp 开关（默认开）；设 0 关闭主动更新（仅 broken 时由自动修复层升版）
YDLP_PROACTIVE_UPDATE = os.environ.get("VDL_HEALTH_YDLP_UPDATE", "1").strip() not in ("0", "false", "no")
# 更新后是否自动重启 vdl-web 使其生效（默认开；设 0 则只装影子目录并写 PENDING_RESTART 标记）
APPLY_RESTART = os.environ.get("VDL_HEALTH_APPLY_RESTART", "1").strip() not in ("0", "false", "no")


def _http(url: str, timeout: int = 20, data: bytes | None = None) -> tuple[int, str]:
    """返回 (status, body)。data 非空则 POST JSON。"""
    headers = {"User-Agent": UA}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(getattr(r, "status", 200)), r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return int(e.code), e.read().decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        return 0, "%s: %s" % (type(e).__name__, e)


def _resolve(url: str) -> tuple[bool, str, str]:
    """打一次解析。返回 (ok, category, msg)。"""
    status, body = _http(ENDPOINT, timeout=PER_URL_TIMEOUT,
                         data=json.dumps({"url": url}).encode())
    try:
        d = json.loads(body)
    except Exception:  # noqa: BLE001
        return False, "bad_response", "HTTP %s %s" % (status, body[:160])
    if isinstance(d, dict) and d.get("error"):
        # 带上 hint：稿件级不可播（删稿/地区/会员）的判据只写在 hint 里，
        # 不带就分不出「平台挂了」和「这条样本本身没了」（见 _is_content_unavailable）。
        _msg = str(d.get("error"))[:200]
        if d.get("hint"):
            _msg += "｜" + str(d.get("hint"))[:200]
        return False, str(d.get("category") or "unknown"), _msg
    if isinstance(d, dict) and d.get("detail"):
        return False, "http_error", str(d.get("detail"))[:200]
    _title = d.get("title") or (d.get("video") or {}).get("title") or ""
    if isinstance(d, dict) and (d.get("url") or _title or d.get("formats")):
        return True, "ok", str(_title)[:80]
    return False, "no_payload", body[:160]


def _is_content_unavailable(msg: str) -> bool:
    """解析失败是「这条稿件没了」而不是「平台/提取器坏了」。

    热门榜/发现器取到的是当下热视频，删稿、未过审、地区限制、会员专享都很常见；
    把它们算进平台健康分母，会让整个平台凭空判成 degraded。
    """
    low = msg or ""
    return any(m in low for m in _CONTENT_UNAVAILABLE_MARKERS)


def _is_busy(msg: str) -> bool:
    """解析服务自己忙（同一时刻只跑一个任务），不是平台坏了。

    2026-11-03 实测：连着打两条微博样本，第二条被服务端「上一个解析任务未完成」挡回，
    整平台就凭空判成 degraded。这类失败必须重试，重试仍忙就不计入分母。
    """
    low = msg or ""
    return any(m in low for m in ("正忙", "未完成", "稍后重试", "busy", "already running"))


def _resolve_with_retry(url: str, attempts: int = 3) -> tuple[bool, str, str]:
    ok, cat, msg = False, "", ""
    for i in range(max(1, attempts)):
        ok, cat, msg = _resolve(url)
        if ok or not _is_busy(msg):
            return ok, cat, msg
        if i + 1 < attempts:
            time.sleep(3)
    return ok, cat, msg


def _sample_alive(url: str) -> bool:
    """样本链接本身是否还活着（区分「样本失效」与「平台失效」）。"""
    status, body = _http(url, timeout=20)
    if status == 0 or status >= 400:
        return False
    low = body.lower()
    return not any(m.lower() in low for m in _DEAD_MARKERS)


def _discover_bilibili(n: int) -> list[str]:
    """B站公开热门 API 取当前真实 bvid（无需登录）。"""
    _, body = _http("https://api.bilibili.com/x/web-interface/popular?ps=%d&pn=1" % max(n, 1))
    try:
        items = (json.loads(body).get("data") or {}).get("list") or []
    except Exception:  # noqa: BLE001
        return []
    return ["https://www.bilibili.com/video/%s" % i["bvid"]
            for i in items if i.get("bvid")][:n]


def _discover_acfun(n: int) -> list[str]:
    """AcFun 公开日榜 API（无需登录）：rankList[].contentId ⇒ https://www.acfun.cn/v/ac{id}。

    2026-11-03 实测：ECS 直连 200，解析同链路可用（1.9s 出结果）。
    """
    _, body = _http("https://www.acfun.cn/rest/pc-direct/rank/channel"
                    "?channelId=0&rankLimit=%d&rankPeriod=DAY" % max(n * 3, 10))
    try:
        rows = json.loads(body).get("rankList") or []
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for r in rows:
        cid = r.get("contentId") or r.get("id")
        if cid:
            out.append("https://www.acfun.cn/v/ac%s" % cid)
        if len(out) >= n:
            break
    return out


_DISCOVERERS = {"bilibili_popular": _discover_bilibili,
                "acfun_rank": _discover_acfun}


def _samples_for(cfg: dict, n: int) -> list[str]:
    """取该平台本次要测的样本。

    `cfg["n"]` 优先于全局缺省：样本成本不一样（爱奇艺一条要 20s+），
    没有公开榜单可自动发现的平台只留 1 条，免得整轮巡检拖到十几分钟。
    """
    n = int(cfg.get("n") or n) or 1
    disc = cfg.get("discoverer")
    if disc and disc in _DISCOVERERS:
        try:
            got = _DISCOVERERS[disc](n)
        except Exception:  # noqa: BLE001 — 发现器挂了就退回固定样本，不能让整轮巡检失败
            got = []
        if got:
            return got
    return list(cfg.get("urls") or [])[:n]


def _host_matches(host: str, domains: list[str]) -> bool:
    """与 platforms.py 同口径：host == domain 或 host 以 .domain 结尾。"""
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in (domains or []))


def _load_evidence() -> tuple[dict, dict]:
    """读成功遥测与失败需求，返回 (success_by_host, demand_by_host)。

    两个源都在 VDL_DATA_DIR 下（与 web 服务写入位置一致）。任一不可读则为空，
    绝不因此中断巡检——退化为只有样本实测的健康表，仍然有用。
    """
    succ: dict[str, dict] = {}
    dem: dict[str, dict] = {}
    try:
        if platform_telemetry is not None:
            succ = platform_telemetry.host_success_map(days=TELEMETRY_DAYS,
                                                       base_dir=Path(DATA_DIR))
    except Exception:  # noqa: BLE001
        succ = {}
    try:
        if resolve_demand is not None:
            dem = {r["host"]: r for r in resolve_demand.demand_summary(
                days=DEMAND_DAYS, base_dir=Path(DATA_DIR))}
    except Exception:  # noqa: BLE001
        dem = {}
    return succ, dem


def _evidence_for(domains: list[str], succ: dict, dem: dict) -> dict:
    """把该平台名下所有域的成功/需求证据合并。"""
    days = total = last_ts = 0
    hosts_hit: list[str] = []
    for host, r in succ.items():
        if _host_matches(host, domains):
            days += int(r.get("days_count") or 0)
            total += int(r.get("total") or 0)
            last_ts = max(last_ts, int(r.get("last_ts") or 0))
            hosts_hit.append(host)
    d_weeks = d_total = d_last = 0
    d_hosts: list[str] = []
    for host, r in dem.items():
        if _host_matches(host, domains):
            d_weeks += int(r.get("weeks_count") or 0)
            d_total += int(r.get("total") or 0)
            d_last = max(d_last, int(r.get("last_ts") or 0))
            d_hosts.append(host)
    return {
        "success_days": days,
        "success_total": total,
        "last_success_ts": last_ts,
        "success_hosts": sorted(set(hosts_hit))[:5],
        "demand_weeks": d_weeks,
        "demand_total": d_total,
        "last_demand_ts": d_last,
        "demand_hosts": sorted(set(d_hosts))[:5],
    }


# ---------------- 异常提醒（2026-11-03，2026-11-10 扩到修复/停滞） ----------------
# 巡检结果只写进 ECS 的 JSON + 日志，没人会主动去翻 ⇒ 等于没巡检。
# 这里只做两件事：① **比上一轮更差才提醒**（防刷屏）；② 提醒落到后台首屏（红条），
# 可选再往群里发一条（企业微信/钉钉/Bark，见 _notify）：覆盖新增异常 + 自动修复成功/失败
# + 巡检停滞（>30h 无新表）。配置走环境变量 VDL_HEALTH_WEBHOOK 或 data/notify.json。
_ALERT_PATH = os.environ.get(
    "VDL_HEALTH_ALERT_OUT",
    os.path.join(os.path.dirname(OUT_PATH), "platform_health_alerts.json"))
# 严重度：数字变大 = 变差。提醒只在「跨过 _ALERT_MIN_SEV 且变差」时产生。
_SEV = {"healthy": 0, "no_sample": 0, "unknown": 0,
        "limited": 1, "demand_only": 2,
        "degraded": 3, "sample_invalid": 3, "needs_manual": 3,
        "repaired_pending_restart": 3, "broken": 4}
_ALERT_MIN_SEV = 3
_MAX_ALERT_EVENTS = 100


def _load_prev_doc() -> dict:
    """读取上一轮完整健康表（含 generated_at）；文件不存在=首次跑，返回空。"""
    try:
        with open(OUT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _load_prev_status(prev: dict | None = None) -> dict:
    """上一轮健康表里每个平台的状态（首次跑/prev=None 时返回空）。"""
    if prev is None:
        prev = _load_prev_doc()
    return {it.get("platform"): it.get("status")
            for it in (prev.get("items") or [])
            if isinstance(it, dict) and it.get("platform")}


def _first_fail(it: dict) -> str:
    for r in it.get("results") or []:
        if not r.get("ok"):
            return str(r.get("msg") or "")[:160]
    return ""


def _diff_alerts(items: list, prev: dict) -> list:
    """挑出「这轮比上轮更差」的平台。首轮/新平台不报，避免一次刷出一堆假警报。"""
    out = []
    for it in items:
        cur, old = it.get("status"), prev.get(it.get("platform"))
        if not old:
            continue
        if _SEV.get(cur, 0) >= _ALERT_MIN_SEV and _SEV.get(cur, 0) > _SEV.get(old, 0):
            out.append({"ts": int(time.time()), "platform": it["platform"],
                        "name": it.get("name") or it["platform"],
                        "from": old, "to": cur,
                        "detail": (it.get("detail") or _first_fail(it))[:160],
                        "ack": False})
    return out


def _save_alerts(new_events: list) -> dict:
    """把本轮新增异常并入提醒文件（未读的留着，直到有人在后台点「知道了」）。"""
    doc: dict = {"updated_at": "", "events": []}
    try:
        with open(_ALERT_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            doc = loaded
    except Exception:  # noqa: BLE001
        pass
    events = [e for e in (doc.get("events") or []) if isinstance(e, dict)]
    # 一天可能跑多次：同一平台同一条异常不重复堆叠
    seen = {(e.get("platform"), e.get("to")) for e in events if not e.get("ack")}
    for e in new_events:
        if (e["platform"], e["to"]) in seen:
            continue
        events.append(e)
    events = events[-_MAX_ALERT_EVENTS:]
    unread = [e for e in events if not e.get("ack")]
    doc = {"updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "unseen": len(unread), "events": events,
           "last_run_changes": new_events}
    try:
        os.makedirs(os.path.dirname(_ALERT_PATH), exist_ok=True)
        tmp = _ALERT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _ALERT_PATH)
    except Exception as e:  # noqa: BLE001
        print("提醒写入失败: %s" % e)
    return doc


_NOTIFY_CONFIG_PATH = os.environ.get(
    "VDL_HEALTH_NOTIFY_CONFIG",
    os.path.join(os.path.dirname(OUT_PATH), "notify.json"))


def _load_notify_config() -> tuple[str, str]:
    """Webhook 来源：环境变量优先，其次 data/notify.json（{"webhook","kind"}）。
    企业微信/钉钉通用 text 体；kind=bark 走 GET。未配置则返回空（不推送）。"""
    url = (os.environ.get("VDL_HEALTH_WEBHOOK") or "").strip()
    kind = (os.environ.get("VDL_HEALTH_WEBHOOK_KIND") or "").strip().lower()
    if not url:
        try:
            with open(_NOTIFY_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
            url = (cfg.get("webhook") or "").strip()
            kind = (kind or (cfg.get("kind") or "")).strip().lower()
        except Exception:  # noqa: BLE001
            pass
    if not kind:
        kind = "wecom"
    return url, kind


def _notify(msg: str) -> None:
    """可选推送。企业微信/钉钉群机器人同样是 {"msgtype":"text","text":{"content"}}，
    所以缺省用这个体；Bark 用 VDL_HEALTH_WEBHOOK_KIND=bark（GET /标题/内容）。
    配置：环境变量 VDL_HEALTH_WEBHOOK，或 data/notify.json（免改 systemd）。"""
    url, kind = _load_notify_config()
    if not url:
        return
    try:
        if kind == "bark":
            from urllib.parse import quote
            _http("%s/%s/%s" % (url.rstrip("/"), quote("视频工坊·巡检异常"),
                                quote(msg)), timeout=10)
        else:
            _http(url, timeout=10, data=json.dumps(
                {"msgtype": "text", "text": {"content": msg}},
                ensure_ascii=False).encode())
    except Exception as e:  # noqa: BLE001
        print("提醒推送失败: %s" % e)


# ---------------------------------------------------------------- 工具版本采集
def _run_ver(args: list[str], timeout: int = 25) -> str:
    """跑一条命令拿首行输出；失败返回空串（绝不因探测工具缺失而崩巡检）。"""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception:  # noqa: BLE001
        return ""
    out = (r.stdout or r.stderr or "").strip()
    return out.splitlines()[0] if out else ""


def _collect_tool_versions() -> dict:
    """采集依赖工具当前版本（只读，不自动升级；除 yt-dlp 外的更新由人决定）。"""
    tools: dict[str, str] = {}
    # ffmpeg（解析/合并必需）
    ff = _run_ver(["ffmpeg", "-version"])
    m = re.search(r"ffmpeg version (\S+)", ff)
    tools["ffmpeg"] = m.group(1) if m else (ff[:60] if ff else "未安装")
    # Node.js（仅构建期需要：前端预编译后静态伺服，运行期无需）
    nd = _run_ver(["node", "--version"])
    tools["node"] = nd.lstrip("v") if nd.startswith("v") else (nd[:40] if nd else "未安装")
    # Python 解释器（决定 yt-dlp 能跟到多新）
    tools["python"] = "%d.%d.%d" % sys.version_info[:3]
    # Playwright（无头 Chromium 解析依赖）+ Chromium 浏览器版本。
    # 直接按安装路径 glob 找可执行文件跑 --version，避免启动 Playwright 驱动进程
    # （否则每日巡检退出时会有 asyncio「Task destroyed」噪声告警）。
    try:
        from importlib.metadata import version as _meta_ver
        tools["playwright"] = _meta_ver("playwright")
        import glob
        _base = os.path.expanduser("~/.cache/ms-playwright")
        cands = sorted(
            glob.glob(os.path.join(_base, "chromium*", "chrome-linux*", "chrome"))
            + glob.glob(os.path.join(_base, "chromium*", "chrome-linux*", "headless_shell")),
            reverse=True)
        if cands and os.path.isfile(cands[0]):
            cr = _run_ver([cands[0], "--version"])
            tools["chromium"] = cr or "已安装(版本未知)"
        else:
            tools["chromium"] = "未安装"
    except Exception:  # noqa: BLE001
        tools["playwright"] = "未安装"
        tools["chromium"] = "未安装"
    return tools


def _restart_vdl_web() -> dict:
    """重启 vdl-web 使新装到影子目录的 yt-dlp 真正生效；失败只告警不崩。"""
    cmd = os.environ.get("VDL_HEALTH_RESTART_CMD", "systemctl restart vdl-web")
    try:
        r = subprocess.run(cmd.split(), capture_output=True, text=True, timeout=90)
        if r.returncode == 0:
            print("✅ 已自动重启 vdl-web 使 yt-dlp 更新生效")
            return {"ok": True, "cmd": cmd}
        print("⚠️ 重启 vdl-web 失败(退出码 %d)：%s" % (r.returncode, (r.stderr or "")[:200]))
        return {"ok": False, "cmd": cmd, "code": r.returncode,
                "error": (r.stderr or r.stdout or "")[:200]}
    except Exception as e:  # noqa: BLE001
        print("⚠️ 重启 vdl-web 异常：%s" % e)
        return {"ok": False, "error": str(e)}


def _ydlp_proactive_update() -> dict:
    """每次巡检主动检查并安装当前解释器可用的最新 yt-dlp；返回结果摘要。"""
    if ydlp_update is None:
        return {"available": False, "error": "ydlp_update 模块不可用"}
    info: dict = {"available": True}
    try:
        info["report"] = ydlp_update.version_report()
    except Exception as e:  # noqa: BLE001
        info["report"] = None
        info["report_error"] = str(e)
    if not YDLP_PROACTIVE_UPDATE:
        info["update"] = {"skipped": True, "reason": "VDL_HEALTH_YDLP_UPDATE=0"}
        return info
    try:
        info["update"] = ydlp_update.update()
    except Exception as e:  # noqa: BLE001
        info["update"] = {"ok": False, "error": str(e)}
    return info


def _maybe_restart_for_ydlp(ydlp_info: dict, pending_path: str) -> None:
    """本次装了新 yt-dlp 或有平台修好待重启 ⇒ 重启 vdl-web（默认开，可关）。"""
    if not APPLY_RESTART:
        return
    did_update = bool((ydlp_info.get("update") or {}).get("updated"))
    pending = os.path.isfile(pending_path)
    if not did_update and not pending:
        return
    _restart_vdl_web()


def main() -> int:
    try:
        with open(SAMPLES_PATH, "r", encoding="utf-8") as f:
            samples = json.load(f)
    except Exception as e:  # noqa: BLE001
        print("样本库读取失败: %s (%s)" % (SAMPLES_PATH, e))
        return 2

    # 主动更新 yt-dlp（2026-11-10）：每次巡检都检查并安装当前解释器可用的最新兼容版，
    # 让自动修复层的升版「永远是最新」且巡检结束自动重启生效；同时采集依赖工具版本。
    ydlp_info = _ydlp_proactive_update()
    tool_versions = _collect_tool_versions()
    if (ydlp_info.get("update") or {}).get("updated"):
        print("yt-dlp 主动更新：%s → %s（巡检结束自动重启 vdl-web 生效）"
              % ((ydlp_info.get("report") or {}).get("current"),
                 (ydlp_info.get("update") or {}).get("version")))
    elif (ydlp_info.get("report") or {}).get("latest"):
        print("yt-dlp：当前 %s / 可用最新 %s（已是最新兼容版）"
              % ((ydlp_info.get("report") or {}).get("current"),
                 (ydlp_info.get("report") or {}).get("latest")))
    print("依赖工具版本：%s" % "  ".join("%s=%s" % (k, v) for k, v in tool_versions.items()))

    succ, dem = _load_evidence()
    only = set(sys.argv[1:])
    per_platform = int(os.environ.get("VDL_HEALTH_SAMPLE_N", "3"))
    items = []
    for key, cfg in samples.items():
        # 样本库里 `_comment` 等非平台条目是说明文字，不是平台配置，必须跳过
        # （早期版本全量跑会拿到 str 直接崩）。
        if not isinstance(cfg, dict):
            continue
        if only and key not in only:
            continue
        ev = _evidence_for(cfg.get("hosts") or [], succ, dem)
        urls = _samples_for(cfg, per_platform)
        known_limit = (cfg.get("known_limit") or "").strip()
        if not urls:
            # 无样本 ⇒ 靠证据判定，不编造结论
            if ev["success_days"] > 0:
                status, source = "healthy", "telemetry"
            elif ev["demand_weeks"] > 0:
                status, source = "demand_only", "demand"
            elif known_limit:
                # 已知限制（服务器侧永远解析不了）：诚实标 limited 而非 unknown，
                # 不触发自动修复、不进待办；把 known_limit 文本带进 detail，
                # 后台才能把「为什么受限」如实展示（否则只显示笼统的「无样本」。
                status, source = "limited", "known_limit"
                detail = known_limit
            else:
                status, source = "unknown", "none"
                detail = "无样本，按遥测/需求证据判定"
            items.append({"platform": key, "name": cfg.get("name", key),
                          "status": status, "source": source,
                          "ok": 0, "total": 0, "evidence": ev,
                          "detail": detail})
            continue

        results = []
        for u in urls:
            t0 = time.time()
            ok, cat, msg = _resolve_with_retry(u)
            rec = {"url": u, "ok": ok, "category": cat, "msg": msg,
                   "elapsed_s": round(time.time() - t0, 1)}
            if not ok and _is_busy(msg):
                # 服务自己忙，跟平台无关：重试过仍忙的样本不计入分母
                rec["busy"] = True
            elif not ok:
                if any(m in (msg or "") for m in _PLATFORM_BLOCK_MARKERS):
                    # 平台级风控：与样本死活无关，按存活计（否则会被误判 sample_invalid）
                    rec["sample_alive"] = True
                    rec["platform_blocked"] = True
                else:
                    rec["sample_alive"] = _sample_alive(u)
                    # 稿件级不可播（删稿/地区/会员）⇒ 这条样本失效，不是平台失效。
                    # 热门榜里混进一条死稿就把整个平台判 degraded，是纯误报（2026-11-01 实测）。
                    if _is_content_unavailable(msg):
                        rec["content_unavailable"] = True
            results.append(rec)
        # 分母只算「可用样本」：稿件不可播的、服务忙没测出结果的都不参与判定
        usable = [r for r in results
                  if not r.get("content_unavailable") and not r.get("busy")]
        ok_n = sum(1 for r in usable if r["ok"])
        alive_n = sum(1 for r in usable if r.get("sample_alive", True))
        detail = ""
        if not usable and any(r.get("busy") for r in results):
            # 全都卡在「服务正忙」：没有结论，就别硬判 sample_invalid（那是维护问题的结论）
            status = "unknown"
            detail = "解析服务当时正忙，本轮没测出结论"
        elif not usable:
            status = "sample_invalid"
        elif ok_n == len(usable):
            status = "healthy"
        elif ok_n > 0:
            status = "degraded"
        elif alive_n == 0:
            status = "sample_invalid"
        else:
            status = "broken"
        if known_limit and ok_n == 0:
            # 已知限制（服务器侧缺 Cookie/播放签名、只有桌面端能解析）：
            # 它**不是**「平台坏了」，也不能进待办和自动修复 —— 否则每天报同一条
            # 假警报，把真正需要处理的平台淹掉。样本通了立即回到 normal 判定。
            status = "limited"
            detail = known_limit
        items.append({"platform": key, "name": cfg.get("name", key), "status": status,
                      "source": "sample", "ok": ok_n, "total": len(usable),
                      "evidence": ev, "results": results, "detail": detail})

    # ---- 自动修复层（2026-11-01）----
    # 只对 broken 触发：degraded 实测多为**内容级**限制（如 B站某视频 412 地区/会员），
    # 升版对它无效，却要承担「新版弄坏别的平台」的全局风险 —— 不值得。
    # broken = 样本仍活着但全挂，最符合「提取器老化」特征，正是升版能治的那类。
    repair_log: list[dict] = []
    if AUTOREPAIR and platform_autorepair is not None:
        broken = [it for it in items if it["status"] == "broken"]
        if broken:
            # 回归校验池：其它平台的样本（升版是整进程共用，必须防「修好 A 弄坏 B」）
            reg: dict[str, list[str]] = {}
            broken_keys = {b["platform"] for b in broken}
            for k, c in samples.items():
                if not isinstance(c, dict) or k in broken_keys:
                    continue
                u = _samples_for(c, 1)
                if u:
                    reg[k] = u[:1]
            for it in broken:
                urls = [r["url"] for r in (it.get("results") or [])]
                if not urls:
                    continue
                rep = platform_autorepair.repair_platform(
                    it["platform"], it.get("name") or it["platform"], urls,
                    baseline_ok=int(it.get("ok") or 0), regression_urls=reg,
                    log=lambda m: print(m))
                it["repair"] = rep
                repair_log.append(rep)
                rs = rep.get("status")
                if rs in ("fixed", "improved"):
                    # 修好了，但线上进程还在跑旧 yt-dlp ⇒ 必须重启才生效
                    it["status"] = "repaired_pending_restart"
                elif rs == "already_latest" and (
                        (rep.get("root_cause") or {}).get("code")
                        in ("login_wall", "geo_or_paywall", "no_extractor",
                            "content_gone", "anti_bot")):
                    # 升版修不了、且根因明确不在版本 ⇒ 归「需人工」，不再天天当 broken 报
                    it["status"] = "needs_manual"
                # 其余（not_fixed / regressed / update_failed）保持 broken，且已落派单

    # 排序：**需要行动的排前面**，无证据（=无真实流量）的沉底。
    # 不排序的话，unknown 会淹没真正坏掉的平台——健康表的价值在于「先看什么」。
    _RANK = {"broken": 0, "needs_manual": 1, "sample_invalid": 2, "degraded": 3,
             "repaired_pending_restart": 4, "demand_only": 5, "limited": 6,
             "healthy": 7, "no_sample": 8, "unknown": 9}
    items.sort(key=lambda r: (_RANK.get(r["status"], 9), r["platform"]))
    counts: dict[str, int] = {}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    actionable = sum(counts.get(s, 0) for s in
                     ("broken", "needs_manual", "sample_invalid", "degraded",
                      "repaired_pending_restart", "demand_only"))

    # 提醒：与上一轮比，只报「变差」的那几个（首次跑不报，否则一次刷一堆）
    prev_doc = _load_prev_doc()
    prev_status = _load_prev_status(prev_doc)
    new_events = _diff_alerts(items, prev_status)
    alert_doc = _save_alerts(new_events)
    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "endpoint": ENDPOINT, "telemetry_days": TELEMETRY_DAYS,
           "demand_days": DEMAND_DAYS, "data_dir": DATA_DIR,
           "telemetry_available": bool(succ), "demand_available": bool(dem),
           "autorepair_enabled": bool(AUTOREPAIR and platform_autorepair is not None),
           "summary": {"total": len(items), "actionable": actionable,
                       "counts": counts,
                       "no_evidence": counts.get("unknown", 0),
                       "limited": counts.get("limited", 0),
                       "alerts_unseen": int(alert_doc.get("unseen") or 0),
                       "repaired_pending_restart": counts.get(
                           "repaired_pending_restart", 0),
                       "needs_manual": counts.get("needs_manual", 0)},
           "repairs": repair_log,
           "yt_dlp": ydlp_info,
           "tools": tool_versions,
           "items": items}
    try:
        os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        print("健康表写入失败: %s" % e)

    # 「修好了但还没重启」必须显眼，否则等于白修：自动修复只把新版 yt-dlp 解到
    # 影子目录，线上 vdl-web 仍在跑旧模块（我们不自动重启生产服务）。落一个标记
    # 文件，任何巡检/人一眼就能看到该重启了；恢复后自动清掉。
    _pending_path = os.path.join(os.path.dirname(OUT_PATH), "PENDING_RESTART.json")
    _pending = [{"platform": it["platform"], "name": it.get("name"),
                 "note": (it.get("repair") or {}).get("note")}
                for it in items if it["status"] == "repaired_pending_restart"]
    try:
        if _pending:
            with open(_pending_path, "w", encoding="utf-8") as f:
                json.dump({"since": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "action": "restart vdl-web to apply upgraded yt-dlp",
                           "platforms": _pending}, f, ensure_ascii=False, indent=2)
        elif os.path.isfile(_pending_path):
            os.remove(_pending_path)
    except Exception:  # noqa: BLE001
        pass

    # 让 yt-dlp 更新真正生效：本次装了新版本 或 有平台修好待重启 ⇒ 重启 vdl-web。
    # （默认开；VDL_HEALTH_APPLY_RESTART=0 则只装影子目录 + 写 PENDING_RESTART 标记，不自动重启。）
    _maybe_restart_for_ydlp(ydlp_info, _pending_path)

    print("== 平台健康表 %s ==" % out["generated_at"])
    print("遥测可用=%s 需求可用=%s（窗口 成功%d天 / 需求%d天）"
          % (out["telemetry_available"], out["demand_available"],
             TELEMETRY_DAYS, DEMAND_DAYS))
    s = out.get("summary") or {}
    print("共 %d 个平台：待处理 %d（%s）｜无证据（无真实流量）%d"
          % (s.get("total", 0), s.get("actionable", 0),
             " ".join("%s=%d" % (k, v) for k, v in sorted(
                 (s.get("counts") or {}).items(),
                 key=lambda kv: kv[0]) if k in
                 ("broken", "needs_manual", "sample_invalid", "degraded",
                  "repaired_pending_restart", "demand_only", "limited")) or "无",
             s.get("no_evidence", 0)))
    if new_events:
        print("⚠️  新增异常 %d 个（后台「平台健康」首屏会亮红条）：" % len(new_events))
        for e in new_events:
            print("      %s %s → %s  %s"
                  % (e["name"], e["from"], e["to"], (e.get("detail") or "")[:80]))
    elif alert_doc.get("unseen"):
        print("未读异常仍有 %d 条（此前产生的，后台首屏可见；点「知道了」可清）"
              % alert_doc["unseen"])
    if out.get("autorepair_enabled") and repair_log:
        print("自动修复：尝试 %d 个平台，需要重启生效 %d 个"
              % (len(repair_log), s.get("repaired_pending_restart", 0)))
        if s.get("repaired_pending_restart"):
            print("⚠️  有平台已修好但未生效：请重启 vdl-web "
                  "（systemctl restart vdl-web）后复测；标记见 %s"
                  % _pending_path)
    for it in items:
        ev = it.get("evidence") or {}
        extra = ""
        if ev.get("success_days"):
            extra = " 成功%d天/最后%s" % (
                ev["success_days"],
                time.strftime("%m-%d %H:%M", time.localtime(ev["last_success_ts"]))
                if ev.get("last_success_ts") else "?")
        if ev.get("demand_weeks"):
            extra += " 需求%d周" % ev["demand_weeks"]
        print("%-12s %-12s %-14s %s(%d/%d)%s"
              % (it["platform"], it["name"], it["status"], it.get("source", ""),
                 it["ok"], it.get("total", 0), extra))
        for r in it.get("results") or []:
            if not r["ok"]:
                print("      FAIL %s alive=%s %s"
                      % (r["url"][:60], r.get("sample_alive"), r["msg"][:90]))
        rp = it.get("repair") or {}
        if rp:
            _rc = rp.get("root_cause") or {}
            print("      REPAIR %s / 根因 %s：%s%s"
                  % (rp.get("status"), _rc.get("code"), rp.get("note", ""),
                     "  ⚠️需重启 vdl-web 才在线上生效"
                     if rp.get("restart_required") else ""))
    # ---- 汇总推送到 IM（企业微信/钉钉/Bark）：一轮只发一条，仅在有「值得说」的事时发 ----
    # 覆盖：新增异常（变差）、自动修复成功/失败、巡检停滞（>30h 无新表）。
    _nparts = []
    if new_events:
        _nparts.append("【新增异常 %d 个】" % len(new_events))
        for e in new_events[:8]:
            _nparts.append("- %s %s→%s %s" % (e["name"], e["from"], e["to"],
                                              (e.get("detail") or "")[:60]))
    if repair_log:
        for r in repair_log:
            _rs = r.get("status")
            if _rs in ("fixed", "improved"):
                _nparts.append("✅ 自动修复成功：%s%s" % (
                    r.get("name") or r.get("platform"),
                    "（需重启 vdl-web 生效）" if r.get("restart_required") else ""))
            elif _rs not in ("already_latest",):
                _rc = (r.get("root_cause") or {})
                _nparts.append("❌ 修复失败：%s 根因=%s %s" % (
                    r.get("name") or r.get("platform"),
                    _rc.get("label") or _rc.get("code") or "未知",
                    (r.get("note") or "")[:60]))
    _prev_gen = prev_doc.get("generated_at") or ""
    if _prev_gen:
        try:
            _pg = time.mktime(time.strptime(_prev_gen, "%Y-%m-%d %H:%M:%S"))
            if (time.time() - _pg) > 30 * 3600:
                _nparts.append("⚠️ 巡检停滞：上次生成于 %s，已超 30 小时未见新巡检，请检查 vdl-platform-health.timer" % _prev_gen)
        except Exception:  # noqa: BLE001
            pass
    if _nparts:
        _notify("【视频工坊·平台巡检】%s\n%s" % (
            time.strftime("%m-%d %H:%M"), "\n".join(_nparts)))

    print("-> %s" % OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
