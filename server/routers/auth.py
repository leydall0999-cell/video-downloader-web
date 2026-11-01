"""server/routers/auth.py — VDL 账号（/api/auth/*，A1）。

注册/登录/查询当前用户/本地重置密码（验证码流程）。token 为 HMAC 签名的无状态 Bearer，
前端存于 localStorage('vdl_auth_token')，后续请求经 Authorization: Bearer <token> 携带。

🔴 2026-09-26「打通 web 与 App 用户数据」后，**账号权威是授权中心（ECS 8902）**：
注册/登录先打云端（`server/cloud_link.py`），成功后在**本机 auth_store 落一份同号同密码
的镜像** —— 本机仍是 bearer 签发方、功能门禁与离线登录的兜底；会员/积分由云端
`authority` 快照覆盖本机 `memberships/{uid}.json`。云端不可达时退回纯本机账号（fail-open），
只在响应里带 `cloud_notice` 如实说明，不让用户白屏。

浏览器设备号：cookie `vdl_dev`（服务端 uuid，首次登录/注册响应 Set-Cookie）→ 作为
授权中心的 device.fp，网页版因此占一个设备位（与 App 的 2 台设备额度同一套语义）。

找回密码流程：先 POST /api/auth/reset-code 获取验证码（dev 模式**仅本机**展示 / smtp 真实投递），
再 POST /api/auth/reset 携带 code 完成改密；改完密码会用上一次登录留下的云端 token
把新密码同步到授权中心（否则两端密码分叉，换端登录会报「密码不正确」）。

🔴 dev 模式下验证码**只回传给本机调用方**（桌面 App 的界面走 127.0.0.1，UX 不变）。公网部署
若因缺少 `smtp.json` 落到 dev，把验证码写进响应体＝把任意账号的改密权交给调用方（账号接管），
故按调用方地址卡死；网页版要能重置密码就得配 `smtp.json`（模式自动切 smtp），而不是靠回传。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from fastapi import APIRouter, Body, Request, Response

router = APIRouter()

from stats import record_event
import cloud_link


# 头像上传/读取相关依赖（懒加载，避免模块顶层循环 import）
def _avatar_helpers():
    from auth_store import set_user_avatar, user_avatar_url, _avatar_path
    return set_user_avatar, user_avatar_url, _avatar_path

# 邮箱：标准格式校验，支持 QQ 邮箱（@qq.com/@foxmail.com）、Google 邮箱
# （@gmail.com/@googlemail.com）及其常见变体（用户名含 . + % - 等）。
# 不限制特定域名，所有合法邮箱均可通过。
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
# 手机号：支持中国大陆 11 位（1[3-9]...）或 E.164 国际格式（+ 开头）
_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")


def _is_valid_identifier(ident: str) -> bool:
    """账号为合法邮箱（支持 QQ/Google 等）或有效手机号（不强求 + 开头）。"""
    if "@" in ident:
        return bool(_EMAIL_RE.match(ident))
    return bool(_PHONE_RE.match(ident) or _E164_RE.match(ident))


def _is_valid_email(ident: str) -> bool:
    """仅邮箱。2026-11-01 起：注册与找回密码只收邮箱（无短信通道，手机号收不到码）。

    登录仍走 `_is_valid_identifier` —— 早前用手机号注册的老账号得能登进来，
    否则用户手里有密码却进不去。
    """
    return "@" in ident and bool(_EMAIL_RE.match(ident))


# 本机回环地址：桌面 App 的前端固定访问 127.0.0.1:8321，故其调用方恒为其中之一。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_loopback(request: Request) -> bool:
    """判断调用方是否来自**本机直连**。

    安全审计 2026-10-13（F-06）：原实现只比 `request.client.host`，而 uvicorn 默认开
    `--proxy-headers` —— `scope["client"]` 已被 X-Forwarded-For 重写，客户端自填 XFF
    即可自称本机。本函数正 gate 着「dev 模式回传密码重置验证码」，一旦被骗过等于
    **任意账号接管**，故判据必须不依赖反代语义。
    现统一走 `app._is_local_direct`：socket 对端环回 **且** 未携带任何代理头。
    """
    try:
        import app as _app
        return _app._is_local_direct(request)
    except Exception:  # noqa: BLE001 — 取不到就当公网，宁可少回传
        return False


def _auth_user_id(request: Request) -> Optional[str]:
    return request.headers.get("Authorization", "")


def _require_user(request: Request) -> Optional[str]:
    """解析已登录用户 user_id（未登录返回 None）。"""
    import app as _app
    return _app.get_current_user_id(request)


def _bearer(request: Request) -> Optional[str]:
    from auth_store import token_from_header
    return token_from_header(request.headers.get("Authorization"))


# ── 云端账号联动的小工具（2026-09-26：网页版账号接授权中心）─────────────────
def _cloud_device(request: Request, response: Response) -> tuple[str, str]:
    """取浏览器设备号：(fp, 设备名)。首次访问签发 cookie `vdl_dev`。

    授权中心 login/register 强制要求 device.fp（MAX_DEVICES=2 防共享），网页版用它
    占一个设备位。cookie 缺失时当场签发并在响应里 Set-Cookie（三年有效）。
    """
    fp = cloud_link.ensure_fp(request)
    if not fp:
        fp = cloud_link.issue_fp()
    try:
        response.set_cookie(cloud_link.COOKIE_NAME, fp,
                            max_age=cloud_link.COOKIE_MAX_AGE,
                            httponly=True, samesite="lax")
    except Exception:  # noqa: BLE001 — 设不上 cookie 也继续（本次请求仍带 fp 打云端）
        pass
    return fp, cloud_link.device_name(request)


def _cloud_login_or_register(request: Request, response: Response, ident: str,
                             pw: str, is_reg: bool) -> tuple[Optional[dict], str, str]:
    """打云端 register/login。返回 (结果或 None, fp, 设备名)。"""
    if not cloud_link.link_enabled():
        return None, "", ""
    fp, name = _cloud_device(request, response)
    res = (cloud_link.try_register(ident, pw, fp, name) if is_reg
           else cloud_link.try_login(ident, pw, fp, name))
    return res, fp, name


def _role_for_user(uid: str, is_admin: bool) -> str:
    """后台角色标签：super（唯一超管）/ admin（管理员·只读）/ ""（普通用户）。

    2026-10-22 权限拆两级（与桌面端 app-dev 同口径）。
    🔴 网页版特有两难：is_admin 的真源可能是**云端**（登录时同步过来），而超管
    判定 `auth_store.super_admin_identifier()` 只看本机。两者不一致时按最小权限：
    有管理身份但本机判不出 super ⇒ 一律标 admin（只读），绝不因判定缺口放成超管。
    """
    if not is_admin:
        return ""
    try:
        from auth_store import user_role
        return user_role(uid) or "admin"
    except Exception:  # noqa: BLE001 — 判定异常不该让登录 500
        return "admin"


def _finish_login(request: Request, uid: str, ident: str, cloud: Optional[dict],
                  fp: str, name: str, *, registered: bool = False) -> dict[str, Any]:
    """登录收尾：签发本机 bearer + 落地云端登录态与权益快照。"""
    from auth_store import (issue_token, ensure_superusers, user_is_admin,
                            set_user_admin)
    # 安全审计 2026-10-13（F-03）：仅在「本机直连」登录时才允许无名单兜底提权，
    # 公网首注册不得自动成为超管（见 auth_store.ensure_superusers）。
    ensure_superusers(_is_loopback(request))
    token = issue_token(uid)
    notice = ""
    synced = False
    cloud_is_admin: Optional[bool] = None
    if cloud and cloud.get("ok"):
        synced = True
        try:
            import user_membership
            store = user_membership.get_user_store(uid)
            cloud_link.apply_login(store, cloud.get("email") or ident,
                                   cloud.get("token") or "",
                                   cloud.get("account") or {}, fp, name)
            # 2026-10-16：is_admin 上云 —— 云端快照是权威，落到本机镜像，
            # 超管提权/降级因此全端生效；断网时本机标记仍可兜底门禁。
            cloud_is_admin = bool((cloud.get("account") or {}).get("is_admin"))
            if cloud_is_admin != bool(user_is_admin(uid)):
                set_user_admin(uid, cloud_is_admin)
        except Exception as e:  # noqa: BLE001 — 权益落地失败不影响登录本身
            logging.getLogger("vdl.auth").warning("云端权益落地失败: %s", e)
            notice = "已登录，但会员权益同步失败，可稍后在个人中心刷新"
        if cloud.get("evicted"):
            ev = cloud.get("evicted") or []
            names = "、".join(str(x.get("name") or "未知设备") for x in ev[:2])
            notice = f"已占满 2 台设备额度，自动退出了最久未使用的设备（{names}）"
    elif cloud is not None:
        notice = "授权中心暂时不可达，本次按本机账号登录；会员权益可能不是最新"
    record_event("register" if registered else "login", {"identifier": ident})
    is_admin = cloud_is_admin if cloud_is_admin is not None else bool(user_is_admin(uid))
    return {"ok": True, "token": token, "user_id": uid, "identifier": ident,
            "is_admin": bool(is_admin),
            "role": _role_for_user(uid, bool(is_admin)),
            "registered": bool(registered),
            "cloud_synced": synced,
            "cloud_notice": notice}


@router.post("/api/auth/register")
def auth_register(request: Request, response: Response,
                  payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """注册。账号权威在授权中心 —— 云端注册成功后在本机落镜像账号。

    云端说「已存在」→ 用同一密码试云端登录（用户多半以前在 App 注册过）；
    云端不可达 → 退回纯本机注册（带 cloud_notice，账号暂不跨端）。
    """
    ident = str(payload.get("identifier") or "").strip().lower()
    pw = str(payload.get("password") or "")
    if not ident:
        return {"ok": False, "error": "请输入邮箱"}
    if "@" not in ident:
        # 注册只走邮箱：没有短信通道，手机号账号既收不到验证码也无法自助找回密码。
        return {"ok": False, "error": "手机号注册已关闭，请改用邮箱注册（此前注册的手机号账号仍可正常登录）"}
    if not _is_valid_email(ident):
        return {"ok": False, "error": "邮箱格式不正确，请检查后重试"}
    if len(pw) < 6:
        return {"ok": False, "error": "密码至少 6 位"}

    from auth_store import create_user, user_is_admin
    cloud, fp, name = _cloud_login_or_register(request, response, ident, pw, True)

    if cloud and cloud.get("ok"):
        uid = cloud_link.mirror_local_account(ident, pw)
        if not uid:
            return {"ok": False, "error": "本机账号写入失败，请重试"}
        return _finish_login(request, uid, ident, cloud, fp, name, registered=True)

    if cloud is not None and str(cloud.get("code")) == "EXISTS":
        # 云端已有该账号 → 用同一密码登录，成功后本机落镜像（老用户不该被卡住）
        relogin = cloud_link.try_login(ident, pw, fp, name)
        if relogin.get("ok"):
            uid = cloud_link.mirror_local_account(ident, pw)
            if uid:
                return _finish_login(request, uid, ident, relogin, fp, name)
        return {"ok": False, "error": "该账号已注册，请直接登录"}

    if cloud is not None and str(cloud.get("code")) in (
            "CLOUD_UNREACHABLE", "NO_SECRET", "NO_DEVICE"):
        # 云端不可用 → 本机注册兜底（离线可用；跨端要等云端恢复后首次登录自愈）
        uid = create_user(ident, pw)
        if not uid:
            return {"ok": False, "error": "该账号已注册，请直接登录"}
        res = _finish_login(request, uid, ident, None, fp, name, registered=True)
        res["cloud_notice"] = "授权中心暂时不可达，账号已在本机创建；联网后登录会自动并入账号"
        res["cloud_synced"] = False
        return res

    if cloud is not None:
        return {"ok": False, "error": cloud.get("error") or "注册失败",
                "code": cloud.get("code") or "REJECTED"}

    # 云端联动被显式关闭（VDL_CLOUD_LINK=0）→ 纯本机注册（排障/离线测试用）
    uid = create_user(ident, pw)
    if not uid:
        return {"ok": False, "error": "该账号已注册，请直接登录"}
    res = _finish_login(request, uid, ident, None, "", "", registered=True)
    res["cloud_synced"] = False
    res["cloud_notice"] = "本机模式：账号未同步到云端（VDL_CLOUD_LINK=0）"
    return res


@router.post("/api/auth/login")
def auth_login(request: Request, response: Response,
               payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """登录。云端优先（账号权威）→ 本机镜像兜底（离线也能登）。"""
    ident = str(payload.get("identifier") or "").strip().lower()
    pw = str(payload.get("password") or "")
    if not ident or not pw:
        return {"ok": False, "error": "请输入账号和密码"}

    from auth_store import authenticate
    cloud, fp, name = _cloud_login_or_register(request, response, ident, pw, False)

    uid = authenticate(ident, pw)                    # 本机镜像 / 离线兜底
    if not uid and cloud and cloud.get("ok"):
        # 云端认了但本机没有这个号（如 App 注册、或网页版首次登录）→ 落镜像
        uid = cloud_link.mirror_local_account(ident, pw)
    if not uid:
        # 公网错误文案保持模糊（防账号枚举）；只有封禁这类必须告知的才照实回
        code = str((cloud or {}).get("code") or "")
        if code == "ACCOUNT_BANNED":
            return {"ok": False, "error": (cloud or {}).get("error") or "账号已被停用",
                    "code": "ACCOUNT_BANNED"}
        if code == "CLOUD_UNREACHABLE":
            return {"ok": False,
                    "error": "账号或密码错误（授权中心暂时不可达，可稍后重试）",
                    "code": "BAD_CREDENTIALS"}
        return {"ok": False, "error": "账号或密码错误", "code": "BAD_CREDENTIALS"}

    return _finish_login(request, uid, ident, cloud, fp, name)


@router.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    uid = _bearer(request)
    if not uid:
        return {"ok": False, "error": "未登录", "code": "NO_AUTH"}
    from auth_store import user_identifier, user_is_admin
    out: dict[str, Any] = {"ok": True, "user_id": uid,
                           "identifier": user_identifier(uid) or uid,
                           "is_admin": bool(user_is_admin(uid)),
                           "role": _role_for_user(uid, bool(user_is_admin(uid)))}
    # 云端账号快照（设备/被挤/封禁状态）——前端据此提示「已在其他设备登录」
    try:
        import user_membership
        out["account"] = user_membership.get_user_store(uid).account_view()
        out["cloud_linked"] = cloud_link.link_enabled()
    except Exception:  # noqa: BLE001 — 老数据/异常不影响登录态查询
        pass
    return out


@router.get("/api/account/profile")
def account_profile(request: Request, usage_period: str = "today") -> dict[str, Any]:
    """个人中心聚合数据：身份信息 + 会员有效时间 + 使用记录 + 购买记录 + 积分消耗记录。

    usage_period: today | 3d | 7d | month
    """
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录", "code": "NO_AUTH"}
    if usage_period not in ("today", "3d", "7d", "month"):
        usage_period = "today"
    import user_membership
    from auth_store import user_identifier, user_is_admin, _load_users
    ident = user_identifier(uid) or uid
    store = user_membership.get_user_store(uid)
    # 个人中心是看会员/积分的正主 → 先与云端权威对齐（节流 60s）
    try:
        cloud_link.refresh_authority(store)
    except Exception:  # noqa: BLE001 — 云端异常不影响个人中心其余内容
        pass
    st = store.status()
    raw_hist = (store._state.get("meta", {}) or {}).get("history", [])
    purchases: list[dict] = []
    credits: list[dict] = []
    for h in raw_hist:
        t = h.get("type")
        if t == "spend" or t == "admin_adjust":
            credits.append(dict(h))
        else:  # None / "activate" → 购买/激活
            purchases.append(dict(h))
    purchases.sort(key=lambda x: x.get("at", 0), reverse=True)
    credits.sort(key=lambda x: x.get("at", 0), reverse=True)
    # 积分流水补充变动值与变动后余额（从当前余额倒推，保证与现状一致）
    balance = int(st.get("credits_total", 0))
    for h in credits:
        if h.get("type") == "spend":
            delta = -int(h.get("amount", 0))
        elif h.get("type") == "admin_adjust":
            code = str(h.get("code", ""))
            try:
                delta = int(code.split(":", 1)[1]) if ":" in code else 0
            except ValueError:
                delta = 0
        else:
            delta = 0
        h["delta"] = delta
        h.setdefault("balance_after", balance)
        balance -= delta
    # 注册时间
    created_at = None
    data = _load_users()
    u = next((x for x in data.get("users", []) if x.get("user_id") == uid), None)
    if u:
        created_at = u.get("created_at")
    from auth_store import user_avatar_url
    from membership import feature_usage_status
    return {
        "ok": True,
        "user_id": uid,
        "identifier": ident,
        "is_admin": bool(user_is_admin(uid)),
        "role": _role_for_user(uid, bool(user_is_admin(uid))),
        "created_at": created_at,
        "avatar_url": user_avatar_url(uid),
        "membership": st,
        "purchases": purchases,
        "credit_history": credits,
        "usage": st.get("daily_usage", {}),
        "usage_period": usage_period,
        "usage_features": feature_usage_status(store, period=usage_period),
    }


@router.post("/api/account/avatar")
def account_avatar_upload(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """上传/更新当前用户头像。payload 支持 base64 data URL 或纯 base64 字符串。"""
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录", "code": "NO_AUTH"}
    raw = payload.get("image") or ""
    if not raw:
        return {"ok": False, "error": "请选择图片"}
    import base64
    data = raw
    if "," in raw:
        data = raw.split(",", 1)[1]
    try:
        image_bytes = base64.b64decode(data)
    except Exception:
        return {"ok": False, "error": "图片解码失败，请重新选择"}
    set_user_avatar, _, _ = _avatar_helpers()
    return set_user_avatar(uid, image_bytes)


@router.get("/api/account/avatar/{user_id}")
def account_avatar_get(request: Request, user_id: str) -> Any:
    """读取指定用户头像。允许任何人读取（头像为公开资源），但文件必须存在。"""
    import base64
    import mimetypes
    from fastapi.responses import FileResponse
    _, _, _avatar_path = _avatar_helpers()
    p = _avatar_path(user_id)
    if not p.exists():
        return {"ok": False, "error": "未设置头像", "code": "NOT_FOUND"}
    ctype = mimetypes.guess_type(str(p))[0] or "image/png"
    return FileResponse(str(p), media_type=ctype)


@router.post("/api/auth/change-password")
def auth_change_password(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """本人凭当前密码修改密码（需登录）。"""
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录", "code": "NO_AUTH"}
    cur = str(payload.get("current_password") or "")
    new = str(payload.get("new_password") or "")
    if not cur or not new:
        return {"ok": False, "error": "请输入当前密码和新密码"}
    if len(new) < 6:
        return {"ok": False, "error": "新密码至少 6 位"}
    from auth_store import user_identifier, authenticate, reset_password
    ident = user_identifier(uid)
    if not ident:
        return {"ok": False, "error": "账号不存在"}
    if not authenticate(ident, cur):
        return {"ok": False, "error": "当前密码错误"}
    reset_password(ident, new)
    record_event("change_pw", {"identifier": ident})
    # 新密码同步到授权中心（否则两端密码分叉：网页版改完，App 那边还是旧密码）
    synced = True
    try:
        import user_membership
        sess = user_membership.get_user_store(uid).cloud_session()
        r = cloud_link.push_password(ident, new, old_password=cur,
                                     token=str(sess.get("token") or ""))
        synced = bool(r.get("ok")) and bool(r.get("synced", True))
    except Exception as e:  # noqa: BLE001 — fail-open：本机已改成功，不能回滚
        logging.getLogger("vdl.auth").warning("改密同步云端失败: %s", e)
        synced = False
    out: dict[str, Any] = {"ok": True, "cloud_synced": synced}
    if not synced:
        out["notice"] = "本机密码已修改；云端账号暂时没同步上，下次登录会用新密码自动补齐"
    return out


@router.post("/api/account/deactivate")
def account_deactivate(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """注销当前登录账号（软删除），注销后不可再用该邮箱/手机号登录或重新注册。"""
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录", "code": "NO_AUTH"}
    from auth_store import user_identifier, deactivate_user
    ident = user_identifier(uid)
    result = deactivate_user(uid)
    if result.get("ok"):
        record_event("deactivate", {"identifier": ident})
    return result


@router.post("/api/auth/reset-code")
def auth_reset_code(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    ident = str(payload.get("identifier") or "").strip().lower()
    if not ident:
        return {"ok": False, "error": "请输入注册邮箱"}
    if not _is_valid_email(ident):
        if "@" in ident:
            return {"ok": False, "error": "邮箱格式不正确，请检查后重试"}
        return {"ok": False, "error": "请输入注册邮箱（手机号账号请联系客服重置）"}
    from auth_store import (
        generate_reset_code,
        reset_code_cooldown_ok,
        deliver_reset_code,
        smtp_quota_ok,
        _send_mode,
    )
    # 手机号账号没有短信通道（deliver_reset_code 的 sms 分支仍是 NotImplemented），
    # 硬推给 SMTP 会拿 "15014313254" 当收件人去发信 → QQ 必定拒收，用户最后看到
    # 一句「请检查邮件服务配置」的内部错误。上面的 "@" 判断已提前拦掉。
    if not reset_code_cooldown_ok(ident):
        return {"ok": False, "error": "验证码已发送，请稍后再试（60 秒冷却）"}
    quota_ok, quota_msg = smtp_quota_ok()
    if not quota_ok:
        return {"ok": False, "error": quota_msg}
    code = generate_reset_code(ident)
    if code:
        try:
            deliver_reset_code(ident, code)
        except Exception as e:  # noqa: BLE001
            logging.getLogger("vdl.auth").error("投递验证码失败: %s", e)
            return {"ok": False, "error": "验证码发送失败，请检查邮件服务配置"}
    # 无论账号是否存在都返回 ok（防账号枚举）。
    # 🔴 dev 模式**只在本机**附带 dev_code（桌面 App 本地调试用）。公网部署一旦因缺少
    #    smtp.json 落到 dev，回传验证码＝调用方可直接改任意账号密码（账号接管）。
    dev_code = code if (_send_mode() == "dev" and _is_loopback(request)) else None
    return {"ok": True, "dev_code": dev_code, "expires_in": 300}


@router.post("/api/auth/reset")
def auth_reset(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    ident = str(payload.get("identifier") or "").strip().lower()
    pw = str(payload.get("password") or "")
    code = str(payload.get("code") or "").strip()
    if not ident:
        return {"ok": False, "error": "请输入注册邮箱"}
    if not _is_valid_email(ident):
        return {"ok": False, "error": "邮箱格式不正确，请检查后重试"}
    if not code:
        return {"ok": False, "error": "请输入验证码"}
    if len(pw) < 6:
        return {"ok": False, "error": "新密码至少 6 位"}
    from auth_store import verify_reset_code, reset_password
    if not verify_reset_code(ident, code):
        return {"ok": False, "error": "验证码错误或已过期"}
    if not reset_password(ident, pw):
        return {"ok": False, "error": "该账号不存在，无法重置"}
    record_event("reset_pw", {"identifier": ident})
    # 重置后的新密码同步到授权中心：此时已无原密码，只能靠该账号上次登录留下的
    # 云端 token 自证身份（见 license_server.password_impl 的鉴权二选一）。
    synced = True
    try:
        from auth_store import authenticate
        uid = authenticate(ident, pw)
        if uid:
            import user_membership
            sess = user_membership.get_user_store(uid).cloud_session()
            r = cloud_link.push_password(ident, pw, token=str(sess.get("token") or ""))
            synced = bool(r.get("ok")) and bool(r.get("synced", True))
        else:
            synced = False
    except Exception as e:  # noqa: BLE001 — fail-open
        logging.getLogger("vdl.auth").warning("重置密码同步云端失败: %s", e)
        synced = False
    out: dict[str, Any] = {"ok": True, "cloud_synced": synced}
    if not synced:
        out["notice"] = "密码已重置；云端账号未同步（需先在网页版或 App 登录一次即可自动对齐）"
    return out
