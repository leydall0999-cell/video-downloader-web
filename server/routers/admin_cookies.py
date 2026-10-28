"""server/routers/admin_cookies.py — 后台「Cookie 池」管理（2026-10-19）。

为什么要有这一页
----------------
公共 Cookie 池是网页版解析的命脉：池里某个平台的登录态集体失效时，症状是
「网页版解析全失败」，但在后台看不见——现有接口只有 `verify_and_prune` 的
数字，没人知道**哪个平台的哪个账号**是死的、还剩几份、多久没更新。运营要
补 Cookie 也只能靠 ssh 上去看文件。

两个部署角色（同一份代码）
------------------------
  · **云端节点**（vdl-worker，网页版解析机）：池就在本机磁盘，直接读写。
  · **桌面 App**：池在云端，本机通常没有数据。所以后台默认走「代理到云端」，
    否则管理员看到的是空的 —— 这一条是本次最容易做错的地方：桌面端的本机池
    和网页版用的云端池是**两个池**。

  代理走 `POST /api/cookie/pool-admin`，鉴权复用既有的
  `VDL_COOKIE_SYNC_TOKEN`（桌面端 `~/.videodownloader/cloud_sync.json` 已配）。
  该接口只暴露脱敏信息（账号哈希 / 时间 / 状态），不返回任何 Cookie 明文。

四个动作
--------
  GET  /api/admin/cookies          —— 全平台概览（账号数 / 有效 / 无效 / 未校验 /
     过期 / 每账号保存多少项 Cookie），**默认不联网**，毫秒级返回。
  POST /api/admin/cookies/verify   —— 真联网验真（并发 + 缓存），B 站额外取昵称。
  POST /api/admin/cookies/inject   —— 粘贴 Cookie 立刻入池（入池前强制验真）。
  POST /api/admin/cookies/delete   —— 按条目删（清无效项）。

为什么 GET 不联网
----------------
10 个平台 × 每平台若干账号，串行验真要几十秒，页面会白屏、网关会超时。
所以「有效性」拆成两段：静态体检（解不出密文 / 已过期 / 缺登录字段）随取随有；
联网结论只来自显式点「立即校验」，并按条目缓存 30 分钟。

状态口径（三态，绝不混为一谈）
----------------------------
  valid   —— 站点明确回答「已登录」（或结构校验命中登录态字段，`probe=struct`）
  invalid —— 站点明确回答「未登录」；或本机静态判定为死（过期/解不开）
  unknown —— 没有可用探针 / 网络不可达。**unknown 不等于无效**，页面必须写明，
             否则运营会把好 Cookie 删掉。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

import atomic_io
import cookie_pool as cp
from routers.admin import require_admin, require_super

router = APIRouter()

# 验真结果「新鲜期」：跟联网冷却对齐（6 小时）。以前是 30 分钟，但云端校验
# 会从机房 IP 打扰用户的家宽会话，频率必须降下来——结论继续展示，只是不重打。
_VERIFY_TTL = 6 * 3600
_MAX_WORKERS = 6            # 并发探针数：再高容易触发平台风控（B 站 412）

# 🔴🔴 联网验真的最短间隔（2026-10-21 快手事故倒逼）：
# 云端是**机房 IP**，用户的登录 session 却在家宽浏览器里活着。同一份 session
# 在「家宽 + 机房」两个 IP 之间高频交替，平台风控会直接作废会话（快手实测：
# 后台每 5~10 分钟自动全量校验一批，10:22 还能打开自己主页，19:00 已回
# 「sid 不合法」）。结果就是死循环：用户重新登录 → 后台校验 → 被踢 → 显示无效。
# 结论：同一份 Cookie 6 小时内只联网验一次，手动点校验也至少隔 10 分钟。
_VERIFY_MIN_INTERVAL = 6 * 3600      # 自动/全量校验
_VERIFY_MANUAL_INTERVAL = 10 * 60    # 手动点「校验」
_STALE_SEC = 7 * 24 * 3600  # 超过 7 天没更新 ⇒ 标「陈旧」，提醒补注入

_VERIFY_LOCK = threading.Lock()

# 平台中文名（后台展示用）。白名单里还有几个「派生域」（xhslink 短链等），
# 没有独立登录态，统一归到主平台名下显示。
_TITLES: dict[str, str] = {
    "bilibili.com": "哔哩哔哩",
    "youtube.com": "YouTube",
    "douyin.com": "抖音",
    "iesdouyin.com": "抖音（分享页）",
    "kuaishou.com": "快手",
    "chenzhongtech.com": "快手（极速版）",
    "gifshow.com": "快手（小剧场）",
    "xiaohongshu.com": "小红书",
    "xhslink.com": "小红书（短链）",
    "xhslink.cn": "小红书（短链）",
    "youku.com": "优酷",
    "qq.com": "腾讯视频 / 微信",
    "tiktok.com": "TikTok",
    "instagram.com": "Instagram",
    "chrqj.com": "短剧站",
}

# 有独立登录态的「主平台」：注入下拉只列这些（短链域没有自己的 Cookie 意义）。
_PRIMARY = (
    "bilibili.com", "youtube.com", "douyin.com", "kuaishou.com",
    "xiaohongshu.com", "youku.com", "qq.com", "tiktok.com",
    "instagram.com", "chrqj.com",
)

# 结构校验通过 ≠ 联网验过。前端据此在状态旁标「结构」，避免运营误以为
# 「这些也真验过了」——B 站以外大多数站没有可用探针，只能到这一步。
_PROBE_NET = "net"
_PROBE_STRUCT = "struct"
_PROBE_NONE = "none"

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 小红书首页：页面 __INITIAL_STATE__ 里直接带 `"loggedIn": true|false`，
# 不需要 x-s 签名（edith 那套要签名，实测 406）。2026-10-21 实测：
# 游客页 36KB / loggedIn=false；带登录 Cookie 206KB / loggedIn=true + nickname + redId。
_XHS_EXPLORE = "https://www.xiaohongshu.com/explore"

# 抖音：`/aweme/v1/web/user/profile/self/` **不需要 a_bogus 签名**（2026-10-21 实测，
# 带 Cookie 直接回 JSON）。判定语义：status_code=0 且 user 非空 ⇒ 已登录
# （能读到 nickname / sec_uid，后台直接看到「池里是谁的号」）；status_code=8 且
# status_msg="用户未登录" ⇒ 这份 Cookie 已被服务端判定失效。
# 这条接口是抖音唯一免签名的自检入口，别再去找 im/user/info（回 url doesn't match）。
#
# 🔴 **`aid=6383` 必带，否则恒假阴性**（2026-10-21 线上踩坑）：裸 URL 或只带
# `device_platform=webapp` 时，同一份**真实登录中**的 Cookie 也回
# `status_code=8 / 用户未登录`，于是后台把刚同步上来的好 Cookie 全标成「无效」，
# 运营以为「浏览器明明登着却校验不出」。加 `?aid=6383` 后立刻回 status_code=0 +
# user{nickname}。实测矩阵（同一 Cookie、同一时刻）：
#   裸 URL                          → 8
#   ?device_platform=webapp         → 8
#   ?aid=6383                       → 0 ✅
#   ?aid=6383&device_platform=webapp→ 0 ✅
# 语义上 aid 是抖音的「应用 ID」，缺了等于匿名调用方，服务端不认这份 Cookie。
_DY_PROFILE_SELF = ("https://www.douyin.com/aweme/v1/web/user/profile/self/"
                    "?aid=6383&device_platform=webapp")

# 快手：GraphQL `visionProfile` **不校验 __NS_sig3 签名**（实测裸 POST 即返回 data）。
# 三个语义必须成对看（2026-10-21 同一台机同一时刻实测）：
#   空 Cookie（真匿名） → result=2,  userProfile=null
#   带失效 Cookie       → result=21, userProfile=null   ← 与匿名不同
#   已登录              → userProfile.profile.user_name 有值
# 21 是快手的「凭据不合法」码，不是「探针没生效」。独立通道
# /pass/kuaishou/login/passToken 回 {"result":100110085,"error_msg":"sid不合法"}
# 交叉印证。两者互为对照，才能排除「服务端压根没认这份 Cookie」的假阴性。
_KS_GRAPHQL = "https://www.kuaishou.com/graphql"
_KS_VISION_PROFILE = (
    "query visionProfile { visionProfile { result hostName"
    " userProfile { ownerCount { fan follow photo photo_public }"
    " profile { gender user_name user_id headurl user_text } } } }"
)
_KS_PASS_CHECK = "https://id.kuaishou.com/pass/kuaishou/login/passToken"


# ─── 基础工具 ────────────────────────────────────────────────────────────────

def _entry_id(header: str) -> str:
    """条目标识：明文 Cookie 的短哈希（稳定、不可逆，前端靠它删条目）。"""
    return hashlib.sha256((header or "").encode()).hexdigest()[:12]


def _parts_count(header: str) -> int:
    """这条 Cookie 头里有多少个 k=v（= 管理员说的「这个账号保存了多少项 Cookie」）。"""
    n = 0
    for part in (header or "").split(";"):
        k, sep, _ = part.strip().partition("=")
        if sep and k:
            n += 1
    return n


def _title(domain: str) -> str:
    d = cp._strip_sub(domain)
    return _TITLES.get(d) or d


def _cache_file() -> Path:
    """验真缓存放在池目录**旁边**，不放进池目录：池目录里每个 *.json 都当域文件，
    混一个 `_verify.json` 进去会让「目录里有什么」和「池里有什么」对不上。"""
    return Path(cp._POOL_DIR).parent / "cookie_pool_verify.json"


def _load_cache() -> dict:
    try:
        f = _cache_file()
        if f.exists():
            d = json.loads(f.read_text())
            if isinstance(d, dict) and isinstance(d.get("items"), dict):
                return d
    except Exception:
        pass
    return {"ts": 0, "items": {}}


def _save_cache(items: dict, checked_at: int) -> None:
    try:
        f = _cache_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        atomic_io.atomic_write_json(f, {"ts": checked_at, "items": items}, indent=None)
    except Exception:
        # 缓存写不进去不影响主流程（下次打开重新验真而已），但要留痕
        try:
            import logging
            logging.getLogger(__name__).warning("[admin_cookies] save cache failed", exc_info=True)
        except Exception:
            pass


# ─── 读池（不联网）────────────────────────────────────────────────────────────

def _read_domain(domain: str, now: float, cache: dict) -> dict:
    """读一个域（含 www 变体）的全部条目，做静态体检，不联网。"""
    entries: list[dict] = []
    seen_files: set = set()
    broken = 0

    for host in cp._candidates(domain):
        f = cp._pool_file(host)
        try:
            key = str(f.resolve())
        except Exception:
            key = str(f)
        if key in seen_files or not f.exists():
            continue
        seen_files.add(key)
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue

        for c in data.get("cookies") or []:
            header = cp._decrypt_item(c)
            if not header:
                # 解不开的密文（换机 / 密钥不匹配 / 历史写坏）：必须暴露出来，
                # 否则管理员只看到「条数变少」却不知道为什么。
                broken += 1
                continue
            eid = _entry_id(header)
            ts = int(c.get("ts") or 0)
            age = max(0, int(now - ts)) if ts else 0
            # 「最近一次使用」：get_cookie 命中时由 cookie_pool._mark_used 落盘。
            # 0 = 从未被使用过（不是「刚刚用过」），页面必须区分这两种。
            used_ts = int(c.get("used_ts") or 0)
            used_age = max(0, int(now - used_ts)) if used_ts else 0
            ident = cp._entry_identity(host, c) or ""
            cached = cache.get(f"{domain}|{eid}") or {}
            status = str(cached.get("status") or "")
            detail = str(cached.get("detail") or "")
            probe = str(cached.get("probe") or _PROBE_NONE)
            verified_ts = int(cached.get("ts") or 0)

            expired = bool(ts) and (now - ts) > cp._TTL
            suspect = False          # 未联网确认、但静态看像游客态
            if expired:
                # 过期优先于任何历史结论：池的读取与裁剪都按 30 天 TTL 算，
                # 一条「曾经有效」的过期货在页面标绿会误导运营。
                status, detail, probe = "invalid", "已超过 30 天有效期", probe or _PROBE_NONE
            elif status not in ("valid", "invalid", "unknown"):
                # 没有缓存结论：先做本机静态判定（缺登录态字段 = 疑似游客态）
                st, why = _static_status(domain, header)
                status, detail = st, why
                probe = _PROBE_NONE
                verified_ts = 0
            elif status == "unknown":
                # 联网给不出结论（该站没探针）时，补一条静态旁证：状态仍保持
                # unknown（诚实：没验过就不是「无效」），但如果连常见登录字段都
                # 没有，得让运营看见——腾讯那 3 份 20 项 Cookie 全是游客态，
                # 只写「未校验」会被当成「还能用」。
                st, why = _static_status(domain, header)
                if st == "invalid":
                    detail = "；".join([x for x in (detail, why) if x])
                    suspect = True

            entries.append({
                "id": eid,
                "host": host,
                "ident": ident,
                "parts": _parts_count(header),
                "ts": ts,
                "age_sec": age,
                "used_ts": used_ts,
                "used_age_sec": used_age,
                "source": str(c.get("source") or ""),
                "status": status,
                "detail": detail,
                "probe": probe,
                "verified_ts": verified_ts,
                "expired": expired,
                "suspect": suspect,
                # 「结论是从同会话的旧快照继承来的」——前端可据此显示更淡的徽标，
                # 运营点「立即校验」后会被真实结论覆盖。
                "inherited": False,
                # 临时字段：第二阶段的同会话继承要用，聚合前 pop 掉，不外泄。
                "_sk": _session_key_safe(host, header),
            })

    # ─── 新快照继承「同会话」已有结论 ────────────────────────────────────
    # 浏览器每同步上来一次就多一份快照（内容/时间不同、会话相同），而校验缓存
    # 按 entry_id 存 ⇒ 新快照必然 miss，页面把它显示成「未校验」——哪怕它所属
    # 的会话 2 分钟前刚被验成「有效」。2026-10-21 线上实测：小红书 3 份里 2 份
    # valid、最新那份 unknown；YouTube 3 份全 unknown 且都诞生于上次校验之后。
    # 运营看到「1 个账号 · 3 份（未校验 3）」只会以为整批都废了。
    #
    # 继承的边界（缺一不可）：
    #   · 只认同一个 `_session_key`（身份 + 会话字段哈希）。换账号或重新登录 ⇒
    #     会话字段变 ⇒ key 变 ⇒ 不会把新登录误标成「有效」。
    #   · 只继承有真实校验结论（verified_ts>0）的条目，不继承静态猜测。
    #   · 本地静态已判 invalid（缺登录字段）的不继承：那种 Cookie 不会是同一会话。
    donors: dict[str, dict] = {}
    for e in entries:
        if not e["verified_ts"] or e["status"] not in ("valid", "invalid"):
            continue
        cur = donors.get(e["_sk"])
        if cur is None or e["ts"] >= cur["ts"]:
            donors[e["_sk"]] = e
    for e in entries:
        if e["verified_ts"] or e["status"] != "unknown":
            continue
        d0 = donors.get(e["_sk"])
        if not d0:
            continue
        mins = max(0, int((now - d0["verified_ts"]) / 60))
        when = "刚刚" if mins < 1 else f"{mins} 分钟前"
        e["status"] = d0["status"]
        e["detail"] = f"同会话{when}已校验：{d0['detail'] or d0['status']}"
        e["probe"] = d0["probe"]
        e["verified_ts"] = d0["verified_ts"]
        e["inherited"] = True
    for e in entries:
        e.pop("_sk", None)

    # 按账号聚合：管理员关心「这个号还能用吗、存了几份、多少项」
    groups: dict[str, list[dict]] = {}
    for e in entries:
        groups.setdefault(e["ident"] or "anon", []).append(e)

    accounts: list[dict] = []
    for ident, items in groups.items():
        items.sort(key=lambda x: x["ts"], reverse=True)
        latest = items[0]
        # 账号状态取「最新一份」的结论：同一账号的多份快照是同一会话的不同时刻，
        # 最新那份最能代表现在能不能用。
        # 「最近一次使用」取所有快照里最新的那次：任意一份被用过，这个号就在服役。
        used_ts = max([e["used_ts"] for e in items], default=0)
        used_age = max(0, int(now - used_ts)) if used_ts else 0
        accounts.append({
            "ident": ident,
            "label": _ident_label(ident),
            "snapshots": len(items),
            "parts": latest["parts"],
            "ts": latest["ts"],
            "age_sec": latest["age_sec"],
            "used_ts": used_ts,
            "used_age_sec": used_age,
            "source": latest["source"],
            "status": latest["status"],
            "detail": latest["detail"],
            "probe": latest["probe"],
            "verified_ts": latest["verified_ts"],
            "suspect": bool(latest.get("suspect")),
            "inherited": bool(latest.get("inherited")),
            "entries": [
                {k: it[k] for k in ("id", "host", "parts", "ts", "age_sec",
                                    "used_ts", "used_age_sec",
                                    "source", "status", "detail", "probe",
                                    "verified_ts", "suspect", "inherited")}
                for it in items
            ],
        })
    accounts.sort(key=lambda a: (a["status"] != "valid", -a["ts"]))

    def _count(st: str) -> int:
        return sum(a["snapshots"] for a in accounts if a["status"] == st)

    last_ts = max([a["ts"] for a in accounts], default=0)
    return {
        "domain": domain,
        "title": _title(domain),
        # 「账号数」按登录身份去重；「条目数」= 池里实际存了几份快照
        "accounts": len(accounts),
        "entries": sum(a["snapshots"] for a in accounts),
        "valid": _count("valid"),
        "invalid": _count("invalid"),
        "unknown": _count("unknown"),
        # 「未联网确认但静态看像游客态」的份数：既不能算无效（没验过），
        # 也不能算「还行」。单独拎出来，只用于决定要不要提醒补注入。
        "suspect": sum(a["snapshots"] for a in accounts if a.get("suspect")),
        "broken": broken,
        "last_ts": last_ts,
        "stale": bool(last_ts) and (now - last_ts) > _STALE_SEC,
        "items": accounts,
    }


def _session_key_safe(host: str, header: str) -> str:
    """`cp._session_key` 的安全包装。

    概览是「只读展示」入口，一条形态异常的 Cookie 不该让整个后台面板 500。
    失败时退化成该条目自己的 hash（唯一 ⇒ 谁都不匹配 ⇒ 不继承），
    绝不能用固定串兜底：那会让所有异常条目共享 key 并互相继承。
    """
    try:
        return cp._session_key(host, header)
    except Exception:
        return "?" + _entry_id(header)


def _ident_label(ident: str) -> str:
    """把 `bilibili.com:DedeUserID=1a2b3c4d5e` 变成可读短标签。

    故意不显示原始 ID：后台截图/投屏时不该出现能直接对应到真人的账号标识，
    哈希前 8 位足够区分「这是不同的号」。
    """
    if not ident or ident == "anon":
        return "游客 / 无身份字段"
    tail = ident.split("=", 1)[-1] if "=" in ident else ident
    return "账号 " + (tail[:8] or "?")


_LOGIN_FIELDS_HINT: dict[str, tuple[str, ...]] = {
    "bilibili.com": ("SESSDATA",),
    "youtube.com": ("SID", "APISID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID"),
    # 🔴 只认「登录后才下发」的会话凭证。passport_csrf_token 是 CSRF 令牌，
    # **游客态同样会下发**——拿它当登录判据，游客 Cookie 会被判成「含登录字段」
    # 从而永远停在 unknown（2026-10-21 实测抖音游客 Cookie 里就带着它）。
    "douyin.com": ("sessionid", "sessionid_ss", "sid_tt", "uid_tt", "sid_guard"),
    "iesdouyin.com": ("sessionid", "sessionid_ss", "sid_tt", "uid_tt"),
    "kuaishou.com": ("passToken", "userId", "kuaishou.webid"),
    "chenzhongtech.com": ("passToken", "userId"),
    # 同理：a1 是设备指纹（游客也发），只有 web_session 才是登录会话
    "xiaohongshu.com": ("web_session",),
    "youku.com": ("P__yk__uck", "yktk", "unb"),
    "qq.com": ("vus_session", "uin", "wxuin", "skey", "wap_sid2"),
    "tiktok.com": ("sessionid", "sid_tt"),
    "instagram.com": ("sessionid",),
}


def _static_status(domain: str, header: str) -> tuple[str, str]:
    """本机静态判定：只认「登录后才存在的字段」，缺了就是游客态。

    返回 (status, detail)。这里**只下 invalid 结论，不下 valid** —— 字段存在
    不等于站点认（Cookie 可能早被服务端吊销），所以字段齐全时给 unknown，
    等联网校验说话。这条界线是 2026-10-18 那次事故的教训：1902 条游客态
    Cookie 全都「字段看着像登录态」，只有问站点才分得出来。
    """
    d = cp._strip_sub(domain)
    fields = _LOGIN_FIELDS_HINT.get(d)
    if not fields:
        return "unknown", "该站无本机判定规则"
    keys: set[str] = set()
    for part in (header or "").split(";"):
        k = part.split("=", 1)[0].strip()
        if k:
            keys.add(k)
    if not (keys & set(fields)):
        return "invalid", "缺登录态字段（疑似未登录/游客态）"
    return "unknown", "含登录态字段，未经站点确认"


# ─── 联网验真 ────────────────────────────────────────────────────────────────

def _probe(domain: str, header: str) -> dict:
    """真跑一次探针，返回 {status, detail, probe}。

    B 站单独走 nav：它能同时给出 `isLogin` 和昵称，后台能直接看到「池里是谁的号」，
    比一串哈希有用得多。其余站沿用 cookie_pool.verify_cookie 的口径。
    """
    d = cp._strip_sub(domain)
    if d == "bilibili.com" and cp.requests is not None:
        try:
            r = cp.requests.get(cp._BILI_NAV, headers={
                "Cookie": header,
                "User-Agent": cp._CHRQJ_UA,
                "Referer": "https://www.bilibili.com/",
            }, timeout=8)
            if r.status_code != 200:
                return {"status": "unknown", "detail": f"站点返回 HTTP {r.status_code}（可能风控）",
                        "probe": _PROBE_NET}
            data = r.json()
            code = data.get("code")
            if code == 0:
                info = data.get("data") or {}
                if info.get("isLogin"):
                    uname = str(info.get("uname") or "").strip()
                    vip = (info.get("vipStatus") == 1) or (info.get("vip") or {}).get("status") == 1
                    bits = [b for b in (uname, "大会员" if vip else "") if b]
                    return {"status": "valid", "detail": " · ".join(bits) or "已登录",
                            "probe": _PROBE_NET}
                return {"status": "invalid", "detail": "站点判定：未登录", "probe": _PROBE_NET}
            if code == -101:
                return {"status": "invalid", "detail": "站点判定：账号未登录", "probe": _PROBE_NET}
            return {"status": "unknown", "detail": f"站点返回 code={code}", "probe": _PROBE_NET}
        except Exception as e:
            return {"status": "unknown", "detail": f"网络不可达（{type(e).__name__}）",
                    "probe": _PROBE_NET}

    # 小红书：首页「探索页」的 __INITIAL_STATE__ 里带 `"loggedIn": true|false`，
    # 纯 HTTP 就能取，不需要 x-s 签名（edith 的 user/me 要签名，实测 406）。
    # 实测判据极干净：游客页 36KB + loggedIn=false；带登录 Cookie 206KB +
    # loggedIn=true 且能读到 nickname / redId。这是除 B 站 nav 之外唯一
    # 「能直接看到池里是谁的号」的探针，比字段猜测可靠得多。
    if d == "xiaohongshu.com" and cp.requests is not None:
        try:
            r = cp.requests.get(
                _XHS_EXPLORE,
                headers={
                    "Cookie": header,
                    "User-Agent": _UA,
                    "Accept": "text/html,application/xhtml+xml",
                },
                timeout=12,
            )
            if r.status_code != 200:
                return {"status": "unknown",
                        "detail": f"站点返回 HTTP {r.status_code}（可能风控）",
                        "probe": _PROBE_NET}
            m = re.search(r'"loggedIn"\s*:\s*(true|false)', r.text)
            if not m:
                return {"status": "unknown",
                        "detail": "页面无 loggedIn 标记（小红书前端结构可能已变）",
                        "probe": _PROBE_NET}
            if m.group(1) == "true":
                nk = re.search(r'"nickname"\s*:\s*"([^"]{1,30})"', r.text)
                rid = re.search(r'"redId"\s*:\s*"([^"]{2,30})"', r.text)
                bits = [b for b in (
                    (nk.group(1) if nk else ""),
                    (f"小红书号 {rid.group(1)}" if rid else ""),
                ) if b]
                return {"status": "valid",
                        "detail": " · ".join(bits) or "已登录",
                        "probe": _PROBE_NET}
            return {"status": "invalid",
                    "detail": "站点判定：未登录（loggedIn=false）",
                    "probe": _PROBE_NET}
        except Exception as e:
            return {"status": "unknown",
                    "detail": f"网络不可达（{type(e).__name__}）",
                    "probe": _PROBE_NET}

    # 抖音：唯一免签名的自检入口。/aweme/v1/web/user/profile/self/ 直接回
    # status_code=8 +「用户未登录」或 status_code=0 + user{nickname,sec_uid}。
    # 注意这个结论**不能**由静态字段推出：抖音游客态也发 sessionid 之外的一堆
    # 反爬 Cookie，而 8 月登录留下的 sessionid 会一直躺在浏览器里（2026-10-21
    # 实测那份 login_time 已 66 天，接口明确回「用户未登录」）。
    if d == "douyin.com" and cp.requests is not None:
        try:
            r = cp.requests.get(
                _DY_PROFILE_SELF,
                headers={
                    "Cookie": header,
                    "User-Agent": _UA,
                    "Referer": "https://www.douyin.com/",
                    "Accept": "application/json, text/plain, */*",
                },
                timeout=12,
            )
            if r.status_code != 200:
                return {"status": "unknown",
                        "detail": f"站点返回 HTTP {r.status_code}（可能风控）",
                        "probe": _PROBE_NET}
            data = r.json()
            code = data.get("status_code")
            msg = str(data.get("status_msg") or "")
            user = data.get("user") or {}
            if code == 0 and user:
                nk = str(user.get("nickname") or "").strip()
                return {"status": "valid",
                        "detail": nk or "已登录",
                        "probe": _PROBE_NET}
            if code == 8 or "未登录" in msg:
                return {"status": "invalid",
                        "detail": "站点判定：未登录（Cookie 已失效，需在抖音重新登录）",
                        "probe": _PROBE_NET}
            return {"status": "unknown",
                    "detail": f"站点返回 status_code={code}",
                    "probe": _PROBE_NET}
        except Exception as e:
            return {"status": "unknown",
                    "detail": f"网络不可达（{type(e).__name__}）",
                    "probe": _PROBE_NET}

    # 快手：GraphQL visionProfile 免签名。判据必须成对看 result：
    # 2 = 真匿名（服务端没认到凭据）；21 = 认到了但凭据不合法。
    # 单看 21 会把「服务端压根没认 Cookie」误读成「失效」，所以再用
    # /pass/kuaishou/login/passToken 交叉验证一次（回 error_msg 含「sid不合法」）。
    if d == "kuaishou.com" and cp.requests is not None:
        try:
            r = cp.requests.post(
                _KS_GRAPHQL,
                data=json.dumps({"query": _KS_VISION_PROFILE}),
                headers={
                    "Cookie": header,
                    "User-Agent": _UA,
                    "Referer": "https://www.kuaishou.com/",
                    "Origin": "https://www.kuaishou.com",
                    "Content-Type": "application/json",
                    "Accept": "*/*",
                },
                timeout=15,
            )
            if r.status_code != 200:
                return {"status": "unknown",
                        "detail": f"站点返回 HTTP {r.status_code}（可能风控）",
                        "probe": _PROBE_NET}
            data = r.json()
            vp = ((data.get("data") or {}).get("visionProfile") or {})
            prof = ((vp.get("userProfile") or {}).get("profile") or {})
            name = str(prof.get("user_name") or "").strip()
            result = vp.get("result")
            if name:
                return {"status": "valid", "detail": name, "probe": _PROBE_NET}
            if result in (21, 2):
                # 21 = 服务端认了凭据但失效；2 = 完全匿名。两者差异说明探针真的
                # 把 Cookie 发出去了（同一时刻空 Cookie 只会回 2）。
                why = ("凭据已失效" if result == 21 else "匿名请求（未带有效登录凭据）")
                # 交叉验证：排除「服务端没认到 Cookie」被误判成失效。
                try:
                    rc = cp.requests.get(
                        _KS_PASS_CHECK,
                        headers={"Cookie": header, "User-Agent": _UA,
                                 "Accept": "application/json"},
                        timeout=10,
                    )
                    cj = rc.json() if rc.status_code == 200 else {}
                    if str(cj.get("error_msg") or "").find("sid不合法") >= 0:
                        why += "（passToken 检查：sid 不合法）"
                except Exception:
                    pass
                return {"status": "invalid",
                        "detail": f"站点判定：{why}，需在快手重新登录",
                        "probe": _PROBE_NET}
            return {"status": "unknown",
                    "detail": f"站点返回 result={result}",
                    "probe": _PROBE_NET}
        except Exception as e:
            return {"status": "unknown",
                    "detail": f"网络不可达（{type(e).__name__}）",
                    "probe": _PROBE_NET}

    try:
        ok = cp.verify_cookie(domain, header)
    except Exception as e:
        return {"status": "unknown", "detail": f"探针异常（{type(e).__name__}）", "probe": _PROBE_NET}
    if ok is True:
        # verify_cookie 对 youtube/优酷/腾讯 走的是结构校验（不联网），
        # 这里必须标 struct，否则后台会把「有字段」当成「站点确认过」。
        struct_only = cp._structural_check(d, header) is True
        return {
            "status": "valid",
            "detail": "含登录态字段（未连站确认）" if struct_only else "探针通过",
            "probe": _PROBE_STRUCT if struct_only else _PROBE_NET,
        }
    if ok is False:
        return {"status": "invalid", "detail": "站点判定：无效", "probe": _PROBE_NET}
    # 走到这里 = 该站没有可用的联网探针。2026-10-21 实测确认这不是「待补」而是
    # 客观限制：抖音/快手登录态接口需 a_bogus/X-Bogus 签名（直连一律 403 或
    # 「url doesn't match」）；小红书 edith 接口 406（需 x-s）；腾讯
    # access.video.qq.com 回 35004；TikTok 在国内机房根本连不上。
    # 所以此时唯一可靠的判据是**静态登录字段**——登录后必然带
    # sessionid / web_session / uin / skey，一个都没有即可确定是游客态。
    # 这类结论不需要联网，也不该继续挂着「未校验」误导运营。
    st, why = _static_status(domain, header)
    if st == "invalid":
        return {"status": "invalid",
                "detail": "Cookie 内无任何登录态字段（可确定未登录）；请在该站登录后重新注入",
                "probe": _PROBE_STRUCT}
    return {"status": "unknown",
            "detail": "含登录态字段，但该站无公开验真接口（需签名/需真实浏览器），无法联网确认",
            "probe": _PROBE_NONE}


def _collect_targets(now: float) -> list[tuple[str, list[str], str]]:
    """收集待验真的 (domain, [entry_id...], header)。

    两件事必须同时做对，否则页面会出现「验过了但还是未校验」：

    1. **同一会话只验一次**：同账号多份快照共享同一有效性，逐条验真既浪费又会
       触发平台风控（B 站 1905 条那次就是被自己的轮询打成 412）。
    2. **结论回写到该会话的每一个 entry_id**：池文件是「旧的在前、新的在后」，
       同一会话可能存了 3 份快照；只给其中一份（尤其只给最旧那份）写结论，
       页面按「最新一份」展示时就会命中不到缓存，显示成未校验 —— 2026-10-19
       实测就踩了这个：B 站 3 份里只有最旧的写进了缓存。

    取**最新那份**的 header 去验：不同快照的 buvid 会变，最新那份最接近用户
    此刻的浏览器状态。
    """
    groups: dict[str, dict] = {}
    for domain in sorted(cp._root_domains()):
        seen_files: set = set()
        for host in cp._candidates(domain):
            f = cp._pool_file(host)
            try:
                key = str(f.resolve())
            except Exception:
                key = str(f)
            if key in seen_files or not f.exists():
                continue
            seen_files.add(key)
            try:
                data = json.loads(f.read_text())
            except Exception:
                continue
            for c in data.get("cookies") or []:
                header = cp._decrypt_item(c)
                if not header:
                    continue
                ts = int(c.get("ts") or 0)
                if ts and (now - ts) > cp._TTL:
                    continue  # 过期的不必再打平台
                sk = f"{domain}|{cp._session_key(host, header)}"
                g = groups.get(sk)
                if g is None:
                    groups[sk] = {"domain": domain, "ids": [_entry_id(header)],
                                  "header": header, "ts": ts}
                else:
                    g["ids"].append(_entry_id(header))
                    if ts >= g["ts"]:
                        g["ts"] = ts
                        g["header"] = header
    return [(g["domain"], g["ids"], g["header"]) for g in groups.values()]


# ─── 请求体规范化 ────────────────────────────────────────────────────────────

def _normalize_header(raw: Any) -> str:
    """把管理员粘贴的各种形态统一成一行 `k=v; k2=v2`。

    支持三种真实来源：
      · DevTools → Network → 请求头里的 `Cookie: xxx`
      · Application → Cookies 表里逐行抄出来的 `k=v` 多行
      · 「Copy as cURL」整段（从中抽 -H 'cookie: ...'）

    最后强制拒绝换行：这条字符串会直接进 `Cookie:` 请求头发给平台，
    带 CRLF 等于请求头注入。
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    # copy as cURL：-H 'cookie: a=b; c=d' / --header "Cookie: ..."
    m = re.search(r"""(?:-H|--header)\s+(['"])cookie\s*:\s*(.*?)\1""", s, re.I | re.S)
    if m:
        s = m.group(2)
    # 去掉可能残留的 `Cookie:` 前缀
    s = re.sub(r"^\s*cookie\s*:\s*", "", s, flags=re.I)
    # 多行（每行一个 k=v）：拼成一条。
    # 🔴 每行必须严格是 `name=value`。这条字符串最终会进 `Cookie:` 请求头，
    # 若把 `X-Injected: yes` 这种行也「拼接」进来，等于承认任意文本可以混入
    # 请求头；单行形态无法区分它，所以多行一律按 k=v 白名单校验，不合格整条拒绝。
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    if "\n" in s:
        parts = []
        for line in s.split("\n"):
            line = line.strip().rstrip(";").strip()
            if not line:
                continue
            if not re.match(r"^[^=;:\s]+=.*$", line):
                return ""
            parts.append(line)
        s = "; ".join(parts)
    # 统一分隔符空白，去掉空段
    s = re.sub(r"\s*;\s*", "; ", s).strip().strip(";").strip()
    if "\n" in s or "\r" in s:
        return ""
    # 至少要有一个 k=v
    if "=" not in s:
        return ""
    return s


