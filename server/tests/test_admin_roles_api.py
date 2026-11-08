#!/usr/bin/env python3
"""后台写接口的超管闸门 —— 路由层回归（2026-10-22，网页版）。

test_admin_roles.py 验的是「谁算超管」；本文件验「闸门有没有真的装上」：
  · 管理员（admin）调写接口 → 403，且**不是** 401（身份没问题，是权限不够）
  · 超管（super）→ 放行
  · 非 admin → 401（连后台都不该进）
  · 静态扫描：每一个 @router.post/delete 都必须用 require_super，
    **不允许**残留 require_admin —— 漏一个就等于给管理员开了个后门。

🔴 与桌面端的两处刻意差异（网页版是公网部署，不能照抄）：
  1. 写接口基线是 11 个而非 16 个 —— 网页版后台没有 volcengine/dashscope/
     deepseek 凭据下发、会员公告、用量弹窗那一批端点。
  2. 多一节【5】CSRF：网页版的 require_admin/require_super 都带跨源兜底
     （app._cross_site_rejected）。这层若在移植中被丢掉，外部站点就能借
     已登录管理员的 Cookie 触发状态变更，故必须钉死。

跑法：python3 server/tests/test_admin_roles_api.py
"""
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def ok(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}   {extra}")


def make_env(users, admin_json=None):
    d = tempfile.mkdtemp(prefix="vdl_rolesapi_")
    (Path(d) / "users.json").write_text(
        json.dumps({"users": users}, ensure_ascii=False), encoding="utf-8")
    if admin_json is not None:
        (Path(d) / "admin.json").write_text(
            json.dumps(admin_json, ensure_ascii=False), encoding="utf-8")
    os.environ["VDL_DATA_DIR"] = d
    os.environ.pop("VDL_ADMIN_IDENTIFIER", None)
    return d


def u(uid, ident, created, is_admin):
    return {"user_id": uid, "identifier": ident, "created_at": created, "is_admin": is_admin}


D = make_env([
    u("u_super", "1812804606@qq.com", 1788916708, True),
    u("u_admin", "15638319956", 1791102569, True),
    u("u_plain", "15014313254", 1788918967, False),
])
import auth_store                                    # noqa: E402
importlib.reload(auth_store)
import routers.admin as adm                          # noqa: E402
importlib.reload(auth_store)          # admin 内部是延迟 import，这里再保证一次新鲜

from fastapi import HTTPException                    # noqa: E402


class FakeReq:
    """最小 Request 打桩：只需 headers（_cross_site_rejected 只读 origin）。"""

    def __init__(self, origin: str = ""):
        self.headers = {"origin": origin} if origin else {}


def _call_guard(who: str, origin: str = ""):
    """把「当前登录者」打桩成 who，再跑一次 require_super。"""
    orig = adm.get_current_user_id
    adm.get_current_user_id = lambda _req=None: who
    try:
        adm.require_super(FakeReq(origin))
        return None
    except HTTPException as e:
        return e
    finally:
        adm.get_current_user_id = orig


print("【1】require_super 的三种身份")
e = _call_guard("u_super")
ok("超管放行（无异常）", e is None, repr(e))
e = _call_guard("u_admin")
ok("管理员被拦", e is not None and e.status_code == 403, getattr(e, "status_code", None))
ok("管理员收到的是 403 而不是 401（不能把已登录的人踢回登录页）",
   getattr(e, "status_code", None) == 403)
ok("403 文案说清了「只读」", "只读" in str(getattr(e, "detail", "")), getattr(e, "detail", ""))
e = _call_guard("u_plain")
ok("普通用户 401", getattr(e, "status_code", None) == 401, getattr(e, "status_code", None))
e = _call_guard("")
ok("未登录 401", getattr(e, "status_code", None) == 401, getattr(e, "status_code", None))

