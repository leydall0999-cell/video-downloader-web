#!/usr/bin/env python3
"""liblib.tv（AI 绘图 / canvas 分享）解析 —— VPS Playwright worker。

liblib 的 /canvas/share?spaceId=..&projectId=.. 分享的是 AI 生成的成图（非视频）。
分享页需登录态（游客态弹「登录失效」），故用持久化登录态 profile（liblib_profile）
注入账号 Cookie，真实浏览器加载分享画布，拦截图片直链（content-type image/* 或
.jpg/.png/.webp 后缀）返回给用户下载。

对外暴露 resolve(url) 供守护进程 vdl_cookie_daemon.py 复用。
"""
import os
import re
import sys
import time

PROFILE = "/opt/vdl-worker/liblib_profile"
HOST = "liblib.tv"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


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


def resolve(url: str, timeout: int = 60) -> dict:
    """解析 liblib 分享画布里的成图，返回 dict（成功）或抛 RuntimeError（失败）。"""
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
                "--mute-audio",
            ],
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        try:
            # 注入运维落盘的账号登录态 Cookie（<PROFILE>/_cookies.json）
            try:
                _cks = _load_cookies(os.path.join(PROFILE, "_cookies.json"))
                if _cks:
                    context.add_cookies(_cks)
            except Exception as _ce:
                print("[liblib] add_cookies 失败(已忽略): %s" % str(_ce)[:160],
                      file=sys.stderr)
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = context.new_page()
            img_hits = []

            def _on_response(resp):
                try:
                    ct = (resp.headers or {}).get("content-type", "") or ""
                    u = resp.url or ""
                    if ("image/" in ct.lower()) and u not in img_hits:
                        img_hits.append(u)
                except Exception:
                    pass

            page.on("response", _on_response)
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            login_wall = False
            title = ""
            deadline = time.time() + (timeout - 10)
            while time.time() < deadline:
                # DOM 里的 img src 也收集（懒加载的图）
                try:
                    dom_imgs = page.evaluate(
                        "() => Array.from(document.images)"
                        ".map(i => i.currentSrc || i.src)"
                        ".filter(s => s && /^https?:/.test(s))"
                    ) or []
                    for s in dom_imgs:
                        if s not in img_hits:
                            img_hits.append(s)
                except Exception:
                    pass
                if img_hits:
                    break
                try:
                    txt = page.evaluate(
                        "() => document.body ? document.body.innerText.slice(0, 600) : ''"
                    ) or ""
                    if any(k in txt for k in ("登录失效", "请登录", "Login required", "登录后查看")):
                        login_wall = True
                        break
                except Exception:
                    pass
                try:
                    page.mouse.wheel(0, 600)
                except Exception:
                    pass
                time.sleep(1.5)

            # 标题
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
                title = "liblib分享"

            if not img_hits:
                if login_wall:
                    raise RuntimeError(
                        "liblib 分享页需要登录态"
                        "（请在 VPS 持久 profile 注入账号 Cookie）"
                    )
                raise RuntimeError(
                    "未从 liblib 分享页解析到图片（链接失效/需登录/画布为空）。"
                    "可换一条分享链接重试"
                )

            # 去重，保留顺序
            seen = []
            for s in img_hits:
                if s not in seen:
                    seen.append(s)
            images = seen[:50]

            return {
                "ok": True,
                "title": title,
                "video_id": "",
                "images": images,
                "webpage_url": page.url or url,
                "thumbnail": images[0] if images else "",
                "ext": "jpg",
                # 兼容下游视频管线：把第一张图当作 video_url，便于走直链下载单图；
                # 多图全部保留在 images 字段，供前端多图下载（待 UI 支持）。
                "video_url": images[0] if images else "",
            }
        finally:
            context.close()
    finally:
        pw.stop()


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else ""
    if not u:
        print("用法: python liblib_resolve.py <liblib分享链接>")
        sys.exit(1)
    import json
    try:
        r = resolve(u)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:
        print("FAIL:", str(e))
        sys.exit(2)
