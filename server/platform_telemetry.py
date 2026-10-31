"""解析成功遥测 —— 「平台到底还活着吗」的**成功侧**证据。

与 `resolve_demand`（失败侧需求登记）成对，构成完整的平台健康度：
  - resolve_demand  = 用户**要但没拿到**（pending_extractor / unsupported_platform）；
  - platform_telemetry = 用户**真的拿到了**（解析成功）。

为什么需要它（2026-10-31 结论）：
  健康巡检原本依赖「样本 URL 库」，但实测发现样本采集才是真正的成本——
  25 个平台里 24 个拿不到可用样本（测试固件只有占位链接，编造的会 404）。
  域名级成功遥测**不需要任何样本库**：谁被真实用户解析成功了，就证明谁活着。
  天然只覆盖「有真实流量」的平台，而那恰好就是「坏了才值得修」的那些。

隐私底线（硬约束）：
  **只记 host（域名）+ 时间戳 + 平台 key，绝不记 URL、不记标题、不记 Cookie。**
  理由：URL 可能含分享者身份、私密视频 ID、带签名 token；记 URL 等于把用户
  看了什么存进服务器日志。判定「平台活着」根本不需要知道具体是哪条视频。

口径说明：
  - 与需求榜不同，成功侧**按天去重**而非按周：健康度关心的是「最近还在成功」，
    天粒度才能反映「昨天还行、今天挂了」这种突变；
  - 写入节流：同 host 60s 内只落一条（解析成功往往是用户反复试同一条链接）；
  - JSONL append-only + 2MB 截断，与需求榜同一套抗失控设计。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

LOG_NAME = ".resolve_success.log"
DEFAULT_WINDOW_DAYS = 30
MAX_BYTES = 2 * 1024 * 1024
KEEP_TAIL = 4000
THROTTLE_SECONDS = 60.0

_lock = threading.Lock()
_last_write: dict[str, float] = {}   # host -> 上次落库 ts（进程内节流）


def _log_path(base_dir: Path | None = None) -> Path:
    base = Path(base_dir) if base_dir else Path(
        os.environ.get("VDL_DATA_DIR") or (Path.home() / ".video-downloader"))
    return base / LOG_NAME


def record_success(host: str, platform_key: str = "", base_dir: Path | None = None) -> bool:
    """记一次解析成功。返回是否真写了（节流命中返回 False）。

    旁路统计：任何异常一律静默吞掉，绝不能影响用户正在等的解析结果。
    """
    host = (host or "").strip().lower()
    if not host or "/" in host or " " in host:
        return False
    now = time.time()
    with _lock:
        last = _last_write.get(host, 0.0)
        if now - last < THROTTLE_SECONDS:
            return False
        _last_write[host] = now
    try:
        path = _log_path(base_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": int(now),
            "host": host,
            "key": (platform_key or "")[:64],
        }
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        # 超限截断：读出来 → 保留尾部 → 整文件重写（与需求榜同款，append 不截断
        # 等于没做，文件会无限涨）。
        if path.exists() and path.stat().st_size > MAX_BYTES:
            data = path.read_bytes()
            tail = b"".join(data.splitlines(True)[-KEEP_TAIL:])
            with open(path, "wb") as f:
                f.write(tail)
                f.write(line.encode("utf-8"))
        else:
            with open(path, "ab") as f:
                f.write(line.encode("utf-8"))
    except Exception:
        return False
    return True


def _day_key(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).date().isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


def success_summary(days: int = DEFAULT_WINDOW_DAYS,
                    base_dir: Path | None = None) -> list[dict]:
    """按域名聚合成功遥测：[{host, days_count, total, last_ts, keys}]。

    days_count = 窗口内**有成功的不同天数**（天粒度，反映持续可用）；
    total      = 窗口内原始成功记录数；
    keys       = 命中的平台 key 集合（一个域可能被多个 key 命中，供诊断）。
    """
    path = _log_path(base_dir)
    if not path.exists():
        return []
    cutoff = time.time() - max(1, days) * 86400
    per_host: dict[str, dict] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                ts = float(rec.get("ts") or 0)
                if ts < cutoff:
                    continue
                host = str(rec.get("host") or "").lower()
                if not host:
                    continue
                agg = per_host.setdefault(host, {
                    "host": host, "days": set(), "total": 0,
                    "last_ts": 0.0, "keys": {},
                })
                day = _day_key(ts) or time.strftime("%Y-%m-%d", time.localtime(ts))
                agg["days"].add(day)
                agg["total"] += 1
                if ts >= agg["last_ts"]:
                    agg["last_ts"] = ts
                k = str(rec.get("key") or "")
                if k:
                    agg["keys"][k] = agg["keys"].get(k, 0) + 1
    except Exception:
        return []
    out = []
    for host, agg in per_host.items():
        out.append({
            "host": host,
            "days_count": len(agg["days"]),
            "total": agg["total"],
            "last_ts": int(agg["last_ts"]),
            "keys": sorted(agg["keys"].keys()),
        })
    out.sort(key=lambda r: (-r["days_count"], -r["last_ts"]))
    return out


def host_success_map(days: int = DEFAULT_WINDOW_DAYS,
                     base_dir: Path | None = None) -> dict[str, dict]:
    """按 host 索引的成功聚合，供健康表合成时 O(1) 查。"""
    return {r["host"]: r for r in success_summary(days=days, base_dir=base_dir)}