print("\n【2】静态扫描：写接口一个都不能漏（admin.py + admin_cookies.py）")
WRITE = re.compile(r"@router\.(post|put|patch|delete)\(")


# 机器对机器令牌鉴权的接口不算用户权限接口：/api/cookie/pool-admin 用
# VDL_COOKIE_SYNC_TOKEN（供桌面 App 后台代理云端池），本就没有用户身份。
_TOKEN_AUTH_ROUTES = ("/api/cookie/pool-admin",)

# 登录入口**必须**匿名可访问（登录页 + 登录接口本身），任何令牌都不该要求 ——
# 否则谁也登不进后台。这是唯一允许「无闸门」的白名单；新增条目必须在此写明理由。
_PUBLIC_ROUTES = ("/admin/login", "/api/admin/login")

# 「只改操作者自己的查看状态」的写接口：允许 require_admin，但必须由下面
# _assert_ui_state_only() 逐条证明「真的只动 UI 状态文件」。
#   /api/admin/platform-health/ack —— 把巡检异常提醒标已读（后台首屏红条的「知道了」）。
#     管理员是只读**配置**，把红条标已读不涉及任何业务写入；若强制超管，
#     负责盯盘的管理员就只能一直看着红条、无法处理，属于职责错配。
#     但它一旦开始写业务数据（套餐/用户/额度），下面的断言必须立刻变红。
_UI_STATE_ROUTES = ("/api/admin/platform-health/ack",)

# 认可的写闸门写法（2026-11-03 补齐）：三者都**不弱于** require_super。
#   require_super(request)         —— 桌面 App 后台兼容（普通/后台令牌都收，但必须超管）
#   require_super_backend(request) —— 只认 2h 后台令牌，比上面更严（改价/改额度专用）
#   _ops_guard(request)            —— core._require_admin（adm 令牌 + 超管 / X-Admin-Key / 本机）
# 只认字面量 require_super(request) 会把后两种**更严**的闸门误报成「没闸门」，
# 于是守卫常红、真回退反而被噪音淹没。
_WRITE_GATES = ("require_super(request)", "require_super_backend(request)", "_ops_guard(request)")
_READ_GATES = ("require_admin(request)", "_ops_guard(request)")


def scan(fname: str):
    """返回 (写接口数, 只读接口数, 漏闸门的写路由行号, 误用 super 的读路由行号)。

    ⚠️ 关键：**没找到任何闸门**的路由也必须记 bad —— 否则「删掉 require_super
    那一行」这种回退会被静默放过，等于给管理员开后门还测不出来。
    """
    lines = (ROOT / "routers" / fname).read_text(encoding="utf-8").split("\n")
    w = r = 0
    bad_w, bad_r = [], []
    pending = None      # (kind, 行号, 路由原文)

    def close(kind, line_no):
        if pending is None:
            return
        if pending[0] in ("write", "write_ui"):
            bad_w.append(pending[1])
        else:
            bad_r.append(pending[1])

    for i, line in enumerate(lines, 1):
        is_write = bool(WRITE.match(line))
        is_read = bool(re.match(r"@router\.get\(", line))
        if is_write or is_read:
            # 上一个路由已结束但从未见到闸门 ⇒ 漏了
            if pending is not None:
                close(pending[0], pending[1])
            route = line
            if any(t in route for t in _TOKEN_AUTH_ROUTES + _PUBLIC_ROUTES):
                pending = None          # 令牌鉴权 / 登录入口，跳过
            elif is_write and any(t in route for t in _UI_STATE_ROUTES):
                # 纯 UI 状态写接口：允许降到管理员级，但**仍然必须挂闸门**
                # （漏挂 = 匿名可改状态，照样记 bad；见 _UI_STATE_ROUTES）
                pending = ("write_ui", i, route)
            else:
                pending = ("write", i, route) if is_write else ("read", i, route)
            if is_write:
                w += 1
            else:
                r += 1
            continue
        if pending and pending[0] == "write_ui":
            if any(g in line for g in _WRITE_GATES + _READ_GATES):
                pending = None
                continue
        if pending and pending[0] == "write":
            if any(g in line for g in _WRITE_GATES):
                pending = None
                continue
            if "require_admin(request)" in line:
                bad_w.append(pending[1])   # 写接口只挂了管理员闸门 = 给管理员开后门
                pending = None
                continue
        if pending and pending[0] == "read":
            if any(g in line for g in _READ_GATES):
                pending = None
                continue
            if "require_super" in line:    # 读接口误用超管闸门 ⇒ 管理员看不到
                bad_r.append(pending[1])
                pending = None
                continue
    if pending is not None:             # 文件末尾未闭合
        close(pending[0], pending[1])
    return w, r, bad_w, bad_r


