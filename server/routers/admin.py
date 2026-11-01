"""server/routers/admin.py — 后台管理面板 API（/api/admin/*）。

全部接口需「已登录 + 超级用户」权限：经 Authorization: Bearer <user_token> 携带
用户 token，并由 users.json 中 is_admin=True 的账号放行。非超级用户或缺失 token
一律 401。

超级用户名单由 server/auth_store.ensure_superusers 维护：admin.json 的 admin_identifiers
或环境变量 VDL_ADMIN_IDENTIFIER；两者均未配置时，首个注册的账号自动成为超级用户。
后台内可经 /api/admin/users/{id}/set-admin 提权 / 降权其他账号。

功能模块：用户管理 / 会员管理 / 使用统计 / 系统配置。
"""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter()

from admin_store import (
    list_users,
    set_user_disabled,
    reset_user_password,
    delete_user,
    list_memberships,
    grant_membership,
    adjust_credits,
    usage_stats,
    system_config,
    reset_stats,
    save_smtp_accounts,
    save_plan_overrides,
)
from user_membership import get_current_user_id
from auth_store import user_is_admin, set_user_admin
import license_client  # 纯 stdlib 叶子模块：面板对云端账号的操作直接走它


def _resolve_identifier(user_id: str) -> str:
    """user_id → 账号标识（邮箱/手机号，云端主键）。

    云端行的 user_id 就是规范化邮箱；本机行是 u_xxx，需查本机账号表。

    🔴 2026-10-22 修（与桌面端同源，手机号账号提权「点了没反应」的根因）：
    入参也可能**本身就是账号标识** —— 云端 uid = 手机号（如 "15638319956"），
    而面板行的 user_id 恰恰就是云端 uid。旧实现只认「含 @ 的字符串」，其余
    一律当本机 user_id 去查表 —— 本机 user_id 是 u_xxx，查不到 ⇒ 返回空 ⇒
    `_cloud_sync("")` 直接跳过云端（notice「未找到对应账号标识，本次仅本机生效」）
    ⇒ 本机改了、**云端没改**，而面板列表读的是云端数据 ⇒ 用户看到「点了完全没反应」。
    """
    raw = (user_id or "").strip()
    if not raw:
        return ""
    if "@" in raw:
        return raw.lower()
    try:
        from auth_store import user_identifier
        ident = str(user_identifier(raw) or "").strip().lower()
        if ident:
            return ident
    except Exception:
        pass
    # 本机按 user_id 没查到：入参很可能就是账号标识本身（手机号 / 非邮箱标识）。
    # 云端 uid 就是规范化标识 ⇒ 原样（小写）返回即可。
    # u_ 开头的是本机 user_id，查不到说明账号已删 ⇒ 返回空。
    if raw.startswith("u_"):
        return ""
    return raw.lower()


def _cloud_sync(ident: str, op) -> str:
    """把超管写操作同步到云端授权中心（2026-10-16「每端数据统一」：全端生效）。

    op(admin_token) -> dict。未配令牌 / 云端不可达时返回提示文案（附在响应
    notice 里），**不阻断本机操作** —— 本机是离线兜底，云端是真源。
    """
    if not ident:
        return "（未找到对应账号标识，本次仅本机生效）"
    import admin_store
    import license_client
    token = admin_store._license_admin_token()  # noqa: SLF001 — 同文件家族的内部约定
    if not token:
        return "（未配置授权中心管理令牌，本次仅本机生效）"
    try:
        r = op(token)
    except Exception as e:  # noqa: BLE001 — 云端异常只提示不阻断
        return f"（云端同步失败：{e}）"
    if isinstance(r, dict) and not r.get("ok"):
        return f"（云端同步失败：{r.get('error') or r.get('detail') or '未知'}）"
    return ""


def _local_uid(user_id: str) -> str:
    """入参是本机 user_id（u_xxx）或账号标识（邮箱/手机号）→ 本机 user_id。

    云端行的 user_id 就是账号标识（云端 uid = 规范化邮箱），本机没有该账号
    镜像时返回 "" —— 调用方据此走「纯云端」分支。
    """
    raw = (user_id or "").strip()
    if not raw:
        return ""
    try:
        import auth_store
        data = auth_store._load_users()
    except Exception:  # noqa: BLE001 — 账号表读失败不该让面板 500
        return ""
    for u in data.get("users", []):
        if u.get("user_id") == raw and not u.get("deleted_at"):
            return raw
    uid = (data.get("by_identifier") or {}).get(raw.strip().lower())
    return str(uid or "")


def _admin_mutate(user_id: str, local_op, cloud_op, *,
                  cloud_when_local: bool = True) -> dict[str, Any]:
    """超管写操作统一入口：本机（存在时）+ 云端授权中心（唯一真源）。

    🔴 2026-10-16 修「面板按钮点了没反应 / 报用户不存在」：用户与会员列表改为
    云端总库后，行上的 user_id 是**账号标识**（云端 uid = 邮箱），而各操作端点
    一律按本机 user_id 去本机账号表里查 ⇒ 云端账号全部「用户不存在」。
    这里统一解析标识：
      · 本机有该账号 → 先本机（沿用原语义，失败即返回），再同步云端；
      · 本机没有（纯云端账号）→ 只写云端，云端成功才算成功。
    """
    ident = _resolve_identifier(user_id)
    lu = _local_uid(user_id)
    if lu:
        out = local_op(lu)
        if not (isinstance(out, dict) and out.get("ok")):
            return out if isinstance(out, dict) else {"ok": False, "error": "本机操作失败"}
    else:
        out = {"ok": True, "user_id": ident or user_id, "cloud_only": True}
    if lu and not cloud_when_local:
        import admin_store
        out["notice"] = ("" if admin_store._license_admin_token()
                         else "（未配置授权中心管理令牌，本次未同步云端）")
        return out
    out["notice"] = _cloud_sync(ident, lambda tok: cloud_op(ident, tok))
    if not lu:
        # 本机没有这个账号：云端是唯一落点，云端没成就是整单失败
        if not ident:
            return {**out, "ok": False, "error": "未找到对应账号标识，无法定位云端账号"}
        if out["notice"]:
            return {**out, "ok": False, "error": str(out["notice"]).strip("（）")}
    return out


