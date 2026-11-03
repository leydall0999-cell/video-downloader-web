"""server/routers/webstats.py — 网页版**访客统计**端点（/api/webstats/*）。

  POST /api/webstats/track    前端埋点上报（page_view / resolve_ok / resolve_fail /
                              download_done / member_click / desktop_click / promo_click …）
  GET  /api/webstats/summary  最近 N 天汇总（PV/UV/事件/来源/设备），仅超管可见

设计要点：
  - IP 只用于派生 UV 哈希，**绝不落库明文**（web_stats.record 内部单向哈希）；
    CF 走 CF-Connecting-IP，nginx 反代取 X-Forwarded-For 最左。
  - track 是公开提交端点 → 做秒级节流（同 IP 每 10s 至多 20 次），防刷爆存储；
    属于「提交类」端点，限流只影响统计完整性，不影响任何业务功能。
  - 全程 try/except 静默：埋点失败绝不能影响主流程或返回 5xx。
  - summary 走 admin.require_admin（Bearer token + is_admin），与后台面板同一套鉴权。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from fastapi import APIRouter, Body, Query, Request
from urllib.parse import urlsplit

import web_stats
from routers.admin import require_admin

router = APIRouter()

_KIND_MAXLEN = 32
_REF_MAXLEN = 512

# —— 节流（防刷）：ip -> 窗口内上报时间戳列表 ——
_bucket_lock = threading.Lock()
_buckets: dict = {}
_WINDOW = 10.0        # 统计窗口（秒）
_MAX_IN_WINDOW = 20   # 窗口内同 IP 最多接受次数
_MAX_BUCKETS = 5000   # 内存上限，超出整体清空


def _throttle_ok(ip: str, now: float) -> bool:
    key = ip or "-"
    with _bucket_lock:
        if len(_buckets) > _MAX_BUCKETS:
            _buckets.clear()
        arr = _buckets.setdefault(key, [])
        arr[:] = [t for t in arr if now - t < _WINDOW]
        if len(arr) >= _MAX_IN_WINDOW:
            return False
        arr.append(now)
        return True


def _client_ip(request: Request = None) -> str:
    """真实客户端 IP：CF → nginx 反代 → 直连。仅用于派生哈希，不落库。"""
    if request is None:
        return ""
    h = request.headers
    v = (h.get("cf-connecting-ip") or "").strip()
    if v:
        return v[:64]
    v = (h.get("x-forwarded-for") or "").strip()
    if v:
        return v.split(",")[0].strip()[:64]
    v = (h.get("x-real-ip") or "").strip()
    if v:
        return v[:64]
    return ((request.client.host if request.client else "") or "")[:64]


def _site_of(raw: Any) -> str:
    """把前端传来的站点（可能是裸 host 或完整 URL）归一成 host，认不出就返回空串。

    解析失败明细只存 host：既能回答「是哪个站」，又不落用户具体看了哪条视频
    （URL 里常带私密 ID / 分享者身份 / 签名 token），与 web_stats 的隐私口径一致。
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    if "://" in s:
        try:
            s = urlsplit(s).hostname or ""
        except ValueError:
            return ""
    s = s.split("/")[0].split("?")[0].split("#")[0].strip().lower()
    if s.startswith("www."):
        s = s[4:]
    if not s or "." not in s or any(c in s for c in " \t\r\n"):
        return ""
    return s[:64]


@router.post("/api/webstats/track")
def track(payload: dict = Body(default=None), request: Request = None) -> dict:
    """前端埋点上报。任何异常一律吞掉并返回 ok:false，绝不抛 5xx。"""
    try:
        body = payload if isinstance(payload, dict) else {}
        kind = str(body.get("kind") or "")[:_KIND_MAXLEN]
        ref = str(body.get("ref") or "")[:_REF_MAXLEN]
        # 解析失败明细（2026-11-03）：site=哪个站，err=失败类别短标签（如 unknown/403）。
        site = _site_of(body.get("site")) if kind == "resolve_fail" else ""
        err = str(body.get("err") or "").strip()[:40] if kind == "resolve_fail" else ""
        ip = _client_ip(request)
        if not _throttle_ok(ip, time.time()):
            return {"ok": False, "dropped": "throttled"}
        own = ""
        if request is not None:
            own = (request.headers.get("host") or "").split(":")[0].strip().lower()
        ua = (request.headers.get("user-agent") or "") if request is not None else ""
        ok = web_stats.record(kind, ip=ip, ua=ua, ref=ref, own_host=own, site=site, err=err)
        return {"ok": bool(ok)}
    except Exception:  # noqa: BLE001
        return {"ok": False, "dropped": "error"}


def _require_viewer(request: Request = None) -> None:
    """查看权限：运维密钥（X-Admin-Key，环境变量 VDL_ADMIN_KEY）**或**超管用户 token，任一通过。

    与 /api/admin/visits 共用同一套运维密钥，这样运维控制台一个页面就能同时看到
    nginx 口径（总请求 / 独立 IP）和站内转化口径（PV → 解析成功 → 下载完成 → 会员点击）。
    未配置 VDL_ADMIN_KEY 时该分支永不放行，只能走超管鉴权——不会误暴露。
    """
    admin_key = (os.environ.get("VDL_ADMIN_KEY") or "").strip()
    if admin_key:
        key = (request.headers.get("X-Admin-Key") or "").strip() if request is not None else ""
        if key and key == admin_key:
            return
    # 🔴 2026-11-01 修正：运维页整体嵌进后台之后，后台手里只有 adm 令牌（2 小时，
    # adm 作用域），而这里的 require_admin 走 get_current_user_id → token_from_header，
    # **只认 30 天普通登录令牌** ⇒ 后台里「网页转化」那一格永远 401「加载失败」。
    # 与旁边 /api/admin/visits、/api/admin/events 保持同一套判定（core 的 _require_admin：
    # adm 令牌 + 必须超管 / 或运维密钥 / 或本机直连）。
    try:
        from routers.core import _require_admin as _ops_guard
        _ops_guard(request)
        return
    except Exception:  # noqa: BLE001 — 落到下面给更明确的 401
        pass
    require_admin(request)


@router.get("/api/webstats/summary")
def summary(days: int = Query(7, ge=1, le=90), request: Request = None) -> Any:
    """最近 N 天网页访客汇总。运维密钥或超管可见，不对公众开放。"""
    _require_viewer(request)
    return web_stats.summary(days)
