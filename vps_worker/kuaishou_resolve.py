#!/usr/bin/env python3
"""ECS（VPS）上用 Playwright 真实浏览器解析快手作品，提取真实视频流 URL。

背景：快手 2024 后改全客户端渲染（CSR）+ GraphQL + kwpsec 反爬，SSR 的
window.__APOLLO_STATE__ 在无「设备指纹 Cookie」时是空的（did/kpf/kwpsec 等），
纯 requests/curl 拿不到主视频数据。但真实浏览器首次访问会建立设备指纹 Cookie，
二次访问后 __APOLLO_STATE__ 里就有完整视频数据（photoH265Url/photoH264Url 直链）。

本脚本用无头 Chromium 打开快手作品页（游客态、无需登录），提取：
  - 标题（caption 剥离话题标签；纯话题视频用「作者昵称：首个话题」）
  - 作者昵称（uploader）
  - 封面图（photoUrl）
  - 真实视频流 URL（kwaicdn.com 的合并 mp4，音视频已合并、无需 ffmpeg 再合）
  - 时长 / 视频 ID

短链 v.kuaishou.com/xxx 会被浏览器自动 302 展开为 H5 浏览页
（v.m.chenzhongtech.com/fw/photo/<id>?...&subBiz=BROWSE_SLIDE_PHOTO）。
⚠️ BROWSE_SLIDE_PHOTO 是 H5 浏览页的业务线标识，视频分享链接也带它——
绝不据此判作品类型；视频一律「先提流，H5 页拿不到就切桌面详情页再试」。

对外暴露 resolve(url) 供守护进程 vdl_cookie_daemon.py 复用。
"""
import json
import os
import re
import sys
import time

PROFILE = "/opt/vdl-worker/kuaishou_profile"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_PHOTOID_RE = re.compile(
    r"/short-video/([\w-]+)|/fw/photo/([\w-]+)|/photo/([\w-]+)"
    r"|photoId=([\w-]+)|/f/([\w-]+)|v\.kuaishou\.com/([\w-]+)")
_TAG_RE = re.compile(r"#[\w\u4e00-\u9fa5·]+")
_AT_RE = re.compile(r"@[\w\u4e00-\u9fa5]+(?:\([^)]*\))?")


def _pick_video_id(url: str) -> str:
    m = _PHOTOID_RE.search(url)
    if not m:
        return ""
    for g in m.groups():
        if g:
            return g
    return ""


def _extract_meta(page) -> tuple[str, str, str]:
    """从 APOLLO_STATE 提取 (caption, author_name, thumbnail)。

    注意：page.evaluate 返回的 JS 字符串是原始 Unicode（中文不做转义），
    绝不能再做 unicode_escape 解码（会把 UTF-8 字节搞坏）。
    """
    try:
        meta = page.evaluate(
            "() => { const st = window.__APOLLO_STATE__ || {};"
            " let caption = '', author = '', thumb = '';"
            " const walk = (o, d) => {"
            "  if (d > 24 || o == null) return;"
            "  if (typeof o === 'object') for (const [k, v] of Object.entries(o)) {"
            "   if (!caption && k === 'caption' && typeof v === 'string' && v.length > 1) caption = v;"
            "   if (!author && k === 'name' && typeof v === 'string' && v.length >= 2 && v.length <= 24"
            "     && !/精彩|热榜|直播|同城|剧场|音乐|视频|首页|关注|推荐|搜索|创作|个人/.test(v)) author = v;"
            "   if (!thumb && k === 'photoUrl' && typeof v === 'string' && v.startsWith('http')) thumb = v;"
            "   walk(v, d + 1); }"
            " }; walk(st, 0); return {caption: caption, author: author, thumb: thumb}; }"
        ) or {}
    except Exception:
        meta = {}
    caption = str(meta.get("caption") or "")
    author = str(meta.get("author") or "")
    thumb = str(meta.get("thumb") or "")
    # 兜底：DOM 找作者名
    if not author:
        try:
            author = page.evaluate(
                "() => { const el = document.querySelector('[class*=author] [class*=name], "
                "[class*=userInfo] [class*=name], .author-name');"
                " return el ? el.textContent.trim().slice(0, 40) : ''; }") or ""
        except Exception:
            author = ""
    return caption, author, thumb


