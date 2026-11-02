#!/usr/bin/env python3
"""ECS（VPS）上用 Playwright 真实浏览器解析抖音视频，提取真实视频/音频流 URL。

背景：yt-dlp 抖音提取器在抖音 2026 初反爬升级后失效（aweme_detail API 需要
a_bogus 签名，yt-dlp 未实现，报 "Fresh cookies needed"），即使带完整登录态
cookie 也无解。但真实浏览器执行 JS 后能拿到真实视频流 URL。

默认游客态（持久 profile，无需登录）。2026-11-02 起支持 cookies 参数：当传入
云端 Cookie 池的有效抖音登录态时，用干净临时 profile + Windows Chrome UA 注入
登录态，以真实浏览器身份突破抖音 Argus 第二层 UIFID 设备指纹校验（纯 HTTP 直连
的游客 a_bogus 签名因设备指纹与登录态 UIFID 不绑定而被确定性拦截）。

对外暴露 resolve(url[, cookies]) 供守护进程 vdl_cookie_daemon.py 复用。
"""
import os
import re
import shutil
import sys
import tempfile
import time

PROFILE = "/opt/vdl-worker/douyin_resolve_profile"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# 登录态 Cookie 来自 Windows Chrome 130（池里贡献者的设备），注入时用同款 UA
# 声明，避免 UA 与 Cookie 设备平台不符触发额外风控。
WINDOWS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def _parse_header_cookies(header_str, domain=".douyin.com"):
    """把 Cookie 池的 `k=v; k2=v2` header 文本解析成 Playwright add_cookies 列表。

    统一落到 .douyin.com 通配域（覆盖 www/v/iesdouyin 等子域）；跳过非 cookie
    属性的伪字段（path/domain/expires 等可能混入）。返回 [] 表示无有效 cookie。
    """
    out = []
    if not header_str:
        return out
    for part in header_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        k, v = k.strip(), v.strip()
        if k.lower() in ("path", "domain", "expires", "max-age",
                         "secure", "httponly", "samesite"):
            continue
        out.append({"name": k, "value": v, "domain": domain, "path": "/"})
    return out


_VID_RE = re.compile(r"/video/(\d{15,})")
_MODAL_RE = re.compile(r"[?&]modal_id=(\d{15,})")
_NOTE_RE = re.compile(r"/note/(\d{15,})")

# 收集所有 video 标签的 src + 时长（真实视频在 douyinvod.com，静态占位在 douyinstatic.com）
_JS_COLLECT = (
    "() => Array.from(document.querySelectorAll('video'))"
    ".map(v => ({src: v.src || v.currentSrc || '', dur: v.duration || 0}))"
    ".filter(x => x.src.startsWith('http'))"
)
# 触发真实视频加载：滚动 + 对 douyinvod 视频先静音起播（绕过浏览器 autoplay 拦截），
# 1.5s 后取消静音——抖音播放器此时才会加载分离的音频轨（media-audio-und-mp4a），
# 否则 audio_url 恒空 → 下载合并后无声（2026-08-23 实测根因）。
_JS_PLAY_ALL = (
    "() => Array.from(document.querySelectorAll('video')).forEach(v => { "
    "if (v.src && v.src.indexOf('douyinvod') >= 0) { v.muted = true; "
    "if (v.play) v.play().catch(() => {}); "
    "setTimeout(() => { try { v.muted = false; if (v.play) v.play().catch(() => {}); } catch (e) {} }, 1500); } })"
)


def _pick_video_id(url: str) -> str:
    m = _VID_RE.search(url) or _MODAL_RE.search(url)
    return m.group(1) if m else ""