# ─── 业务动作（本地 / 云端共用）───────────────────────────────────────────────

def _overview() -> dict:
    """池的全平台概览。不联网，只读文件 + 缓存。"""
    now = time.time()
    cache_doc = _load_cache()
    cache_items = cache_doc.get("items") or {}
    checked_at = int(cache_doc.get("ts") or 0)

    domains: list[dict] = []
    for d in sorted(cp._root_domains()):
        row = _read_domain(d, now, cache_items)
        # 池里没有文件的域不占屏（15 个白名单域里常年只有 8~10 个有货）
        if row["entries"] or row["broken"]:
            domains.append(row)

    total = {
        "accounts": sum(r["accounts"] for r in domains),
        "entries": sum(r["entries"] for r in domains),
        "valid": sum(r["valid"] for r in domains),
        "invalid": sum(r["invalid"] for r in domains),
        "unknown": sum(r["unknown"] for r in domains),
        "broken": sum(r["broken"] for r in domains),
    }
    # 「缺口」平台：真正需要有人去补 Cookie 的。判据要克制，否则名单会变成
    # 全站都在催——抖音那种「有 51 项 Cookie、只是该站没有联网探针」不该被当成
    # 缺口（用户补一次也还是 unknown）。只认三种：
    #   ① 超 7 天没更新（Cookie 大概率已过期）  ② 有失效项且一份都不有效
    #   ③ 未联网确认、但连常见登录字段都没有（静态看就是游客态）
    gaps = [r["domain"] for r in domains
            if r["stale"] or (r["invalid"] and not r["valid"]) or (r.get("suspect") and not r["valid"])]

    return {
        "ok": True,
        "now": int(now),
        "pool_dir": str(cp._POOL_DIR),
        "checked_at": checked_at,
        "check_age_sec": max(0, int(now) - checked_at) if checked_at else 0,
        "verify_ttl": _VERIFY_TTL,
        "summary": total,
        "gaps": gaps,
        "domains": domains,
        # 注入下拉用：带中文名、只列有独立登录态的主平台
        "injectable": [{"domain": d, "title": _title(d)} for d in _PRIMARY],
    }