def handler_body(fname: str, route: str) -> str:
    """取某个路由装饰器到下一个路由装饰器之间的源码（含 docstring 与函数体）。"""
    raw = (ROOT / "routers" / fname).read_text(encoding="utf-8")
    start = raw.find('"' + route + '"')
    if start < 0:
        return ""
    start = raw.rfind("@router.", 0, start)
    nxt = raw.find("\n@router.", start + 1)
    return raw[start: nxt if nxt > 0 else len(raw)]


# 业务数据关键词：出现在「纯 UI 状态」接口里 ⇒ 它已经越界成业务写入，必须改超管闸门
_BUSINESS_WRITE_TOKENS = ("users.json", "admin.json", "plans_set", "membership",
                          "subscription", "quota", "license", "payment")


def assert_ui_state_only(fname: str, route: str, expect_file: str) -> list[str]:
    """证明 _UI_STATE_ROUTES 里的接口只动一个 UI 状态文件，返回违规项。"""
    body = handler_body(fname, route)
    bad = []
    if not body:
        return [f"找不到路由 {route}（白名单失效，请删掉对应条目）"]
    if expect_file not in body:
        bad.append(f"未写 {expect_file}")
    for tok in _BUSINESS_WRITE_TOKENS:
        if tok in body:
            bad.append(f"出现业务写入痕迹 {tok}")
    return bad


w1, r1, bw1, br1 = scan("admin.py")
ok(f"admin.py：写接口 {w1} 个全部有闸门（1 条纯 UI 状态见 _UI_STATE_ROUTES）", not bw1, str(bw1))
ok(f"admin.py：只读接口 {r1} 个仍用 require_admin（管理员能看）", not br1, str(br1))
ok("admin.py 写接口数与改造基线一致（新增写接口请同步本基线）", w1 == 16, w1)
# 基线 11 → 13（2026-11-03 同步）：2026-11-01 后台改造新增两条写路由
#   · /api/admin/login           —— 登录入口本身（匿名，见 _PUBLIC_ROUTES）
#   · /api/admin/config/free-quota —— 免费额度下发（require_super_backend）
# 基线 13 → 14（2026-11-04 同步）：11-03 晚「巡检异常提醒」新增
#   · /api/admin/platform-health/ack —— 只改提醒文件 ack 标记（require_admin，
#     见 _UI_STATE_ROUTES；其「不得写业务数据」由 _assert_ui_state_only 钉死）
# 基线 14 → 16（2026-11-08 同步）：云端背景音乐曲库新增两条写路由
#   · /api/admin/bgm/upload —— 超管上传曲目（require_super_backend）
#   · /api/admin/bgm/delete —— 超管删除曲目（require_super_backend）
#   闸门强弱 / 闸门排在写盘之前由 test_admin_bgm.py【6】逐条钉死。

for _route in _UI_STATE_ROUTES:
    _bad = assert_ui_state_only("admin.py", _route, "platform_health_alerts.json")
    ok(f"{_route} 只写 UI 状态文件（不碰业务数据）", not _bad, str(_bad))