def _classify(urls):
    """把捕获到的 douyinvod.com URL 分成 混合轨 / 分离视频轨 / 音频轨。

    抖音 PC 网页两种流形态（2026-08-23 无声问题根因）：
    - 混合轨（v{id}-web.douyinvod.com/.../video/tos/...mp4）：视频+音频一体，有声
    - 分离轨：media-video-avc1（纯视频，无声）+ media-audio-und-mp4a（音频）
    混合轨优先返回；只有分离轨时才需要 audio_url 配对合并。
    """
    mixed, video, audio = [], [], []
    for u in urls:
        if "media-video-avc1" in u or "media-video" in u:
            video.append(u)
        elif "media-audio-und-mp4a" in u or "media-audio" in u or "mime_type=audio" in u:
            audio.append(u)
        else:
            mixed.append(u)  # 其余 douyinvod URL（v26-web 等）→ 混合轨（自带音轨）
    return mixed, video, audio


def _normalize_douyin_url(url: str) -> str:
    """抖音链接归一化：v.douyin.com 短链 / iesdouyin.com/xg/video/ID
    统一转为 https://www.douyin.com/video/<ID>，解决 iesdouyin 域名下
    Playwright 拿不到真实视频流的问题。"""
    # 1) 长链里直接有 video_id（含西瓜 ixigua.com/<ID>，字节系视频ID互通）
    m = re.search(r"(?:iesdouyin\.com/xg/video/|douyin\.com/video/|ixigua\.com/(?:video/|i)?|modal_id=)(\d{15,})", url)
    if m:
        return "https://www.douyin.com/video/%s" % m.group(1)
    # 2) v.douyin.com 短链：用 urllib 跟随 302 展开
    if "v.douyin.com" in url:
        try:
            import urllib.request
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
                    "Accept": "text/html,*/*",
                },
            )
            final = urllib.request.urlopen(req, timeout=15).geturl()
            m2 = re.search(r"(?:iesdouyin\.com/xg/video/|douyin\.com/video/|modal_id=)(\d{15,})", final)
            if m2:
                return "https://www.douyin.com/video/%s" % m2.group(1)
        except Exception:
            pass
    return url


