"""B站 API 兜底的 buvid3 注入与「稿件不可播放」可读化回归。

背景（2026-11-01 实测）：`api.bilibili.com/x/web-interface/view` 起强制要求请求带
`buvid3`，缺失时返回 **HTTP 412 验证页（HTML）** ⇒ `r.json()` 抛异常 ⇒ 整个
`_bilibili_api_extract` 兜底静默失效（任何 yt-dlp 412/403 的 B站 视频都会彻底失败）。
补一个形态合法的 buvid3 即恢复 200。

另：稿件级不可播放（playurl `-404 啥都木有`）不是网络/提取器问题，兜底拿不到流，
必须给出可读原因，而不是把 yt-dlp 的 412 原文抛给用户。

本测试全程离线：`requests.get` 被替换为假实现，不触网。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import downloader as dl  # noqa: E402


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.status_code = 200
        self.text = str(payload)

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _view_payload(bvid: str = "BV11yDyBZEwo") -> dict:
    return {
        "code": 0,
        "data": {
            "bvid": bvid,
            "title": "测试稿件",
            "pages": [{"cid": 123456}],
            "owner": {"name": "UP", "mid": 1},
            "stat": {"view": 1},
            "duration": 60,
        },
    }


def _play_payload() -> dict:
    return {
        "code": 0,
        "data": {
            "support_formats": [],
            "dash": {
                "video": [{
                    "id": 80,
                    "baseUrl": "https://cdn.example/v.m4s",
                    "mimeType": "video/mp4",
                    "width": 1920,
                    "height": 1080,
                    "bandwidth": 1000000,
                }],
                "audio": [],
            },
        },
    }


def _patch_get(monkeypatch, view: dict, play: dict) -> list[dict]:
    """替换 requests.get，返回被调用时携带的 headers 列表（便于断言 Cookie）。"""
    seen: list[dict] = []

    def fake_get(url, headers=None, proxies=None, timeout=None, **kw):  # noqa: ANN001
        seen.append({"url": url, "headers": dict(headers or {})})
        return _Resp(play if "playurl" in url else view)

    monkeypatch.setattr(requests, "get", fake_get)
    return seen


def test_buvid3_injected_when_cookie_missing(monkeypatch) -> None:
    seen = _patch_get(monkeypatch, _view_payload(), _play_payload())

    info = dl._bilibili_api_extract("https://www.bilibili.com/video/BV11yDyBZEwo", cookie="SESSDATA=abc")

    assert info is not None, "缺 buvid3 时兜底必须仍能成功（否则线上 412 全挂）"
    assert info["formats"], "应构造出至少一条 format"
    assert seen, "requests.get 应被调用"
    for call in seen:
        ck = call["headers"].get("Cookie", "")
        assert "buvid3=" in ck, f"请求 {call['url']} 未注入 buvid3：{ck!r}"
        assert "SESSDATA=abc" in ck, "原有 Cookie 不得被覆盖"


def test_buvid3_injected_when_no_cookie(monkeypatch) -> None:
    seen = _patch_get(monkeypatch, _view_payload(), _play_payload())

    info = dl._bilibili_api_extract("https://www.bilibili.com/video/BV11yDyBZEwo")

    assert info is not None
    assert all("buvid3=" in c["headers"].get("Cookie", "") for c in seen)


def test_buvid3_not_duplicated_when_already_present(monkeypatch) -> None:
    seen = _patch_get(monkeypatch, _view_payload(), _play_payload())

    dl._bilibili_api_extract(
        "https://www.bilibili.com/video/BV11yDyBZEwo",
        cookie="buvid3=REAL-VALUE-infoc; SESSDATA=abc",
    )

    ck = seen[0]["headers"].get("Cookie", "")
    assert ck.count("buvid3=") == 1, f"不应重复注入：{ck!r}"
    assert "REAL-VALUE-infoc" in ck, "已有 buvid3 必须保留原值"


@pytest.mark.parametrize(
    "code,keyword",
    [
        (-404, "不可播放"),
        (62002, "不可见"),
        (-10403, "大会员"),
    ],
)
def test_content_unavailable_raises_readable_error(monkeypatch, code: int, keyword: str) -> None:
    _patch_get(monkeypatch, _view_payload(), {"code": code, "message": "x"})

    with pytest.raises(dl.ResolveError) as ei:
        dl._bilibili_api_extract("https://www.bilibili.com/video/BV1vVxi68EWD")

    err = ei.value
    assert keyword in (err.hint or ""), f"应给出可读原因，实际 hint={err.hint!r}"
    assert "HTTP Error 412" not in (err.hint or ""), "不得把 yt-dlp 的 412 原文当提示"


def test_unknown_playurl_code_still_returns_none(monkeypatch) -> None:
    _patch_get(monkeypatch, _view_payload(), {"code": -9999, "message": "unknown"})

    assert dl._bilibili_api_extract("https://www.bilibili.com/video/BV11yDyBZEwo") is None