def _do_verify(only_domain: str = "", *, force: bool = False) -> dict:
    """真联网验真（全量或指定域），结果写缓存。

    `force=True` 跳过冷却（运营明确要求「现在就重新验一次」时用）。默认不强制：
    云端从机房 IP 反复探同一会话会被平台风控当作盗号，直接作废会话。
    """
    now = time.time()
    targets = _collect_targets(now)
    if only_domain:
        only_root = cp._strip_sub(only_domain)
        targets = [t for t in targets if cp._strip_sub(t[0]) == only_root]

    cache_items = dict((_load_cache().get("items") or {}))
    started = time.time()

    # 冷却：结论还新鲜就别再打平台（见 _VERIFY_MIN_INTERVAL 注释）。
    # 🔴 按「会话」(sk) 而不是 entry_id 算：一次登录会被同步成很多份快照，
    # 每份的 eid 都不同（ktrace-context 之类每次都变），按 eid 算等于没冷却。
    # 会话变了（重新登录 ⇒ passToken/sessionid 变）⇒ 新 sk ⇒ 立刻放行。
    sk_ts: dict[str, int] = {}
    for _v in cache_items.values():
        if isinstance(_v, dict) and _v.get("sk"):
            _sk = str(_v["sk"])
            sk_ts[_sk] = max(sk_ts.get(_sk, 0), int(_v.get("ts") or 0))
    interval = _VERIFY_MANUAL_INTERVAL if only_domain else _VERIFY_MIN_INTERVAL
    pending: list[tuple[str, list[str], str]] = []
    skipped = 0
    for t in targets:
        sk = _session_key_safe(t[0], t[2])
        pts = max(sk_ts.get(sk, 0),
                  int((cache_items.get(f"{t[0]}|{t[1][0]}") or {}).get("ts") or 0))
        if not force and pts and (now - pts) < interval:
            skipped += 1
            continue
        pending.append(t)
    targets = pending

    def _run(t: tuple[str, list[str], str]) -> list[tuple[str, dict]]:
        domain, eids, header = t
        try:
            res = _probe(domain, header)
        except Exception as e:  # 单条失败不拖垮整批
            res = {"status": "unknown", "detail": f"异常（{type(e).__name__}）",
                   "probe": _PROBE_NET}
        res = dict(res)
        res["ts"] = int(now)
        # 记下会话指纹，下一轮冷却才能按会话命中（而不是按易变的 eid）
        res["sk"] = _session_key_safe(domain, header)
        # 同一会话的每一份快照都写同一个结论：页面按最新一份展示也能命中
        return [(f"{domain}|{eid}", res) for eid in eids]

    if targets:
        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(targets))) as pool:
            for pairs in pool.map(_run, targets):
                for key, res in pairs:
                    cache_items[key] = res

    with _VERIFY_LOCK:
        _save_cache(cache_items, int(now))

    counted = {"valid": 0, "invalid": 0, "unknown": 0}
    for t in targets:
        # 按「会话」计数（不是按快照）：checked 表示这次真打了几次平台
        first = cache_items.get(f"{t[0]}|{t[1][0]}") or {}
        st = str(first.get("status") or "unknown")
        counted[st if st in counted else "unknown"] += 1

    return {
        "ok": True,
        "checked": len(targets),
        "skipped": skipped,     # 因冷却跳过（结论仍用缓存，不是失败）
        "elapsed": round(time.time() - started, 1),
        **counted,
    }


