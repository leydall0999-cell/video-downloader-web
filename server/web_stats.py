"""server/web_stats.py — 网页版**访客**统计（PV / UV / 事件 / 来源）。

与 server/stats.py 的分工（别混用）：
  - stats.py     = 业务**功能**成功计数（下载/转换/抠图/字幕…），回答「功能用了多少次」；
  - web_stats.py = 网页**访客**统计，回答「网页版到底有没有流量、从哪来、在哪一步转化」，
                   用于判断值不值得为网页版投入（广告 / CPS / 内容站等商业决策的输入）。

隐私设计（硬约束，改动前请先看这段）：
  - 全库不落任何 IP 明文。UV 只存 sha256(secret|ip|日期) 的前 16 位；
    secret 安装时随机生成并只存本地，日期参与派生 → 跨天不可关联同一访客，
    只能回答「今天有多少人」，无法反推是谁、也无法跨天追踪个人。
  - UA 不存原文，只归并成 mobile / desktop / bot / unknown 四类。
  - 外链来源只存 host（如 google.com），不存完整 URL。

存储：~/.video-downloader/web_stats.json（与 auth_store 同目录），90 天滚动裁剪。
纯标准库、零依赖；路径与时间均可注入，可离线单测。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

_lock = threading.Lock()
_default_now: Callable[[], float] = time.time

KEEP_DAYS = 90          # 滚动保留天数
_MAX_UV_PER_DAY = 5000  # 单日 UV 哈希上限（防刷爆文件）
_MAX_HOSTS = 20         # 单日来源/设备维度保留的 key 数
_MAX_FAIL_SITES = 20    # 单日「解析失败」按站点保留的 key 数

# 事件白名单：不在表内的一律丢弃，避免被任意 key 塞爆。
EVENTS = (
    "page_view",      # 首屏加载
    "resolve_ok",     # 解析成功
    "resolve_fail",   # 解析失败
    "download_done",  # 下载完成
    "member_click",   # 点击会员入口
    "desktop_click",  # 点击桌面端下载
    "promo_click",    # 点击站内自有推广位
    "share_done",     # 分享成功
    "convert_done",   # 转换完成
    "login_ok",       # 登录成功
)


def _base_dir() -> Path:
    # 与 server/auth_store._base_dir 保持一致（VDL_DATA_DIR 优先）。
    override = os.environ.get("VDL_DATA_DIR", "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        base = Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
    else:
        base = Path.home() / ".video-downloader"
    return base


def default_path() -> Path:
    return _base_dir() / "web_stats.json"


def _empty_state() -> dict[str, Any]:
    return {"secret": secrets.token_hex(16), "days": {}, "updated_at": 0.0}


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    st = _empty_state()
    secret = data.get("secret")
    if isinstance(secret, str) and len(secret) >= 8:
        st["secret"] = secret
    days = data.get("days")
    if isinstance(days, dict):
        st["days"] = {k: v for k, v in days.items() if isinstance(k, str) and isinstance(v, dict)}
    st["updated_at"] = float(data.get("updated_at", 0) or 0)
    return st


def _save(path: Path, state: dict[str, Any]) -> None:
    """原子写（tmp + replace），任何 OSError 静默——统计失败绝不影响业务。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def _day_of(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def _device_of(ua: str) -> str:
    u = (ua or "").lower()
    if not u:
        return "unknown"
    if any(k in u for k in ("bot", "spider", "crawler", "slurp", "headless",
                            "python-requests", "curl/", "wget")):
        return "bot"
    if any(k in u for k in ("mobile", "android", "iphone", "ipad", "ipod", "harmony")):
        return "mobile"
    return "desktop"


def _ref_host_of(ref: str, own_hosts: Optional[set] = None) -> str:
    """外链来源只保留 host；同源跳转归为 direct（不算外来源）。"""
    r = (ref or "").strip()
    if not r:
        return "direct"
    if "://" not in r:
        r = "http://" + r
    try:
        host = (urlsplit(r).hostname or "").strip().lower()
    except ValueError:
        return "direct"
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return "direct"
    if own_hosts and host in own_hosts:
        return "direct"
    return host


def _trim(d: dict, limit: int) -> dict:
    """只保留计数最高的前 limit 个 key（防止维度无限膨胀）。"""
    if len(d) <= limit:
        return d
    top = sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return dict(top)


def record(kind: str, *, ip: str = "", ua: str = "", ref: str = "",
           own_host: str = "", now: Optional[float] = None,
           path: Optional[Path] = None, site: str = "", err: str = "") -> bool:
    """记录一次网页事件。线程安全；返回是否真的落库（白名单外/超限返回 False）。

    ``site`` / ``err`` 只对 ``resolve_fail`` 有意义（2026-11-03 补）：
    原来失败只有一个计数，运维页显示「解析失败 5」却查不到是哪个站、什么原因
    ——需求榜只收「未收录新站/缺提取器」两类，其余失败（链接不合法 / Cookie /
    会员墙 / 超时）完全不落痕，看上去就像数据丢了。这里按「站点×失败原因」
    落一份明细，只存 host 与原因短标签，不存 URL、不存 IP。

    任何异常一律静默——埋点绝不能影响业务主流程。
    """
    try:
        kind = (kind or "").strip()
        if kind not in EVENTS:
            return False
        site = (site or "").strip().lower()[:64]
        err = (err or "").strip()[:40]
        p = path or default_path()
        ts = _default_now() if now is None else float(now)
        day = _day_of(ts)
        own = {own_host.strip().lower()} if own_host else set()
        with _lock:
            st = _load(p)
            d = st["days"].setdefault(day, {"pv": 0, "uv": {}, "events": {},
                                            "refs": {}, "devices": {}})
            # PV 口径：只有 page_view 计 PV，其余是站内事件。
            if kind == "page_view":
                d["pv"] = int(d.get("pv", 0)) + 1
                host = _ref_host_of(ref, own)
                refs = d.setdefault("refs", {})
                refs[host] = int(refs.get(host, 0)) + 1
                dev = _device_of(ua)
                devices = d.setdefault("devices", {})
                devices[dev] = int(devices.get(dev, 0)) + 1
                # UV：secret 参与派生且随日期变化 → 跨天不可关联。
                uv = d.setdefault("uv", {})
                if len(uv) < _MAX_UV_PER_DAY:
                    digest = hashlib.sha256(
                        f"{st['secret']}|{ip or '-'}|{day}".encode("utf-8")
                    ).hexdigest()[:16]
                    uv[digest] = 1
            ev = d.setdefault("events", {})
            ev[kind] = int(ev.get(kind, 0)) + 1
            if kind == "resolve_fail":
                # 站点×原因明细：host 缺省（链接连主机名都没有）归到「(无法识别)」，
                # 这样「失败了 1 次」永远有一条可读的原因，而不是一个孤零零的数字。
                fails = d.setdefault("fails", {})
                key = site or "(无法识别)"
                cur = fails.get(key)
                if isinstance(cur, dict):
                    cur["n"] = int(cur.get("n", 0)) + 1
                    cur["last"] = int(ts)
                    if err:
                        cur["err"] = err
                else:
                    fails[key] = {"n": 1, "last": int(ts), "err": err}
                if len(fails) > _MAX_FAIL_SITES:
                    d["fails"] = dict(sorted(
                        fails.items(),
                        key=lambda kv: int((kv[1] or {}).get("n", 0)),
                        reverse=True)[:_MAX_FAIL_SITES])
            d["refs"] = _trim(d.get("refs", {}), _MAX_HOSTS)
            d["devices"] = _trim(d.get("devices", {}), 8)
            st["updated_at"] = ts
            # 滚动裁剪：只保留最近 KEEP_DAYS 天。
            if len(st["days"]) > KEEP_DAYS:
                for old in sorted(st["days"].keys())[:-KEEP_DAYS]:
                    st["days"].pop(old, None)
            _save(p, st)
        return True
    except Exception:  # noqa: BLE001
        return False


def summary(days: int = 7, *, now: Optional[float] = None,
            path: Optional[Path] = None) -> dict[str, Any]:
    """最近 N 天汇总。uv 为各天之和（按天派生的哈希无法跨天去重，口径=人次）。"""
    p = path or default_path()
    n = 7 if not isinstance(days, int) else max(1, min(days, KEEP_DAYS))
    ts = _default_now() if now is None else float(now)
    with _lock:
        st = _load(p)
        store = st["days"]
        seq = []
        for i in range(n - 1, -1, -1):
            day = _day_of(ts - i * 86400)
            d = store.get(day, {})
            events = {k: int(v) for k, v in (d.get("events") or {}).items()}
            refs = d.get("refs") or {}
            fails = [{"host": k, "n": int((v or {}).get("n", 0)),
                      "last": int((v or {}).get("last", 0)),
                      "err": str((v or {}).get("err") or "")}
                     for k, v in (d.get("fails") or {}).items() if isinstance(v, dict)]
            fails.sort(key=lambda r: (-r["n"], -r["last"]))
            seq.append({
                "date": day,
                "pv": int(d.get("pv", 0)),
                "uv": len(d.get("uv") or {}),
                "events": events,
                "top_refs": sorted(((k, int(v)) for k, v in refs.items()),
                                   key=lambda kv: kv[1], reverse=True)[:5],
                "devices": {k: int(v) for k, v in (d.get("devices") or {}).items()},
                "fails": fails[:10],
            })
    totals: dict[str, int] = {"pv": 0, "uv": 0}
    ev_total: dict[str, int] = {}
    fail_agg: dict[str, dict] = {}
    for d in seq:
        totals["pv"] += d["pv"]
        totals["uv"] += d["uv"]
        for k, v in d["events"].items():
            ev_total[k] = ev_total.get(k, 0) + v
        for r in d["fails"]:
            agg = fail_agg.setdefault(r["host"], {"host": r["host"], "n": 0, "last": 0, "err": ""})
            agg["n"] += r["n"]
            if r["last"] >= agg["last"]:
                agg["last"] = r["last"]
                if r["err"]:
                    agg["err"] = r["err"]
    totals.update(ev_total)
    fail_sites = sorted(fail_agg.values(), key=lambda r: (-r["n"], -r["last"]))[:12]
    return {
        "days": seq,
        "totals": totals,
        "fail_sites": fail_sites,
        "uv_note": "uv 为各天独立去重之和（按天派生哈希，跨天不可关联），口径=人次",
        "keep_days": KEEP_DAYS,
        "updated_at": st["updated_at"],
    }


def reset(path: Optional[Path] = None) -> dict[str, Any]:
    """清空网页统计（后台面板「重置」用）。"""
    p = path or default_path()
    with _lock:
        _save(p, _empty_state())
    return {"ok": True}
