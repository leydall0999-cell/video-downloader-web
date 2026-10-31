"""VDL 云端授权客户端（App 侧 → 授权中心）。

授权中心部署在国内 ECS（`8.138.223.3:8902`，nginx `location /api/license/` 分流；
源码见 deploy/license_server.py）。**web 版（ECS /opt/vdl-worker）2026-09-26 起也复用
本文件**，用来把网页版账号接到同一份云端账号库 —— 见 server/cloud_link.py。
web 侧部署时务必注入 `VDL_LICENSE_BASE=http://127.0.0.1:8902`（同机直连，绕开公网）。

**账号制（2026-09-22 定版）**：客户端不做卡密验签、不做「设备是否被绑过」判定，
只做三件事：
  register / login   拿云端签发的 token（同时占一个设备位）
  redeem             卡密充值到账号（云端负责验签 + 防重复核销）
  heartbeat          心跳续期，顺带问一句「我这台还在不在两台名额里」

被别的机器挤掉时 heartbeat 返回 code=DEVICE_EVICTED；此时本机会员降级为免费档，
提示用户重新登录（登录 = 抢回设备位）。

**拿 outbound 失败当网络问题，别当授权失败**：网络异常一律抛 LicenseCloudError，
调用方按 fail-open 处理（已购买的权益不会因为断网消失）。

可注入性：base_url / opener 均可替换，单测无需真实网络。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

DEFAULT_BASE = "https://hanyuxz.top"

_BASE_CACHE: dict[str, str] = {}


def license_base() -> str:
    """授权中心基址（2026-09-22 授权中心自香港迁国内 ECS 后引入）。

    解析顺序：
      1. env `VDL_LICENSE_BASE`（部署侧显式覆盖）；
      2. `~/.videodownloader/cloud_sync.json` 的 `url`（worker 与授权中心同在
         ECS 8888，nginx 按 location 分流 —— 一个 origin 服务两个后端）；
      3. 回落 DEFAULT_BASE（hanyuxz.top，历史兼容）。

    背景：原 DEFAULT_BASE 走 hanyuxz.top → Cloudflare → 香港，实测单次
    login 7.5s，贴着 12s 超时线抖动，用户登录时随机「云端未同步」。
    迁 ECS 后同链路实测 ~86ms。结果进程内缓存，避免每次请求都读盘。
    """
    cached = _BASE_CACHE.get("base")
    if cached:
        return cached
    base = (os.environ.get("VDL_LICENSE_BASE") or "").strip()
    if not base:
        try:
            cfg_path = os.path.join(os.path.expanduser("~"), ".videodownloader", "cloud_sync.json")
            with open(cfg_path, "r", encoding="utf-8") as fh:
                cfg = json.load(fh) or {}
            base = str(cfg.get("url") or "").strip()
        except Exception:
            base = ""
    if not base:
        base = DEFAULT_BASE
    _BASE_CACHE["base"] = base.rstrip("/")
    return _BASE_CACHE["base"]


class LicenseCloudError(Exception):
    """云端不可达 / 非 JSON 响应。调用方按「网络异常」向用户展示。"""

    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status


def _post(path: str, payload: dict[str, Any], base_url: Optional[str],
          timeout: float, opener: Optional[Callable] = None) -> dict[str, Any]:
    if not base_url:
        base_url = license_base()
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "VDL-LicenseClient/2.0"},
        method="POST")
    urlopen = opener or urllib.request.urlopen
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310  https 固定基址
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # 云端业务错误（4xx 带 JSON body）也要把 error/code 传回去
        try:
            return json.loads(e.read().decode("utf-8", "replace") or "{}")
        except Exception:
            raise LicenseCloudError(f"HTTP {e.code}", e.code)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise LicenseCloudError(f"无法连接授权中心: {e}")
    try:
        return json.loads(body or "{}")
    except json.JSONDecodeError:
        raise LicenseCloudError("授权中心响应不是 JSON")


# ---- 账号 ----
def register_remote(email: str, password: str, fp: str, name: str = "",
                    base_url: Optional[str] = None, timeout: float = 12.0,
                    opener: Optional[Callable] = None,
                    meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """注册并自动登录。返回 {ok, token, account, evicted?}。

    meta：设备画像（platform/app_version/ext）——授权中心据此按端统计
    （2026-10-16 起网页版传 platform=web，桌面端心跳本就上报）。
    """
    device = {"fp": fp, "name": name or "未命名设备"}
    if meta:
        device["meta"] = meta
    return _post("/api/license/register",
                 {"email": email, "password": password, "device": device},
                 base_url, timeout, opener)


def login_remote(email: str, password: str, fp: str, name: str = "",
                 base_url: Optional[str] = None, timeout: float = 12.0,
                 opener: Optional[Callable] = None,
                 meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """登录。返回 {ok, token, account:{email, devices, purchases, max_devices}, evicted?}。

    meta：同 register_remote（设备画像，按端统计用）。
    """
    device = {"fp": fp, "name": name or "未命名设备"}
    if meta:
        device["meta"] = meta
    return _post("/api/license/login",
                 {"email": email, "password": password, "device": device},
                 base_url, timeout, opener)


def heartbeat_remote(token: str, fp: str, base_url: Optional[str] = None,
                     timeout: float = 8.0,
                     opener: Optional[Callable] = None) -> dict[str, Any]:
    """心跳续期 / 配额校验。被挤掉时返回 {"ok": False, "code": "DEVICE_EVICTED"}。"""
    return _post("/api/license/heartbeat", {"token": token, "fp": fp},
                 base_url, timeout, opener)


def redeem_remote(token: str, code: str, base_url: Optional[str] = None,
                  timeout: float = 12.0,
                  opener: Optional[Callable] = None) -> dict[str, Any]:
    """卡密充值到账号。返回 {ok, plan_code, purchase_id, account}。"""
    return _post("/api/license/redeem", {"token": token, "code": code.strip()},
                 base_url, timeout, opener)


def spend_remote(token: str, items: list, base_url: Optional[str] = None,
                 timeout: float = 8.0,
                 opener: Optional[Callable] = None) -> dict[str, Any]:
    """积分扣减上报（幂等）。items: [{pool: permanent|ai, cost, op, id}]。

    返回 {ok, applied, authority}；云端按 id 去重，断网重发不会重复扣。
    网络异常抛 LicenseCloudError —— 调用方把 items 入 pending 队列下次补报。
    """
    return _post("/api/license/spend", {"token": token, "items": items},
                 base_url, timeout, opener)


def daily_remote(token: str, items: list, base_url: Optional[str] = None,
                 timeout: float = 8.0,
                 opener: Optional[Callable] = None) -> dict[str, Any]:
    """每日用量上报（幂等）。items: [{res, n, id}]。

    同一账号在 App / 网页版共用同一份每日配额：本地 use_daily 只是预检缓存，
    真实用量以授权中心按账号累计为准。返回 {ok, applied, authority}；云端按 id
    去重，断网重发不会重复计。网络异常抛 LicenseCloudError —— 调用方把 items
    入 pending 队列下次补报。
    """
    return _post("/api/license/daily", {"token": token, "items": items},
                 base_url, timeout, opener)


def trial_claim_remote(token: str, op: str, mode: str = "once",
                       base_url: Optional[str] = None,
                       timeout: float = 8.0,
                       opener: Optional[Callable] = None) -> dict[str, Any]:
    """免费体验名额跨端原子领取。返回 {ok, claimed, already, authority}。

    already=False → 本端成功领取全局唯一名额（放行）；already=True → 另一台已领走
    （本端应拒绝）。网络异常抛 LicenseCloudError —— 调用方按 fail-open 处理
    （视为本端领取，离线不惩罚已付费/免费用户）。
    """
    return _post("/api/license/trial_claim",
                 {"token": token, "op": op, "mode": mode},
                 base_url, timeout, opener)


def cloud_quota_remote(token: str, lifetime: int = 0, daily: int = 0,
                       refund: bool = False,
                       resource: str = "cloud_commentary",
                       base_url: Optional[str] = None,
                       timeout: float = 8.0,
                       opener: Optional[Callable] = None) -> dict[str, Any]:
    """免费云端额度跨端原子扣减 / 退还 / 查询（2026-10-06）。

    🔴 这个端点存在的理由：两端原先把「终身 3 次云端额度」各记各的
    （桌面记本机 quota.json、网页记 use_daily("cloud") 的每日配额），
    换端/重装即重置。统一后由授权中心按**账号**记账，App / 网页共享一份。

    参数三态（服务端语义）：
      · lifetime/daily > 0 → 扣减
      · refund=True       → 退还（任务失败补偿）
      · 都不传             → 只查询余额（冷启动/心跳回灌用）

    返回 {ok, allowed, reason, applied, cloud_quota:{lifetime:{res:n},
    lifetime_remaining:{res:n}, lifetime_used(旧中心=总池 int), daily_auto_used, ...}}。
    allowed=False 表示额度已用尽（reason=lifetime_exhausted / daily_auto_exhausted），
    此时**不落账**。网络异常抛 LicenseCloudError —— 调用方按 fail-open 处理
    （沿用本机计数，绝不因断网把已付费会员拦在门外）。
    """
    return _post("/api/license/cloud_quota",
                 {"token": token, "lifetime": int(lifetime or 0),
                  "daily": int(daily or 0), "refund": bool(refund),
                  "resource": str(resource or "cloud_commentary")},
                 base_url, timeout, opener)


def set_password_remote(email: str, new_password: str, old_password: str = "",
                        token: str = "", base_url: Optional[str] = None,
                        timeout: float = 12.0,
                        opener: Optional[Callable] = None,
                        admin_token: str = "") -> dict[str, Any]:
    """把本机账号的新密码同步到云端（两端一套密码，见 license_server.password_impl）。

    鉴权三选一：知道原密码（改密）/ 持有云端 token（忘记密码重置）/
    持授权中心 ADMIN_TOKEN（2026-10-16 超管后台重置，admin_token 通道）。
    云端没有该账号时返回 {"ok": True, "synced": False}（老的本机专属账号），不是失败。
    网络故障抛 LicenseCloudError —— 调用方 **fail-open**：本机改密已生效，不能因为
    云端连不上就把本机也回滚。
    """
    payload: dict[str, Any] = {"email": email, "new_password": new_password,
                               "old_password": old_password, "token": token}
    if admin_token:
        payload["admin_token"] = admin_token
    return _post("/api/license/password", payload,
                 base_url, timeout, opener)


def devices_remote(token: str, fp: str = "", base_url: Optional[str] = None,
                   timeout: float = 8.0,
                   opener: Optional[Callable] = None) -> dict[str, Any]:
    """我的设备列表（含 max_devices）。传 fp 时云端会标出哪台是本机。"""
    return _post("/api/license/devices", {"token": token, "fp": fp},
                 base_url, timeout, opener)


def unbind_remote(token: str, fp: str, base_url: Optional[str] = None,
                  timeout: float = 8.0,
                  opener: Optional[Callable] = None) -> dict[str, Any]:
    """登出/移除某台设备，腾位置给别人。"""
    return _post("/api/license/unbind", {"token": token, "fp": fp},
                 base_url, timeout, opener)


def check_remote(code: str, fingerprint: str = "", base_url: Optional[str] = None,
                 timeout: float = 8.0,
                 opener: Optional[Callable] = None) -> dict[str, Any]:
    """卡密状态查询（兼容保留）。网络故障抛 LicenseCloudError（调用方 fail-open）。"""
    return _post("/api/license/check", {"code": code, "fingerprint": fingerprint},
                 base_url, timeout, opener)


# ---- 用户信息类数据云备份 · 网页版通道（2026-10-17 接入）-------------------- #
# 与桌面端共用同一个「账号资料包」通道，但用 `scope="web"` **分文件**存放
# （服务端 userdata/<账号sha>.web.json）⇒ 桌面端与网页版各存各的，互不覆盖
# （桌面端的资料包是「整体替换」组装的，共用一份会互相抹掉）。
#
# ⚠️ 隐私边界（改这里前先想清楚）：网页版的平台登录态（用户粘贴的 Cookie）由
# **浏览器**用登录密码派生的密钥（PBKDF2-SHA256 20 万轮 + AES-256-GCM）加密后
# 才上传 ⇒ 本服务与授权中心都只拿到密文。本模块只做**转发**，绝不解析内容。
def userdata_pull_remote(token: str, scope: str = "", base_url: Optional[str] = None,
                         timeout: float = 15.0,
                         opener: Optional[Callable] = None) -> dict[str, Any]:
    """取回本人云端资料包（scope 留空 = 桌面端那份；"web" = 网页版那份）。"""
    body: dict[str, Any] = {"token": token}
    if scope:
        body["scope"] = scope
    return _post("/api/license/data-pull", body, base_url, timeout, opener)


def userdata_push_remote(token: str, payload: dict[str, Any], scope: str = "",
                         base_url: Optional[str] = None, timeout: float = 40.0,
                         opener: Optional[Callable] = None) -> dict[str, Any]:
    """上传/覆盖本人云端资料包（幂等）。返回 {ok, updated_at, meta}。"""
    body: dict[str, Any] = {"token": token, "payload": payload}
    if scope:
        body["scope"] = scope
    return _post("/api/license/data-push", body, base_url, timeout, opener)


def userdata_meta_remote(token: str, scope: str = "", base_url: Optional[str] = None,
                         timeout: float = 10.0,
                         opener: Optional[Callable] = None) -> dict[str, Any]:
    """只看「云端备份了什么」的摘要（不含内容/密文）。返回 {ok, meta(可为 None)}。

    网页版只把这份摘要回给前端显示（「已备份 N 项偏好 / M 个登录态」）；
    真正的资料包内容永远不下发给除本人浏览器外的任何一方。
    """
    body: dict[str, Any] = {"token": token}
    if scope:
        body["scope"] = scope
    return _post("/api/license/data-meta", body, base_url, timeout, opener)


# ---- 管理员：全端账号（2026-10-16「所有端用户上云端」）----
def users_remote(admin_token: str, base_url: Optional[str] = None,
                 timeout: float = 12.0) -> dict[str, Any]:
    """管理员查全量账号列表（网页后台用户管理的唯一数据源）。

    授权中心是各端（桌面 App / 网页版 cn + hk）账号的唯一真源；返回全部账号 +
    设备画像（platform / app_version）+ 权益快照 + is_admin + 封禁态。
    """
    return _post("/api/license/users", {"token": admin_token},
                 base_url, timeout)


def set_admin_remote(email: str, is_admin: bool, admin_token: str,
                     base_url: Optional[str] = None,
                     timeout: float = 10.0) -> dict[str, Any]:
    """管理员设置 / 取消超管（is_admin 上云后的写入口）。"""
    return _post("/api/license/set-admin",
                 {"token": admin_token, "email": email, "is_admin": bool(is_admin)},
                 base_url, timeout)


def ban_remote(email: str, banned: bool, admin_token: str, note: str = "",
               base_url: Optional[str] = None,
               timeout: float = 10.0) -> dict[str, Any]:
    """管理员封禁 / 解封账号（全端生效：登录被拒、心跳回 ACCOUNT_BANNED）。"""
    return _post("/api/license/ban",
                 {"token": admin_token, "email": email,
                  "banned": bool(banned), "note": note},
                 base_url, timeout)


def import_account_remote(email: str, salt: str, pw_hash: str, admin_token: str,
                          created_at: float = 0.0, is_admin: bool = False,
                          authority: Optional[dict[str, Any]] = None,
                          devices: Optional[list] = None,
                          base_url: Optional[str] = None,
                          timeout: float = 10.0) -> dict[str, Any]:
    """存量账号全量搬迁（本机账号表 + memberships → 授权中心）。

    salt/pw_hash 格式与服务端一致（PBKDF2-HMAC-SHA256 20 万轮，hex 盐 / hex 摘要）
    ⇒ 用户密码无感、无需重置。authority/devices：本机权益态与设备画像一并搬来
    （2026-10-16 用户「真实账号的所有数据也得导入」）。已存在账号不覆盖（云端权威）。
    """
    return _post("/api/license/import-account",
                 {"token": admin_token, "email": email, "salt": salt,
                  "pw_hash": pw_hash, "created_at": float(created_at or 0),
                  "is_admin": bool(is_admin),
                  "authority": authority if isinstance(authority, dict) else None,
                  "devices": devices if isinstance(devices, list) else None},
                 base_url, timeout)


def delete_user_remote(email: str, admin_token: str,
                       base_url: Optional[str] = None,
                       timeout: float = 10.0) -> dict[str, Any]:
    """删除云端账号（幂等）。

    2026-10-16 历史测试号清理：管理端持令牌调用；账号不存在时服务端亦返回 ok。
    """
    return _post("/api/license/delete-user",
                 {"token": admin_token, "email": email},
                 base_url, timeout)


def usage_remote(email: str, admin_token: str, base_url: Optional[str] = None,
                 timeout: float = 10.0) -> dict[str, Any]:
    """管理员查某账号的权益与流水（面板用量视图改云端后的数据源）。"""
    return _post("/api/license/usage",
                 {"token": admin_token, "email": email},
                 base_url, timeout)


# ---- 解析需求榜 / 全站功能用量（2026-10-30「全端需求同步 + 全功能用量」）---- #
def demand_report_remote(token: str, items: list, base_url: Optional[str] = None,
                        timeout: float = 10.0,
                        opener: Optional[Callable] = None) -> dict[str, Any]:
    """网页版（worker）解析失败需求上报授权中心（旁路信号，只落日志文件）。

    items: [{host, url?, category?, ts?}]，最多 20 条/请求。中心按 账号×host 60s
    节流，但对 ts 在 1 小时前的历史记录放行 —— 供客户端首装回填积压用。
    """
    return _post("/api/license/demand_report",
                 {"token": token, "items": items},
                 base_url, timeout, opener)


def demand_query_remote(admin_token: str, days: int = 84,
                       base_url: Optional[str] = None,
                       timeout: float = 10.0,
                       opener: Optional[Callable] = None) -> dict[str, Any]:
    """管理员查授权中心聚合的**桌面端**解析需求榜（nodes=["app"]）。

    授权中心是桌面 App 解析失败需求的统一收口（Mac/Win 经 demand_report 上报），
    worker 后台视图叠加这份数据，才能让「Windows 新需求」在「Mac 后台」也看得到。
    """
    return _post("/api/license/demand_query",
                 {"token": admin_token, "days": int(days or 84)},
                 base_url, timeout, opener)


def usage_overview_remote(admin_token: str, days: int = 30,
                         base_url: Optional[str] = None,
                         timeout: float = 10.0,
                         opener: Optional[Callable] = None) -> dict[str, Any]:
    """管理员查**全站**功能用量聚合（功能侧重调整的数据底座）。

    遍历所有账号的每日计数 + 历史归档，按 resource 汇总（不再只看本机 stats.json）。
    返回 {ok, days, by_resource:{res:n}, by_day:{date:{res:n}}, active_users}。
    """
    return _post("/api/license/usage_overview",
                 {"token": admin_token, "days": int(days or 30)},
                 base_url, timeout, opener)


def grant_remote(email: str, plan: str, admin_token: str, note: str = "",
                 base_url: Optional[str] = None,
                 timeout: float = 10.0) -> dict[str, Any]:
    """管理员在云端直接给账号开套餐（2026-10-16：后台「赠送」对云端账号生效）。

    plan 传套餐 code（如 download_month / ai_5500 / credits_5000）。仅用于本机
    没有镜像的账号（云端行）。
    """
    return _post("/api/license/grant",
                 {"token": admin_token, "email": email,
                  "plan": str(plan or "").strip(), "note": str(note or "")[:120]},
                 base_url, timeout)


def adjust_remote(admin_token: str, email: str, pool: str, delta: int, note: str = "",
                  base_url: Optional[str] = None, timeout: float = 8.0) -> dict[str, Any]:
    """管理员调整云端账号积分（可负=强扣）：pool ∈ permanent|ai。

    与 app-dev 版同签名（token 在前）：云端 ai 池改 ai_grant_total、permanent 改
    perm_credits；账号不存在时云端返回 NOT_FOUND。
    """
    return _post("/api/license/adjust",
                 {"token": admin_token, "email": email, "pool": pool,
                  "delta": int(delta), "note": note[:120]},
                 base_url, timeout)


# ---- 支付（支付宝，内地 ECS pay.hanyuxz.top /api/pay/*）----
PAY_BASE = "https://pay.hanyuxz.top"


def pay_create_remote(token: str, plan_code: str, client: str = "",
                      base_url: str = PAY_BASE, timeout: float = 15.0,
                      opener: Optional[Callable] = None) -> dict[str, Any]:
    """下单：拿支付二维码 / 收银台地址。

    返回 {ok, order_id, mode, qr_png, qr, pay_url, amount, plan_code}。

    client 显式声明 'desktop' | 'mobile'：支付服务据此决定收银台形态；不传时
    支付服务按 User-Agent 兜底判定，而本客户端 UA 是 VDL-LicenseClient/2.0，
    会被判为 desktop —— 因此**由后端转发时必须显式传 client**，否则移动端拿不到 wap。
    """
    payload: dict[str, Any] = {"token": token, "plan_code": plan_code}
    if client:
        payload["client"] = client
    return _post("/api/pay/create", payload, base_url, timeout, opener)


def pay_query_remote(order_id: str, base_url: str = PAY_BASE,
                     timeout: float = 8.0,
                     opener: Optional[Callable] = None) -> dict[str, Any]:
    """订单状态轮询。返回 {ok, order_id, status: PENDING|PAID|GRANT_FAILED}。"""
    return _post("/api/pay/query", {"order_id": order_id},
                 base_url, timeout, opener)
