"""守卫：全端账号统一（2026-10-16「每端数据统一到一起」）。

背景：用户要求所有端（Mac/Windows 桌面 + 网页版 cn/hk）的用户数据统一到云端，
超级管理员后台可见全部。落地形态：
  · 授权中心（deploy/license_server.py）是账号/权益/设备/is_admin 的唯一真源；
  · is_admin 上云：登录/心跳快照带出，set-admin 管理端点可写；
  · 存量搬迁 import-account：本机账号表记录原样搬入（盐+哈希格式一致，密码无感）；
  · 超管改密 admin_token 通道（管理员不知道原密码）；
  · 后台 list_users 以云端为主、本机兜底合并；写操作双写云端。

本守卫钉住：
  1) 云端快照/列表带 is_admin；set-admin 写了就生效（登录快照可见）；
  2) import-account 搬进来的账号能直接用原密码登录（哈希可移植）；
  3) import-account 不覆盖已存在账号（云端权威）；
  4) password_impl 的 admin_token 通道：持令牌可改、错令牌拒；
  5) list_users 合并：云端行带端维度、本机独有账号不丢、重叠去重；
  6) 路由双写 helper：标识解析 + 云端失败只出 notice 不阻断。
"""
import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "server"))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def _load_license_server(tmp: Path):
    """从 deploy/ 加载授权中心模块（零依赖 stdlib HTTP 服务，impl 可直接调）。"""
    p = REPO / "deploy" / "license_server.py"
    if not p.exists():
        return None
    spec = importlib.util.spec_from_file_location("vdl_license_server_test", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    # 测试隔离：数据/事件流水/密钥全部指到临时目录，绝不碰真库
    m.DATA_PATH = tmp / "cards.json"
    m.EVENT_LOG_PATH = tmp / "events.jsonl"
    m.SECRET = "test-secret"
    m.ADMIN_TOKEN = "test-admin-token"
    return m


def test_cloud_is_admin_and_import(tmp: Path) -> None:
    print("\n[A] is_admin 上云 + 存量搬迁（密码无感）")
    m = _load_license_server(tmp)
    if m is None:
        print("  ⏭ deploy/license_server.py 不在本树，跳过")
        return
    st: dict = {}
    now = time.time()

    # 注册即登录：快照应带 is_admin（默认 False）
    out = m.register_impl(st, "realtester@qq.com", "pass1234", now,
                          m.SECRET, {"fp": "fp-a", "name": "Mac"})
    check("注册成功", out.get("ok") is True, str(out))
    check("快照带 is_admin=False", (out.get("account") or {}).get("is_admin") is False)

    # set-admin 写了，登录快照立即可见
    r = m.set_admin_impl(st, "realtester@qq.com", True, now)
    check("set-admin 返回 ok", r.get("ok") is True and r.get("is_admin") is True)
    out2 = m.login_impl(st, "realtester@qq.com", "pass1234", now, m.SECRET,
                        {"fp": "fp-a", "name": "Mac"})
    check("提权后登录快照 is_admin=True",
          (out2.get("account") or {}).get("is_admin") is True)

    # 存量搬迁：本机表的盐+哈希原样搬入，原密码直接能登
    salt, pw_hash = m.hash_password("localpw999")
    imp = m.import_account_impl(st, "migrated@139.com", salt, pw_hash, now,
                                created_at=now - 86400, is_admin=False)
    check("搬迁创建成功", imp.get("ok") is True and imp.get("created") is True)
    out3 = m.login_impl(st, "migrated@139.com", "localpw999", now, m.SECRET,
                        {"fp": "fp-b", "name": "Win"})
    check("搬迁账号原密码可登录（哈希可移植）", out3.get("ok") is True, str(out3))
    check("搬迁保留 created_at",
          abs(float(st["users"]["migrated@139.com"]["created_at"]) - (now - 86400)) < 2)

    # 不覆盖已存在账号（云端权威）：改盐哈希后再 import，密码不应变
    salt2, pw_hash2 = m.hash_password("hacked-pw")
    imp2 = m.import_account_impl(st, "realtester@qq.com", salt2, pw_hash2, now)
    check("已存在账号不覆盖", imp2.get("created") is False)
    try:
        m.login_impl(st, "realtester@qq.com", "hacked-pw", now, m.SECRET,
                     {"fp": "fp-a", "name": "Mac"})
        ok = False
    except m.ApiError:
        ok = True
    check("覆盖尝试后原密码仍有效", ok)

    # users_impl（后台列表）带 is_admin/created_at
    lst = m.users_impl(st)
    row = next(u for u in lst["users"] if u["email"] == "realtester@qq.com")
    check("列表带 is_admin=True", row.get("is_admin") is True)
    check("列表带 created_at", float(row.get("created_at") or 0) > 0)

    # 设备画像白名单含 web（网页端统计）
    meta = m._clean_device_meta({"platform": "web", "app_version": "1.0.69"})
    check("web 平台被接受", meta.get("platform") == "web", str(meta))

    # 全量搬迁：权益态 + 设备画像一并搬入（用户「真实账号的所有数据也得导入」）
    salt3, pw_hash3 = m.hash_password("fullpw123")
    imp3 = m.import_account_impl(
        st, "full@139.com", salt3, pw_hash3, now, created_at=now - 7200,
        is_admin=False,
        authority={"member_until_dl": now + 86400 * 30, "member_until_ai": 0,
                   "ai_grant_total": 100, "ai_spent_total": 40,
                   "perm_credits": 7,
                   "daily": {"date": "2026-10-16",
                             "counts": {"download": 3, "cloud": 1}}},
        devices=[{"fp": "web-abc", "name": "网页版 · Chrome",
                  "last_seen": now - 60}])
    check("全量搬迁创建成功", imp3.get("ok") is True and imp3.get("created") is True)
    rec = st["users"]["full@139.com"]
    check("权益 member_until_dl 搬迁",
          abs(float(rec.get("member_until_dl") or 0) - (now + 86400 * 30)) < 2)
    check("权益 ai 积分搬迁",
          int(rec.get("ai_grant_total") or 0) == 100
          and int(rec.get("ai_spent_total") or 0) == 40)
    check("权益永久积分搬迁", int(rec.get("perm_credits") or 0) == 7)
    check("每日用量搬迁",
          (rec.get("daily") or {}).get("date") == "2026-10-16"
          and (rec.get("daily") or {}).get("counts", {}).get("download") == 3)
    check("设备画像搬迁",
          len(rec.get("devices") or []) == 1
          and (rec["devices"][0].get("fp")) == "web-abc")
    out4 = m.login_impl(st, "full@139.com", "fullpw123", now, m.SECRET,
                        {"fp": "web-abc", "name": "网页版 · Chrome"})
    check("全量搬迁账号原密码可登录", out4.get("ok") is True, str(out4))
    snap = (out4.get("account") or {}).get("authority") or {}
    check("登录快照带出搬迁的权益",
          abs(float(snap.get("member_until_dl") or 0) - (now + 86400 * 30)) < 2
          and int(snap.get("perm_credits") or 0) == 7)

    # 删除账号（2026-10-16 历史测试号清理）：真删 + 幂等 + 删后不可登录
    check("删除已存在账号返回 deleted=True",
          m.delete_user_impl(st, "full@139.com", now).get("deleted") is True)
    check("删除后账号不在库中", "full@139.com" not in st["users"])
    check("删除不存在的账号幂等（deleted=False 且不报错）",
          m.delete_user_impl(st, "nobody@139.com", now).get("deleted") is False)
    try:
        _del_login = m.login_impl(st, "full@139.com", "fullpw123", now, m.SECRET,
                                  {"fp": "web-abc", "name": "网页版 · Chrome"})
        _del_login_ok = bool(_del_login.get("ok"))
    except Exception:
        _del_login_ok = False
    check("删除后原密码无法登录（账号已不存在）", _del_login_ok is False)


def test_admin_password_channel(tmp: Path) -> None:
    print("\n[B] 超管改密通道（admin_token）")
    m = _load_license_server(tmp)
    if m is None:
        return
    st: dict = {}
    now = time.time()
    m.register_impl(st, "resetme@qq.com", "old-pass-1", now, m.SECRET,
                    {"fp": "fp-a", "name": "Mac"})
    # 持 ADMIN_TOKEN：不知道原密码也能改
    r = m.password_impl(st, "resetme@qq.com", "new-pass-2", now, m.SECRET,
                        admin_token=m.ADMIN_TOKEN)
    check("管理端改密成功", r.get("ok") is True and r.get("synced") is True)
    out = m.login_impl(st, "resetme@qq.com", "new-pass-2", now, m.SECRET,
                       {"fp": "fp-a", "name": "Mac"})
    check("改密后新密码可登录", out.get("ok") is True)
    # 错令牌必须拒（fail-closed）
    try:
        m.password_impl(st, "resetme@qq.com", "evil-pass", now, m.SECRET,
                        admin_token="wrong-token")
        ok = False
    except m.ApiError:
        ok = True
    check("错误 admin_token 被拒", ok)
    # 原密码通道仍然有效（回归）
    r2 = m.password_impl(st, "resetme@qq.com", "third-pass", now, m.SECRET,
                         old_password="new-pass-2")
    check("原密码通道仍可用", r2.get("ok") is True)


def test_list_users_merge() -> None:
    print("\n[C] 后台用户列表：云端为主 + 本机兜底合并")
    import admin_store
    import license_client

    cloud_rows = [
        {"user_id": "a@qq.com", "email": "a@qq.com", "created_at": 2000,
         "last_login": 3000, "banned": False, "is_admin": True,
         "devices": [{"name": "Mac", "platform": "macos",
                      "app_version": "1.0.69", "last_seen": 3000}],
         "authority": {"member_until_dl": 9e12, "member_until_ai": 0,
                       "perm_credits": 5, "ai_credits_left": 0}},
        {"user_id": "b@qq.com", "email": "b@qq.com", "created_at": 1000,
         "last_login": 0, "banned": True, "is_admin": False, "devices": [],
         "authority": {"member_until_dl": 0, "member_until_ai": 0,
                       "perm_credits": 0, "ai_credits_left": 0}},
    ]
    local_rows = [
        {"user_id": "u_1", "identifier": "a@qq.com", "created_at": 2000,
         "is_admin": True},                      # 与云端重叠 → 应去重
        {"user_id": "u_2", "identifier": "local-only@qq.com",
         "created_at": 1500, "is_admin": False},  # 云端没有 → 应保留
        {"user_id": "u_3", "identifier": "gone@qq.com", "created_at": 800,
         "is_admin": False, "deleted_at": 1},     # 已删除 → 不出现
    ]

    orig_users, orig_load = license_client.users_remote, admin_store._load_users
    orig_token = admin_store._license_admin_token
    admin_store._license_admin_token = lambda: "tok"
    license_client.users_remote = lambda tok: {"ok": True, "users": cloud_rows}
    admin_store._load_users = lambda: {"users": local_rows, "by_identifier": {}}
    try:
        rows = admin_store.list_users()
    finally:
        license_client.users_remote = orig_users
        admin_store._load_users = orig_load
        admin_store._license_admin_token = orig_token

    by_ident = {r["identifier"]: r for r in rows}
    check("云端账号全部可见", "a@qq.com" in by_ident and "b@qq.com" in by_ident)
    check("本机独有账号不丢", "local-only@qq.com" in by_ident)
    check("已删除账号不出现", "gone@qq.com" not in by_ident)
    check("重叠账号去重（不双现）",
          sum(1 for r in rows if r["identifier"] == "a@qq.com") == 1)
    a = by_ident.get("a@qq.com") or {}
    check("云端行 source=cloud", a.get("source") == "cloud")
    check("云端行带设备端维度", a.get("device_count") == 1
          and (a.get("devices") or [{}])[0].get("platform") == "macos")
    check("云端行会员态映射", (a.get("membership") or {}).get("download_active") is True
          and (a.get("membership") or {}).get("credits_total") == 5)
    check("封禁态映射", (by_ident.get("b@qq.com") or {}).get("disabled") is True)
    lo = by_ident.get("local-only@qq.com") or {}
    check("本机行 source=local", lo.get("source") == "local")

    # 云端不可达 → 退化为本机列表（不空、不炸）
    def _boom(tok):
        raise RuntimeError("cloud down")
    admin_store._license_admin_token = lambda: "tok"
    license_client.users_remote = _boom
    admin_store._load_users = lambda: {"users": local_rows, "by_identifier": {}}
    try:
        rows2 = admin_store.list_users()
    finally:
        license_client.users_remote = orig_users
        admin_store._license_admin_token = orig_token
        admin_store._load_users = orig_load
    check("云端故障退化为本机列表",
          any(r["identifier"] == "local-only@qq.com" for r in rows2))


def test_router_dual_write_helpers() -> None:
    print("\n[D] 路由双写 helper（标识解析 + 失败只出 notice）")
    import admin_store
    import license_client
    from routers import admin as admin_router

    orig_identifier = admin_router.__dict__.get("_resolve_identifier")

    # 标识解析：邮箱直接用；u_xxx 查本机表
    check("邮箱直接解析",
          admin_router._resolve_identifier("A@QQ.com") == "a@qq.com")
    import auth_store
    orig_uid_of = getattr(auth_store, "user_identifier", None)
    auth_store.user_identifier = lambda uid: "Mirror@QQ.com "
    try:
        check("u_xxx 查本机表解析",
              admin_router._resolve_identifier("u_abc") == "mirror@qq.com")
    finally:
        if orig_uid_of is not None:
            auth_store.user_identifier = orig_uid_of

    # 云端同步失败 → 只回 notice，不抛
    orig_token = admin_store._license_admin_token
    admin_store._license_admin_token = lambda: "tok"

    def _boom(*a, **kw):
        raise RuntimeError("net")
    orig_set_admin = license_client.set_admin_remote
    license_client.set_admin_remote = _boom
    try:
        notice = admin_router._cloud_sync(
            "a@qq.com", lambda tok: license_client.set_admin_remote("a@qq.com", True, tok))
        check("云端异常只出 notice 不抛", notice.startswith("（云端同步失败"))
    finally:
        license_client.set_admin_remote = orig_set_admin
        admin_store._license_admin_token = orig_token

    # 未配令牌 → 明确提示仅本机
    admin_store._license_admin_token = lambda: ""
    try:
        notice2 = admin_router._cloud_sync("a@qq.com", lambda tok: {"ok": True})
        check("无令牌提示仅本机", "仅本机生效" in notice2)
    finally:
        admin_store._license_admin_token = orig_token

    # 空标识 → 不调云端
    notice3 = admin_router._cloud_sync("", lambda tok: {"ok": True})
    check("空标识不调云端", "未找到" in notice3)
    if orig_identifier:
        admin_router._resolve_identifier = orig_identifier


def test_panel_ops_on_cloud_rows() -> None:
    """E 组：列表改云端总库后，面板各按钮对「云端行」（user_id = 邮箱）必须能用。

    真实事故（2026-10-16 用户截图）：点「使用详情」报「用户不存在」；「会员管理」
    只有本机 3 条而不是 8 条。根因是各操作端点一律按本机 user_id 查本机表。
    """
    print("\n[E] 面板操作对云端行（user_id = 邮箱）")
    import admin_store
    import license_client
    import auth_store
    import user_membership
    from routers import admin as admin_router

    now = time.time()
    local_users = [
        {"user_id": "u_1", "identifier": "a@qq.com", "created_at": 1000, "is_admin": False},
        {"user_id": "u_2", "identifier": "me@qq.com", "created_at": 1000, "is_admin": True},
        {"user_id": "u_3", "identifier": "local-only@qq.com", "created_at": 900,
         "is_admin": False},
    ]
    cloud_users = [
        {"user_id": "a@qq.com", "email": "a@qq.com", "created_at": 1900, "banned": False,
         "is_admin": False, "devices": [], "purchases": [],
         "authority": {"member_until_dl": 0, "member_until_ai": 0,
                       "perm_credits": 0, "ai_credits_left": 0}},
        {"user_id": "cloudonly@qq.com", "email": "cloudonly@qq.com", "created_at": 800,
         "banned": False, "is_admin": False, "devices": [],
         "purchases": [{"plan_code": "download_1day", "at": 700}],
         "authority": {"member_until_dl": now + 30 * 86400, "member_until_ai": 0,
                       "perm_credits": 190, "ai_credits_left": 0}},
    ]

    class _Store:
        def status(self):
            return {"download_member": {"active": False, "plan": None, "expire_at": 0,
                                        "source": None},
                    "ai_member": {"active": False, "plan": None, "expire_at": 0,
                                  "credits_left": 0},
                    "permanent_credits": 7, "credits_total": 7, "daily_usage": {}}

    orig = {
        "load": admin_store._load_users,
        "cloud": admin_store._cloud_users,
        "auth_load": auth_store._load_users,
        "token": admin_store._license_admin_token,
        "store": user_membership.get_user_store,
        "usage_cloud": getattr(admin_router, "_usage_cloud", None),
        "require": admin_router.require_admin,
        "cur": admin_router.get_current_user_id,
    }
    table = {"users": local_users, "by_identifier": {"a@qq.com": "u_1", "me@qq.com": "u_2",
                                                     "local-only@qq.com": "u_3"}}
    admin_store._load_users = lambda: table
    auth_store._load_users = lambda: table
    admin_store._cloud_users = lambda: cloud_users
    user_membership.get_user_store = lambda uid: _Store()
    admin_store._license_admin_token = lambda: "tok"
    admin_router.require_admin = lambda request=None: None
    admin_router.get_current_user_id = lambda request=None: "u_2"

    try:
        # --- 标识解析：本机 uid / 本机邮箱 / 纯云端邮箱 ---
        check("_local_uid 认本机 uid", admin_router._local_uid("u_1") == "u_1")
        check("_local_uid 认本机邮箱（反查）", admin_router._local_uid("A@QQ.com") == "u_1")
        check("_local_uid 对纯云端账号返回空",
              admin_router._local_uid("cloudonly@qq.com") == "")

        # --- 纯云端行：只写云端，云端成功才算成功 ---
        called = {"local": 0, "cloud": []}

        def _local_should_not_run(uid):
            called["local"] += 1
            return {"ok": True}

        def _cloud_ok(ident, tok):
            called["cloud"].append(ident)
            return {"ok": True}

        out = admin_router._admin_mutate("cloudonly@qq.com", _local_should_not_run, _cloud_ok)
        check("云端行不碰本机表", called["local"] == 0)
        check("云端行写到了云端（用账号标识）", called["cloud"] == ["cloudonly@qq.com"])
        check("云端行成功 → ok + cloud_only", out.get("ok") is True
              and out.get("cloud_only") is True)

        def _cloud_fail(ident, tok):
            return {"ok": False, "error": "BAD_PLAN"}

        out = admin_router._admin_mutate("cloudonly@qq.com", _local_should_not_run, _cloud_fail)
        check("云端行云端失败 → 不能假装成功",
              out.get("ok") is False and "BAD_PLAN" in str(out.get("error")))

        admin_store._license_admin_token = lambda: ""
        out = admin_router._admin_mutate("cloudonly@qq.com", _local_should_not_run, _cloud_ok)
        check("云端行未配令牌 → 失败并说明",
              out.get("ok") is False and "令牌" in str(out.get("error")))
        admin_store._license_admin_token = lambda: "tok"

        # --- 本机行：先本机，失败即返回；成功再推云 ---
        called2 = {"local": [], "cloud": []}
        out = admin_router._admin_mutate(
            "a@qq.com",
            lambda uid: (called2["local"].append(uid), {"ok": True})[1],
            lambda ident, tok: (called2["cloud"].append(ident), {"ok": True})[1])
        check("本机行先落本机（uid 正确）", called2["local"] == ["u_1"])
        check("本机行也推云（用邮箱）", called2["cloud"] == ["a@qq.com"])

        called3 = {"local": [], "cloud": []}
        out = admin_router._admin_mutate(
            "a@qq.com",
            lambda uid: {"ok": False, "error": "本机拒绝"},
            lambda ident, tok: (called3["cloud"].append(ident), {"ok": True})[1])
        check("本机失败即返回、不推云",
              out.get("ok") is False and called3["cloud"] == [])

        # 本机操作内部已推云（赠送/调分）→ 不得二次推云
        called4 = {"local": [], "cloud": []}
        out = admin_router._admin_mutate(
            "a@qq.com",
            lambda uid: (called4["local"].append(uid), {"ok": True})[1],
            lambda ident, tok: (called4["cloud"].append(ident), {"ok": True})[1],
            cloud_when_local=False)
        check("本机路径下不重复推云", called4["cloud"] == [] and out.get("ok") is True)

        # --- 使用详情：网页版**代理授权中心**（2026-11-01 后台补齐后改的口径） ---
        # 桌面端时代这里是 admin_user_usage + _usage_cloud（本机表兜底 + 云端合并，
        # 返回 cloud_only / device_fp 这类合并形状）；网页版没有本机账号表，实现改成
        # 直接 proxy 中心 usage_remote，返回 {"ok":True,"usage":<中心载荷原样>}。
        # 因此断言改为「透传 + 失败可见」，不再要求桌面端的合并字段 —— 否则守的是
        # 一个本树已不存在的实现，永远红。
        if not hasattr(admin_router, "admin_user_usage"):
            print("  ⏭ 本树无使用详情端点（网页版无该面板），跳过")
        else:
            import license_client
            _orig_usage = license_client.usage_remote
            _orig_tok = admin_store._license_admin_token
            _center = {
                "ok": True,
                "authority": {"member_until_dl": now + 86400 * 3, "member_until_ai": 0,
                              "perm_credits": 12, "ai_credits_left": 0,
                              "usage": {"download": 2}},
                "usage_history": [{"date": "2026-11-02", "counts": {"download": 2}}],
                "devices": [{"fp": "fp-1", "name": "Mac", "last_seen": 700}],
                "purchases": [{"plan_code": "download_3day", "created_at": 700}],
            }
            admin_store._license_admin_token = lambda: "tok-test"
            license_client.usage_remote = lambda ident, tok, **kw: dict(_center, email=ident)
            try:
                res = admin_router.admin_user_usage("cloudonly@qq.com", request=None)
            finally:
                license_client.usage_remote = _orig_usage
                admin_store._license_admin_token = _orig_tok
            u = res.get("usage") or {}
            check("云端行使用详情返回 ok",
                  res.get("ok") is True and not res.get("error"), str(res)[:120])
            check("使用详情透传中心权益（永久积分 12）",
                  (u.get("authority") or {}).get("perm_credits") == 12, str(u)[:120])
            check("使用详情透传设备指纹",
                  ((u.get("devices") or [{}])[0] or {}).get("fp") == "fp-1")
            check("使用详情透传订单",
                  ((u.get("purchases") or [{}])[0] or {}).get("plan_code") == "download_3day")
            check("使用详情透传近 14 天用量",
                  ((u.get("usage_history") or [{}])[0] or {}).get("date") == "2026-11-02")

            # 中心查不到 / 未配令牌：必须 ok=False 且带可读原因，绝不能假装成功
            admin_store._license_admin_token = lambda: "tok-test"
            license_client.usage_remote = lambda ident, tok, **kw: {
                "ok": False, "error": "账号不存在"}
            try:
                bad = admin_router.admin_user_usage("ghost@qq.com", request=None)
            finally:
                license_client.usage_remote = _orig_usage
                admin_store._license_admin_token = _orig_tok
            check("中心查不到 → ok=False 且带原因",
                  bad.get("ok") is False and "不存在" in str(bad.get("error")), str(bad)[:120])
            admin_store._license_admin_token = lambda: ""
            try:
                notok = admin_router.admin_user_usage("cloudonly@qq.com", request=None)
            finally:
                admin_store._license_admin_token = _orig_tok
            check("未配管理令牌 → 明确提示而非静默成功",
                  notok.get("ok") is False
                  and "TOKEN" in str(notok.get("error")).upper(), str(notok)[:120])

        # --- 会员管理：云端为主 + 本机兜底（原事故：只有本机 3 条） ---
        mrows = admin_store.list_memberships()
        mby = {r["identifier"]: r for r in mrows}
        check("会员列表含云端账号", "cloudonly@qq.com" in mby)
        check("会员列表含本机独有账号", "local-only@qq.com" in mby)
        check("会员列表重叠账号不双现",
              sum(1 for r in mrows if r["identifier"] == "a@qq.com") == 1)
        check("重叠账号取云端权益（本机 perm=7 不得露头）",
              (mby.get("a@qq.com") or {}).get("permanent_credits") == 0)
        co = mby.get("cloudonly@qq.com") or {}
        check("云端会员态映射（active/积分 190）",
              co.get("download_active") is True and co.get("permanent_credits") == 190)
        check("云端档位名与到期日自洽（30 天 → download_month）",
              co.get("download_plan") == "download_month")
        check("本机行档位名照旧（不反推）",
              (mby.get("local-only@qq.com") or {}).get("download_plan") is None)

        # --- 档位反推边界 ---
        f = admin_store._plan_name_by_remaining
        check("反推 1 天档", f(now + 86400, now) == "download_1day")
        check("反推 365 天档", f(now + 365 * 86400, now) == "download_year")
        check("反推对不上任何档位 → None（不瞎猜）",
              f(now + 1000 * 86400, now) is None)
        check("已过期 → None", f(now - 10, now) is None)
    finally:
        admin_store._load_users = orig["load"]
        auth_store._load_users = orig["auth_load"]
        admin_store._cloud_users = orig["cloud"]
        user_membership.get_user_store = orig["store"]
        admin_store._license_admin_token = orig["token"]
        if orig["usage_cloud"] is not None:
            admin_router._usage_cloud = orig["usage_cloud"]
        admin_router.require_admin = orig["require"]
        admin_router.get_current_user_id = orig["cur"]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="vdl_cloudacct_") as td:
        tmp = Path(td)
        test_cloud_is_admin_and_import(tmp)
        test_admin_password_channel(tmp)
        test_list_users_merge()
        test_router_dual_write_helpers()
        test_panel_ops_on_cloud_rows()
    print(f"\n{'=' * 46}")
    print(f"通过 {PASS} / 失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