def _do_inject(domain: str, raw_header: Any) -> dict:
    """粘贴 Cookie → 验真 → 入池。

    入池前**强制**验真（`add_cookie` 内部同样会验），明确无效直接拒：池是给所有
    用户共用的，一条死 Cookie 混进去会让 `get_cookie` 取到它、把整站解析带崩
    （2026-10-18 的 B 站事故就是这么来的）。
    """
    domain = str(domain or "").strip()
    header = _normalize_header(raw_header)
    if not domain or not cp.is_allowed(domain):
        return {"ok": False, "error": "域名不在公共 Cookie 池白名单内"}
    if not header:
        return {"ok": False, "error": "Cookie 为空，或格式无法识别（需要 k=v; k2=v2）"}

    if "RELAYTEST" in header:
        return {"ok": False, "error": "这是健康检查占位 Cookie，不是真实登录态"}

    probe = _probe(domain, header)
    if probe.get("status") == "invalid":
        return {"ok": False, "error": f"Cookie 无效：{probe.get('detail') or '站点判定未登录'}",
                "probe": probe.get("probe"), "parts": _parts_count(header)}

    ok = cp.add_cookie(domain, header, source="admin")
    if ok:
        # 入池成功后把这条的结论写进缓存，页面立刻能看到它（不必再等一次全量校验）
        try:
            eid = _entry_id(header)
            items = dict((_load_cache().get("items") or {}))
            # key 必须与 `_read_domain` 一致（根域）：管理员粘贴时常带 www./m.，
            # 若按原样入 key，概览永远命中不到这份结论，刚注入就显示「未校验」。
            items[f"{cp._strip_sub(domain)}|{eid}"] = {
                "status": probe.get("status") or "unknown",
                "detail": probe.get("detail") or "",
                "probe": probe.get("probe") or _PROBE_NONE,
                "ts": int(time.time()),
            }
            _save_cache(items, int(time.time()))
        except Exception:
            pass

    return {
        "ok": bool(ok),
        "added": bool(ok),
        "parts": _parts_count(header),
        "verified": probe.get("status") == "valid",
        "probe": probe.get("probe"),
        "detail": probe.get("detail") or "",
        "error": "" if ok else "入池失败（可能是该 Cookie 已存在且无法更新，或域名不被允许）",
    }


