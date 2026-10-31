"""三端用量 → 平台全景表（2026-11-01）。

为什么需要这个模块：Mac / Win / 网页三端的「成功使用」其实早就汇聚到授权中心了
—— 每次消耗配额都会 `daily_remote` 上报，落进 `daily.details` / `usage_history[].details`，
每条带 `t`（标题）、`p`（平台）、`at`（时间）、`r`（结果）。数据一直在写，但后台没有
任何入口读它，等于管理员只看得见「谁要了但没拿到」，看不见「谁真的用上了、用的什么」。

本模块把中心 `usage_overview.by_platform` 归一化成「平台」，再叠加需求榜的失败数，
给后台一张跨端的平台全景表。

口径（界面文案必须与这里一致，不要美化）：
  · 只有**真消耗配额**的动作才在这里；纯解析失败进需求榜，不进这里。
  · 明细里**没有端标识**（没有 mac/win/web 字段），所以这是三端合计，分不出哪条
    来自哪端；要分端必须改桌面端上报（= 发版），不是改这里能解决的。
  · `p` 是客户端自由填的文本，历史数据里混着中文名 / 平台 key / 域名，甚至有
    `example.com` / 自己的域名这种误报。归一化只覆盖已知别名，覆盖不到的**原样
    保留并标 suspect**，绝不强行合并 —— 把「金牌影院」并进「其他」只会让人误判。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

CACHE_TTL = 300.0          # 中心一次聚合约 1~2s，5 分钟内复用，避免每次刷新都打中心
CACHE_NAME = "platform_usage.json"

# 平台名 → (key, 显示名)。中文名 / 英文 key / 常见写法都收进来。
ALIASES: dict[str, tuple[str, str]] = {
    "百度网盘": ("baidu_pan", "百度网盘"),
    "baidupan": ("baidu_pan", "百度网盘"),
    "baidu": ("baidu_pan", "百度网盘"),
    "哔哩哔哩": ("bilibili", "哔哩哔哩"),
    "bilibili": ("bilibili", "哔哩哔哩"),
    "b站": ("bilibili", "哔哩哔哩"),
    "哔哩": ("bilibili", "哔哩哔哩"),
    "抖音": ("douyin", "抖音"),
    "douyin": ("douyin", "抖音"),
    "小红书": ("xiaohongshu", "小红书"),
    "xiaohongshu": ("xiaohongshu", "小红书"),
    "youtube": ("youtube", "YouTube"),
    "油管": ("youtube", "YouTube"),
    "爱奇艺": ("iqiyi", "爱奇艺"),
    "iqiyi": ("iqiyi", "爱奇艺"),
    "腾讯视频": ("tencent", "腾讯视频"),
    "腾讯": ("tencent", "腾讯视频"),
    "qq": ("tencent", "腾讯视频"),
    "优酷": ("youku", "优酷"),
    "youku": ("youku", "优酷"),
    "快手": ("kuaishou", "快手"),
    "kuaishou": ("kuaishou", "快手"),
    "微博": ("weibo", "微博"),
    "weibo": ("weibo", "微博"),
    "西瓜视频": ("xigua", "西瓜视频"),
    "xigua": ("xigua", "西瓜视频"),
    "liblib": ("liblib", "Liblib"),
    "哩布哩布": ("liblib", "Liblib"),
    "lespark": ("lespark", "lespark"),
    "网盘": ("cloud_pan", "网盘"),
    "cloud_drive": ("cloud_pan", "网盘"),
}

# 域名后缀 → (key, 显示名)。命中即认为是该平台（含子域）。
HOST_SUFFIX: dict[str, tuple[str, str]] = {
    "pan.baidu.com": ("baidu_pan", "百度网盘"),
    "baidu.com": ("baidu_pan", "百度网盘"),
    "bilibili.com": ("bilibili", "哔哩哔哩"),
    "b23.tv": ("bilibili", "哔哩哔哩"),
    "douyin.com": ("douyin", "抖音"),
    "iesdouyin.com": ("douyin", "抖音"),
    "xiaohongshu.com": ("xiaohongshu", "小红书"),
    "xhslink.com": ("xiaohongshu", "小红书"),
    "youtube.com": ("youtube", "YouTube"),
    "youtu.be": ("youtube", "YouTube"),
    "iqiyi.com": ("iqiyi", "爱奇艺"),
    "v.qq.com": ("tencent", "腾讯视频"),
    "qq.com": ("tencent", "腾讯视频"),
    "youku.com": ("youku", "优酷"),
    "kuaishou.com": ("kuaishou", "快手"),
    "weibo.com": ("weibo", "微博"),
    "weibo.cn": ("weibo", "微博"),
    "ixigua.com": ("xigua", "西瓜视频"),
    "liblib.art": ("liblib", "Liblib"),
    "liblib.tv": ("liblib", "Liblib"),
    "lespark.us": ("lespark", "lespark"),
}

# 明显不是「内容平台」的值：自己的域名、示例域名、localhost。单列出来，
# 不参与平台排序，也不并进任何真实平台 —— 它们是上报口径的脏数据。
SUSPECT_EXACT = {"example.com", "example.org", "localhost", "127.0.0.1",
                 "hanyuxz.top", "test", "test.com", "unknown", "none", "null"}


def _norm(p: str) -> tuple[str, str, bool]:
    """归一化单个上报值 → (key, 显示名, 是否可疑)。

    可疑 = 明显不是平台（示例域名 / 本站域名），或无法识别的域名。
    无法识别的**英文 key**（如 pomo）不算可疑，原样保留 —— 它可能就是个真站点。
    """
    raw = str(p or "").strip()
    low = raw.lower()
    if not low:
        return ("__empty__", "（未填平台）", True)
    if low in SUSPECT_EXACT:
        return ("suspect:" + low, raw or low, True)
    if low in ALIASES:
        key, name = ALIASES[low]
        return (key, name, False)
    for suf, (key, name) in HOST_SUFFIX.items():
        if low == suf or low.endswith("." + suf):
            return (key, name, False)
    if "." in low:
        # 未识别的域名：保留原样但标可疑，别假装认识它
        return ("host:" + low, raw, True)
    return ("raw:" + low, raw, False)


def _center_payload(days: int) -> tuple[dict[str, Any] | None, str]:
    """拉中心用量聚合。返回 (payload, 错误说明)；失败时 payload 为 None。"""
    try:
        from admin_store import _license_admin_token  # noqa: PLC0415 — 延迟导入避免循环
    except Exception:
        return None, "admin_store 不可用"
    tok = _license_admin_token()
    if not tok:
        return None, "未配管理令牌（VDL_LICENSE_ADMIN_TOKEN）"
    try:
        import license_client  # noqa: PLC0415

        r = license_client.usage_overview_remote(tok, days=days, timeout=20.0)
    except Exception as exc:  # noqa: BLE001 — 中心不可达不该影响后台其它面板
        return None, f"授权中心不可达：{type(exc).__name__}"
    if not isinstance(r, dict) or not r.get("ok"):
        return None, "授权中心返回异常"
    return r, ""


def _demand_by_key(days: int = 84) -> dict[str, dict[str, Any]]:
    """需求榜里每个平台的失败数（中心 demand_query）。失败就返回空表。"""
    try:
        from admin_store import _license_admin_token  # noqa: PLC0415

        tok = _license_admin_token()
        if not tok:
            return {}
        import license_client  # noqa: PLC0415

        r = license_client.demand_query_remote(tok, days=days, timeout=15.0)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(r, dict) or not r.get("ok"):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in r.get("demand") or []:
        host = str(row.get("host") or "")
        if not host:
            continue
        key, _name, _sus = _norm(host)
        cur = out.setdefault(key, {"total": 0, "last_ts": 0.0})
        cur["total"] += int(row.get("total") or 0)
        cur["last_ts"] = max(cur["last_ts"], float(row.get("last_ts") or 0))
    return out


def _cache_path(base_dir: Path | None = None) -> Path:
    base = Path(base_dir) if base_dir else Path(
        os.environ.get("VDL_DATA_DIR") or (Path.home() / ".video-downloader"))
    return base / CACHE_NAME


def _read_cache(base_dir: Path | None = None) -> dict[str, Any] | None:
    try:
        p = _cache_path(base_dir)
        if p.is_file():
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001
        pass
    return None


def _write_cache(payload: dict[str, Any], base_dir: Path | None = None) -> None:
    try:
        p = _cache_path(base_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        pass


_mem: dict[int, tuple[float, dict[str, Any]]] = {}


def overview(days: int = 30, force: bool = False,
             base_dir: Path | None = None) -> dict[str, Any]:
    """平台全景表。中心不可达时回落到上一次成功结果（明示 stale），绝不编数字。"""
    try:
        days = max(1, min(int(days or 30), 366))
    except (TypeError, ValueError):
        days = 30

    now = time.time()
    hit = _mem.get(days)
    if hit and not force and (now - hit[0]) < CACHE_TTL:
        return hit[1]

    center, err = _center_payload(days)
    if center is None:
        cached = _read_cache(base_dir)
        if cached and isinstance(cached.get("platforms"), list):
            cached["stale"] = True
            cached["note"] = err
            return cached
        return {"ok": False, "error": err, "days": days, "platforms": [],
                "totals": {"events": 0, "accounts": 0, "platforms": 0},
                "source": "license-center"}

    demand = _demand_by_key()
    rows: dict[str, dict[str, Any]] = {}
    suspect_rows: dict[str, dict[str, Any]] = {}
    events = 0
    for r in center.get("by_platform") or []:
        if not isinstance(r, dict):
            continue
        raw = str(r.get("p") or "")
        key, name, suspect = _norm(raw)
        total = int(r.get("total") or 0)
        events += total
        bucket = suspect_rows if suspect else rows
        cur = bucket.setdefault(key, {
            "key": key, "name": name, "suspect": suspect,
            "total": 0, "ok": 0, "accounts": 0, "last_ts": 0.0,
            "by_resource": {}, "aliases": [],
        })
        cur["total"] += total
        cur["ok"] += int(r.get("ok") or 0)
        # 不同别名的账号数**不能相加**（同一个人可能两种写法都上报过）⇒ 取下限 max
        cur["accounts"] = max(cur["accounts"], int(r.get("accounts") or 0))
        cur["last_ts"] = max(cur["last_ts"], float(r.get("last_ts") or 0))
        for res, n in (r.get("by_resource") or {}).items():
            cur["by_resource"][res] = cur["by_resource"].get(res, 0) + int(n or 0)
        if raw and raw not in cur["aliases"]:
            cur["aliases"].append(raw)

    for row in list(rows.values()):
        d = demand.get(row["key"]) or {}
        row["demand_total"] = int(d.get("total") or 0)
        row["demand_last_ts"] = float(d.get("last_ts") or 0)

    platforms = sorted(rows.values(), key=lambda x: (-x["total"], x["name"]))
    leftovers = sorted(suspect_rows.values(), key=lambda x: (-x["total"], x["name"]))
    out = {
        "ok": True,
        "days": days,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "source": "license-center",
        "platforms": platforms,
        "unmapped": leftovers,
        "totals": {
            "events": events,
            "accounts": int(center.get("active_users") or 0),
            "platforms": len(platforms),
            "unknown_platform": int(center.get("platform_unknown") or 0),
        },
        "by_resource": center.get("by_resource") or {},
    }
    _mem[days] = (now, out)
    _write_cache(out, base_dir)
    return out