def _cloud_credit_ops(ident: str, tok: str, delta: int, pool: str) -> list:
    """云端调分序列：把本地 pool 语义映射成云端 adjust(pool, delta) 调用列表。

    - ai / permanent：原样一条；
    - auto：正=充永久；负=先扣 AI 订阅余量再扣永久（与 membership.add_credits 一致）。
      云端账号没有本地 store，AI 余量改从云端 authority 取（多一次只读调用）。
    """
    if pool in ("ai", "permanent"):
        return [(pool, delta)]
    if delta >= 0:
        return [("permanent", delta)]
    amount = -delta
    ai_left = 0
    try:
        u = license_client.usage_remote(ident, tok)
        ai_left = int(((u or {}).get("authority") or {}).get("ai_credits_left") or 0)
    except Exception:  # noqa: BLE001 — 取不到就保守按「全扣永久」
        ai_left = 0
    ai_d = min(max(ai_left, 0), amount)
    ops: list = []
    if ai_d > 0:
        ops.append(("ai", -ai_d))
    if amount - ai_d > 0:
        ops.append(("permanent", -(amount - ai_d)))
    return ops


def require_admin(request: Request) -> None:
    from fastapi import HTTPException
    import app  # noqa: E402 - 共享内核（_cross_site_rejected 来源校验）
    # 安全审计 2026-10-13（F-11）：管理端点补跨源 CSRF 兜底。后台面板为同源前端，
    # 同源请求无 Origin 头或命中本机白名单 → 放行；外部站点跨源 → 403。
    if app._cross_site_rejected(request):
        raise HTTPException(status_code=403, detail="拒绝来自外部站点的跨源请求")
    uid = get_current_user_id(request)
    if not uid or not user_is_admin(uid):
        raise HTTPException(status_code=401, detail="未授权：需要管理员权限")


def require_super_backend(request: Request) -> None:
    """改「三端生效值」的闸门：只认 2 小时的后台令牌（adm 作用域）。

    🔴 为什么不能直接用 require_super（2026-11-01 实测发现）：`require_super` 走
    `get_current_user_id` → `token_from_header`，而它**两种令牌都收** —— 这是为
    桌面 App 后台刻意留的兼容（App 拿普通令牌进后台）。后果是：前台 30 天的普通
    登录会话也能改套餐价格 / 免费额度，会话一旦泄露就能改三端售价，而那条会话
    既没有 2 小时过期，也没有登录失败限流。
    改价 / 改额度属于「影响所有人钱包」的写操作，必须由后台登录（/admin/login）
    发起 —— 这里只认 adm 令牌，普通登录态一律 403。
    """
    from fastapi import HTTPException
    import app as _app  # noqa: E402 - 共享内核（跨源校验）
    if _app._cross_site_rejected(request):
        raise HTTPException(status_code=403, detail="拒绝来自外部站点的跨源请求")
    import auth_store
    raw = request.headers.get("Authorization") or ""
    tok = raw[7:].strip() if raw.startswith("Bearer ") else ""
    uid = auth_store.verify_admin_token(tok) if tok else None
    if not uid:
        raise HTTPException(
            status_code=403,
            detail="改价 / 改额度需要先在 /admin/login 登录后台（普通登录态不够）")
    if not user_is_admin(uid):
        raise HTTPException(status_code=403, detail="未授权：需要超级管理员权限")


def require_super(request: Request) -> None:
    """写操作闸门：只有**唯一超管**放行（2026-10-22 权限拆两级，自桌面端移植）。

    分工：
      require_admin —— 能进后台、能看（用户列表 / 会员 / 统计 / 配置只读）
      require_super —— 能改（设/撤管理员、封号、发权益、改价、改配置、重置密码、删除）

    管理员拿合法 token 调写接口一律 **403**（不是 401）：身份没问题，是权限不够。
    前端据此提示「仅超级管理员可操作」，而不是把已登录的人踢回登录页。

    🔴 CSRF 兜底必须保留：网页版是公网部署，写接口若只查角色不查来源，
    外部站点可借已登录管理员的浏览器 Cookie 触发状态变更。这里与 require_admin
    共用同一份 `_cross_site_rejected` 判定（走共享内核 app 模块）。
    """
    from fastapi import HTTPException
    import app  # noqa: E402 - 共享内核（_cross_site_rejected 来源校验）
    if app._cross_site_rejected(request):
        raise HTTPException(status_code=403, detail="拒绝来自外部站点的跨源请求")
    uid = get_current_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="未授权：请先登录")
    if not user_is_admin(uid):
        raise HTTPException(status_code=401, detail="未授权：需要超级管理员权限")
    from auth_store import user_role
    if user_role(uid) != "super":
        raise HTTPException(status_code=403,
                            detail="仅超级管理员可执行此操作（管理员为只读）")