def _do_delete(domain: str, ids: Any) -> dict:
    """按条目 id 删除（清无效项）。

    只删显式给出的 id，不做「一键清空无效」这类批量动作——后台误点一次就可能
    把整个平台清空，而补回来需要有人正好登录着那个站。前端每条删除都要二次确认。
    """
    domain = str(domain or "").strip()
    if not domain or not cp.is_allowed(domain):
        return {"ok": False, "error": "域名不在白名单内"}
    if not isinstance(ids, list) or not ids:
        return {"ok": False, "error": "没有指定要删除的条目"}
    ids = {str(x) for x in ids if str(x)}

    removed = 0
    for host in cp._candidates(domain):
        f = cp._pool_file(host)
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        cookies = data.get("cookies") or []
        kept = []
        for c in cookies:
            header = cp._decrypt_item(c)
            if header and _entry_id(header) in ids:
                removed += 1
                continue
            kept.append(c)
        if len(kept) != len(cookies):
            cp._save(host, kept)

    # 删完把缓存里对应条目也清掉，否则下次打开页面这些 id 还会「幽灵」存在
    if removed:
        try:
            doc = _load_cache()
            items = dict(doc.get("items") or {})
            for k in list(items.keys()):
                if k.split("|", 1)[-1] in ids:
                    items.pop(k, None)
            _save_cache(items, int(doc.get("ts") or 0))
        except Exception:
            pass

    return {"ok": True, "removed": removed}


