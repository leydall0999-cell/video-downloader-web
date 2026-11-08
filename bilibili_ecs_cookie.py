#!/usr/bin/env python3
"""ECS（VPS）上用 Playwright 无头浏览器过 B站 412 风控，导出 Cookie 推送到网页版公共池。

为什么需要它：
  B站 视频页对无头/海外/无登录环境返回 412 验证页（security.bilibili.com/412.js），
  该页需要真实浏览器执行 JS 才能完成验证。纯 requests/curl 拿不到「验证通过」的 Cookie，
  所以网页版（Railway 海外 IP）即使用 VDL_PROXY_CN 走了国内出口，仍被 412 拦。
  本脚本在【国内 VPS】用无头 Chromium 真实访问 B站、自动完成 412 验证，
  导出匿名初始化 Cookie（buvid3/buvid4/b_nut 等），推送到 Railway 公共池。
  网页端（Railway）借 VDL_PROXY_CN 走这台 VPS 的国内出口 + 这枚 Cookie，即可绕过 412。

与抖音脚本 douyin_ecs_login.py 的区别：
  抖音需要「登录」（手机号+验证码），B站 只需「过匿名风控」，无需登录、无需交互。
  （可选）若提供 --sessdata，则注入登录态导出完整 Cookie，能下更高清晰度。

用法：
  .venv/bin/python bilibili_ecs_cookie.py --url https://hanyuxz.top --token <云端同步令牌>
  .venv/bin/python bilibili_ecs_cookie.py --url https://hanyuxz.top --token <令牌> --sessdata <SESSDATA>
  .venv/bin/python bilibili_ecs_cookie.py --url https://hanyuxz.top --token <令牌> --loop --interval 86400

令牌(url/token)也可走环境变量 VDL_COOKIE_SYNC_URL / VDL_COOKIE_SYNC_TOKEN。

对外暴露 push_once(url, token, sessdata) 供守护进程 vdl_cookie_daemon.py 复用。
"""
import argparse
import os
import sys
import time
from pathlib import Path

PROFILE = "/opt/vdl-worker/bili_profile"
_TRIGGER_VIDEO = "https://www.bilibili.com/video/BV1gtgE6AEmZ"

# 纯缓存目录：只存 HTTP/媒体/GPU 缓存，删了不影响登录态。
# 登录态在 Cookies / Local Storage / IndexedDB / Login Data，均不在此列，绝不可删。
_CACHE_DIRS = (
    "Default/Cache",
    "Default/Code Cache",
    "Default/GPUCache",
    "Default/DawnWebGPUCache",
    "Default/DawnGraphiteCache",
    "Default/Service Worker/CacheStorage",
    "Default/Service Worker/ScriptCache",
)


