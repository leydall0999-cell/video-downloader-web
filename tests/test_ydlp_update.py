"""ydlp_update 单元测试：bootstrap 路径插入 / Zip Slip 防护 / sha256 校验。"""
import hashlib
import importlib
import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import ydlp_update


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    importlib.reload(ydlp_update)
    return ydlp_update


def _make_wheel(members: dict) -> bytes:
    buf = io.BytesIO()
    zf = zipfile.ZipFile(buf, "w")
    for name, content in members.items():
        zf.writestr(name, content)
    zf.close()
    return buf.getvalue()


class _FakeResp:
    def __init__(self, content):
        self._c = content

    def raise_for_status(self):
        pass

    @property
    def content(self):
        return self._c


def _mount(fresh, monkeypatch, wheel_bytes, sha=None, version="9999.0"):
    meta = {
        "info": {"version": version},
        "urls": [
            {"filename": "y.whl", "url": "https://x/y.whl", "digests": {"sha256": sha} if sha else {}},
        ],
    }

    class FakeMeta:
        def raise_for_status(self):
            pass

        def json(self):
            return meta

    def fake_get(url, *a, **k):
        if url.endswith("/json"):
            return FakeMeta()
        return _FakeResp(wheel_bytes)

    monkeypatch.setattr(fresh.requests, "get", fake_get)


def test_bootstrap_inserts_sys_path(fresh, tmp_path):
    pkg = tmp_path / ".videodownloader" / "yt_dlp" / "yt_dlp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    sys.path[:] = [p for p in sys.path if "yt_dlp" not in p]
    fresh.bootstrap()
    assert sys.path[0] == str(tmp_path / ".videodownloader" / "yt_dlp")


def test_update_rejects_zip_slip(fresh, tmp_path, monkeypatch):
    wb = _make_wheel({"yt_dlp/__init__.py": "# ok", "../sibling_evil.py": "pwn"})
    _mount(fresh, monkeypatch, wb, sha=hashlib.sha256(wb).hexdigest())
    res = fresh.update()
    assert res["ok"] is False
    assert not (tmp_path / ".videodownloader" / "sibling_evil.py").exists()


def test_update_success_when_valid(fresh, tmp_path, monkeypatch):
    wb = _make_wheel({"yt_dlp/__init__.py": "# ok", "yt_dlp/__main__.py": "x"})
    _mount(fresh, monkeypatch, wb, sha=hashlib.sha256(wb).hexdigest())
    res = fresh.update()
    assert res["ok"] is True and res["updated"] is True
    assert (tmp_path / ".videodownloader" / "yt_dlp" / "yt_dlp" / "__init__.py").exists()


def test_update_rejects_bad_sha(fresh, tmp_path, monkeypatch):
    wb = _make_wheel({"yt_dlp/__init__.py": "# ok"})
    _mount(fresh, monkeypatch, wb, sha="0" * 64)
    res = fresh.update()
    assert res["ok"] is False
    assert "校验和" in res["error"]


# ---------------------------------------------------------------- 解释器兼容性
# 2026-10-30 事故守卫：yt-dlp 2026.10.26 起要求 Python>=3.11，线上 venv 是 3.10.12。
# 旧实现无条件装「最新版」⇒ 影子目录里躺着不能 import 的包 ⇒ 下次重启 ImportError
# 崩溃循环。以下测试钉死「只装当前解释器能用的版本」。


def test_spec_allows_matrix(fresh):
    assert fresh._spec_allows(">=3.11", (3, 10)) is False
    assert fresh._spec_allows(">=3.10", (3, 10)) is True
    assert fresh._spec_allows(">=3.11", (3, 12)) is True
    assert fresh._spec_allows(">=3.10,<4", (3, 10)) is True
    assert fresh._spec_allows("!=3.10.*", (3, 10)) is False
    assert fresh._spec_allows(None, (3, 10)) is True   # 未声明 = 无限制
    assert fresh._spec_allows("", (3, 10)) is True
    assert fresh._spec_allows("垃圾字符串", (3, 10)) is False  # 解析不了 ⇒ 保守拒绝


def _meta_with_releases(monkeypatch, fresh, mapping):
    """mapping: {version: requires_python}"""
    meta = {
        "info": {"version": max(mapping, key=fresh._ver_key)},
        "releases": {
            v: [{"filename": f"yt_dlp-{v}-py3-none-any.whl",
                 "url": f"https://x/{v}.whl",
                 "digests": {"sha256": "0" * 64},
                 "requires_python": rp}]
            for v, rp in mapping.items()
        },
        "urls": [],
    }
    monkeypatch.setattr(fresh, "_fetch_meta", lambda: meta)
    return meta


def test_latest_compatible_picks_newest_supported(fresh, monkeypatch):
    _meta_with_releases(monkeypatch, fresh, {
        "2026.10.21": ">=3.10",
        "2026.10.26": ">=3.11",
        "2026.10.23.233055.dev0": ">=3.11",   # 预发布应被跳过
        "2026.9.1": ">=3.10",
    })
    assert fresh.latest_compatible_version((3, 10)) == "2026.10.21"
    assert fresh.latest_compatible_version((3, 12)) == "2026.10.26"


def test_update_refuses_when_no_compatible_release(fresh, monkeypatch):
    # 把解释器伪装成 3.10：线上 ECS venv 就是 3.10.12，而测试机是 3.13
    monkeypatch.setattr(fresh.sys, "version_info", (3, 10, 12))
    _meta_with_releases(monkeypatch, fresh, {"2026.10.26": ">=3.11"})
    res = fresh.update()
    assert res["ok"] is False
    assert "兼容" in res["error"]
    assert not (fresh.VDL_YTDLP_DIR / "yt_dlp").exists()


def test_version_report_separates_compatible_and_upstream(fresh, monkeypatch):
    _meta_with_releases(monkeypatch, fresh, {
        "2026.10.21": ">=3.10",
        "2026.10.26": ">=3.11",
    })
    monkeypatch.setattr(fresh.sys, "version_info", (3, 10, 12))
    rep = fresh.version_report()
    assert rep["latest"] == "2026.10.21"
    assert rep["latest_upstream"] == "2026.10.26"
