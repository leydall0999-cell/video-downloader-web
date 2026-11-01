"""VDL 后台管理面板数据层（server/admin_store.py）。

职责：
  - 用户管理：列表（含会员态、禁用标记、is_admin 超级用户标记）、禁用/启用、重置密码。
  - 会员管理：列表、按 code 赠送会员、按 delta 调整积分。
  - 使用统计：聚合 stats.json。
  - 系统配置：套餐/成本/SMTP 状态（不暴露任何密码/密钥）。

超级用户（is_admin）的授权名单由 server/auth_store.ensure_superusers 维护
（admin.json 的 admin_identifiers 或环境变量 VDL_ADMIN_IDENTIFIER；无配置时首个
注册账号自动成为超级用户），后台内可经 /api/admin/users/{id}/set-admin 提权/降权。

纯本机、零外部依赖。
"""
from __future__ import annotations

import json
import os
import sys
import time
import atomic_io
from pathlib import Path
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def _base_dir() -> Path:
    # 覆盖位：与 server/auth_store._base_dir 保持一致（VDL_DATA_DIR 优先）。
    override = os.environ.get("VDL_DATA_DIR", "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        base = Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
    else:
        base = Path.home() / ".video-downloader"
    return base


def _users_path() -> Path:
    return _base_dir() / "users.json"


# --------------------------------------------------------------------------- #
# 用户管理
# --------------------------------------------------------------------------- #
def _load_users() -> dict[str, Any]:
    p = _users_path()
    if not p.exists():
        return {"users": [], "by_identifier": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return {"users": [], "by_identifier": {}}
    data.setdefault("users", [])
    data.setdefault("by_identifier", {})
    return data


def _save_users(data: dict[str, Any]) -> None:
    p = _users_path()
    # 原子写（唯一临时名 + os.replace），权限严格 0600。
    # 调用方必须已在 auth_store.users_mutation() 临界区内（并发读改写会互相覆盖丢账号）。
    atomic_io.atomic_write_json(p, data, mode=0o600, indent=2, ensure_ascii=False)


def _license_admin_token() -> str:
    """授权中心管理员令牌：env VDL_LICENSE_ADMIN_TOKEN → {base}/license_admin.json。

    2026-10-16「所有端用户上云端」：后台用户管理改以云端授权中心为数据源，
    管理操作（提权 / 封禁 / 改密）也双写云端 ⇒ 全端生效。未配置时空串，
    调用方退化为仅本机（等同历史行为）。
    """
    tok = (os.environ.get("VDL_LICENSE_ADMIN_TOKEN") or "").strip()
    if tok:
        return tok
    try:
        with open(_base_dir() / "license_admin.json", "r", encoding="utf-8") as fh:
            tok = str((json.load(fh) or {}).get("admin_token") or "").strip()
    except Exception:
        tok = ""
    return tok


def _cloud_users() -> Optional[list[dict[str, Any]]]:
    """拉云端全量账号；不可达 / 未配令牌返回 None（调用方走本机兜底）。"""
    token = _license_admin_token()
    if not token:
        return None
    try:
        import license_client
        r = license_client.users_remote(token)
    except Exception:
        return None
    if not isinstance(r, dict) or not r.get("ok"):
        return None
    users = r.get("users")
    return users if isinstance(users, list) else None


def _role_of(identifier: str, is_admin: bool) -> str:
    """角色标签：super（唯一超管）/ admin（管理员·只读后台）/ ""（普通用户）。

    2026-10-22 起后台拆两级权限（自桌面端同口径移植）。is_admin 仍是「能进后台」
    的布尔（云端真源不变），但**只有匹配唯一超管标识**的那一个才是 super，
    其余全部是只读的管理员。判定规则集中在 auth_store.super_admin_identifier()，
    这里只做标签映射。延迟 import 避免 auth_store ↔ admin_store 循环依赖。

    ⚠️ 网页版是公网部署：判定异常时按最小权限处理（宁可标成只读管理员，
    也绝不误判成超管）——误判成 super 等于把所有写接口开放出去。
    """
    if not is_admin:
        return ""
    try:
        from auth_store import is_super_identifier
        return "super" if is_super_identifier(identifier) else "admin"
    except Exception:
        return "admin"


def _cloud_user_row(u: dict[str, Any]) -> dict[str, Any]:
    """云端账号 → 后台行（保持既有字段键名不变，前端无感；新增端维度字段）。"""
    import time as _t
    now = _t.time()
    auth = u.get("authority") or {}
    devs = u.get("devices") or []
    return {
        "user_id": u.get("user_id") or u.get("email"),
        "identifier": u.get("email"),
        "created_at": u.get("created_at"),
        "last_login": u.get("last_login"),
        "disabled": bool(u.get("banned")),
        "is_admin": bool(u.get("is_admin")),
        "role": _role_of(str(u.get("email") or ""), bool(u.get("is_admin"))),
        "source": "cloud",
        "device_count": len(devs),
        "devices": [
            {"name": d.get("name"), "platform": d.get("platform"),
             "app_version": d.get("app_version"),
             "last_seen": d.get("last_seen")}
            for d in devs
        ],
        "membership": {
            "download_active": float(auth.get("member_until_dl") or 0) > now,
            "ai_active": float(auth.get("member_until_ai") or 0) > now,
            "credits_total": int(auth.get("perm_credits") or 0)
            + int(auth.get("ai_credits_left") or 0),
        },
    }


def list_users() -> list[dict[str, Any]]:
    """全端用户列表（2026-10-16：云端授权中心是唯一真源）。

    合并策略：云端账号为主（桌面 App + 网页版 cn/hk 的账号全部在此）；
    本机表中云端没有的账号（离线兜底分叉）以 source=local 附列，不丢人。
    云端不可达时整体退化为本机列表（source=local），后台仍可用。
    """
    from user_membership import get_user_store
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    cloud = _cloud_users()
    if cloud is not None:
        for u in cloud:
            email = str(u.get("email") or "").strip().lower()
            if not email:
                continue
            seen.add(email)
            out.append(_cloud_user_row(u))
    # 本机兜底 / 补充：已删除（deleted_at）账号不出现
    data = _load_users()
    for u in data.get("users", []):
        if u.get("deleted_at"):
            continue
        ident = str(u.get("identifier") or "").strip().lower()
        if ident in seen:
            continue
        uid = u.get("user_id")
        rec: dict[str, Any] = {
            "user_id": uid,
            "identifier": u.get("identifier"),
            "created_at": u.get("created_at"),
            "disabled": bool(u.get("disabled", False)),
            "is_admin": bool(u.get("is_admin", False)),
            "role": _role_of(ident, bool(u.get("is_admin", False))),
            "source": "local",
        }
        try:
            st = get_user_store(uid).status()
            rec["membership"] = {
                "download_active": st["download_member"]["active"],
                "ai_active": st["ai_member"]["active"],
                "credits_total": st["credits_total"],
            }
        except Exception:
            rec["membership"] = None
        out.append(rec)
    # 按注册时间倒序（新注册在前）
    out.sort(key=lambda r: (r.get("created_at") or 0), reverse=True)
    return out


def set_user_disabled(user_id: str, disabled: bool) -> dict[str, Any]:
    import auth_store  # 复用账号表的跨进程临界区（与 auth_store 同写 users.json）
    with auth_store.users_mutation():
        data = _load_users()
        user = next((u for u in data["users"] if u["user_id"] == user_id), None)
        if not user:
            return {"ok": False, "error": "用户不存在"}
        user["disabled"] = bool(disabled)
        user["updated_at"] = int(time.time())
        # 安全审计 2026-10-13（F-02）：禁用 / 解禁都撤销该账号已签发 token。
        # 此前 web 端禁用完全不撤 token ⇒ 后台「封禁」对已登录用户无效（实测确认）。
        # ⚠️ 本模块的 `_save_users` 是**直接原子写**、不经 auth_store._save_users，
        #    故必须显式失效鉴权状态缓存，否则 verify_token 的快照最多滞后 1 秒才看到「已禁用」。
        auth_store.bump_token_version(user)
        _save_users(data)
        auth_store._invalidate_status_cache()
    return {"ok": True, "disabled": bool(disabled)}


def delete_user(user_id: str) -> dict[str, Any]:
    """后台删除账号（软删除，2026-10-03）：标记 deleted_at + 禁用。

    与用户自助注销同一落盘形态：该 identifier 此后不可登录、不可再注册；
    记录保留在 users.json 里可审计。超级用户不可删（先降权），
    「不能删自己」由路由层拦。
    """
    import auth_store
    with auth_store.users_mutation():
        data = _load_users()
        user = next((u for u in data["users"] if u["user_id"] == user_id), None)
        if not user:
            return {"ok": False, "error": "用户不存在"}
        if user.get("deleted_at"):
            return {"ok": False, "error": "账号已删除"}
        if user.get("is_admin"):
            return {"ok": False, "error": "超级用户不可删除，请先取消其管理员"}
        user["deleted_at"] = int(time.time())
        user["disabled"] = True
        user["updated_at"] = int(time.time())
        # identifier 索引由 _save_users → _rebuild_index 重建（已跳过 deleted_at）
        _save_users(data)
    return {"ok": True}


def reset_user_password(user_id: str, new_password: str) -> dict[str, Any]:
    if not new_password or len(new_password) < 6:
        return {"ok": False, "error": "新密码至少 6 位"}
    data = _load_users()
    user = next((u for u in data["users"] if u["user_id"] == user_id), None)
    if not user:
        return {"ok": False, "error": "用户不存在"}
    from auth_store import reset_password
    ok = reset_password(user["identifier"], new_password)
    if not ok:
        return {"ok": False, "error": "重置失败（账号异常）"}
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 会员管理
# --------------------------------------------------------------------------- #
# 下载档「天数 → 档位名」（与 license_server.PLAN_EFFECT 一致）。
# 云端只存到期时间戳、不存档位名，故会员列表的档位名按剩余时长反推 ——
# 这样「有效至 X 日」与「档位」互相自洽（显示 download_1day 却到三个月后
# 会让管理员以为数据错了）。
_DL_TIER_DAYS = (("download_1day", 1), ("download_3day", 3),
                 ("download_7day", 7), ("download_month", 30),
                 ("download_half_year", 180), ("download_year", 365))


def _plan_name_by_remaining(until: float, now: float) -> Optional[str]:
    """按剩余时长反推下载档位名；明显对不上任何档位（叠加购买/手工改期）返回 None。"""
    if until <= now:
        return None
    days = (until - now) / 86400.0
    code, td = min(_DL_TIER_DAYS, key=lambda x: abs(x[1] - days))
    # 允许 25% + 1 天误差（下单当天起算、时区取整都会差一两天）
    return code if abs(td - days) <= max(1.0, td * 0.25) else None


def _cloud_membership_row(u: dict[str, Any], now: float) -> dict[str, Any]:
    """云端账号 → 会员行（2026-10-16：会员管理同样以云端总库为准）。

    权益取云端 authority；套餐名优先按剩余时长反推，推不出时退回「最近一笔
    同类购买」的 plan_code（下载档以 download_ 开头、AI 档以 ai_ 开头）。
    """
    auth = u.get("authority") or {}
    dl_until = float(auth.get("member_until_dl") or 0)
    ai_until = float(auth.get("member_until_ai") or 0)
    ai_left = int(auth.get("ai_credits_left") or 0)
    perm = int(auth.get("perm_credits") or 0)
    plan_dl: Optional[str] = None
    plan_ai: Optional[str] = None
    for p in reversed(list(u.get("purchases") or [])):
        code = str(p.get("plan_code") or "")
        if not code:
            continue
        if plan_dl is None and code.startswith("download_"):
            plan_dl = code
        if plan_ai is None and code.startswith("ai_"):
            plan_ai = code
        if plan_dl and plan_ai:
            break
    dl_active = dl_until > now
    ai_active = ai_until > now
    return {
        "user_id": u.get("user_id") or u.get("email"),
        "identifier": u.get("email"),
        "download_active": dl_active,
        "download_plan": (_plan_name_by_remaining(dl_until, now) or plan_dl) if dl_active else None,
        "download_expire_at": dl_until,
        "ai_active": ai_active,
        "ai_plan": plan_ai if ai_active else None,
        "ai_expire_at": ai_until,
        "ai_credits_left": ai_left,
        "permanent_credits": perm,
        "credits_total": ai_left + perm,
        "disabled": bool(u.get("banned")),
        "source": "cloud",
    }


def list_memberships() -> list[dict[str, Any]]:
    """会员列表（2026-10-16：云端授权中心是唯一真源，与本机行合并）。

    与 list_users 同一策略：云端账号为主（含各端全部账号的权益），本机表里
    云端没有的账号以 source=local 附列；云端不可达时整体退化本机列表。
    """
    from user_membership import get_user_store
    now = time.time()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    cloud = _cloud_users()
    if cloud is not None:
        for u in cloud:
            email = str(u.get("email") or "").strip().lower()
            if not email:
                continue
            seen.add(email)
            out.append(_cloud_membership_row(u, now))
    data = _load_users()
    for u in data.get("users", []):
        if u.get("deleted_at"):
            continue
        ident = str(u.get("identifier") or "").strip().lower()
        if ident and ident in seen:
            continue          # 云端已有该账号，以云端权益为准
        uid = u.get("user_id")
        try:
            st = get_user_store(uid).status()
        except Exception:
            continue
        out.append({
            "user_id": uid,
            "identifier": u.get("identifier"),
            "download_active": st["download_member"]["active"],
            "download_plan": st["download_member"]["plan"],
            "download_expire_at": st["download_member"]["expire_at"],
            "ai_active": st["ai_member"]["active"],
            "ai_plan": st["ai_member"]["plan"],
            "ai_expire_at": st["ai_member"].get("expire_at") or 0.0,
            "ai_credits_left": st["ai_member"]["credits_left"],
            "permanent_credits": st["permanent_credits"],
            "credits_total": st["credits_total"],
            "disabled": bool(u.get("disabled", False)),
            "source": "local",
        })
    out.sort(key=lambda r: (r.get("download_active") or r.get("ai_active") or False), reverse=True)
    return out


def grant_membership(user_id: str, code: str) -> dict[str, Any]:
    from user_membership import get_user_store
    data = _load_users()
    user = next((u for u in data["users"] if u["user_id"] == user_id), None)
    if not user:
        return {"ok": False, "error": "用户不存在"}
    try:
        res = get_user_store(user_id).activate(code)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"激活失败：{e}"}
    return res


def adjust_credits(user_id: str, delta: int) -> dict[str, Any]:
    from user_membership import get_user_store
    data = _load_users()
    user = next((u for u in data["users"] if u["user_id"] == user_id), None)
    if not user:
        return {"ok": False, "error": "用户不存在"}
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        return {"ok": False, "error": "delta 必须为整数"}
    try:
        res = get_user_store(user_id).add_credits(delta)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"调分失败：{e}"}
    return res


