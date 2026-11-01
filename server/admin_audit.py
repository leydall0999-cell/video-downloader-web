"""后台写操作审计（2026-11-01）。

为什么需要它：后台是**公网可达**的。删号、封号、撤管理员、发权益这些动作一旦
被误点、被脚本打、或超管账号被盗，没有痕迹就只能靠猜 —— 而这几件事都不可逆。
所以只对 `/api/admin/` 下的**写请求**留痕（GET 不记，否则光是翻面板就会把
日志写满，真正要查的那条反而被淹掉）。

落盘 `server/data/admin_audit.log`，JSONL 一行一条，与 platform_health.json 同目录：

    {"ts":1793500000,"actor":"boss@example.com","method":"POST",
     "path":"/api/admin/users/u1/delete","status":200,"ip":"1.2.3.4"}

设计取舍：
  · 只记「谁、什么时候、对哪个接口、成没成」，**不记请求体** —— 请求体里可能有
    新密码明文，日志是要能给人看的，不能夹带凭据。
  · 写入失败一律吞掉：审计是旁路，绝不能因为它挂了导致后台操作失败。
  · 超过 2MB 自动滚成 `.1`，只留一代 —— 后台操作的量级很低，够查最近几个月。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
LOG_PATH = Path(os.environ.get("VDL_ADMIN_AUDIT_LOG", str(_HERE / "data" / "admin_audit.log")))
_MAX_BYTES = 2 * 1024 * 1024


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > _MAX_BYTES:
            os.replace(path, path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass


def append(rec: dict[str, Any]) -> None:
    """追加一条审计记录。任何异常都吞掉 —— 审计不能反过来影响业务。"""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(LOG_PATH)
        row = {
            "ts": int(rec.get("ts") or time.time()),
            "actor": str(rec.get("actor") or ""),
            "method": str(rec.get("method") or ""),
            "path": str(rec.get("path") or ""),
            "status": int(rec.get("status") or 0),
            "ip": str(rec.get("ip") or ""),
        }
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def tail(limit: int = 200) -> list[dict[str, Any]]:
    """读最近 limit 条（新的在前）。日志不存在时返回空，不报错。"""
    try:
        if not LOG_PATH.exists():
            return []
    except OSError:
        return []
    n = max(1, min(int(limit or 200), 2000))
    rows: list[dict[str, Any]] = []
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
    except OSError:
        return []
    return list(reversed(rows[-n:]))