def resolve(url, timeout=30, cookies=None):
    """解析抖音视频，返回 dict（成功）或抛 RuntimeError（失败）。

    cookies：可选登录态 Cookie（云端池 header 文本）。提供时：用临时干净 profile
        注入登录态 + Windows Chrome UA（与登录态设备一致），以真实浏览器身份
        突破抖音 Argus 第二层 UIFID 设备指纹校验（纯 HTTP 直连的游客 a_bogus
        签名因设备指纹与登录态 UIFID 不绑定而被确定性拦截）。缺省走游客态
        （持久 profile，无登录态，仅用于无登录态时的兜底）。
    """
    url = _normalize_douyin_url(url)
    from playwright.sync_api import sync_playwright

    use_login = bool(cookies)
    tmp_profile = None
    if use_login:
        # 干净临时 profile：避免持久 profile 里的游客 UIFID_TEMP 与登录态 UIFID 冲突
        tmp_profile = tempfile.mkdtemp(prefix="dyr_login_")
        profile_dir = tmp_profile
        ua = WINDOWS_UA
    else:
        profile_dir = PROFILE
        ua = UA
        os.makedirs(PROFILE, exist_ok=True)

    pw = sync_playwright().start()
    try:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--autoplay-policy=no-user-gesture-required",
            ],
            user_agent=ua,
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )
        if use_login:
            # 注入登录态：Chrome 原生生成的签名/指纹与登录态天然一致，破 Argus
            try:
                ck_list = _parse_header_cookies(cookies)
                if ck_list:
                    context.add_cookies(ck_list)
            except Exception as e:  # noqa: BLE001
                print("[douyin_resolve] 注入登录态失败: %s" % str(e)[:120])
        try:
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = context.new_page()
            captured = []

            def _on_resp(resp):
                u = resp.url
                # ⚠️ 必须同时捕获音视频轨：抖音 PC 网页音视频分离，音频轨 URL
                # （media-audio-und-mp4a / mime_type=audio）不含 "video" 字样，
                # 旧过滤条件只收 video → audio_url 恒空 → 合并后无声（2026-08-23 实测）
                if "douyinvod.com" in u and (
                    "video" in u or "audio" in u or "mime_type=" in u or "media-" in u
                ):
                    captured.append(u)

            page.on("response", _on_resp)

            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # 轮询：滚动 + 触发播放，挑真实视频流（douyinvod.com，排除静态占位）
            chosen = None
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    page.mouse.wheel(0, 600)
                except Exception:
                    pass
                try:
                    page.evaluate(_JS_PLAY_ALL)
                except Exception:
                    pass
                try:
                    items = page.evaluate(_JS_COLLECT) or []
                except Exception:
                    items = []
                vod = [x for x in items if "douyinvod.com" in x["src"]]
                if vod:
                    chosen = vod[0]
                    if chosen["src"]:
                        break
                time.sleep(1.0)

            # 分类捕获的 URL：混合轨 / 分离视频轨 / 音频轨
            c_mixed, c_video, c_audio = _classify(captured)

            # 视频轨：优先 video 标签 src（混合轨），再优先捕获的混合轨，兜底分离视频轨
            video_url = chosen["src"] if (chosen and chosen["src"].startswith("http")) else ""
            if not video_url.startswith("http") and c_mixed:
                video_url = c_mixed[0]
            if not video_url.startswith("http") and c_video:
                video_url = c_video[0]
            # 音频轨：仅分离视频轨场景需要配对（混合轨自带音轨）；从捕获里取
            audio_url = c_audio[0] if c_audio else ""
            # 标记视频轨是否自带音频：混合轨 = 有声；分离轨 = 无声需合并
            video_has_audio = bool(c_mixed) or (chosen and chosen["src"].startswith("http"))

            final_url = page.url
            vid = _pick_video_id(final_url) or _pick_video_id(url)
            if not video_url.startswith("http") or not vid:
                # 拿不到 video_id 且无视频流 → 短链失效 / 视频被删除 / 私密 / 短链过期
                # final_url 通常会落到首页/jingxuan 精选页 / 抖音返回未登录提示
                # 2026-10-27：抖音上线 Argus 风控后，无头浏览器页面也拿不到视频流
                # （页面只剩壳 + 永久加载圈），无法区分「视频不存在」与「被风控」，
                # 文案不再断言链接无效，避免误导用户。
                raise RuntimeError(
                    "抖音解析失败：抖音近期升级风控（Argus），网页端匿名解析与无头浏览器均被拦截，"
                    "暂无法区分是风控拦截还是视频已删除/私密。请稍后重试"
                    "（streams=%d, final_url=%s）" % (len(captured), final_url[:60])
                )

            # 标题：抖音页面 title 可能异步写入，等 1-2s 直到非空
            title = ""
            for _ in range(6):
                title = (page.title() or "").replace(" - 抖音", "").strip()
                if title:
                    break
                time.sleep(0.5)
            if not title:
                title = vid

            duration = None
            if chosen and chosen.get("dur"):
                try:
                    duration = int(chosen["dur"])
                except Exception:
                    duration = None

            width = int(chosen.get("vw") or 0) if chosen else 0
            height = int(chosen.get("vh") or 0) if chosen else 0

            return {
                "ok": True,
                "title": title,
                "duration": duration,
                "video_id": vid,
                "video_url": video_url,
                "audio_url": audio_url,
                "video_has_audio": video_has_audio,
                "width": width,
                "height": height,
                "webpage_url": "https://www.douyin.com/video/%s" % vid,
                "thumbnail": "",
                "ext": "mp4",
            }
        finally:
            context.close()
    finally:
        pw.stop()
        if tmp_profile and os.path.isdir(tmp_profile):
            shutil.rmtree(tmp_profile, ignore_errors=True)


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else ""
    if not u:
        print("用法: python douyin_resolve.py <抖音链接> [cookie_header]")
        sys.exit(1)
    import json
    ck = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        r = resolve(u, cookies=ck)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:
        print("FAIL:", str(e))
        sys.exit(2)