def _looks_like_album(page) -> bool:
    """从 APOLLO_STATE 判断是否真·图集/图文作品。

    ⚠️ 只认数据，不认 URL：快手分享短链展开后统一带 `subBiz=BROWSE_SLIDE_PHOTO`，
    视频分享链接也带它，绝不能作为图集判据。只有页面数据里真出现 atlas / imageData /
    multiImage / imageList 等图集结构，才确认是图集。
    """
    try:
        flag = page.evaluate(
            "() => { const st = window.__APOLLO_STATE__ || {}; let hit = false;"
            " const walk = (o, d) => { if (d > 22 || o == null || hit) return;"
            "  if (typeof o === 'object') for (const [k, v] of Object.entries(o)) {"
            "   if (/atlas|imageData|multiImage|imageList|albumPhoto|worksImage/i.test(k))"
            "     { hit = true; return; } walk(v, d + 1); } };"
            " walk(st, 0); return hit; }"
        )
        return bool(flag)
    except Exception:
        return False


def _poll_streams(page, seconds: float) -> list:
    """在给定时间窗内轮询 __APOLLO_STATE__ / <video> 里的真实流地址。

    ⚠️ 2026-08 实测：快手新版签名直链（oskwai.com/ksc2/...）已不带 .mp4 后缀，
       只靠 .mp4 判定会漏掉真实流。改为「CDN 域 + (含 .mp4 或含 /ksc2/ 签名)」。
    """
    mp4s = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            mp4s = page.evaluate(
                "() => { const st = window.__APOLLO_STATE__ || {};"
                " const out = [];"
                " const walk = (o, d) => {"
                "  if (d > 20 || o == null) return;"
                "  if (typeof o === 'string') { const low = o.toLowerCase();"
                "   if (low.startsWith('http') && (low.includes('kwaicdn') || low.includes('oskwai') || low.includes('ksc2'))"
                "       && (low.includes('.mp4') || low.includes('/ksc2/') || low.includes('v2.kwaicdn'))) out.push(o); return; }"
                "  if (typeof o === 'object') for (const v of Object.values(o)) walk(v, d + 1);"
                " }; walk(st, 0);"
                " const v = document.querySelector('video');"
                " if (v && (v.src || v.currentSrc)) out.push(v.src || v.currentSrc);"
                " return [...new Set(out)]; }"
            ) or []
        except Exception:
            mp4s = []
        if mp4s:
            break
        try:
            page.mouse.wheel(0, 600)
        except Exception:
            pass
        time.sleep(1.5)
    return mp4s


