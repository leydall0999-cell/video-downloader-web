"""安装包端点「302 跳 R2 + 本机兜底」契约回归（2026-10-15）。

背景：`/download/installer?p=macos` 原在本机流式回给用户（FileResponse）。
2026-10-15 安装包本体改托管 CF R2（download.hanyuxz.top）：ECS 出口仅
~3.2Mbps，461MB DMG 直连也要 ~20 分钟；R2 走 CF 全球边缘、egress 免费。
端点保留（下载计数 + 历史链接兼容），GET/HEAD 一律 302 跳 R2；R2 探测
不达时回退本机 /opt/vdl-update/ 文件（安全网）。

本测试钉住七件事：
  1. `?p=macos` → 302：Location 指向 R2 上的 DMG、Cache-Control no-store；
  2. `?p=macos-zip` → 302：Location 指向 R2 上的 ZIP；
  3. `?p=windows` → 302：Location 指向 R2 上的 Inno Setup 安装程序（2026-10-16）；
  4. 统计口径按平台归且只计 GET：macos-zip 也记 "installer/macos"，
     windows 记 "installer/windows"，HEAD（下载管理器预检）不计；
  5. R2 探测不达 → 回退本机：200 + dmg 媒体类型 + 真实文件字节；
  6. 默认映射源码级回归：R2 地址、/opt/vdl-update 兜底路径、
     媒体类型映射都在（防手滑改回单一通道）；
  7. 未知平台（如 linux）仍 404。

不联网（_r2_reachable 打桩）、不写真实统计文件（record 替换为内存记录）、
退出还原。
"""
import inspect
import os
import sys
import tempfile
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))
os.environ.setdefault("VDL_DATA_DIR", tempfile.mkdtemp(prefix="vdl_r2_"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import dl_stats  # noqa: E402
from routers import extension as ext  # noqa: E402

_R2_DMG = "https://download.hanyuxz.top/VideoDownloader.dmg"
_R2_ZIP = "https://download.hanyuxz.top/VideoDownloader.app.zip"
# 不在这里写死 exe 地址：版本号内嵌在对象名里，写死过一次（1.0.74）与产品脱钩。
# 期望值一律从 `ext._R2_INSTALLER_URLS[...]` 取。


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(ext.router)
    return TestClient(app)


class _Recorder:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, bucket, platform, n=1):
        self.calls.append((bucket, platform))
        return {}


def _with_recorder(fn):
    rec = _Recorder()
    orig = dl_stats.record
    dl_stats.record = rec
    try:
        fn(rec)
    finally:
        dl_stats.record = orig


def _patch_reachable(value: bool):
    orig = ext._r2_reachable
    ext._r2_reachable = lambda url: value
    return orig


def _fake_packages():
    """造一个假 DMG + 假 ZIP，返回 (dmg_path, zip_path, 清理函数)。"""
    dmg = tempfile.NamedTemporaryFile(suffix=".dmg", delete=False)
    dmg.write(b"\x00" * 2048)  # DMG 无简单魔数，用零填充占位
    dmg.close()
    zp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    zp.write(b"PK\x03\x04" + b"z" * 100)
    zp.close()

    def cleanup():
        for p in (dmg.name, zp.name):
            try:
                os.unlink(p)
            except OSError:
                pass

    return Path(dmg.name), Path(zp.name), cleanup


def test_macos_default_redirects_to_r2():
    """p=macos → 302：Location 指向 R2 上的 DMG，no-store。"""
    orig = _patch_reachable(True)
    try:
        c = _client()
        for method in ("head", "get"):
            r = getattr(c, method)("/download/installer?p=macos",
                                   follow_redirects=False)
            assert r.status_code == 302, \
                f"{method.upper()} 应 302，实际 {r.status_code}"
            assert r.headers.get("location") == _R2_DMG, \
                f"Location 应指向 R2 DMG: {r.headers.get('location')}"
            assert r.headers.get("cache-control") == "no-store"
    finally:
        ext._r2_reachable = orig


def test_macos_zip_fallback_redirects():
    """p=macos-zip → 302：Location 指向 R2 上的 ZIP。"""
    orig = _patch_reachable(True)
    try:
        c = _client()
        r = c.head("/download/installer?p=macos-zip", follow_redirects=False)
        assert r.status_code == 302, r.status_code
        assert r.headers.get("location") == _R2_ZIP, \
            f"Location 应指向 R2 ZIP: {r.headers.get('location')}"
    finally:
        ext._r2_reachable = orig


def test_windows_installer_gate_follows_switch():
    """windows 开关闸门：GET/HEAD 一律 302，去向随 `_INSTALLER_ENABLED` 自适应。

    关闭期（2026-10-18 起，用户反馈 Win 包装上跑不起来）：一律 302 回
    /download/。只把页面按钮置灰不够 —— 知道 URL 的人仍能下到装不了的包，
    所以服务端同源拦截。
    2026-10-31：R2 上的坏包已用 CI 官方产物覆盖修复，开关重开 ⇒ 本用例自动
    转为「必须 302 到 R2 exe」的正向断言（避免重开了但路由还写死回下载页）。
    """
    enabled = bool(ext._INSTALLER_ENABLED.get("windows"))
    expected = ext._R2_INSTALLER_URLS["windows"] if enabled else "/download/"
    orig = _patch_reachable(True)
    try:
        c = _client()
        for method in ("head", "get"):
            r = getattr(c, method)("/download/installer?p=windows",
                                   follow_redirects=False)
            assert r.status_code == 302, \
                f"{method.upper()} 应 302，实际 {r.status_code}"
            assert r.headers.get("location") == expected, \
                f"{method.upper()} 应去 {expected}，实际 {r.headers.get('location')}"
            assert r.headers.get("cache-control") == "no-store"
    finally:
        ext._r2_reachable = orig


def test_windows_enabled_redirects_to_r2():
    """开关打开后的契约（桩模拟）：302 到 R2 上的 Inno Setup 安装程序 + 计数。

    🔴 期望地址**从源码取**，不写死版本号：写死过 `...-Setup-1.0.74.exe`，
    而 R2 上最新已是 1.0.76 ⇒ 用例与产品一起「假绿」，等真开窗口时给用户
    下发一个旧包（本次漂移就是这么攒下的）。
    """
    orig = _patch_reachable(True)
    orig_flag = ext._INSTALLER_ENABLED.get("windows")
    ext._INSTALLER_ENABLED["windows"] = True
    try:
        def body(rec):
            c = _client()
            for method in ("head", "get"):
                r = getattr(c, method)("/download/installer?p=windows",
                                       follow_redirects=False)
                assert r.status_code == 302, \
                    f"{method.upper()} 应 302，实际 {r.status_code}"
                assert r.headers.get("location") == ext._R2_INSTALLER_URLS["windows"], \
                    f"Location 应指向 R2 exe: {r.headers.get('location')}"
                assert r.headers.get("cache-control") == "no-store"
            assert rec.calls == [("installer", "windows")], \
                f"windows 应独立计一行且 HEAD 不计，实际 {rec.calls}"
        _with_recorder(body)
    finally:
        ext._INSTALLER_ENABLED["windows"] = orig_flag
        ext._r2_reachable = orig


def test_stats_merged_by_platform():
    """计数口径：macos-zip 并入 macos；HEAD 不计、GET 计；**关闭的平台不计数**。"""
    orig = _patch_reachable(True)
    try:
        def body(rec):
            c = _client()
            c.head("/download/installer?p=macos-zip", follow_redirects=False)
            assert rec.calls == [], f"HEAD 不应计数，实际 {rec.calls}"
            c.get("/download/installer?p=macos", follow_redirects=False)
            c.get("/download/installer?p=macos-zip", follow_redirects=False)
            assert rec.calls == [("installer", "macos"), ("installer", "macos")], \
                f"macos-zip 应归并进 macos，实际 {rec.calls}"
            # 平台开关决定「要不要下发」：关闭期（2026-10-18 起，Win 装不上）
            # 一个字节都没下发 ⇒ 不该计数，否则后台长期挂着「有人在下 Windows」
            # 的假数据，排障被带偏；2026-10-31 重开后则必须正常计一行。
            n_before = len(rec.calls)
            r = c.get("/download/installer?p=windows", follow_redirects=False)
            assert r.status_code == 302, f"应 302，实际 {r.status_code}"
            if ext._INSTALLER_ENABLED.get("windows"):
                assert r.headers.get("location") == ext._R2_INSTALLER_URLS["windows"], \
                    f"开放的 windows 应去 R2 exe，实际 {r.headers.get('location')}"
                assert len(rec.calls) == n_before + 1, \
                    f"开放的平台应计一行，实际 {rec.calls}"
            else:
                assert r.headers.get("location") == "/download/", \
                    f"关闭的平台应回下载页，实际 {r.headers.get('location')}"
                assert len(rec.calls) == n_before, \
                    f"关闭的平台不应计数，实际 {rec.calls}"
        _with_recorder(body)
    finally:
        ext._r2_reachable = orig


def test_fallback_to_local_when_r2_unreachable():
    """R2 探测不达 → 回退本机文件：200 + dmg 类型 + 真实字节。"""
    dmg, zp, cleanup = _fake_packages()
    orig_files = ext._INSTALLER_FILES
    orig_reach = _patch_reachable(False)
    ext._INSTALLER_FILES = {"macos": dmg, "macos-zip": zp}
    try:
        c = _client()
        r = c.head("/download/installer?p=macos")
        assert r.status_code == 200, f"回退应 200，实际 {r.status_code}"
        ct = r.headers.get("content-type", "")
        assert ct.startswith("application/x-apple-diskimage"), \
            f"回退的 DMG Content-Type 错: {ct}"
        assert r.headers.get("content-length") == "2048"
        r2 = c.get("/download/installer?p=macos")
        assert r2.content == b"\x00" * 2048, "回退 GET body 应为本机 DMG 内容"
    finally:
        ext._r2_reachable = orig_reach
        ext._INSTALLER_FILES = orig_files
        cleanup()


def test_default_mapping_source_guard():
    """源码级回归：R2 地址 + 本机兜底路径 + 媒体类型映射都在。"""
    src = inspect.getsource(ext)
    assert "https://download.hanyuxz.top/VideoDownloader.dmg" in src, \
        "macos 默认应 302 到 R2 DMG"
    assert "https://download.hanyuxz.top/VideoDownloader.app.zip" in src, \
        "macos-zip 兜底应 302 到 R2 ZIP"
    assert "https://download.hanyuxz.top/VideoDownloader-Setup-" in src, \
        "windows 默认应 302 到 R2 Inno Setup 安装程序"
    assert '"macos-zip"' in src, "映射必须有 macos-zip 兜底键"
    assert '"windows"' in src, "映射必须有 windows 键（2026-10-16 上线）"
    assert "/opt/vdl-update/VideoDownloader.dmg" in src, \
        "R2 不可达时的本机兜底路径缺失"
    assert "/opt/vdl-update/VideoDownloader.app.zip" in src, \
        "R2 不可达时的本机兜底 ZIP 路径缺失"
    assert '".dmg": "application/x-apple-diskimage"' in src, \
        "DMG 媒体类型映射缺失"
    assert '".exe": "application/x-msdownload"' in src, \
        "EXE 媒体类型映射缺失（Windows 安装程序）"
    assert "RedirectResponse(url, status_code=302" in src, \
        "端点应 302 跳 R2（改回直连会让下载又慢回去）"


def test_unknown_platform_still_404():
    """未知平台（如 linux）仍 404（不因新增 key 放行）。"""
    orig = _patch_reachable(True)
    try:
        c = _client()
        for p in ("linux", "android", ""):
            r = c.get(f"/download/installer?p={p}", follow_redirects=False)
            assert r.status_code == 404, f"p={p!r} 应 404，实际 {r.status_code}"
    finally:
        ext._r2_reachable = orig


if __name__ == "__main__":  # pragma: no cover
    import traceback
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception:
            failed += 1
            traceback.print_exc()
    print("FAILED" if failed else "ALL PASS", failed)
    sys.exit(1 if failed else 0)
