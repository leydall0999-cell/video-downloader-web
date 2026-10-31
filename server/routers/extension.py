"""浏览器扩展安装包下载（2026-09-28）。

把「视频工坊 媒体嗅探」Chrome MV3 扩展打包成 zip，通过下载模块对外提供：
    GET /api/extension/info      → 扩展元信息 + 安装步骤
    GET /api/extension/package   → 下载 .zip 安装包（Content-Disposition: attachment）

源码优先级（保证开发态最新、冻结包/网页版也能用）：
    1) 仓库根 <repo>/extension/        —— 开发态，永远最新
    2) <this>/assets/extension_src/    —— 冻结包 / 网页版兜底（已随包分发）

不依赖 app / 不触碰磁盘删除，可在沙盒单测中直接 import。
"""
from __future__ import annotations

import io
import json
import os
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

router = APIRouter()

_HERE = Path(__file__).resolve().parent            # server/routers
_SERVER = _HERE.parent                            # server
_REPO_EXT = _SERVER.parent / "extension"          # <repo>/extension
_BUNDLED = _SERVER / "assets" / "extension_src"   # 冻结/网页版兜底

_PKG_NAME = "视频工坊媒体嗅探"


def _is_real_download(request: Request | None) -> bool:
    """是否计入下载统计：只有 GET 才算真实下载。

    2026-10-13：两个下载端点补 HEAD 支持（下载管理器预检用，浏览器无感）。
    HEAD 会命中同一 handler，若照旧计数，「预检 + 真实下载」会让计数虚高一倍。
    故统一在这里判定：HEAD 不计，GET 计；request 缺失（异常路径）按 GET 处理。
    """
    if request is None:
        return True
    return getattr(request, "method", "GET") != "HEAD"


def _source_dir() -> Path:
    if _REPO_EXT.is_dir():
        return _REPO_EXT
    if _BUNDLED.is_dir():
        return _BUNDLED
    raise FileNotFoundError("找不到扩展源码目录（extension/ 与 assets/extension_src/ 均不存在）")


def _manifest() -> dict:
    return json.loads((_source_dir() / "manifest.json").read_text(encoding="utf-8"))


