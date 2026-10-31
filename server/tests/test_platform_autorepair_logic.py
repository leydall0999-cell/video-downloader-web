# -*- coding: utf-8 -*-
"""自动修复层的**纯逻辑**离线测试：判定分支 + 派单落盘 + 根因归类。

为什么需要这组测试：`repair_platform` 里最关键、也最容易写错的是**状态判定**——
「升版后到底算修好了、部分好转、没变化、还是弄坏了」，以及「哪些情况才该出派单」。
这些分支靠真实平台根本压不全（现实中很难恰好造出“升版后全通”的案例，
B站这条链路又常年被 412 风控干扰），只能把 `ydlp_update` 与子进程复测打桩，
把判定矩阵钉死。

打桩的两个点（都是 IO 边界，不是业务逻辑）：
  * `_load_ydlp`  → 伪造「升版结果」，不碰真实 PyPI、不动影子目录；
  * `run_probe_subprocess` → 伪造「复测结果」，不起子进程、不联网。

绝不触碰真实数据目录（TICKET_PATH 全部指向临时目录）。
"""
import json
import pathlib
import sys
import tempfile
from pathlib import Path

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

import platform_autorepair as pa  # noqa: E402


# ---------------------------------------------------------------- 桩件

class _FakeYdlp:
    """冒充 ydlp_update：只提供 repair_platform 用到的两个接口。

    `update()` 成功后要真的写出 version.txt —— 真实实现就是这么干的，
    而 `ytdlp_state()` 正是靠这个文件判断「当前生效版本」。桩件不写的话，
    测试里的「升版后版本」会读成 venv 版本，断言就没意义了。
    """

    def __init__(self, update_result):
        self.VDL_YTDLP_DIR = Path(tempfile.mkdtemp(prefix="fake_ytdlp_"))
        self._update_result = update_result
        self.update_calls = 0

    def update(self):
        self.update_calls += 1
        res = dict(self._update_result)
        if res.get("updated") and res.get("version"):
            (self.VDL_YTDLP_DIR / "version.txt").write_text(res["version"])
        return res


def _probe_result(ok_n: int, total: int, category="anti_bot", msg="视频解析失败"):
    """构造一份 runner 输出：前 ok_n 条成功，其余失败。"""
    results = []
    for i in range(total):
        if i < ok_n:
            results.append({"url": "https://x/%d" % i, "ok": True, "title": "t%d" % i})
        else:
            results.append({"url": "https://x/%d" % i, "ok": False,
                            "category": category, "message": msg,
                            "hint": "[diag] HTTP Error 412: Precondition Failed"})
    return {"ok": True, "yt_dlp": "2026.10.21", "results": results}


def _patch(monkeypatch, tmp_path, update_result, probe_seq):
    """统一打桩：update 结果固定，复测结果按调用顺序依次给出。"""
    monkeypatch.setattr(pa, "TICKET_PATH", str(tmp_path / "tickets.json"))
    fake = _FakeYdlp(update_result)
    monkeypatch.setattr(pa, "_load_ydlp", lambda: fake)
    seq = list(probe_seq)

    def _fake_run(urls, timeout=None):
        return seq.pop(0) if seq else _probe_result(0, len(urls))

    monkeypatch.setattr(pa, "run_probe_subprocess", _fake_run)
    return fake


_UP_OK = {"ok": True, "updated": True, "version": "2026.10.21",
          "restart_required": True}
_UP_LATEST = {"ok": True, "updated": False, "version": "2026.10.21",
              "restart_required": False}
_UP_FAIL = {"ok": False, "error": "无法获取最新版本（请检查网络）"}

_URLS = ["https://x/0", "https://x/1", "https://x/2"]


def _tickets(tmp_path):
    p = tmp_path / "tickets.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 判定矩阵

def test_fixed_when_all_pass_after_upgrade(monkeypatch, tmp_path):
    """升版后全通 ⇒ fixed，要求重启生效，且**不该**出派单（没有待办）。"""
    _patch(monkeypatch, tmp_path, _UP_OK,
           [_probe_result(0, 3), _probe_result(3, 3)])
    rep = pa.repair_platform("bilibili", "哔哩哔哩", _URLS)
    assert rep["status"] == "fixed"
    assert rep["restart_required"] is True
    assert rep["rollback_suggested"] is False
    assert "ticket" not in rep
    assert _tickets(tmp_path) == []


def test_improved_partial_requires_restart(monkeypatch, tmp_path):
    """升版后部分转好 ⇒ improved（可能仍有内容级限制），同样需重启。"""
    _patch(monkeypatch, tmp_path, _UP_OK,
           [_probe_result(0, 3), _probe_result(2, 3)])
    rep = pa.repair_platform("bilibili", "哔哩哔哩", _URLS)
    assert rep["status"] == "improved"
    assert rep["restart_required"] is True
    assert rep["before_ok"] == 0 and rep["after_ok"] == 2


