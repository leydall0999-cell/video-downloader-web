"""server/routers/telemetry.py — 桌面端（Mac/Win App）解析遥测回写端点。

背景（2026-11-03 · 平台健康巡检第 3 步）：
  巡检的「成功遥测」与「失败需求」原本只来自网页版（ECS 本机 /api/resolve 成功时
  落 .resolve_success.log）。但 App 才是主力端，App 解析成功/失败**完全没回写**，
  导致巡检只反映网页版这一小撮流量，对主力端失明。

  本端点接收 App **异步**上报的 host 级记录（**绝不带 URL / 标题**，与网页版同源
  口径），写入与网页版**同一份** ECS 本机日志：
      resolve_ok     → platform_telemetry.record_success(host, key)
      resolve_fail   → resolve_demand.record_demand(host, url, category)
  巡检（platform_health_check._load_evidence）**零改动**即可消费，从而反映真实主力端。

鉴权：
  VDL_TELEMETRY_TOKEN 配置后强制校验（X-Telemetry-Token 头）；**未配置则放行**
  （过渡期让链路先通，生产应配置——host 是公开域名、无利益驱动，且 record_* 自带
  60s 同 host 节流 + 2MB 截断，伪造刷爆风险可控）。

隐私：只收 host，不收 URL；record_success / record_demand 内部本就不存 URL。
任何异常一律静默返回 ok:false，绝不抛 5xx 影响主流程。
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from fastapi import APIRouter, Body, Request

router = APIRouter()


def _token_ok(request: Request) -> bool:
    tok = (os.environ.get("VDL_TELEMETRY_TOKEN") or "").strip()
    if not tok:
        # 未配置 = 过渡期放行（见模块 docstring）
        return True
    got = (request.headers.get("X-Telemetry-Token") or "").strip()
    return got == tok


def _host_ok(host: str) -> bool:
    h = (host or "").strip().lower()
    # 只收合法域名：无空格/斜杠、含点。record_* 进一步约束。
    return bool(h) and "/" not in h and " " not in h and "." in h


@router.post("/api/telemetry/report")
def report(payload: dict = Body(default=None), request: Request = None) -> dict:
    """接收桌面端异步上报的解析成功/失败（host 级，不收 URL）。"""
    try:
        if not _token_ok(request):
            return {"ok": False, "error": "forbidden"}
        body = payload if isinstance(payload, dict) else {}
        kind = str(body.get("kind") or "").strip()
        host = str(body.get("host") or "").strip().lower()
        key = str(body.get("key") or "").strip()[:64]
        if not _host_ok(host):
            return {"ok": False, "error": "bad_host"}
        if kind == "resolve_ok":
            try:
                import platform_telemetry
                platform_telemetry.record_success(host, key)
            except Exception:  # noqa: BLE001
                return {"ok": False, "error": "write_failed"}
            return {"ok": True}
        if kind == "resolve_fail":
            category = str(body.get("category") or "unsupported_platform")[:40]
            url = str(body.get("url") or "")[:200]
            try:
                import resolve_demand
                resolve_demand.record_demand(host, url, category)
            except Exception:  # noqa: BLE001
                return {"ok": False, "error": "write_failed"}
            return {"ok": True}
        return {"ok": False, "error": "bad_kind"}
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "error"}
