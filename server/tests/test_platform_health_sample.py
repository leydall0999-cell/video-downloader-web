"""平台健康巡检：稿件级不可播必须从「平台健康」分母里剔除。

背景（2026-11-01）：B站 热门榜里混进一条已删/未过审稿件（BV1vVxi68EWD，playurl
返回 -404），巡检把它算成平台失败 ⇒ 整个 bilibili 凭空判 degraded，天天误报。
判定必须区分「这条稿件没了」与「平台/提取器坏了」。

全程离线：_resolve / _sample_alive 被替换为假实现，不触网。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import platform_health_check as phc  # noqa: E402


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("视频无法播放｜该视频稿件不可播放（可能已删除、未过审或仅限特定地区）", True),
        ("视频无法播放｜该视频稿件不可见（已下架或设为私有）", True),
        ("视频无法播放｜该视频为大会员专享，需要会员 Cookie 才能取流", True),
        ("[BiliBili] xxx: Failed to download redirection URL: HTTP Error 412", False),
        ("网络请求失败｜连接被重置", False),
    ],
)
def test_is_content_unavailable(msg: str, expected: bool) -> None:
    assert phc._is_content_unavailable(msg) is expected


def _run(monkeypatch, tmp_path, resolve_map: dict) -> dict:
    """把样本库/输出路径换成临时文件，按 resolve_map 伪造解析结果。"""
    samples = tmp_path / "samples.json"
    samples.write_text(json.dumps({
        "bilibili": {"name": "哔哩哔哩", "urls": ["https://b/1", "https://b/2", "https://b/3"]},
    }), encoding="utf-8")
    out = tmp_path / "health.json"

    monkeypatch.setattr(phc, "SAMPLES_PATH", str(samples))
    monkeypatch.setattr(phc, "OUT_PATH", str(out))
    monkeypatch.setattr(phc, "AUTOREPAIR", False)
    monkeypatch.setattr(phc, "_load_evidence", lambda: ({}, {}))
    monkeypatch.setattr(phc, "_sample_alive", lambda url: True)
    monkeypatch.setattr(phc, "_resolve", lambda url: resolve_map[url])
    monkeypatch.setenv("VDL_HEALTH_SAMPLE_N", "3")
    # main() 把 sys.argv[1:] 当平台过滤名单；pytest 自己的参数会把所有平台滤掉
    monkeypatch.setattr(sys, "argv", ["platform_health_check.py"])

    assert phc.main() == 0
    return json.loads(out.read_text(encoding="utf-8"))


def test_content_unavailable_excluded_from_denominator(monkeypatch, tmp_path) -> None:
    out = _run(monkeypatch, tmp_path, {
        "https://b/1": (True, "ok", "标题一"),
        "https://b/2": (True, "ok", "标题二"),
        "https://b/3": (False, "unknown", "视频无法播放｜该视频稿件不可播放（可能已删除）"),
    })

    item = out["items"][0]
    assert item["status"] == "healthy", f"一条死稿不得把平台判 degraded：{item}"
    assert item["total"] == 2, "不可播的样本必须从分母剔除"
    assert item["ok"] == 2
    flagged = [r for r in item["results"] if r.get("content_unavailable")]
    assert len(flagged) == 1 and flagged[0]["url"] == "https://b/3"


def test_all_content_unavailable_is_sample_invalid(monkeypatch, tmp_path) -> None:
    msg = (False, "unknown", "视频无法播放｜该视频稿件不可见（已下架或设为私有）")
    out = _run(monkeypatch, tmp_path, {u: msg for u in ("https://b/1", "https://b/2", "https://b/3")})

    item = out["items"][0]
    assert item["status"] == "sample_invalid", item
    assert item["total"] == 0


def test_real_failure_still_broken(monkeypatch, tmp_path) -> None:
    msg = (False, "unknown", "[BiliBili] x: HTTP Error 412")
    out = _run(monkeypatch, tmp_path, {u: msg for u in ("https://b/1", "https://b/2", "https://b/3")})

    item = out["items"][0]
    assert item["status"] == "broken", "真失败（非稿件级）不得被豁免"
    assert item["total"] == 3