# --------------------------------------------------------------------------- #
# 使用统计 / 系统配置
# --------------------------------------------------------------------------- #
def usage_stats() -> dict[str, Any]:
    from stats import get_stats
    return get_stats()


def system_config() -> dict[str, Any]:
    """套餐、成本、SMTP 状态（超级用户专属；含 SMTP 密码以便编辑，仅带 user token 可见）。

    返回 has_superuser 标记是否存在任一 is_admin 账号，供面板提示超级用户引导。
    """
    from membership import MembershipStore, MATTING_CLOUD_CREDIT_COST, load_plan_overrides
    from auth_store import _smtp_accounts, _load_users
    data = _load_users()
    has_superuser = any(u.get("is_admin", False) for u in data.get("users", []))
    cfg: dict[str, Any] = {
        "plans": MembershipStore().plans(),
        "credit_costs": {"matting_cloud": MATTING_CLOUD_CREDIT_COST},
        "smtp": {"configured": False, "accounts": []},
        "has_superuser": bool(has_superuser),
        "has_plan_overrides": False,
    }
    try:
        ov = load_plan_overrides()
        cfg["has_plan_overrides"] = bool(ov)
        if "credit_costs" in ov:
            cfg["credit_costs"] = ov["credit_costs"]
    except Exception:
        pass
    try:
        accounts = _smtp_accounts() or []
        cfg["smtp"]["configured"] = len(accounts) > 0
        # admin 专属：返回完整账户（含 pass）以便前端编辑；仅带 token 可见
        cfg["smtp"]["accounts"] = [
            {"host": a.get("host"), "port": a.get("port"), "user": a.get("user"),
             "pass": a.get("pass"), "from": a.get("from"),
             "from_name": a.get("from_name"), "use_ssl": a.get("use_ssl"),
             "match_domains": a.get("match_domains"), "default": a.get("default")}
            for a in accounts
        ]
    except Exception:
        pass
    return cfg