# ─── 云端代理（桌面 App → vdl-worker）────────────────────────────────────────

def _cloud_cfg() -> tuple[str, str]:
    """云端池的地址与令牌。与 app.py 的 `_cloud_sync_config` 同源同优先级。

    不直接 import app（会循环依赖：app 在模块末尾才 include 本路由）。
    """
    url = (os.environ.get("VDL_COOKIE_SYNC_URL") or "").strip()
    token = (os.environ.get("VDL_COOKIE_SYNC_TOKEN") or "").strip()
    if not url or not token:
        cfg = Path.home() / ".videodownloader" / "cloud_sync.json"
        try:
            d = json.loads(cfg.read_text())
            url = url or str(d.get("url") or "").strip()
            token = token or str(d.get("token") or "").strip()
        except Exception:
            pass
    return url, token


def _cloud_call(action: str, payload: dict | None = None, timeout: float = 30) -> dict:
    """调云端 `POST /api/cookie/pool-admin`。

    带浏览器 UA：云端在 Cloudflare 后面，Python-urllib 默认 UA 会被 1010 拦。
    """
    url, token = _cloud_cfg()
    if not url or not token:
        raise RuntimeError("未配置云端同步地址/令牌")
    body = json.dumps(dict(payload or {}, token=token, action=action)).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/api/cookie/pool-admin",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": _UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = (json.loads(e.read().decode() or "{}") or {}).get("detail") or ""
        except Exception:
            pass
        raise RuntimeError(f"云端返回 HTTP {e.code}{('：' + detail) if detail else ''}")


def _pick_source(request: Request) -> str:
    """本地 / 云端 / 自动。默认 auto：桌面端优先看云端池（网页版真正在用的那个）。"""
    v = str((request.query_params.get("source") if request else "") or "").strip().lower()
    return v if v in ("local", "cloud") else "auto"


# ─── 后台接口（超管）────────────────────────────────────────────────────────

@router.get("/api/admin/cookies")
def admin_cookies(request: Request = None) -> dict[str, Any]:
    """Cookie 池全平台概览。

    默认 `source=auto`：配了云端同步就代理云端（桌面 App 的本机池通常是空的，
    真正的池在网页版那台机器上）；云端不可达时回退本机并在响应里标注。
    """
    require_admin(request)
    want = _pick_source(request)

    if want in ("auto", "cloud"):
        url, tok = _cloud_cfg()
        if url and tok:
            try:
                d = _cloud_call("overview", timeout=25)
                if d.get("ok"):
                    d["source"] = "cloud"
                    d["source_label"] = "云端公共池（网页版在用）"
                    return d
                raise RuntimeError(str(d.get("error") or "云端返回异常"))
            except Exception as e:
                if want == "cloud":
                    return {"ok": False, "error": f"云端池不可达：{e}"}
                # auto：静默回退，但必须让界面知道看的是本机，不是网页版那个池
                d = _overview()
                d["source"] = "local"
                d["source_label"] = "本机 Cookie 池（云端不可达，已回退）"
                d["cloud_error"] = str(e)
                return d
        if want == "cloud":
            return {"ok": False, "error": "未配置云端同步地址/令牌（~/.videodownloader/cloud_sync.json）"}

    d = _overview()
    d["source"] = "local"
    d["source_label"] = "本机 Cookie 池"
    return d