def resolve(url, timeout=35):
    """解析快手视频，返回 dict（成功）或抛 RuntimeError（失败）。"""
    from playwright.sync_api import sync_playwright

    os.makedirs(PROFILE, exist_ok=True)
    pw = sync_playwright().start()
    try:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE,
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--autoplay-policy=no-user-gesture-required",
            ],
            user_agent=UA,
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )
        try:
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # ⚠️ 2026-11-10 关键修正（视频分享链接被误判图集）：
            # 快手分享短链（v.kuaishou.com/xxx）展开后统一落 H5 浏览页
            # v.m.chenzhongtech.com/fw/photo/<id>?...&subBiz=BROWSE_SLIDE_PHOTO ——
            # 该参数是 H5 浏览页的业务线标识，**视频分享链接也带它**，不是作品类型。
            # 旧逻辑一见到 BROWSE_SLIDE_PHOTO 就立刻抛「图集」，导致大量视频分享链接
            # 被误杀（用户报障：视频链接提示「图集/图文作品」）。
            # 新逻辑：先尽力提取视频流（H5 页 → 桌面详情页 → reload 退化）；流拿不到时
            # 也**绝不**凭 URL 参数判图集——只有页面数据里真挖到图集结构（atlas 等）
            # 才提示「图集」；其余（含风控/captcha 拦截导致数据全空）一律给诚实的
            # 「解析失败」，避免误导用户以为自己是图集。
            first_url = page.url or ""
            photo_id = _pick_video_id(first_url) or _pick_video_id(url)

            # ① 当前页先短轮询（H5 浏览页偶尔能直接给出播放流）
            mp4s = _poll_streams(page, 5)

            # ② H5 浏览页是 SSR 壳：图集作品根本不请数据，视频作品的 H5 播放器也常
            #    为空。有 photoId 且当前不是桌面详情页时，切桌面详情页（成熟路径）再试。
            if not mp4s and photo_id and "/short-video/" not in first_url:
                page.goto(
                    "https://www.kuaishou.com/short-video/" + photo_id,
                    wait_until="domcontentloaded", timeout=30000,
                )
                mp4s = _poll_streams(page, 8)

            if not mp4s:
                # 2026-10-30 实测（冷 profile 复现）：设备指纹 Cookie 缺失/过期时，
                # 首访页面的 <video> 存在但 src 为空、__APOLLO_STATE__ 里零 kwaicdn，
                # 且该 SPA **不会**自己重取数据 —— 只靠单页轮询必然超时。二次访问带
                # 首访建立的 did/kpf/kwpsec 等指纹 Cookie 才返回数据，reload 后
                # 1.5s 内即出直链。故 reload 后再轮询。
                page.reload(wait_until="domcontentloaded", timeout=30000)
                mp4s = _poll_streams(page, 12)

            if not mp4s:
                # 流确实没拿到。区分三种情况，给诚实文案，**绝不凭 URL 参数误判图集**：
                #  ① 真图集：APOLLO_STATE 挖到 atlas/imageData 等图集结构 → 提示图集
                #     （网页版不支持，引导桌面版）；
                #  ② 作品页已加载但无视频流 → 提取失败，提示重试；
                #  ③ 页面数据全空（风控/captcha/IP 被拦，APOLLO_STATE 零内容）→ 诚实
                #     「解析失败」，而不是误导用户「这是图集」。
                caption, author, _ = _extract_meta(page)
                if _looks_like_album(page):
                    raise RuntimeError(
                        "这是快手图集/图文作品，网页版暂不支持下载，请用桌面版 App"
                    )
                if caption or author:
                    raise RuntimeError(
                        "快手视频流提取失败（作品页已加载但无可用直链），请稍后重试"
                    )
                raise RuntimeError(
                    "快手解析失败：页面数据未加载，可能被风控拦截；请稍后重试或用桌面版 App"
                )

            # 选流策略：video 标签 src（播放器实际加载的真实流）最高优先；
            # 无 video src 时退到 APOLLO_STATE 里 hd 最高清的直链（hd15 > hd1）。
            def _hd_rank(u):
                m = re.search(r"_hd(\d+)", u)
                return int(m.group(1)) if m else -1
            video_src = ""
            try:
                video_src = page.evaluate(
                    "() => { const v = document.querySelector('video');"
                    " return v && (v.src || v.currentSrc) || ''; }"
                ) or ""
            except Exception:
                video_src = ""
            if video_src.startswith("http"):
                best = video_src  # 播放器真实流（可能是无后缀签名直链）
            else:
                best = max(mp4s, key=lambda u: (_hd_rank(u), u.startswith("https://") and "upic" in u))
            video_url = best

            caption, author, thumbnail = _extract_meta(page)

            # 标题：caption 剥离话题标签/@提及 → 纯文字；纯话题视频用「作者：首个话题」
            title = ""
            clean = _AT_RE.sub("", _TAG_RE.sub("", caption)).strip()
            clean = re.sub(r"\s+", " ", clean).strip()
            if clean:
                title = clean
            elif author:
                tags = _TAG_RE.findall(caption)
                title = (author + "：" + tags[0].lstrip("#")) if tags else (author + " 的快手视频")
            if not title:
                title = (page.title() or "").replace(" - 快手", "").replace("短视频-快手", "").strip()
            if not title:
                title = _pick_video_id(url)
            title = title[:120]

            # 视频 ID：从最终 URL 或 APOLLO_STATE 里的 photoId
            final_url = page.url
            vid = _pick_video_id(final_url) or _pick_video_id(url)
            try:
                pid = page.evaluate(
                    "() => { const st = JSON.stringify(window.__APOLLO_STATE__ || {});"
                    " const m = st.match(/\"photoId\":\"?(\\d{10,})\"?/); return m ? m[1] : ''; }"
                ) or ""
                if pid:
                    vid = pid
            except Exception:
                pass

            # 时长
            duration = None
            try:
                d = page.evaluate(
                    "() => { const st = window.__APOLLO_STATE__ || {}; let dur = null;"
                    " const walk = (o, d) => {"
                    "  if (d > 20 || o == null || dur) return;"
                    "  if (typeof o === 'object') for (const [k, v] of Object.entries(o)) {"
                    "   if (k === 'duration' && (typeof v === 'number' || (typeof v === 'string' && /^\\d+$/.test(v)))) { dur = Number(v); return; }"
                    "   walk(v, d + 1); }"
                    " }; walk(st, 0);"
                    " return dur && dur > 1000 ? Math.round(dur / 1000) : dur; }"
                )
                duration = int(d) if d and d > 0 else None
            except Exception:
                duration = None

            return {
                "ok": True,
                "title": title,
                "uploader": author,
                "duration": duration,
                "video_id": vid,
                "video_url": video_url,
                "webpage_url": f"https://www.kuaishou.com/short-video/{vid}" if vid else url,
                "thumbnail": thumbnail,
                "ext": "mp4",
            }
        finally:
            context.close()
    finally:
        pw.stop()


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else ""
    if not u:
        print("用法: python kuaishou_resolve.py <快手链接>")
        sys.exit(1)
    try:
        r = resolve(u)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:
        print("FAIL:", str(e))
        sys.exit(2)
