"""server/routers/cloud_drive.py — 网页版百度网盘下载（服务器中转）。

用户 2026-10-25 拍板：网页版也做百度网盘，登录态存服务器（填一次以后不用再填）。

与桌面端（app-dev 那套）的三点差别 —— 这也是网页版必须另写的原因：
  1. 桌面端把 dlink 交给**本机**下载器去下；网页版的浏览器拿不到百度 cookie，
     所以由服务器带登录态去取文件，再**流式中转**给浏览器。
  2. 因此**绝不能**把 dlink / 百度 cookie 下发给浏览器：dlink 用服务端加密的
     不透明令牌承载（bduss_cred_store.seal_token），百度 cookie 一次都不出服务器。
  3. 登录态按用户存（bduss_cred_store），每人一份、互不可见。

下载的三个硬规矩（来自桌面端 2026-10-24 实测，别改）：
  · 直链域白名单：只认 *.baidupcs.com / *.pcs.baidu.com（否则等于让服务器
    变成任意 URL 的代理，SSRF）。
  · UA 用**浏览器 UA**，不能用 P2SP 客户端 UA —— P2SP UA + Range 会 403
    （错误码 31326「无权限」，极具误导性）。
  · **必须带 Range**（bytes=0-）：裸 GET 一律 403，错误码会伪装成 31326
    「无权限」或 31360「直链过期」。

限速说明：百度按**账号**总带宽限速，且多连接是负优化（实测 1 连最快），
所以这里固定单连接。实际速度 = min(百度给该账号的带宽, 服务器出口带宽)。
"""
from __future__ import annotations

import logging
import os
import re
import urllib.parse

from fastapi import APIRouter
from pydantic import BaseModel

import app
import baidu_cred_store
from cloud_drives import baidu

logger = logging.getLogger(__name__)

router = APIRouter()

# Python 内部错误的特征串 —— 这类文本绝不能原样甩给用户（2026-10-27 线上出现过
# 「百度网盘解析失败: 'str' object has no attribute 'get'」这种英文堆栈碎片）。
_PY_ERR_RE = re.compile(
    r"object has no attribute|not subscriptable|NoneType|positional argument|"
    r"unexpected keyword|Traceback|KeyError|IndexError|TypeError|AttributeError"
)


def _human_err(msg: object) -> str:
    """把后端异常洗成一句用户能看懂的中文。"""
    t = str(msg or "").strip()
    if not t:
        return "百度网盘解析失败，请稍后重试"
    if _PY_ERR_RE.search(t) or not re.search(r"[\u4e00-\u9fff]", t):
        return "百度网盘接口返回异常（可能触发了风控，或分享链接已失效），请稍后重试"
    return t[:120]

# 百度网盘直链域（与桌面端 downloader._NETDISK_HOST_RE 同一口径）
_NETDISK_HOST_RE = re.compile(r"(?:^|\.)(?:baidupcs\.com|pcs\.baidu\.com)$")


class CloudDriveResolveReq(BaseModel):
    url: str
    pwd: str = ""


class CloudDriveCredentialReq(BaseModel):
    bduss: str


def _host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _is_netdisk_direct(url: str) -> bool:
    """是否百度网盘直链域（白名单，防 SSRF）。"""
    h = _host_of(url)
    return bool(h) and bool(_NETDISK_HOST_RE.search(h))


def _uid(request) -> str:
    """取当前登录用户（未登录一律 403，且文案含「登录」以触发前端登录框）。"""
    from user_membership import require_login_user
    return require_login_user(request)