@router.post("/api/admin/cookies/verify")
def admin_cookies_verify(payload: dict[str, Any] = Body(default={}),
                         request: Request = None) -> dict[str, Any]:
    """真联网验真（全量或指定域）。

    桌面上必须交给云端跑：解析在云端发生，同一 IP、同一台机器验出来的结论才算数；
    在本机验（家宽 IP）容易得到「有效」而实际解析仍失败。
    """
    require_super(request)
    want = _pick_source(request)
    domain = str((payload or {}).get("domain") or "").strip()
    # force：运营明确要求「现在就重验」。默认走冷却（6h/10min），
    # 因为云端机房 IP 反复探同一会话会被平台判盗号并作废。
    force = bool((payload or {}).get("force"))

    if want in ("auto", "cloud"):
        url, tok = _cloud_cfg()
        if url and tok:
            try:
                r = _cloud_call("verify", {"domain": domain, "force": force}, timeout=120)
                r["source"] = "cloud"
                return r
            except Exception as e:
                if want == "cloud":
                    return {"ok": False, "error": f"云端校验失败：{e}"}
    r = _do_verify(domain, force=force)
    r["source"] = "local"
    return r


@router.post("/api/admin/cookies/inject")
def admin_cookies_inject(payload: dict[str, Any] = Body(...),
                         request: Request = None) -> dict[str, Any]:
    """粘贴 Cookie → 验真 → 入池。默认注入到**云端池**（网页版立刻生效）。"""
    require_super(request)
    want = _pick_source(request)
    domain = str((payload or {}).get("domain") or "").strip()
    header = (payload or {}).get("header")

    if want in ("auto", "cloud"):
        url, tok = _cloud_cfg()
        if url and tok:
            try:
                r = _cloud_call("inject", {"domain": domain, "header": header}, timeout=90)
                r["target"] = "cloud"
                return r
            except Exception as e:
                if want == "cloud":
                    return {"ok": False, "error": f"云端注入失败：{e}"}
                return {"ok": False, "error": f"云端注入失败（本机注入对网页版无效，已中止）：{e}"}
    r = _do_inject(domain, header)
    r["target"] = "local"
    return r


@router.post("/api/admin/cookies/delete")
def admin_cookies_delete(payload: dict[str, Any] = Body(...),
                         request: Request = None) -> dict[str, Any]:
    """按条目 id 删除。默认删云端池（界面上看到的是它）。"""
    require_super(request)
    want = _pick_source(request)
    domain = str((payload or {}).get("domain") or "").strip()
    ids = (payload or {}).get("ids") or []

    if want in ("auto", "cloud"):
        url, tok = _cloud_cfg()
        if url and tok:
            try:
                r = _cloud_call("delete", {"domain": domain, "ids": ids}, timeout=60)
                r["target"] = "cloud"
                return r
            except Exception as e:
                if want == "cloud":
                    return {"ok": False, "error": f"云端删除失败：{e}"}
                return {"ok": False, "error": f"云端删除失败（未动本机池）：{e}"}
    r = _do_delete(domain, ids)
    r["target"] = "local"
    return r


# ─── 回读候选排序（2026-10-28）：别再拿死 Cookie 去撞墙 ───────────────────────
# 实测背景：`get` 原来只按「字段多少」排序，谁 k=v 多谁当 `header`。而桌面端
# 1.0.99 只取 `header` 这一条（不遍历 headers），于是出现两种错配：
#   · 腾讯视频 3 份全是游客态（静态判 invalid）⇒ 桌面端照样当登录态注入，
#     解析必然 403；后台看「池里有 Cookie」，两边结论直接打架；
#   · 同域一份有效 + 一份过期，字段多的那份先返回 ⇒ 好 Cookie 被埋在后面。
# 现在改成「先按有效性分层、再按完整度」，invalid 一律不下发（返回 found=false
# 并带 reason）：调用方据此回退匿名/浏览器兜底，比拿死 Cookie 撞墙更容易恢复。


def _entry_status_map(domain: str) -> dict[str, dict]:
    """该域每条的体检结论 {entry_id: entry}，复用后台概览那套判定，不联网。

    回读在解析热路径上（桌面端超时 15s），绝不能在这里跑联网探针：`_read_domain`
    只读池文件 + 读验真缓存，静态体检 + 同会话继承，毫秒级。
    """
    try:
        # 🔴 这里必须传缓存里的 **items**，不是整包：`_read_domain` 是
        # `cache.get(f"{domain}|{eid}")`（见 786 行调用点）。传整包 ⇒ 全部 miss ⇒
        # 一律退化成静态猜测，实测把 4 条有效优酷全判成「游客态」（2026-10-28 踩过）。
        info = _read_domain(cp._strip_sub(domain), time.time(),
                            _load_cache().get("items") or {})
    except Exception:  # noqa: BLE001 — 体检失败只该降级成「未知」，不该让回读报错
        logging.getLogger(__name__).warning(
            "[cookie_pool] get 体检失败 domain=%s", domain, exc_info=True)
        return {}
    out: dict[str, dict] = {}
    for acct in info.get("items") or []:
        for e in acct.get("entries") or []:
            rid = str(e.get("id") or "")
            if rid:
                out[rid] = e
    return out


def _rank_candidates(domain: str, cands: list[str]) -> tuple[list[dict], int]:
    """按「有效性 → 完整度」排序候选，并剔除已判死的。返回 (可用行, 剔除数)。

    分层：valid(3) > 未校验且非游客态(2) > 疑似游客态/超 TTL(1) > invalid(剔除)。
    expired 仍保留为兜底（TTL 只是保守估计，很多站 Cookie 活得比 30 天长），
    invalid 是「站点明确说没登录」或「缺登录字段」，下发只会把失败推到解析层。
    """
    smap = _entry_status_map(domain)
    rows: list[dict] = []
    rejected = 0
    for h in cands:
        e = smap.get(_entry_id(h)) or {}
        st = str(e.get("status") or "") or "unknown"
        detail = str(e.get("detail") or "")
        suspect = bool(e.get("suspect"))
        age = int(e.get("age_sec") or 0)
        expired = bool(age) and age > cp._TTL
        # 条目投影里**没有** expired 字段（见 _read_domain 的 entries whitelist），
        # 而 TTL 过期会被覆盖成 status=invalid + 固定 detail（256-261 行）。所以
        # 「只是超 TTL」≠「站点说没登录」：前者仍留作兜底（TTL 是保守估计，B 站
        # Cookie 常活 3~6 个月，一刀切会白扔还能用的登录态），后者才真的不下发。
        ttl_only = expired and "已超过" in detail
        if st == "valid":
            rank = 3
        elif st == "invalid" and not ttl_only:
            rejected += 1
            continue
        elif suspect or expired:
            rank = 1
        else:
            rank = 2
        rows.append({"header": h, "status": st, "suspect": suspect,
                     "expired": expired, "parts": _parts_count(h),
                     "detail": str(e.get("detail") or ""), "_rank": rank})
    rows.sort(key=lambda r: (-r["_rank"], -r["parts"]))
    return rows, rejected


