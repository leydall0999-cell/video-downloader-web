#!/usr/bin/env python3
"""lespark.us（短剧/短视频）解析 —— VPS Playwright worker。

lespark 播放页是 Nuxt SSR + 登录墙：目标剧集（is_playlet=1）需登录态 token 头
才能拿到真实 m3u8；游客态或非目标视频只能拿到推荐流的签名 m3u8。故走 Playwright：
用持久化登录态 profile（lespark_profile）注入账号 Cookie，真实浏览器加载播放器，
监听网络响应拦截 tcvod.lespark.cn 的签名 m3u8 直链。

对外暴露 resolve(url) 供守护进程 vdl_cookie_daemon.py 复用。
"""
import os
import re
import sys
import time

PROFILE = "/opt/vdl-worker/lespark_profile"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# tcvod.lespark.cn 的签名 HLS（adp.<id>.m3u8?t=..&us=..&sign=..）
_TCVOD_RE = re.compile(r"tcvod\.lespark\.cn")

HOST = "lespark.us"


def _load_cookies(sidecar: str) -> list:
    """读取 Cookie-Editor 导出的 JSON 文件，转成 Playwright add_cookies 可用结构。

    运维把账号登录态 Cookie（JSON 数组）落到 <PROFILE>/_cookies.json，resolver 每次
    启动注入，确保持久 profile 始终带最新登录态（用户贴新 Cookie 即更新，无需手登）。
    容错：缺字段/非法 sameSite 一律跳过，避免单条坏 Cookie 拖垮整次解析。
    """
    import json
    try:
        with open(sidecar, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return []
    if isinstance(raw, dict) and "cookies" in raw:
        raw = raw.get("cookies") or []
    if not isinstance(raw, list):
        return []
    out = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue
        item = {"name": name, "value": value, "path": c.get("path") or "/"}
        dom = c.get("domain") or ""
        if dom:
            item["domain"] = dom
        else:
            item["url"] = "https://" + HOST
        exp = c.get("expires")
        if isinstance(exp, (int, float)) and exp > 0:
            item["expires"] = exp
        if c.get("httpOnly"):
            item["httpOnly"] = True
        if c.get("secure"):
            item["secure"] = True
        ss = c.get("sameSite")
        if ss in ("Lax", "Strict", "None"):
            item["sameSite"] = ss
        out.append(item)
    return out


def _pick_video_id(url: str) -> str:
    m = re.search(r"/videos/([0-9a-fA-F]+)", url or "")
    return m.group(1) if m else ""


def resolve(url: str, timeout: int = 60) -> dict:
    """解析 lespark 视频，返回 dict（成功）或抛 RuntimeError（失败）。"""
    from playwright.sync_api import sync_playwright

    vid = _pick_video_id(url)
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
                "--mute-audio",
            ],
            user_agent=UA,
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )
        try:
            # 注入运维落盘的账号登录态 Cookie（<PROFILE>/_cookies.json）
            try:
                _cks = _load_cookies(os.path.join(PROFILE, "_cookies.json"))
                if _cks:
                    context.add_cookies(_cks)
            except Exception as _ce:
                print("[lespark] add_cookies 失败(已忽略): %s" % str(_ce)[:160],
                      file=sys.stderr)
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = context.new_page()
            net_hits = []

            def _on_response(resp):
                try:
                    u = resp.url or ""
                    ul = u.lower()
                    if (".m3u8" in ul) and u not in net_hits:
                        if _TCVOD_RE.search(u):
                            net_hits.append(u)
                except Exception:
                    pass

            page.on("response", _on_response)
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            m3u8_url = ""
            title = ""
            login_wall = False
            deadline = time.time() + (timeout - 10)
            while time.time() < deadline:
                if net_hits:
                    m3u8_url = net_hits[0]
                    break
                # 登录墙判定：页面提示需登录且无播放器
                try:
                    txt = page.evaluate(
                        "() => document.body ? document.body.innerText.slice(0, 600) : ''"
                    ) or ""
                    if any(k in txt for k in ("请先登录", "需要登录", "登录后观看", "Login required")):
                        try:
                            has_player = page.evaluate(
                                "() => !!document.querySelector('video') "
                                "|| !!document.querySelector('[class*=player]')"
                            )
                        except Exception:
                            has_player = False
                        if not has_player:
                            login_wall = True
                except Exception:
                    pass
                if login_wall and not net_hits:
                    break
                try:
                    page.evaluate(
                        "() => { const b=document.querySelector('.close-button,"
                        ".login-modal .close,[class*=close],.modal-close');"
                        " if(b) b.click(); }"
                    )
                    page.mouse.wheel(0, 500)
                except Exception:
                    pass
                time.sleep(1.5)

            if not m3u8_url:
                if login_wall:
                    raise RuntimeError(
                        "lespark 需要登录态才能播放该视频"
                        "（请在 VPS 持久 profile 注入账号 Cookie）"
                    )
                raise RuntimeError(
                    "未解析到 lespark 视频流（链接失效/需登录/地区限制）。"
                    "可换一条视频链接重试"
                )

            # 标题：og:title → page.title → 视频 id
            try:
                title = page.evaluate(
                    "() => { const m=document.querySelector('meta[property=\"og:title\"]');"
                    " return m ? m.content : ''; }"
                ) or ""
            except Exception:
                title = ""
            if not title:
                title = (page.title() or "").strip()
            if not title:
                title = "lespark视频"

            return {
                "ok": True,
                "title": title,
                "duration": None,
                "video_id": vid,
                "video_url": m3u8_url,
                "webpage_url": page.url or url,
                "thumbnail": "",
                "ext": "m3u8",
            }
        finally:
            context.close()
    finally:
        pw.stop()


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else ""
    if not u:
        print("用法: python lespark_resolve.py <lespark视频链接>")
        sys.exit(1)
    import json
    try:
        r = resolve(u)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:
        print("FAIL:", str(e))
        sys.exit(2)