w2, r2, bw2, br2 = scan("admin_cookies.py")
ok(f"admin_cookies.py：写接口 {w2} 个全部有闸门（3 个超管 + 1 个令牌鉴权）",
   not bw2 and w2 == 4, f"w={w2} bad={bw2}")
ok("admin_cookies.py：只读接口仍用 require_admin（管理员能看池概况）", not br2, str(br2))

print("\n【3】单一真源：超管判定只允许在 auth_store 里实现一次")
# 用「装饰器后第一处闸门」判定，而不是全文 grep —— 否则 GET 的 require_admin 会误报
raw = (ROOT / "routers" / "admin.py").read_text(encoding="utf-8")
ok("admin.py 复用 auth_store.is_super_identifier，不自己实现一套名单判定",
   "is_super_identifier" in raw, "未找到对 is_super_identifier 的引用")
ok("admin.py 不自己实现名单读取（名单只有 auth_store._admin_identifiers 一处）",
   "def _admin_identifiers" not in raw and "_admin_identifiers(" not in raw,
   "发现第二处名单逻辑")

print("\n【4】set-admin 的自我保护")
setadmin = raw.split("def admin_set_admin")[1].split("\n@router.")[0] if "def admin_set_admin" in raw else ""
ok("拒绝降级超管（否则全库无超管 ⇒ 后台永久锁死）",
   "is_super_identifier" in setadmin, setadmin[:120])
ok("拒绝取消自己", "不能取消当前登录账号的管理员权限" in setadmin)
ok("已改用 require_super", "require_super(request)" in setadmin)

print("\n【5】网页版特有：CSRF 兜底不能随移植丢失")
ok("require_super 里调用了跨源判定", "_cross_site_rejected" in raw.split("def require_super")[1][:900],
   raw.split("def require_super")[1][:300] if "def require_super" in raw else "无 require_super")
e = _call_guard("u_super", origin="https://evil.example.com")
ok("超管身份 + 外部站点 Origin 也必须 403（否则 CSRF 可改数据）",
   getattr(e, "status_code", None) == 403, getattr(e, "status_code", None))
e = _call_guard("u_super", origin="")
ok("无 Origin（同源/服务端调用）不被 CSRF 误伤", e is None, repr(e))
# 白名单口径以 app._LOCAL_ORIGIN_RE 为准：只放本机与浏览器扩展，不是本站域名。
# （⚠️ 由此引出一个既有问题：线上实测 https://hanyuxz.top 带 Origin 的请求一律 403，
#   同源 POST 也会被浏览器带上 Origin ⇒ 网页版 AI 计费页的「保存」在浏览器里点不动。
#   该缺陷在 require_admin 上早已存在，不是本次移植引入；这里如实固化现状口径，
#   修不修由产品决定 —— 放宽白名单属于安全相关改动，不夹带在本次移植里。）
e = _call_guard("u_super", origin="http://127.0.0.1:8888")
ok("本机 Origin 放行（与 app._LOCAL_ORIGIN_RE 口径一致）", e is None, repr(e))
e = _call_guard("u_super", origin="chrome-extension://abcdefghijklmnop/")
ok("浏览器扩展 Origin 放行", e is None, repr(e))

print("\n【6】admin_cookies 的浏览器写接口也走 require_super")
ac = (ROOT / "routers" / "admin_cookies.py").read_text(encoding="utf-8")
ok("已 import require_super", "require_admin, require_super" in ac)
for fn in ("admin_cookies_verify", "admin_cookies_inject", "admin_cookies_delete"):
    seg = ac.split(f"def {fn}")[1].split("\n@router.")[0] if f"def {fn}" in ac else ""
    ok(f"{fn} 用 require_super", "require_super(request)" in seg, seg[:100])

shutil.rmtree(D, ignore_errors=True)
print(f"\n{'=' * 46}\nPASS={PASS}  FAIL={FAIL}")
sys.exit(1 if FAIL else 0)