def _admin_login_cookie() -> str:
    """管理员提供的百度登录态（服务器侧统一一份，所有网页版用户共用）。

    2026-10-25 用户定档：「网盘 cookies 主要由管理员提供，用户自己获取的入口隐藏」。

    来源：环境变量 `VDL_BAIDU_BDUSS` —— 管理员/运维直接在服务器上配这一份。

    取到后顺手写回池里：这样「管理员那份」始终在共享池中，桌面端也能通过共享池
    拿到同一份账号（两端一份凭据，不用各填各的）。
    """
    env = (os.environ.get("VDL_BAIDU_BDUSS") or "").strip()
    if env:
        header = env if "=" in env else f"BDUSS={env}"
        try:
            from cookie_pool import add_cookie
            add_cookie("baidu.com", header, source="admin")
        except Exception:  # noqa: BLE001 — 入池失败不影响本次使用
            pass
        return header
    # 🔴 这里**不再**回退 get_cookie("pan.baidu.com")：那等于把「共享池最新一条」
    # 冒充成「管理员那份」，而池里可能只有百度搜索的匿名 Cookie（BAIDUID/PSTM，
    # 没有 BDUSS）—— 匿名态换直链必拿密文、下载必 403。池里的账号统一在
    # _login_cookies 里作为候选加入（会过滤掉无 BDUSS 的条目）。
    return ""


def _login_cookies(uid: str) -> list:
    """本次可用的百度登录态**候选列表**（首选在前，已去重）。

    为什么是列表：百度 2026-10-27 起只对**会员账号**（vip_type>0）返回明文
    下载直链，普通账号回的是 AES 密文串。池里同时存在会员号和普通号时，只取
    最新一条会随机踩到普通号 ⇒ 解析必报「返回了加密数据」。所以把池里所有
    账号都交给解析器逐个试（见 cloud_drives/baidu.py resolve_share）。

    顺序沿用老口径「管理员那份 > 用户自己存的 > 共享池」，池放最后是因为
    池里可能有普通号（会员号必须由解析器逐个试出来，见 resolve_share）。
    """
    cands: list = []
    admin = _admin_login_cookie()
    if admin:
        cands.append(admin)
    try:
        own = baidu_cred_store.load_bduss(uid) or ""
    except Exception:  # noqa: BLE001
        own = ""
    if own:
        cands.append(own)
    try:
        from cookie_pool import get_cookies
        cands.extend(get_cookies("pan.baidu.com") or [])
    except Exception:  # noqa: BLE001 — 池读失败不该挡住用户自己配的凭据
        logger.warning("[cloud_drive] read cookie pool failed", exc_info=True)

    # 只留含 BDUSS 的（= 真登录态）。池里常混入浏览器自动贡献的百度搜索匿名
    # Cookie（BAIDUID/BIDUPSID/PSTM），拿它换直链必得密文、下载必 403。
    out: list = []
    seen: set = set()
    for c in cands:
        if not c or "BDUSS=" not in c:
            continue
        key = ""
        for part in c.split(";"):
            k, _, v = part.partition("=")
            if k.strip() == "BDUSS":
                key = v.strip()
                break
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _login_cookie(uid: str) -> str:
    """单条登录态（下载中转等只需一条的场合用）。"""
    cands = _login_cookies(uid)
    return cands[0] if cands else ""


# ——————————————————————————————————————————————————————————————
# 解析
# ——————————————————————————————————————————————————————————————