# --------------------------------------------------------------------------- #
# 配置写回（后台管理面板「系统配置」可编辑）
# --------------------------------------------------------------------------- #
def save_smtp_accounts(accounts: Any) -> dict[str, Any]:
    """保存 SMTP 多账户配置到 ~/.video-downloader/smtp.json（0600）。

    accounts: list[dict]，字段 host/port/user/pass/from/from_name/use_ssl/
    match_domains/default。pass 为空字符串时回退保留同索引已有密码（便于只改
    非密码字段）；否则使用传入值。无账户标记 default 时自动设第一个为默认。
    """
    if not isinstance(accounts, list):
        return {"ok": False, "error": "accounts 必须为数组"}
    from auth_store import _smtp_accounts
    existing = _smtp_accounts() or []
    cleaned: list[dict[str, Any]] = []
    for i, a in enumerate(accounts):
        if not isinstance(a, dict):
            return {"ok": False, "error": f"第 {i + 1} 个账户格式错误"}
        host = str(a.get("host") or "").strip()
        user = str(a.get("user") or "").strip()
        if not host or not user:
            return {"ok": False, "error": f"第 {i + 1} 个账户缺少 host 或 user"}
        pw = a.get("pass")
        if not pw:
            pw = (existing[i].get("pass") if i < len(existing) else None) or ""
        if not pw:
            return {"ok": False, "error": f"第 {i + 1} 个账户缺少密码"}
        try:
            port = int(a.get("port") or 465)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"第 {i + 1} 个账户端口非法"}
        cleaned.append({
            "host": host,
            "port": port,
            "user": user,
            "pass": str(pw),
            "from": str(a.get("from") or user),
            "from_name": str(a.get("from_name") or "VideoDownloader"),
            "use_ssl": bool(a.get("use_ssl", True)),
            "match_domains": [str(d).strip().lower() for d in (a.get("match_domains") or []) if str(d).strip()],
            "default": bool(a.get("default", False)),
        })
    if cleaned and not any(c.get("default") for c in cleaned):
        cleaned[0]["default"] = True
    p = _base_dir() / "smtp.json"
    try:
        atomic_io.atomic_write_json(p, {"accounts": cleaned})
    except OSError as e:  # noqa: BLE001
        return {"ok": False, "error": f"写入失败：{e}"}
    return {"ok": True, "count": len(cleaned)}


def save_plan_overrides(data: Any) -> dict[str, Any]:
    """写回套餐/成本覆盖到 plans.json（委托 membership.save_plan_overrides）。"""
    from membership import save_plan_overrides as _save
    try:
        result = _save(data or {})
        return {"ok": True, "overrides": result}
    except OSError as e:  # noqa: BLE001
        return {"ok": False, "error": f"写入失败：{e}"}


def reset_stats(path: Optional[Path] = None) -> dict[str, Any]:
    from stats import reset_stats as _reset
    _reset(path)
    return {"ok": True}
