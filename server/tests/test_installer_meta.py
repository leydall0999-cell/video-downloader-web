"""下载页「体积/版本自动跟随」的回归守卫（2026-10-27）。

背景：`web/download/index.html` 原来把「DMG · 296 MB」写死在文案里，每次发版都要
人工同步改；漏改就会出现「页面写 296MB、实际下到 290MB」。现在页面改为访问
`GET /download/meta`，由服务端 HEAD 真实对象拿体积/更新时间/版本。

本测试钉住（不联网，全部打桩）：
  1. 正常路径：返回各平台 size / updated / version，字段齐全；
  2. 缓存：TTL 内只 HEAD 一次（避免每访问一次页面就打一次 R2）；
  3. 异常路径：HEAD 失败时返回 size=0 而不是 500（页面回退静态文案）；
  4. 已关闭的平台不出现（开关在 extension.py 的 `_INSTALLER_ENABLED`；
     Windows 于 2026-10-18 关闭、2026-10-31 用 CI 官方产物修复后重开）。
"""
import os
import sys
import tempfile
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]
_ROOT = _SERVER.parent
for _p in (str(_SERVER), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("VDL_DATA_DIR", tempfile.mkdtemp(prefix="vdl_meta_"))


def _fresh(monkeypatch, head):
    from routers import extension as ext

    calls = []

    def fake_head(url, timeout=4.0):
        calls.append(url)
        return head(url)

    monkeypatch.setattr(ext, "_head_installer", fake_head)
    monkeypatch.setattr(ext, "_INSTALLER_META_CACHE", {"ts": 0.0, "payload": {}})
    return ext, calls


def test_meta_reports_size_and_version(monkeypatch):
    def head(url):
        assert url.startswith("https://"), url
        return {"size": 308385889, "updated": "2026-10-27", "version": "1.0.97"}

    ext, calls = _fresh(monkeypatch, head)
    data = ext.download_meta()

    assert data["ok"] is True
    mac = data["platforms"]["macos"]
    assert mac["size"] == 308385889
    assert mac["version"] == "1.0.97"
    assert mac["updated"] == "2026-10-27"
    # 每个**开放**的平台各 HEAD 一次（windows 2026-10-31 重开 → 现为 3 次）
    enabled = [p for p in ("macos", "macos-zip", "windows")
               if ext._INSTALLER_ENABLED.get(p, True)]
    assert len(calls) == len(enabled), f"应 HEAD {len(enabled)} 次，实际 {calls}"


def test_meta_is_cached_within_ttl(monkeypatch):
    def head(url):
        return {"size": 123, "updated": "", "version": ""}

    ext, calls = _fresh(monkeypatch, head)
    ext.download_meta()
    first = len(calls)
    ext.download_meta()
    assert len(calls) == first, "TTL 内重复访问不应再次 HEAD 对象存储"


def test_meta_degrades_gracefully_on_error(monkeypatch):
    def head(url):
        raise OSError("network down")

    ext, _ = _fresh(monkeypatch, head)
    data = ext.download_meta()

    assert data["ok"] is True, "取不到元信息也必须返回 200（页面要能正常打开）"
    for plat in ("macos", "macos-zip"):
        assert data["platforms"][plat]["size"] == 0


def test_disabled_platform_is_not_advertised(monkeypatch):
    def head(url):
        return {"size": 1, "updated": "", "version": ""}

    ext, _ = _fresh(monkeypatch, head)
    monkeypatch.setitem(ext._INSTALLER_ENABLED, "macos-zip", False)
    data = ext.download_meta()

    assert "macos" in data["platforms"]
    assert "macos-zip" not in data["platforms"], (
        "已关闭的平台不该再被页面渲染出体积/版本（会给人「还能下」的错觉）")


def test_page_uses_dynamic_markers():
    """页面必须留出动态渲染的挂点，且带静态兜底文案（接口失败时仍可读）。"""
    html = (_ROOT / "web" / "download" / "index.html").read_text(encoding="utf-8")
    assert 'data-dl-size="macos"' in html
    assert 'data-dl-size="macos-zip"' in html
    assert "/download/meta" in html
    assert "296 MB" in html, "静态兜底文案不能删（/download/meta 不可达时仍要显示）"