@router.post('/api/cloud-drive/resolve')
def cloud_drive_resolve(req: CloudDriveResolveReq, request: app.Request) -> dict:
    """解析百度网盘分享链接 → 可下载文件列表。

    返回里**没有** dlink、**没有**百度 cookie：只有服务端签的不透明令牌 id、
    文件名、大小。真正的下载走 /api/cloud-drive/download。
    """
    uid = _uid(request)
    raw_input = (req.url or "").strip()
    if not raw_input:
        return {"ok": False, "error": "请填写百度网盘分享链接"}
    # 百度网盘「复制链接」给的是整段文案（标题 + 「链接:」 + 「提取码:」），
    # 用户整段粘贴是最自然的操作 ⇒ 先提取真正的 URL 与提取码，不再要求手动摘。
    url, text_pwd = baidu.extract_share_url(raw_input)
    if not url:
        return {"ok": False, "error": "链接格式不对，应是 https://pan.baidu.com/..."}

    cands = _login_cookies(uid)
    cookie = cands[0] if cands else ""
    try:
        files = baidu.resolve(url, cookie, (req.pwd or "").strip() or text_pwd, cands)
    except ValueError as e:
        return {"ok": False, "error": str(e), "need_credential": (not cookie)}
    except Exception as e:  # noqa: BLE001
        # exc_info：只打 str(e) 时看不出来是哪个环节炸的（2026-10-27 排查吃过亏）
        logger.warning("[cloud_drive] resolve failed: %s", str(e)[:200], exc_info=True)
        return {"ok": False, "error": f"百度网盘解析失败：{_human_err(e)}", "need_credential": (not cookie)}

    out = []
    for f in files:
        out.append({
            "id": baidu_cred_store.seal_token(uid, f.dlink),
            "name": f.name,
            "size": int(f.size or 0),
            "path": getattr(f, "path", "") or "",
            "need_login": bool(getattr(f, "need_login", False)),
        })
    need_login = any(x["need_login"] for x in out) or (not cookie)
    return {
        "ok": True,
        "platform": "baidupan",
        "files": out,
        # 文件夹分享是一次性展开的（默认最多 300 个文件），到顶了要说清楚，
        # 否则用户会以为大文件夹里只剩这点东西。
        "truncated": len(out) >= getattr(baidu, "_SHARE_MAX_FILES", 300),
        "need_credential": need_login,
        "need_login": need_login,
        "has_credential": bool(cookie),
    }


# ——————————————————————————————————————————————————————————————
# 登录态（存服务器，按用户）
# ——————————————————————————————————————————————————————————————

@router.get('/api/cloud-drive/credential/status')
def cloud_drive_credential_status(request: app.Request) -> dict:
    uid = _uid(request)
    return {
        "set": baidu_cred_store.has_bduss(uid),
        "configured": baidu_cred_store.configured(),
    }


@router.post('/api/cloud-drive/credential')
def cloud_drive_set_credential(req: CloudDriveCredentialReq, request: app.Request) -> dict:
    """保存百度登录态到服务器（加密落盘，权限 600，每用户一份）。"""
    uid = _uid(request)
    try:
        stored = baidu_cred_store.save_bduss(uid, (req.bduss or "").strip())
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except baidu_cred_store.CredentialError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "stored_in": "server", "preview": stored[:24]}


@router.delete('/api/cloud-drive/credential')
def cloud_drive_clear_credential(request: app.Request) -> dict:
    uid = _uid(request)
    return {"ok": True, "cleared": baidu_cred_store.clear_bduss(uid)}


# ——————————————————————————————————————————————————————————————
# 下载（服务器中转）
# ——————————————————————————————————————————————————————————————

def _range_start(raw: str) -> int:
    """取 `Range: bytes=N-` 里的起点 N；不是字节范围 / 没有起点时返回 0（视为从头）。

    只认「单段、开头锚定」这一种形态。百度直链只支持 `bytes=N-` 这种开区间续传，
    `bytes=-500`（尾部 N 字节）和 `bytes=0-99,200-299`（多段）都不支持，一律退回 0，
    宁可从头下一遍，也不要拿一段错位的数据去拼文件。
    """
    try:
        unit, _, spec = (raw or "").partition("=")
        if unit.strip().lower() != "bytes":
            return 0
        if "," in spec:  # 多段范围：不猜，整段从头重来
            return 0
        head = spec.split("-", 1)[0].strip()
        if not head.isdigit():
            return 0
        return int(head)
    except Exception:  # noqa: BLE001 — 解析失败按从头处理，不阻断下载
        return 0