def _do_get(domain: str) -> dict[str, Any]:
    """桌面端回读：取该域最新有效的 Cookie 明文（+ 优酷 ckey）。

    背景（2026-10-25）：桌面 App 的本机池目录在从未喂过池的机器上不存在，
    本机池恒 miss ⇒ 桌面用户没在浏览器登录的站一律解析失败，而云端池是满的。
    桌面端在本机池 miss 时经本动作回读云端登录态，零操作复用。

    本函数为 2026-10-26 自 app-dev（1.0.95）移植到网页版主干，使云端节点
    具备回读能力；桌面端回读通道至此端到端打通。

    安全口径与 inject 对等：同一把同步令牌本就允许「写池」，读不扩大暴露面；
    只放行白名单域；未命中返回空 header（不是错误）。
    """
    if not domain:
        return {"ok": False, "error": "缺少 domain 参数"}
    _log = logging.getLogger(__name__)
    root = cp._strip_sub(domain)
    if not cp.is_allowed(root):
        _log.info("[cookie_pool] get 拒绝非白名单域 domain=%s", domain)
        return {"ok": True, "found": False, "domain": root,
                "header": "", "ckey": "", "headers": []}
    header = cp.get_cookie(domain) or ""
    ckey = ""
    if root == "youku.com":
        # ckey 与 Cookie 同域存储（优酷 UPS 签名用），桌面端一起回读
        ckey = cp.get_ckey(domain) or ""
    # 2026-10-28 修：本函数原先只回 {ok, domain, header, ckey}，**没有 found**，
    # 而桌面端（1.0.99）判的是 `if d.get("ok") and d.get("found")` ⇒ 云端明明有
    # 登录态，桌面却一律当「没取到」，云端回读通道等于空转（实测 n=0）。
    # 同时补 headers（该域全部快照，字段多的在前）：百度同一个 BDUSS 下不同 STOKEN
    # 换直链成功率不同，只回一条等于替调用方赌运气。
    cands: list = []
    try:
        cands = [h for h in (cp.get_cookies(domain) or []) if h]
    except Exception:  # noqa: BLE001 — 老池文件/域未登记都不该让回读报错
        cands = []
    if not cands and header:
        cands = [header]
    # 去重：get_cookie 命中的那条通常也在 get_cookies 里，重复下发只会浪费
    # 调用方的重试预算（百度换直链本来就是逐个候选试，顺序即成本）。
    seen: set = set()
    uniq: list = []
    for h in ([header] if header else []) + cands:
        if h and h not in seen:
            seen.add(h)
            uniq.append(h)
    rows, rejected = _rank_candidates(root, uniq)
    ranked = [r["header"] for r in rows]
    resp: dict[str, Any] = {
        "ok": True, "found": bool(ranked), "domain": root,
        "header": ranked[0] if ranked else "", "ckey": ckey, "headers": ranked,
        # 池里存了几份 / 其中被判死几份：调用方据此区分「池里根本没有」与
        # 「池里有但全死」，后者要提示补注入而不是当作匿名态静默降级。
        "stored": len(uniq), "rejected": rejected,
        "usable": bool(ranked),
        # 口径：valid/unknown 正常；invalid + found=true 只可能是「仅超 30 天 TTL
        # 的兜底候选」（entries[].expired 为真），不是站点判定未登录。
        "status": (rows[0]["status"] if rows else ("invalid" if uniq else "empty")),
        # 逐条状态（不含明文）：调用方可以只按状态判断，不必解 Cookie
        "entries": [{"status": r["status"], "suspect": r["suspect"],
                     "expired": r["expired"], "parts": r["parts"]} for r in rows],
    }
    if uniq and not ranked:
        resp["reason"] = "all_invalid"
        resp["detail"] = "池里这份登录态已判死（过期或站点判定未登录），未下发"
    elif rows:
        resp["detail"] = rows[0]["detail"]
    if root == "youku.com" and not ckey:
        # 优酷 UPS 必须带 ckey，只回 Cookie 等于把「缺钥匙」的失败推给解析层，
        # 现场表现是 -3007 却看不出原因，这里显式标出来。
        resp["ckey_missing"] = True
        resp["detail"] = "；".join([x for x in (resp.get("detail") or "",
                                                "缺 ckey，优酷解析大概率 -3007") if x])
    _log.info("[cookie_pool] get domain=%s stored=%s served=%s rejected=%s ckey=%s",
              root, len(uniq), len(ranked), rejected, bool(ckey))
    return resp


# ─── 云端侧入口（共享令牌，供桌面 App 后台代理）─────────────────────────────

def _pool_get_action(domain: str) -> dict[str, Any]:
    """回读云端公共池：返回指定域有效 Cookie 明文（供桌面端「回读云端池」兜底）。

    与 inject/delete 同持 VDL_COOKIE_SYNC_TOKEN：持有者本就能写池，允许读池
    （仅返回明文、不写盘）不扩大暴露面。

    🔴 2026-10-28：贡献域（baidu.com）**放行读取**。上一版以「BDUSS 敏感度更高」
    为由把百度挡在这里，后果是「用户显式点过贡献、云端也真存了，但谁也取不回来」
    —— 共享池对百度彻底空转（桌面端解析 100% 拿不到别人的登录态）。
    现在的边界：仍需同一把同步令牌（等价于已持有写池权限）、只读显式贡献过的
    账号、不回写、不进自动同步。暴露面与 inject 对等。

    返回**全部候选**（`headers`），不只一条：同账号可能有多份快照，字段完整度
    不同、换直链成功率也不同（2026-10-28 实测：32 字段那份回密文、38 字段那份
    回明文）。只回一条等于替调用方赌运气，所以把候选全给出、由调用方逐个试。
    """
    if not domain:
        raise HTTPException(status_code=400, detail="缺少 domain")
    norm = cp._norm_domain(domain)
    try:
        cands = list(cp.get_cookies(domain) or [])
    except Exception:  # noqa: BLE001 — 池读异常退化成单条口径，不打断回读
        cands = []
    if not cands:
        single = cp.get_cookie(domain)
        if single:
            cands = [single]
    if not cands:
        return {"ok": True, "found": False, "domain": norm,
                "header": None, "headers": [], "stored": 0, "rejected": 0,
                "usable": False, "status": "empty", "reason": "empty"}
    # 有效性优先、完整度次之（见 _rank_candidates 的注释）：换直链要的是完整的
    # pan cookie，但「完整」排在「还活着」之后 —— 字段最多的那份往往正是过期那份。
    rows, rejected = _rank_candidates(norm, cands)
    ranked = [r["header"] for r in rows]
    matched = norm
    for c in cp._candidates(domain):
        try:
            if cp._pool_file(c).exists():
                matched = c
                break
        except Exception:
            pass
    if not ranked:
        return {"ok": True, "found": False, "domain": matched,
                "header": None, "headers": [], "stored": len(cands),
                "rejected": rejected, "usable": False, "status": "invalid",
                "reason": "all_invalid",
                "detail": "池里这份登录态已判死（过期或站点判定未登录），未下发"}
    return {"ok": True, "found": True, "domain": matched,
            "header": ranked[0], "headers": ranked, "stored": len(cands),
            "rejected": rejected, "usable": True, "status": rows[0]["status"],
            "detail": rows[0]["detail"]}


@router.post("/api/cookie/pool-admin")
def cookie_pool_admin(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """云端池的管理入口，仅服务桌面 App 后台代理。

    鉴权用 `VDL_COOKIE_SYNC_TOKEN`（与 /api/cookie/sync 同一把钥匙）：
    这把钥匙本来就允许「写池」，所以再允许「读池概况 / 验真 / 删除」不扩大暴露面。
    注意：`get` 动作会返回指定域 Cookie **明文**（仅持有该令牌的桌面端可取），
    用于「桌面端未登录也能复用云端共享登录态」；贡献域（baidu.com）同样放行，
    但只读显式贡献过的账号，且不回写、不进自动同步（2026-10-28 起）。
    其余动作（overview / verify / delete）仍只暴露脱敏信息。
    """
    tok = (os.environ.get("VDL_COOKIE_SYNC_TOKEN") or "").strip()
    if not tok or str((payload or {}).get("token") or "") != tok:
        raise HTTPException(status_code=403, detail="无效令牌")
    action = str((payload or {}).get("action") or "overview")
    if action == "overview":
        return _overview()
    if action == "get":
        # 桌面端解析回读云端池（2026-10-26 移植）：唯一返回 Cookie 明文的动作
        return _do_get(str((payload or {}).get("domain") or "").strip())
    if action == "verify":
        return _do_verify(str((payload or {}).get("domain") or "").strip(),
                          force=bool((payload or {}).get("force")))
    if action == "inject":
        return _do_inject(str((payload or {}).get("domain") or "").strip(),
                          (payload or {}).get("header"))
    if action == "delete":
        return _do_delete(str((payload or {}).get("domain") or "").strip(),
                          (payload or {}).get("ids") or [])
    # 2026-10-28 修：这里原本又写了一次 `if action == "get"`，被上面 1280 行那个
    # 先 return 掉 ⇒ 永远走不到（死代码），云端池放行改动到不了线上。
    # 有效通路统一为 get → _do_get（已补齐 found / headers）；这里降级成别名，
    # 免得再有人以为「改 _pool_get_action 就能生效」。
    if action == "pool_get":
        return _pool_get_action(str((payload or {}).get("domain") or "").strip())
    raise HTTPException(status_code=400, detail=f"未知动作：{action}")