def test_not_fixed_writes_ticket_with_root_cause(monkeypatch, tmp_path):
    """升版无效 ⇒ not_fixed，必须落派单，且根因从 hint 的 412 正确归到 anti_bot。"""
    _patch(monkeypatch, tmp_path, _UP_OK,
           [_probe_result(0, 3), _probe_result(0, 3)])
    rep = pa.repair_platform("bilibili", "哔哩哔哩", _URLS)
    assert rep["status"] == "not_fixed"
    assert rep["restart_required"] is False
    assert rep["root_cause"]["code"] == "anti_bot"
    tickets = _tickets(tmp_path)
    assert len(tickets) == 1
    t = tickets[0]
    assert t["platform"] == "bilibili"
    assert t["root_cause"] == "anti_bot"
    assert t["ytdlp_after"] == "2026.10.21"
    assert t["evidence"], "派单必须带失败样本证据，否则无法排查"


def test_regressed_when_worse(monkeypatch, tmp_path):
    """升版后反而更差 ⇒ regressed，建议回滚（这是最危险的分支，必须能被发现）。"""
    _patch(monkeypatch, tmp_path, _UP_OK,
           [_probe_result(2, 3), _probe_result(0, 3)])
    rep = pa.repair_platform("bilibili", "哔哩哔哩", _URLS)
    assert rep["status"] == "regressed"
    assert rep["rollback_suggested"] is True
    assert rep["restart_required"] is False
    assert len(_tickets(tmp_path)) == 1


def test_regression_on_other_platform(monkeypatch, tmp_path):
    """修好 A 却弄坏 B ⇒ regressed_other（升版是全局动作，必须防连带损伤）。"""
    _patch(monkeypatch, tmp_path, _UP_OK,
           [_probe_result(0, 3), _probe_result(3, 3), _probe_result(0, 2)])
    rep = pa.repair_platform(
        "bilibili", "哔哩哔哩", _URLS,
        regression_urls={"douyin": ["https://y/0", "https://y/1"]})
    assert rep["status"] == "regressed_other"
    assert rep["regression"]["douyin"]["passed"] is False
    assert rep["rollback_suggested"] is True


def test_already_latest_does_not_claim_fixed(monkeypatch, tmp_path):
    """已是最新仍坏 ⇒ already_latest，不能谎报「修好了」，也不该说「升级失败」。"""
    _patch(monkeypatch, tmp_path, _UP_LATEST, [_probe_result(0, 3)])
    rep = pa.repair_platform("bilibili", "哔哩哔哩", _URLS)
    assert rep["status"] == "already_latest"
    assert rep["restart_required"] is False
    assert len(_tickets(tmp_path)) == 1


def test_update_failure_is_reported_not_silent(monkeypatch, tmp_path):
    """升版本身失败（网络等）⇒ update_failed，必须如实报告，不能假装修过。"""
    _patch(monkeypatch, tmp_path, _UP_FAIL, [_probe_result(0, 3)])
    rep = pa.repair_platform("bilibili", "哔哩哔哩", _URLS)
    assert rep["status"] == "update_failed"
    assert "网络" in (rep.get("note") or "")
    assert len(_tickets(tmp_path)) == 1


def test_dry_run_never_writes_ticket(monkeypatch, tmp_path):
    """干跑只取证：不升版、不出派单（否则人工排查会污染工单列表）。"""
    fake = _patch(monkeypatch, tmp_path, _UP_OK, [_probe_result(0, 3)])
    rep = pa.repair_platform("bilibili", "哔哩哔哩", _URLS, do_update=False)
    assert rep["status"] == "dry_run"
    assert fake.update_calls == 0, "干跑不允许真的调 update()"
    assert _tickets(tmp_path) == []


# ---------------------------------------------------------------- 根因归类

@pytest.mark.parametrize("category,hint,expect", [
    ("", "HTTP Error 412: Precondition Failed", "anti_bot"),
    ("", "HTTP Error 403 Forbidden", "anti_bot"),
    ("cookie_required", "", "login_wall"),
    ("", "该视频为会员专享，请购买后观看", "geo_or_paywall"),
    ("", "not available in your country", "geo_or_paywall"),
    ("unsupported_platform", "", "no_extractor"),
    ("", "视频不存在或已被删除", "content_gone"),
    ("network", "", "network"),
    ("parse_failed", "", "extractor_stale"),
])
def test_classify_root_cause(category, hint, expect):
    results = [{"ok": False, "category": category, "message": "视频解析失败",
                "hint": hint, }]
    got = pa.classify_root_cause(results)
    assert got["code"] == expect, got


def test_classify_takes_majority():
    """多样本取多数：3 条里 2 条登录墙、1 条风控 ⇒ 判登录墙。"""
    results = [
        {"ok": False, "category": "cookie_required", "message": "", "hint": ""},
        {"ok": False, "category": "cookie_required", "message": "", "hint": ""},
        {"ok": False, "category": "", "message": "", "hint": "HTTP Error 412"},
    ]
    assert pa.classify_root_cause(results)["code"] == "login_wall"


def test_ticket_dedup_keeps_latest():
    """同一平台+同一根因重复出单时只保留一条，避免每天刷屏。"""
    t = Path(tempfile.mkdtemp(prefix="tk_"))
    old = pa.TICKET_PATH
    try:
        pa.TICKET_PATH = str(t / "tickets.json")
        for note in ("第一次", "第二次"):
            pa.write_ticket({"platform": "x", "root_cause": "anti_bot", "note": note})
        data = json.loads((t / "tickets.json").read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["note"] == "第二次"
    finally:
        pa.TICKET_PATH = old