@router.get('/api/cloud-drive/download')
def cloud_drive_download(request: app.Request):
    """按令牌取文件：服务器带登录态去百度取，再流式转发给浏览器。

    浏览器这边只看到本服务的普通文件下载，碰不到百度 cookie 和 dlink。
    """
    uid = _uid(request)
    token = (request.query_params.get("id") or "").strip()
    if not token:
        raise app.HTTPException(status_code=400, detail="缺少下载令牌")
    dlink = baidu_cred_store.open_token(uid, token)
    if not dlink:
        raise app.HTTPException(status_code=400, detail="下载令牌无效或已过期，请重新解析")
    if not _is_netdisk_direct(dlink):
        raise app.HTTPException(status_code=400, detail="下载地址不是百度网盘直链，已拒绝")

    cookie = _login_cookie(uid)
    if not cookie:
        raise app.HTTPException(status_code=403, detail="暂无可用的百度登录态，请联系管理员提供")

    name = (request.query_params.get("name") or "").strip() or "baidu-file"

    # —— 断点续传：浏览器带 Range 就从断点续，不带就从头 ——
    # 百度直链**裸 GET 一律 403**（还伪装成 31326/31360），所以无论如何都必须带 Range；
    # 区别只在于起点取浏览器的断点还是 0。实测这台机器出口带宽只有约 3 Mbps，
    # 400MB 级文件要下十几分钟，中途抖一下整包作废的代价太大 ⇒ 透传续传。
    _client_range = (request.headers.get("range") or "").strip()
    _resume_from = _range_start(_client_range)

    # —— 每日配额门禁（资源键 cloud_drive，独立计次，不污染普通下载配额）——
    # 免费 3 次/日、会员 200 次/日（见 membership.py）。预检超限抛 402 引导开通会员；
    # 放行即计数（同步流式中转没有「任务完成」回写点，请求即代表一次使用）。
    # 🔴 续传请求（起点 > 0）**不重复计次**：同一个文件重连十次也只算用了一次，
    #    否则网络每抖一次就扣一次，用户的每日次数会被白白吃光。
    _cd_ref = ""
    if not _resume_from:
        _cd_store = app.current_member_store(request)
        _cd_q = _cd_store.quota_state("cloud_drive")
        if not (_cd_q.get("unlimited") or _cd_q.get("unknown") or _cd_q.get("allowed", True)):
            if _cd_q.get("tier") == "free":
                _cd_detail = (f"今日免费网盘下载次数已用尽（{_cd_q['limit']}/日），"
                              f"开通VIP会员可解锁 {_cd_q.get('member_limit', 0)} 次/日")
            else:
                _cd_detail = f"今日网盘下载配额已用尽（{_cd_q['limit']}/日）"
            raise app.HTTPException(status_code=402, detail=_cd_detail)
        try:
            # 接住 ref：它是这次用量的幂等键。前端在文件**真正保存成功/失败**后
            # 拿它回写结果（服务端是流式中转，自己不知道用户磁盘上到底存成了没有）。
            _cd_use = _cd_store.use_daily("cloud_drive", 1, title=name, platform="百度网盘")
            _cd_ref = str((_cd_use or {}).get("ref") or "") if isinstance(_cd_use, dict) else ""
        except Exception:  # noqa: BLE001 — 计数失败 fail-open，绝不阻断下载
            pass

    # 复用网页版既有的媒体中继 /api/media/proxy（它已具备 SSRF 护栏、Range 透传、
    # Content-Length 回传、防盗链 Referer、浏览器 UA）。这里只额外做两件事：
    #   ① force_range：百度直链裸 GET 一律 403，必须带 Range —— 起点优先用浏览器的断点，
    #      没有断点才从 0 开始（同时保证 206 / Content-Range 被原样透传回去）；
    #   ② cookie：传服务器存的百度登录态（浏览器拿不到，只能由服务器带）。
    # dlink 不出现在 URL 里（令牌 → dlink 在服务端解开），百度 cookie 也不出服务器。
    from routers.core import media_proxy
    return media_proxy(u=dlink, cookie=cookie, dl=name, request=request,
                       force_range=_client_range or "bytes=0-", usage_ref=_cd_ref)