# --------------------------------------------------------------------------- #
# 后台独立登录（2026-11-01）
# --------------------------------------------------------------------------- #
# 为什么单独一套：原来后台没有自己的登录门，只能"先在前台当普通用户登录，
# 碰巧这个号是管理员"才能进 —— 入口混在前台、令牌还是 30 天长期会话。
# 现在 /admin/login 自己一个登录框，签发只活 2 小时的 adm 作用域令牌
# （见 auth_store.issue_admin_token），跟前台账号会话彻底分开。

# 失败计数放进程内存：vdl-web 是单 worker，重启即清零 —— 对"拦暴力枚举"足够。
# 🔴 键是 **IP 而不是账号**：按账号锁会给攻击者一个"把超管账号锁死"的拒绝服务面。
_LOGIN_FAILS: dict[str, list[float]] = {}
_LOGIN_MAX = 5                    # 窗口内允许的失败次数
_LOGIN_WINDOW = 10 * 60           # 计数窗口 10 分钟
_LOGIN_LOCK = 10 * 60             # 超限后锁 10 分钟


def _client_ip(request: Request) -> str:
    """取真实客户端 IP。

    公网链路是 浏览器 → Cloudflare → CF 隧道 → 本机 nginx → uvicorn，
    `request.client.host` 恒为 127.0.0.1。若直接拿它当限流键，等于**全网共用一个
    计数器** —— 任意一个人输错 5 次就把所有人锁在门外。

    真实 IP：CF-Connecting-IP 由 Cloudflare 覆写（客户端无法伪造）；缺失时退到
    本机 nginx 写的 X-Real-IP / X-Forwarded-For 首段。
    """
    hdrs = getattr(request, "headers", None) or {}
    for key in ("cf-connecting-ip", "x-real-ip"):
        val = str(hdrs.get(key) or "").strip()
        if val:
            return val
    xff = str(hdrs.get("x-forwarded-for") or "").split(",")[0].strip()
    if xff:
        return xff
    try:
        return str(request.client.host or "?")
    except Exception:  # noqa: BLE001
        return "?"


def _lock_left(ip: str) -> int:
    """返回剩余锁定秒数；0 = 未锁。顺手清掉窗口外的旧记录（避免字典无限增长）。"""
    now = time.time()
    rows = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < _LOGIN_LOCK]
    if rows:
        _LOGIN_FAILS[ip] = rows
    else:
        _LOGIN_FAILS.pop(ip, None)
    if len(rows) >= _LOGIN_MAX:
        return max(1, int(_LOGIN_LOCK - (now - rows[-1])))
    return 0


def _note_fail(ip: str) -> None:
    now = time.time()
    rows = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < _LOGIN_WINDOW]
    rows.append(now)
    _LOGIN_FAILS[ip] = rows[-20:]      # 只留最近 20 条，内存有上限


def _admin_token_left(request: Request) -> Optional[int]:
    """当前请求若带后台令牌，返回剩余有效秒数（前端据此显示"还剩 X 分钟"）。"""
    auth = str((getattr(request, "headers", None) or {}).get("Authorization") or "")
    if not auth.startswith("Bearer "):
        return None
    raw = auth[7:].strip()
    try:
        payload = raw.split(".")[1]
        pad = "=" * (-len(payload) % 4)
        import base64
        parts = base64.urlsafe_b64decode(payload + pad).decode("utf-8").split(".")
        if len(parts) >= 5 and parts[4] == "adm":
            return max(0, int(parts[2]) - int(time.time()))
    except Exception:  # noqa: BLE001 — 解析失败只是不显示倒计时
        return None
    return None


@router.get("/admin/login", include_in_schema=False)
def admin_login_page() -> Any:
    """把 /admin/login 指到静态登录页。

    StaticFiles(html=True) 只会把目录映射到 index.html，`/admin/login` 这种
    无扩展名路径会 404 —— 而"单独登录地址"必须短、能直接敲。所以显式给一条路由。
    """
    import os
    import app as _app
    # WEB_DIR 在冻结包里是 Path、源码运行时也是 Path，但用 os.path.join 统一兜住，
    # 免得哪天它变成 str 时这里抛 TypeError（后台登录页直接 500）。
    return FileResponse(os.path.join(str(_app.WEB_DIR), "admin", "login.html"),
                        media_type="text/html; charset=utf-8")