def _build_zip(src: Path) -> bytes:
    """把扩展目录打成 zip（排除测试与 .DS_Store），返回内存字节。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(src)
            if rel.parts[0] == "tests":
                continue
            if p.name == ".DS_Store":
                continue
            z.writestr(str(rel), p.read_bytes())
    return buf.getvalue()


@router.get("/api/extension/info")
def extension_info() -> dict:
    m = _manifest()
    return {
        "name": m.get("name", _PKG_NAME),
        "version": m.get("version", "1.0.0"),
        "manifest_version": m.get("manifest_version", 3),
        "download_url": "/api/extension/package",
        "min_browser": "Chrome / Edge / Brave（支持 Manifest V3）",
        "install_steps": [
            "下载并解压扩展安装包",
            "浏览器地址栏打开 chrome://extensions（Edge 为 edge://extensions）",
            "右上角打开「开发者模式」开关",
            "点击「加载已解压的扩展程序」，选择解压出来的文件夹",
            "点击工具栏扩展图标，即可嗅探网页媒体并一键发回桌面端下载",
        ],
    }


@router.api_route("/api/extension/package", methods=["GET", "HEAD"])
def extension_package(request: Request = None) -> Response:
    src = _source_dir()
    data = _build_zip(src)
    m = _manifest()
    name = m.get("name", _PKG_NAME)
    version = m.get("version", "1.0.0")
    filename = f"{name}-{version}.zip"
    ascii_name = f"vdl-sniffer-extension-{version}.zip"
    disp = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    # 🔴 下载计数（2026-10-13「app 加上安装包/扩展下载次数并统计不同平台」）：
    #    平台维度 = User-Agent 推断的浏览器。统计失败绝不让下载失败。
    #    仅计 GET：HEAD 是下载管理器「预检」（取 Content-Length/Type 判断是否续传），
    #    不是真实下载，计入会把计数虚高一倍（预检 + 下载各一次）。
    try:
        import dl_stats
        if _is_real_download(request):
            ua = (request.headers.get("user-agent") or "") if request is not None else ""
            dl_stats.record("extension", dl_stats.browser_from_ua(ua))
    except Exception:  # noqa: BLE001
        pass
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": disp,
                 "Cache-Control": "no-store"},
    )


# ── 桌面安装包下载（2026-10-13，2026-10-15 默认改 DMG + 托管 R2）──────────────
# 用户要求「安装包下载次数并统计不同平台」。
#
# 2026-10-15：安装包本体改托管 CF R2（download.hanyuxz.top，对象即
# VideoDownloader.dmg / VideoDownloader.app.zip）。原因：ECS 出口只有
# ~3.2Mbps（ecs.e-c1m1.large 固定带宽，实测聚合无增益），461MB DMG 直连
# 也要 ~20 分钟；R2 走 CF 全球边缘且 egress 免费。端点保留（下载计数 +
# 历史链接兼容）：GET/HEAD 一律 302 跳 R2；R2 探测不达时回退本机
# /opt/vdl-update/ 文件（安全网——CF 侧异常时用户至少有慢路可下）。
#
# 默认格式仍是 DMG（macOS 标准安装形态：双击挂载→拖拽），?p=macos-zip
# 为 ZIP 兜底（DMG 挂载异常环境 / 历史链接）；?p=windows 为 Inno Setup
# 安装程序（2026-10-16 上线）。
_INSTALLER_FILES: dict[str, Path] = {
    "macos": Path(os.environ.get("VDL_INSTALLER_MACOS")
                  or "/opt/vdl-update/VideoDownloader.dmg"),
    "macos-zip": Path(os.environ.get("VDL_INSTALLER_MACOS_ZIP")
                      or "/opt/vdl-update/VideoDownloader.app.zip"),
    # 2026-10-30 恢复：R2 探测不达时的本机回退（安全网，保证 CF 侧异常也下得到）
    "windows": Path(os.environ.get("VDL_INSTALLER_WINDOWS")
                    or "/opt/vdl-update/VideoDownloader-Setup-1.1.06.exe"),
}

# 🔴 2026-10-18：Windows 安装包暂时不可用（用户反馈装上后跑不起来），先把该平台
#    的下载入口整条关掉——页面按钮置灰、直连兜底撤掉、服务端 302 回下载页。
#    只藏按钮不够：知道 URL 的人仍能下到装不了的包，所以服务端同源拦截。
#    修复后把 windows 改回 True，**同时**恢复下载页按钮：下载页守卫直接读这张表，
#    漏改任一边都会红（避免「服务端开了但页面没入口」或反过来）。
_INSTALLER_ENABLED: dict[str, bool] = {
    "macos": True,
    "macos-zip": True,
    # 2026-10-30 恢复 True：下架原因是「一开下载连弹 aria2c 黑窗」，已由
    # server/win_no_window.py（CREATE_NO_WINDOW 注入）修复，CI 有真机断言
    # （desktop/ci_assert_win_no_window.py）。下载页按钮已同步还原。
    "windows": True,
}

# R2 公共地址（自定义域 download.hanyuxz.top → 桶 vdl-download 根）。
# 2026-10-16：?p=windows 上线（Windows 版首发，Inno Setup 安装程序）。
# ⚠️ Windows 安装包版本号硬编码在 R2 对象名里（与 mac 两个对象同形态）：每次升版
#    Windows 版都要同步改这里 + web/download/index.html 的大小文案，别漏。
_R2_INSTALLER_URLS: dict[str, str] = {
    "macos": os.environ.get("VDL_R2_INSTALLER_MACOS")
             or "https://download.hanyuxz.top/VideoDownloader.dmg",
    "macos-zip": os.environ.get("VDL_R2_INSTALLER_MACOS_ZIP")
                 or "https://download.hanyuxz.top/VideoDownloader.app.zip",
    "windows": os.environ.get("VDL_R2_INSTALLER_WINDOWS")
               or "https://download.hanyuxz.top/VideoDownloader-Setup-1.1.06.exe",
}

# 安装包媒体类型按扩展名取（DMG≠ZIP≠EXE，写死 zip 会让浏览器把 dmg 也当 zip 处理）。
# .exe 用 x-msdownload：IE/Edge  legacy 行为；现代浏览器对 octet-stream 与
# x-msdownload 都直接下载不内嵌，此处取语义更精确的一个。
_INSTALLER_MEDIA_TYPES: dict[str, str] = {
    ".dmg": "application/x-apple-diskimage",
    ".zip": "application/zip",
    ".exe": "application/x-msdownload",
}

_R2_HEALTH_TIMEOUT = 3.0

# —— 安装包元信息（让下载页的体积/版本跟着真实文件走，不再写死）——————————
# 背景：`web/download/index.html` 原来把「296 MB / 270 MB」写死在文案里，每次发版
# 都要人工改，漏改就会出现「页面写 296MB、实际下到 290MB」。这里直接 HEAD 真实对象
# 取体积与更新时间；版本号取对象自定义元数据（上传时写 x-amz-meta-version）。
# 只读无副作用；结果缓存 5 分钟，避免每访问一次页面就打一次 R2。
_INSTALLER_META_TTL = 300.0
_INSTALLER_META_CACHE: dict = {"ts": 0.0, "payload": {}}

# 版本号来源（2026-10-28）：Cloudflare 自定义域转发 R2 **会剥掉 x-amz-meta-***，
# 所以 HEAD 永远拿不到 version，下载页只能退回「更新于某日」。这里改读发版机本机的
# 权威发布清单（publish_update.sh 每次发版都会写 latest.json），并做**绑定校验**：
# 只有安装包与这次发布对得上（体积/日期）才标版本，绝不凭空标一个可能撒谎的号。
_LATEST_JSON_PATH = os.environ.get("VDL_RELEASE_LATEST") or "/opt/vdl-update/latest.json"


def _read_release_info() -> dict:
    """读本机发版清单；取不到就返回 {}（体积/日期照旧，版本留空）。"""
    try:
        with open(_LATEST_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and str(data.get("version") or "").strip():
            return data
    except Exception:  # noqa: BLE001 — 清单缺失/损坏等价于「版本未知」
        pass
    return {}


def _bind_release_version(info: dict, rel: dict, plat: str) -> None:
    """把发布版本号绑到该安装包上；对不上就不绑（页面退回显示更新日期）。"""
    if info.get("version") or not rel:
        return
    ver = str(rel.get("version") or "").strip()
    if not ver or not info.get("updated"):
        return
    pub = str(rel.get("published_at") or "").strip()
    if pub and info["updated"] < pub:
        return  # 安装包比这次发布还旧 ⇒ 不是同一版，不标
    size = rel.get("size")
    if (plat == "macos-zip" and isinstance(size, int) and size > 0
            and info.get("size") != size):
        return  # 体积对不上 ⇒ 不是该次发布的产物，不标
    info["version"] = ver


def _head_installer(url: str, timeout: float = 4.0) -> dict:
    import email.utils
    import urllib.request

    info = {"size": 0, "updated": "", "version": ""}
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": "vdl-installer-meta/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        info["size"] = int(resp.headers.get("Content-Length") or 0)
        last_mod = resp.headers.get("Last-Modified") or ""
        if last_mod:
            try:
                info["updated"] = (email.utils.parsedate_to_datetime(last_mod)
                                   .astimezone().strftime("%Y-%m-%d"))
            except Exception:  # noqa: BLE001 — 时间格式异常不影响体积
                pass
        ver = (resp.headers.get("x-amz-meta-version") or "").strip()
        if ver:
            info["version"] = ver
    return info


@router.get("/download/meta")
def download_meta() -> dict:
    """返回各平台安装包的真实体积 / 更新时间 / 版本（给下载页自动渲染）。"""
    now = time.time()
    cached = _INSTALLER_META_CACHE
    if cached["payload"] and now - float(cached["ts"] or 0) < _INSTALLER_META_TTL:
        return cached["payload"]
    rel = _read_release_info()
    platforms: dict[str, dict] = {}
    # 2026-10-30：列表补上 windows，否则下载页拿不到 Windows 包的体积，
    # 只能显示 HTML 里写死的兜底文案（发版换包就会对不上）。
    for plat in ("macos", "macos-zip", "windows"):
        url = _R2_INSTALLER_URLS.get(plat)
        if not url or not _INSTALLER_ENABLED.get(plat, True):
            continue
        try:
            platforms[plat] = _head_installer(url)
            # 🔴 Windows 有独立清单 latest-win.json，不能拿 mac 的 latest.json 绑版本
            #    （2026-10-30 实测：会把 mac 的 v1.1.05 标到 Windows 包上，而实际是
            #    1.1.06）。这里对 windows 跳过绑定，页面如实显示「安装包更新于 X 日」。
            if plat != "windows":
                _bind_release_version(platforms[plat], rel, plat)
        except Exception:  # noqa: BLE001 — 取不到就留空，页面回退静态文案
            platforms[plat] = {"size": 0, "updated": "", "version": ""}
    payload = {"ok": True, "platforms": platforms}
    cached["ts"], cached["payload"] = now, payload
    return payload


def _r2_reachable(url: str) -> bool:
    """HEAD 探测 R2 公共地址是否可用（决定 302 还是回退本机）。

    任何异常（超时/DNS/非 2xx-3xx）都按不可达处理——下载慢没关系，
    但不能让用户面对错误页。
    """
    try:
        import urllib.request
        req = urllib.request.Request(
            url, method="HEAD",
            headers={"User-Agent": "vdl-installer-health/1.0"})
        with urllib.request.urlopen(req, timeout=_R2_HEALTH_TIMEOUT) as resp:
            return 200 <= resp.status < 400
    except Exception:  # noqa: BLE001
        return False


@router.api_route("/download/installer", methods=["GET", "HEAD"])
def download_installer(p: str = "macos", request: Request = None) -> Response:
    plat = str(p or "").strip().lower()
    url = _R2_INSTALLER_URLS.get(plat)
    if url is None:
        raise HTTPException(status_code=404, detail="该平台的安装包尚未提供")
    # 平台级开关（2026-10-18）：关闭期间不下发文件，直接送回下载页——那里有
    # 「修复中」的说明，比 404 的 JSON 对用户友好，也不会让人误以为链接坏了。
    if not _INSTALLER_ENABLED.get(plat, True):
        return RedirectResponse("/download/", status_code=302,
                                headers={"Cache-Control": "no-store"})
    # 仅计 GET：HEAD 是预检，不计入下载次数（口径同扩展包）。
    try:
        import dl_stats
        if _is_real_download(request):
            # 统计按平台归并：macos-zip 并入 macos，不按包装格式拆行。
            dl_stats.record("installer", plat.split("-")[0])
    except Exception:  # noqa: BLE001
        pass
    # R2 可达 → 302 到 CF 边缘（用户直连全速）；不可达 → 回退本机文件。
    if _r2_reachable(url):
        return RedirectResponse(url, status_code=302,
                                headers={"Cache-Control": "no-store"})
    path = _INSTALLER_FILES.get(plat)
    if path is not None and path.is_file():
        media_type = _INSTALLER_MEDIA_TYPES.get(
            path.suffix.lower(), "application/octet-stream")
        return FileResponse(path, media_type=media_type,
                            filename=path.name,
                            headers={"Cache-Control": "no-store"})
    raise HTTPException(status_code=502, detail="安装包下载通道暂时不可用，请稍后重试")