def _profile_in_use() -> bool:
    """是否有其它进程正持有本 profile（含卡死残留的 Chromium）。

    🔴 只认 Chromium 的 `--user-data-dir=<PROFILE>` 标记，**不能**用裸路径子串匹配：
    父进程（cron / shell / 运维命令）的 cmdline 里只要出现该路径就会假阳性，
    实测导致清理恒被跳过、缓存继续膨胀（2026-11-06）。
    读 /proc/*/cmdline 判断；非 Linux（如本机跑单测）一律视为未占用。
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    marker = ("--user-data-dir=" + PROFILE).encode()
    me = str(os.getpid())
    try:
        for d in proc.iterdir():
            if not d.name.isdigit() or d.name == me:
                continue
            try:
                cl = (d / "cmdline").read_bytes()
            except OSError:
                continue
            if marker in cl:
                return True
    except OSError:
        return True  # 读不到就保守放弃清理
    return False


def _clear_profile_caches() -> int:
    """启动前清空 Chromium 纯缓存目录，返回删除的文件数。

    为什么必须清：`--disk-cache-size` 只在**长期运行**的浏览器里靠定期 trim 生效，
    短命会话（每次采集十几秒）来不及回收。采集循环每 5 分钟一轮、每轮写 ~2.5MB
    页面/媒体缓存 ⇒ 实测 24h 涨回 716MB（2026-11-06），与 11-05 整机 iowait 雪崩
    同源。缓存对「导出 Cookie」零价值，故每轮启动前直接清空。
    """
    if _profile_in_use():
        return 0
    n = 0
    for rel in _CACHE_DIRS:
        p = Path(PROFILE) / rel
        if not p.is_dir():
            continue
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
    return n


def _need_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except ImportError:
        print("❌ 未安装 playwright。请先执行：")
        print("   cd /opt/vdl-worker && .venv/bin/pip install playwright "
              "&& .venv/bin/python -m playwright install chromium")
        sys.exit(1)


def launch(sessdata: str = ""):
    from playwright.sync_api import sync_playwright

    Path(PROFILE).mkdir(parents=True, exist_ok=True)
    # 每轮启动前清空纯缓存（登录态不动）：短命会话不会触发 Chromium 的缓存回收，
    # 不清则 5 分钟一轮会把 Cache 涨到 700MB+/天。
    _clear_profile_caches()
    pw = sync_playwright().start()
    # 🔴 start() 成功之后任何一步抛错都必须 stop()：playwright driver 是一个独立的
    # node 子进程（run-driver，11 线程），不 stop 就永久滞留。vdl-cookie 的
    # TasksMax=64，5 个滞留 driver（55 线程）就把 cgroup 打满，之后每轮采集都在
    # sync_playwright().start() 处 BlockingIOError(Errno 11) 而彻底停摆
    # （2026-11-09 实测连续 11 小时采集全失败）。
    try:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE,
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                # 🔴 限制磁盘缓存：本 profile 的唯一价值是**登录态**（Cookies / Local
                # Storage），HTTP/媒体缓存是纯浪费。1.6GB 内存机上该缓存曾膨胀到
                # 739MB（profile 共 767MB），每次收集都要在磁盘缓存上反复换页，
                # 直接表现就是渲染器卡在 D 状态、读盘 60MB/s、整机 iowait 90%
                # （2026-11-05 事故）。限制后缓存不再增长，收集稳定在几十秒内完成。
                "--disk-cache-size=10485760",
                "--media-cache-size=10485760",
            ],
            # 🔴 必须显式剔除 Playwright 的默认开关 --disable-dev-shm-usage：
            # 它会把 Chromium 共享内存从 /dev/shm（本机 805MB tmpfs，实测 0% 占用）
            # 改放到 /tmp 所在的**系统盘**。1.6GB 小内存机上内存压力一上来，渲染器就
            # 反复换入换出这些磁盘 shm 文件，形成 50MB/s 级读盘死循环，进程卡在 D 状态
            # 连 SIGKILL 都杀不掉（2026-11-05 实测持续 12min、整机 iowait 90%、load 12、
            # 期间用户解析全被判「解析服务正忙」）。
            # 注意：只从 args 里去掉没用 —— 该参数在 playwright driver 的默认开关表里
            # （coreBundle.js chromiumSwitches），必须走 ignore_default_args 才真正生效。
            ignore_default_args=["--disable-dev-shm-usage"],
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        if sessdata:
            page = context.new_page()
            page.goto("https://www.bilibili.com/", wait_until="domcontentloaded", timeout=30000)
            context.add_cookies([{
                "name": "SESSDATA",
                "value": sessdata,
                "domain": ".bilibili.com",
                "path": "/",
            }])
            page.close()
    except BaseException:
        try:
            pw.stop()
        except Exception:
            pass
        raise
    return pw, context


def _verify_bili_cookie(header: str):
    """用 nav 接口判断 Cookie 是否已渡过 412 风控，返回 (passed, hit_slider)。

    🔴 为什么不再查 DOM：视频页渲染器在 2 核 / 1.6GB 机器上会把主线程堵死，
    `page.locator(...).is_visible()` 这种 CDP 查询会**无限期挂住**（实测 100s+
    不返回，且无超时可打断），直接导致整轮收集卡死、拖垮整机（2026-11-05）。
    改成纯 HTTP 校验：拿到 JSON（code=0 已登录 / -101 未登录）即说明风控已过；
    返回 412 或非 JSON 说明仍被拦（大概率命中滑块）。
    """
    import json
    import urllib.request

    req = urllib.request.Request(
        "https://api.bilibili.com/x/web-interface/nav",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Cookie": header,
            "Referer": "https://www.bilibili.com/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read()
        data = json.loads(raw.decode("utf-8", "replace"))
        code = data.get("code")
        return code in (0, -101), code == -352
    except Exception:
        # 412 验证页 / 网络异常：一律视为未通过，交给下一轮重试
        return False, True


def _collect_cookies(sessdata: str = ""):
    pw, context = launch(sessdata)
    try:
        page = context.new_page()
        page.goto("https://www.bilibili.com/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        # 访问视频页只为触发并渡过 412 风控；**绝不在此查 DOM**（见 _verify_bili_cookie）。
        page.goto(_TRIGGER_VIDEO, wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)  # 给风控 JS 留执行时间
        cookies = context.cookies()
        header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        passed, hit_slider = _verify_bili_cookie(header)
        return header, passed, hit_slider, cookies
    finally:
        # close 也可能因渲染器卡死而挂住；失败就放弃，由调用方/超时兜底清理进程
        try:
            context.browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass


def _push_to_cloud(url: str, token: str, header: str) -> dict:
    import json
    import urllib.request

    target = url.rstrip("/") + "/api/cookie/sync"
    body = json.dumps({"token": token, "domain": "bilibili.com", "cookie": header}).encode()
    req = urllib.request.Request(
        target,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


def push_once(url: str, token: str, sessdata: str = "") -> tuple:
    """收集并推送一次，返回 (header, resp_dict)。守护进程与 CLI 共用。"""
    header, passed, hit_slider, cookies = _collect_cookies(sessdata)
    print(f"✅ 收集到 {len(cookies)} 条 Cookie；验证页通过={'是' if passed else '否'}"
          f"{'；⚠️命中滑块需 VNC 手动过一次' if hit_slider else ''}", flush=True)
    if hit_slider:
        print("   本次未成功过风控，Cookie 可能仍带 412；稍后重试或改用 --sessdata 注入登录态。", flush=True)
    try:
        resp = _push_to_cloud(url, token, header)
        print("   推送结果：", resp, flush=True)
        return header, resp
    except Exception as e:
        print("   ❌ 推送失败：", str(e)[:200], flush=True)
        return header, {"error": str(e)[:200]}


def main():
    _need_playwright()
    ap = argparse.ArgumentParser(description="ECS 无头过 B站 412 并推送 Cookie 到网页版公共池")
    ap.add_argument("--url", default=os.environ.get("VDL_COOKIE_SYNC_URL", ""))
    ap.add_argument("--token", default=os.environ.get("VDL_COOKIE_SYNC_TOKEN", ""))
    ap.add_argument("--sessdata", default="", help="可选：注入 SESSDATA 导出完整登录态 Cookie")
    ap.add_argument("--loop", action="store_true", help="循环刷新")
    ap.add_argument("--interval", type=int, default=86400, help="循环间隔（秒）")
    args = ap.parse_args()

    if not args.url or not args.token:
        print("❌ 需要 --url 和 --token（或设置环境变量 VDL_COOKIE_SYNC_URL / VDL_COOKIE_SYNC_TOKEN）")
        sys.exit(1)

    while True:
        push_once(args.url, args.token, args.sessdata)
        if not args.loop:
            break
        print(f"   等待 {args.interval}s 后刷新...", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