@router.post("/api/admin/login")
def admin_login(request: Request, payload: dict[str, Any] = Body(...)) -> Any:
    """后台专用登录：账号密码 → 2 小时 adm 令牌。

    安全要素：
      · 同 IP 10 分钟内错 5 次 → 锁 10 分钟（429）；
      · 密码错 / 账号不存在 → **统一文案**，不区分（防账号枚举）；
      · 密码对但非管理员 → 403 明确告知「没有后台权限」（能走到这一步已证明是本人，
        不构成枚举风险，且含糊文案会让真人反复重试）；
      · 成功/失败都写审计（含 IP），失败尝试同样留痕。
    """
    import admin_audit

    ident = str(payload.get("identifier") or "").strip().lower()
    pw = str(payload.get("password") or "")
    ip = _client_ip(request)

    left = _lock_left(ip)
    if left:
        admin_audit.append({"actor": ident or "-", "method": "POST",
                            "path": "/api/admin/login·locked", "status": 429, "ip": ip})
        return JSONResponse(status_code=429, content={
            "ok": False, "code": "TOO_MANY_TRIES",
            "error": f"密码错误次数过多，请 {max(1, (left + 59) // 60)} 分钟后再试",
            "retry_after": left})

    if not ident or not pw:
        return JSONResponse(status_code=400, content={
            "ok": False, "code": "MISSING", "error": "请输入账号和密码"})

    # ① 先走本机镜像校验：快，且**不注册云端设备**。
    #    若这里直接调前台 auth_login，每 2 小时登一次后台就会注册一次设备，
    #    反复挤占管理员自己的 2 台设备额度（把这台浏览器当新设备）。
    from auth_store import authenticate
    uid = authenticate(ident, pw)
    warn = ""
    if not uid:
        # ② 本机没有该号 / 镜像密码陈旧 → 交前台登录链路判一次（云端权威）。
        #    这条路径低频（仅在首次或云端改密后），代价是注册一次设备。
        from fastapi import Response as _Resp
        from routers.auth import auth_login
        res = auth_login(request, _Resp(), {"identifier": ident, "password": pw})
        if not res or not res.get("ok"):
            _note_fail(ip)
            admin_audit.append({"actor": ident, "method": "POST",
                                "path": "/api/admin/login", "status": 401, "ip": ip})
            return JSONResponse(status_code=401, content={
                "ok": False, "code": "BAD_CREDENTIALS",
                "error": str((res or {}).get("error") or "账号或密码错误")})
        uid = str(res.get("user_id") or "")
        warn = str(res.get("cloud_notice") or "")

    if not uid:
        _note_fail(ip)
        return JSONResponse(status_code=401, content={
            "ok": False, "code": "BAD_CREDENTIALS", "error": "账号或密码错误"})

    from auth_store import ADMIN_TOKEN_TTL, issue_admin_token, user_identifier, user_role
    role = user_role(uid)
    if role not in ("super", "admin"):
        admin_audit.append({"actor": user_identifier(uid) or ident, "method": "POST",
                            "path": "/api/admin/login", "status": 403, "ip": ip})
        return JSONResponse(status_code=403, content={
            "ok": False, "code": "NOT_ADMIN",
            "error": "该账号没有后台权限（只有管理员/超级管理员可进后台）"})

    token = issue_admin_token(uid)
    _LOGIN_FAILS.pop(ip, None)
    actor = user_identifier(uid) or ident
    admin_audit.append({"actor": actor, "method": "POST",
                        "path": "/api/admin/login", "status": 200, "ip": ip})
    return {"ok": True, "token": token, "user_id": uid, "identifier": actor,
            "role": role, "expires_in": ADMIN_TOKEN_TTL, "notice": warn}


@router.get("/api/admin/whoami")
def admin_whoami(request: Request = None) -> dict[str, Any]:
    """后台身份自查：角色 + 令牌剩余有效期（前端顶部显示「还剩 X 分钟」）。"""
    require_admin(request)
    from auth_store import user_identifier, user_role
    uid = get_current_user_id(request)
    return {"ok": True, "user_id": uid,
            "identifier": user_identifier(uid) or uid,
            "role": user_role(uid),
            "expires_in": _admin_token_left(request)}


@router.get("/api/admin/users")
def admin_list_users(request: Request = None) -> dict[str, Any]:
    require_admin(request)
    rows = list_users()
    # 「当前账号」标记：云端行的 user_id 是邮箱，前端拿本机 uid 比不出来（会把
    # 自己那行也渲染成「取消管理员 / 禁用」）⇒ 由后端按标识判定后回填。
    me = _resolve_identifier(get_current_user_id(request))
    for r in rows:
        r["self"] = bool(me) and str(r.get("identifier") or "").strip().lower() == me
    return {"ok": True, "users": rows, "total": len(rows)}


@router.post("/api/admin/users/{user_id}/set-admin")
def admin_set_admin(user_id: str, payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    require_super(request)
    flag = bool(payload.get("is_admin", False))
    target = _resolve_identifier(user_id)
    # 🔴 唯一超管不可被降级（2026-10-22 权限拆两级）：超管身份由授权名单 /
    #    「最早注册」推导，**不由这个接口决定**。允许在这里取消会直接把后台锁死
    #    （全库无超管 ⇒ 所有写操作 403，且没有入口能恢复）。
    from auth_store import is_super_identifier
    if not flag and is_super_identifier(target):
        return {"ok": False,
                "error": "超级管理员不可被取消（超管唯一，身份由授权名单决定）"}
    # 自己不能被降权（误点「取消管理员」会把当前账号锁在门外）。
    if not flag and target == _resolve_identifier(get_current_user_id(request)):
        return {"ok": False, "error": "不能取消当前登录账号的管理员权限"}
    return _admin_mutate(
        user_id,
        lambda uid: set_user_admin(uid, flag),
        lambda ident, tok: license_client.set_admin_remote(ident, flag, tok))


@router.post("/api/admin/users/{user_id}/disable")
def admin_disable_user(user_id: str, request: Request = None) -> dict[str, Any]:
    require_super(request)
    if _resolve_identifier(user_id) == _resolve_identifier(get_current_user_id(request)):
        return {"ok": False, "error": "不能禁用当前登录的账号"}
    return _admin_mutate(
        user_id,
        lambda uid: set_user_disabled(uid, True),
        lambda ident, tok: license_client.ban_remote(ident, True, tok, note="admin disable"))


@router.post("/api/admin/users/{user_id}/enable")
def admin_enable_user(user_id: str, request: Request = None) -> dict[str, Any]:
    require_super(request)
    return _admin_mutate(
        user_id,
        lambda uid: set_user_disabled(uid, False),
        lambda ident, tok: license_client.ban_remote(ident, False, tok, note="admin enable"))


@router.post("/api/admin/users/{user_id}/reset-password")
def admin_reset_password(user_id: str, payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    require_super(request)
    new_pw = str(payload.get("new_password") or "")
    if not new_pw or len(new_pw) < 6:
        return {"ok": False, "error": "新密码至少 6 位"}
    return _admin_mutate(
        user_id,
        lambda uid: reset_user_password(uid, new_pw),
        lambda ident, tok: license_client.set_password_remote(ident, new_pw, admin_token=tok))


@router.post("/api/admin/users/{user_id}/delete")
def admin_delete_user(user_id: str, request: Request = None) -> dict[str, Any]:
    """删除账号（本机软删除 + 云端销号）：不可删自己；超管不可删。"""
    require_super(request)
    me = _resolve_identifier(get_current_user_id(request))
    ident = _resolve_identifier(user_id)
    if user_id == get_current_user_id(request) or (ident and ident == me):
        return {"ok": False, "error": "不能删除当前登录的账号"}
    row = next((r for r in list_users()
                if r.get("user_id") == user_id
                or str(r.get("identifier") or "").strip().lower() == ident), None)
    if row and row.get("is_admin"):
        return {"ok": False, "error": "超级用户不可删除，请先取消其管理员"}
    return _admin_mutate(
        user_id,
        lambda uid: delete_user(uid),
        lambda i, tok: license_client.delete_user_remote(i, tok))


@router.get("/api/admin/memberships")
def admin_list_memberships(request: Request = None) -> dict[str, Any]:
    require_admin(request)
    items = list_memberships()
    return {"ok": True, "memberships": items, "total": len(items)}


@router.post("/api/admin/memberships/grant")
def admin_grant(payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    require_super(request)
    user_id = str(payload.get("user_id") or "")
    code = str(payload.get("code") or "").strip()
    if not user_id or not code:
        return {"ok": False, "error": "user_id 与 code 必填"}
    # web 版 grant_membership 只落本机账本、不推云 ⇒ 本机路径也要推云端（用
    # 云端 grant 记一笔发放）；纯云端账号同理只能走云端 grant。
    return _admin_mutate(
        user_id,
        lambda uid: grant_membership(uid, code),
        lambda ident, tok: license_client.grant_remote(ident, code, tok, note="admin grant"))


@router.post("/api/admin/memberships/credits")
def admin_credits(payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    require_super(request)
    user_id = str(payload.get("user_id") or "")
    try:
        delta = int(payload.get("delta"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "delta 必须为整数"}

    def _cloud_adjust(ident: str, tok: str) -> dict[str, Any]:
        last: dict[str, Any] = {"ok": True}
        for cpool, cdelta in _cloud_credit_ops(ident, tok, delta, "auto"):
            if cdelta == 0:
                continue
            last = license_client.adjust_remote(tok, ident, cpool, cdelta, note="admin credits")
            if isinstance(last, dict) and not last.get("ok"):
                return last
        return last

    # web 版 adjust_credits 只落本机账本、不推云 ⇒ 本机路径也要推云端
    return _admin_mutate(
        user_id,
        lambda uid: adjust_credits(uid, delta),
        _cloud_adjust)


@router.get("/api/admin/stats")
def admin_stats(request: Request = None) -> dict[str, Any]:
    require_admin(request)
    return {"ok": True, **usage_stats()}


@router.post("/api/admin/stats/reset")
def admin_stats_reset(request: Request = None) -> dict[str, Any]:
    require_super(request)
    return reset_stats()


@router.get("/api/admin/platform-health")
def admin_platform_health(days: int = 30, request: Request = None) -> dict[str, Any]:
    """平台健康证据源（2026-11-01）：成功遥测 + 需求榜 + 最近一次巡检健康表。

    为什么要单独开这个端点：域名级**解析成功**遥测（`platform_telemetry`）原来
    是只落在 ECS 本机的孤儿数据 —— 数据一直在写，但后台没有任何入口读它，
    管理员等于看不见。这里把三份一次给全，且**只读**（require_admin，非 super）。

    隐私口径：success 只有 `host + 时间 + 平台 key`，**不含任何 URL**（URL 可能带
    私密视频 ID / 分享者身份 / 签名 token）。
    """
    require_admin(request)
    import json as _json
    import os
    from pathlib import Path

    base = Path(os.environ.get("VDL_DATA_DIR") or (Path.home() / ".video-downloader"))
    try:
        import platform_telemetry
        _days = max(1, min(int(days or 30), 365))
        succ = platform_telemetry.success_summary(days=_days, base_dir=base)
    except Exception:  # noqa: BLE001 — 后台查询失败不该 500
        succ = []
    try:
        import resolve_demand
        dem = resolve_demand.demand_summary(days=84, base_dir=base)
    except Exception:  # noqa: BLE001
        dem = []
    # 巡检健康表由 cron 产出的 platform_health_check.py 写在**仓库根/data**，
    # 而本文件在 server/routers/ ⇒ ECS 上是 parents[2]/data，本地开发是
    # parents[1]/data。两处都探一下，取第一个存在的。
    check = None
    tickets = []
    _here = Path(__file__).resolve()
    for _root in (_here.parents[2], _here.parents[1]):
        _cand = _root / "data" / "platform_health.json"
        try:
            if check is None and _cand.is_file():
                check = _json.loads(_cand.read_text(encoding="utf-8"))
                check["_file"] = str(_cand)
        except Exception:  # noqa: BLE001
            pass
        # 自动修复的派单（2026-11-01）：只有「升版也修不好」的才进这里，
        # 是真正需要人动手的清单，管理员必须看得见。
        _tk = _root / "data" / "repair_tickets.json"
        try:
            if not tickets and _tk.is_file():
                _d = _json.loads(_tk.read_text(encoding="utf-8"))
                if isinstance(_d, list):
                    tickets = _d[-50:]
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "data_dir": str(base), "days": int(days or 30),
            "success": succ, "demand": dem, "check": check,
            "repair_tickets": tickets}


@router.get("/api/admin/platform-usage")
def admin_platform_usage(days: int = 30, refresh: bool = False,
                         request: Request = None) -> dict[str, Any]:
    """三端用量 → 平台全景（2026-11-01）。只读，require_admin。

    数据来自授权中心：三端每次**真正消耗配额**都会上报一条明细（平台 + 时间 +
    结果），这里按平台聚合。分不出端（明细里没有 mac/win/web 字段），所以是
    三端合计 —— 界面文案必须照实写，别写成「网页版用量」。

    隐私：**不下发标题 `t`**，只给计数 / 涉及账号数 / 最近时间。后台需要的是
    「哪个平台被用得最多」，不是用户看了什么。
    """
    require_admin(request)
    try:
        import platform_usage

        return platform_usage.overview(days=days, force=bool(refresh))
    except Exception as exc:  # noqa: BLE001 — 后台查询失败不该 500
        return {"ok": False, "error": type(exc).__name__, "days": int(days or 30),
                "platforms": [],
                "totals": {"events": 0, "accounts": 0, "platforms": 0}}


@router.get("/api/admin/audit")
def admin_audit_log(request: Request = None, limit: int = 200) -> dict[str, Any]:
    """超级管理员：后台写操作审计。

    谁在什么时候调了哪个写接口、返回什么状态码。只记写请求，**不记请求体**
    （里面可能有新密码明文）。旧日志超过 2MB 会滚成 admin_audit.log.1。
    """
    require_admin(request)
    import admin_audit
    rows = admin_audit.tail(limit)
    return {"ok": True, "rows": rows, "total": len(rows)}


@router.get("/api/admin/config")
def admin_config(request: Request = None) -> dict[str, Any]:
    require_admin(request)
    out: dict[str, Any] = {"ok": True, **system_config()}
    # 2026-11-01：把「免费额度」的本机意图 + 云端真值一并给出，前端才能显示
    # 「本机设了 X / 云端生效 X」—— 两者不一致时管理员要能一眼看出来。
    try:
        import membership

        out["free_quota_local"] = membership.load_plan_overrides().get("free_quota") or {}
        out["free_quota_cloud"] = membership.cloud_free_quota_overrides() or {}
        out["cloud_sync"] = {"ready": bool(membership._admin_token())}
        # 前端要按资源键渲染「免费额度」编辑网格；键名只有后端知道，别让前端
        # 自己猜（猜错就会出现「界面上少一项、那一项永远改不了」）。
        # 🔴 必须取**并集**：代码默认表里的键（本地功能）与云端覆盖里的键
        # （云函数）不是同一套，只给其中一套就会漏掉另一半，漏掉的那半改不了。
        _fqc = out.get("free_quota_cloud") or {}
        _keys = set(membership.DAILY_QUOTA_LIMITS) | set(membership.FREE_DAILY_LIMITS)
        _keys |= set((_fqc.get("daily_free_limits") or {}))
        _keys |= set((_fqc.get("daily_member_limits") or {}))
        out["quota_keys"] = sorted(_keys)
        out["free_daily_defaults"] = dict(membership.FREE_DAILY_LIMITS)
        out["member_daily_defaults"] = dict(membership.DAILY_QUOTA_LIMITS)
    except Exception:  # noqa: BLE001 — 附加信息拿不到不影响主配置
        out["cloud_sync"] = {"ready": False}
    return out


@router.post("/api/admin/config/smtp")
def admin_config_smtp(payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    require_super(request)
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        return {"ok": False, "error": "accounts 必须为数组"}
    return save_smtp_accounts(accounts)


@router.post("/api/admin/config/plans")
def admin_config_plans(payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    require_super_backend(request)
    out = dict(save_plan_overrides(payload or {}))
    # 🔴 2026-11-01 改口径：此前这里**不下发**云端，只写本机 plans.json，并诚实地
    #   回一句「请去桌面端改」。但三端的收款估价与放行判定都在授权中心 ⇒ 网页后台
    #   改价「保存成功、线上没变」。现在保存即下发，与桌面 App 后台同权。
    #   仍保留云端回执：`cloud_plans.ok=False` 时前端必须显示「只改了本机」。
    if out.get("ok"):
        try:
            import membership

            out["cloud_plans"] = membership.push_plans_to_cloud()
            if isinstance(payload, dict) and "free_quota" in payload:
                out["cloud_free_quota"] = membership.push_free_quota_to_cloud()
        except Exception as exc:  # noqa: BLE001 — 下发失败不该把保存结果吞掉
            out["cloud_plans"] = {"ok": False, "reason": f"error: {exc}"}
    return out


@router.post("/api/admin/config/free-quota")
def admin_config_free_quota(payload: dict[str, Any] = Body(...),
                            request: Request = None) -> dict[str, Any]:
    """超级管理员：改「免费额度」并下发授权中心（2026-11-01）。

    与套餐价格同一套路：本机 plans.json 覆盖层只作意图/缓存，云端才是三端放行
    判定的真源。`free_quota` 形状 = {cloud_lifetime, mode, daily_free_limits,
    daily_member_limits}；键**不存在** = 不动该项，值为 null = 恢复默认。
    """
    require_super_backend(request)
    fq = (payload or {}).get("free_quota")
    if not isinstance(fq, dict):
        return {"ok": False, "error": "free_quota 必须是对象"}
    out = dict(save_plan_overrides({"free_quota": fq}))
    if out.get("ok"):
        try:
            import membership

            out["cloud_free_quota"] = membership.push_free_quota_to_cloud()
        except Exception as exc:  # noqa: BLE001
            out["cloud_free_quota"] = {"ok": False, "reason": f"error: {exc}"}
    return out


@router.get("/api/admin/users/{user_id}/usage")
def admin_user_usage(user_id: str, request: Request = None) -> dict[str, Any]:
    """超级管理员：查单个账号的完整档案（2026-11-01，网页后台补齐）。

    数据源是**授权中心**（三端账号的唯一真源），不是本机镜像 —— 网页版没有
    本机账号表，代理本机会统统「用户不存在」。返回含：权益视图 / 订单 /
    积分流水 / 人工调账 / 近一年每日用量 / 设备 / 云端资料元数据。
    """
    require_admin(request)
    try:
        import admin_store
        import license_client

        token = admin_store._license_admin_token()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"取管理令牌失败：{exc}"}
    if not token:
        return {"ok": False, "error": "网页服务未配置 VDL_LICENSE_ADMIN_TOKEN"}
    try:
        r = license_client.usage_remote(user_id, token, timeout=12.0)
    except Exception as exc:  # noqa: BLE001 — 中心不可达不该 500
        return {"ok": False, "error": f"授权中心不可达：{exc}"}
    if isinstance(r, dict) and r.get("ok"):
        return {"ok": True, "usage": r}
    return {"ok": False, "error": str((r or {}).get("error") or "云端未找到该账号"),
            "usage": r if isinstance(r, dict) else None}


@router.get("/api/admin/install-stats")
def admin_install_stats(request: Request = None) -> dict[str, Any]:
    """超级管理员：三端安装 / 插件用量聚合（`stat_query`，只读）。

    原来只有桌面 App 后台能看，网页后台看不到「装了多少台」。数据来自授权中心
    的心跳/登录设备记录，按平台聚合。
    """
    require_admin(request)
    try:
        import admin_store
        import license_client

        token = admin_store._license_admin_token()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"取管理令牌失败：{exc}"}
    if not token:
        return {"ok": False, "error": "网页服务未配置 VDL_LICENSE_ADMIN_TOKEN"}
    try:
        r = license_client.stat_query_remote(token, timeout=15.0)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"授权中心不可达：{exc}"}
    return r if isinstance(r, dict) else {"ok": False, "error": "中心返回异常"}


# ── AI 积分成本 / 免费试用策略（2026-10-05 与桌面端拉齐）─────────────────────
# 这两张表都写在 plans.json 覆盖层，改完即时生效、不需要重启服务。
# 网页版与桌面端**同一套语义**：各功能的积分单价由 plans.json 逐项覆盖，
# `free_trial` 控制「账号首次使用免费一次」。


@router.get("/api/admin/ai/credit-costs")
def admin_ai_credit_costs(request: Request = None) -> dict[str, Any]:
    """超级管理员：读取各 AI 功能的积分单价（**不含任何凭据/密钥**）。

    与桌面端同端点、同返回结构，前端两侧可以复用同一份渲染代码。
    """
    require_admin(request)
    from membership import AI_CREDIT_COSTS, credit_cost_table, free_trial_policy
    return {
        "ok": True,
        "costs": credit_cost_table(),
        "registered": len(AI_CREDIT_COSTS),
        # 免费用户「首次体验」策略（2026-10-05 晚定档：账号首次使用免费一次）
        "free_trial": free_trial_policy(),
        "trial_modes": ["once", "per_op", "off"],
        "policy": {
            "scope": "云端算力 + 服务端算力都计费（2026-10-05 定档）",
            "granularity": "按「次」定价：一次操作内发生多次模型调用只扣一份，重试成本含在单价里",
            # 🔴 网页版特有的成本性质，必须让管理员知道：
            "local_note": "网页版的去水印（LaMa）与字幕提取跑在**服务端 ECS**上，"
                          "占的是服务器 CPU，不像桌面端那样「占用的是用户自己的机器」。",
            "trial": "只要是首次使用的账号，其第一次使用 AI 功能免费一次；"
                     "已经在 AI 会员有效期内或有足够积分的用户不消耗名额（先花自己的积分）。",
        },
    }


@router.post("/api/admin/ai/credit-costs")
def admin_ai_set_credit_costs(
    request: Request = None,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    """超级管理员：调整各功能的积分单价 / 免费试用策略。

    payload: `{"costs": {"commentary_llm": 60, ...}, "reset": ["op", ...],
               "free_trial": {"enabled": true, "mode": "once"|"per_op"|"off", ...}}`
      · 只提交要改的项，其余保持不变（不覆盖别人的改动）。
      · 传 `0` = 该功能免费（显式免费，不再依赖「表外默认 0」）。
      · `reset` 列出要恢复代码默认价的 op。
      · **op 必须已在 `AI_CREDIT_COSTS` 中登记**，未知 op 直接报错 ——
        防止打错字导致配置无效（且避免把 typo 写进 plans.json 变成死配置）。
    """
    require_super(request)
    from fastapi import HTTPException
    from membership import (AI_CREDIT_COSTS, credit_cost_table, free_trial_policy,
                            load_plan_overrides, save_plan_overrides)

    costs_in = payload.get("costs")
    if costs_in is not None and not isinstance(costs_in, dict):
        raise HTTPException(status_code=400, detail="costs 必须是对象 {op: 积分}")

    current = dict(load_plan_overrides().get("credit_costs") or {})
    unknown = [k for k in (costs_in or {}) if k not in AI_CREDIT_COSTS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"未登记的计费项：{unknown}（可用项：{sorted(AI_CREDIT_COSTS)}）")

    for op, v in (costs_in or {}).items():
        try:
            val = int(v)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{op} 的积分必须是整数，收到 {v!r}")
        if val < 0:
            raise HTTPException(status_code=400, detail=f"{op} 的积分不能为负")
        current[op] = val

    reset = payload.get("reset") or []
    if isinstance(reset, list):
        for op in reset:
            if op in AI_CREDIT_COSTS:
                # 🔴 用 `None` 标记删除，**不能**在 Python 侧先 pop 掉再提交：
                #   `save_plan_overrides` 的合并语义是「表内逐条合并、条目值为 null
                #   视为删除」。若提交时少了这个键，旧值会原样留在 plans.json ——
                #   表现为「点了恢复默认，价没变」（桌面端实测踩到）。
                current[op] = None

    trial_in = payload.get("free_trial")
    if trial_in is not None and not isinstance(trial_in, dict):
        raise HTTPException(status_code=400, detail="free_trial 必须是对象")
    if costs_in is None and not reset and not trial_in:
        raise HTTPException(status_code=400, detail="没有要保存的改动")

    # 一次落盘两张表（同一个 plans.json），避免中间态被别人读到
    patch: dict[str, Any] = {"credit_costs": current}
    if isinstance(trial_in, dict) and trial_in:
        patch["free_trial"] = _validate_free_trial(trial_in, AI_CREDIT_COSTS)
    save_plan_overrides(patch)
    return {"ok": True, "costs": credit_cost_table(),
            "free_trial": free_trial_policy()}


def _validate_free_trial(trial_in: dict[str, Any], known_ops: dict) -> dict[str, Any]:
    """校验并归一 `free_trial` 载荷：只留认识的键，非法值直接 400。

    🔴 只认识 `DEFAULT_FREE_TRIAL_POLICY` 里的键 —— 后台把 `per_op` 拼错成
    `per-op` 之类，写进 plans.json 会造成**整条策略静默失效**（读表时收敛回默认值），
    管理员看着「明明配了」却不起效，属于最难查的一类事故，所以宁可当场报错。

    ⚠️ 恢复默认必须传 None（不能用 Python 侧 pop）：合并语义见上方 `reset` 注释。
    """
    from fastapi import HTTPException as _HE
    from membership import DEFAULT_FREE_TRIAL_POLICY, _TRIAL_MODES
    out: dict[str, Any] = {}
    unknown = [k for k in trial_in if k not in DEFAULT_FREE_TRIAL_POLICY]
    if unknown:
        raise _HE(status_code=400,
                  detail=f"未知的试用策略字段：{unknown}（可用：{sorted(DEFAULT_FREE_TRIAL_POLICY)}）")
    for k, v in trial_in.items():
        if v is None:
            out[k] = None            # 显式删除 ⇒ 回退代码默认值
            continue
        if k in ("enabled", "members_too"):
            if not isinstance(v, bool):
                raise _HE(status_code=400, detail=f"{k} 必须是 true/false")
            out[k] = v
        elif k == "mode":
            mode = str(v).strip().lower()
            if mode not in _TRIAL_MODES:
                raise _HE(status_code=400,
                          detail=f"未知的试用口径：{mode}（可选：{list(_TRIAL_MODES)}）")
            out[k] = mode
        elif k == "exclude":
            if not isinstance(v, (list, tuple)):
                raise _HE(status_code=400, detail="exclude 必须是数组 [op, ...]")
            bad = [x for x in v if str(x) not in known_ops]
            if bad:
                raise _HE(status_code=400,
                          detail=f"exclude 里有未登记的计费项：{bad}（可用项：{sorted(known_ops)}）")
            out[k] = [str(x) for x in v]
        elif k == "max_cost":
            try:
                mc = int(v)
            except (TypeError, ValueError):
                raise _HE(status_code=400, detail=f"max_cost 必须是整数，收到 {v!r}")
            if mc < 0:
                raise _HE(status_code=400, detail="max_cost 不能为负")
            out[k] = mc
        else:  # pragma: no cover — 上面已经过一遍键名白名单
            out[k] = v
    return out
