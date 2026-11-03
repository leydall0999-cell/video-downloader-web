#!/usr/bin/env python3
"""平台健康巡检 · 异常提醒链路回归（2026-11-03）。

背景：巡检每天跑，但结果只落 ECS 的 json + 日志，没人会主动去翻。
第 2 步补了「比上一轮变差才提醒」的提醒文件（data/platform_health_alerts.json），
后台首屏亮红条、点「知道了」清零。这条测试把提醒的**判定语义**钉死，免得以后
谁改坏了还不自知：

  1. 首轮（没上一轮）不报 —— 否则上线第一天会把所有平台刷成提醒。
  2. 变差要报（healthy→broken）；变好不报；同档不报。
  3. 无证据→健康 不报（刚被真实流量证明活着，不用打扰人）；
     无证据→部分可用 要报（第一次有样本就发现有问题，正是要看的）。
  4. 已知受限（服务器侧解析不了，如优酷/西瓜）不算异常，不报。
  5. 提醒文件：同一平台同一条不重复堆叠；第二个平台的新异常会追加。
  6. 已读状态不会被下一轮空跑冲掉。
  7. 样本取用：cfg["n"] 优先、发现器优先、发现器崩了退回固定样本
     （不能让整轮巡检因为一个发现器异常而挂掉）。

运行：
    cd server && python tests/test_platform_health_alerts.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 测试隔离：绝不写用户家目录 / 绝不碰线上数据
_TMP = tempfile.mkdtemp(prefix="vdl_test_health_alerts_")
os.environ["VDL_HEALTH_OUT"] = os.path.join(_TMP, "platform_health.json")
os.environ["VDL_HEALTH_ALERT_OUT"] = os.path.join(_TMP, "platform_health_alerts.json")
os.environ["VDL_PLATFORM_SAMPLES"] = os.path.join(_TMP, "samples.json")

import platform_health_check as H  # noqa: E402


def _st(s, p="a"):
    return [{"platform": p, "name": p.upper(), "status": s, "results": []}]


def _must(cond, msg):
    if not cond:
        raise AssertionError("FAIL: " + msg)
    print("  ok -", msg)


print("=== 1. 首轮不报 ===")
_must(H._diff_alerts(_st("broken"), {}) == [], "首轮（无上一轮）不报任何提醒")

print("=== 2. 变差要报 / 变好不报 / 同档不报 ===")
ev = H._diff_alerts(_st("broken"), {"a": "healthy"})
_must(len(ev) == 1 and ev[0]["from"] == "healthy" and ev[0]["to"] == "broken",
      "healthy→broken 要报")
_must(H._diff_alerts(_st("healthy"), {"a": "broken"}) == [], "broken→healthy 不报（恢复了）")
_must(H._diff_alerts(_st("degraded"), {"a": "degraded"}) == [], "degraded→degraded 同档不报")

print("=== 3. 无证据口径 ===")
_must(H._diff_alerts(_st("healthy"), {"a": "unknown"}) == [],
      "无证据→健康 不报（刚被真实流量证明活着）")
_must(len(H._diff_alerts(_st("degraded"), {"a": "unknown"})) == 1,
      "无证据→部分可用 要报（第一次有样本就发现问题）")

print("=== 4. 已知受限不算异常 ===")
_must(H._diff_alerts(_st("limited"), {"a": "healthy"}) == [],
      "limited（服务器侧解析不了）不算异常、不报")

print("=== 5. 提醒去重 / 追加 ===")
doc = H._save_alerts(ev)
_must(doc["unseen"] == 1 and len(doc["events"]) == 1, "首条提醒写入、未读=1")
doc = H._save_alerts(ev)  # 同一平台同一条重复
_must(doc["unseen"] == 1 and len(doc["events"]) == 1, "同平台同一条不重复堆叠")
doc = H._save_alerts([{"ts": 1, "platform": "b", "name": "B", "from": "healthy",
                       "to": "broken", "detail": "x", "ack": False}])
_must(doc["unseen"] == 2 and len(doc["events"]) == 2, "第二个平台新异常追加")

print("=== 6. 已读不被空跑冲掉 ===")
p = os.path.join(_TMP, "platform_health_alerts.json")
d = json.loads(open(p, encoding="utf-8").read())
for e in d["events"]:
    e["ack"] = True
d["unseen"] = 0
open(p, "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False))
doc = H._save_alerts([])
_must(doc["unseen"] == 0 and len(doc["events"]) == 2, "已读状态不被下一轮空跑冲掉")

print("=== 7. 样本取用（n 优先 / 发现器优先 / 发现器崩了回退）===")
_must(H._samples_for({"urls": ["u1", "u2", "u3"], "n": 1}, 3) == ["u1"],
      "cfg['n'] 限制取几条")
cfg = {"urls": ["u1"], "discoverer": "acfun_rank", "n": 2}
H._DISCOVERERS["acfun_rank"] = lambda n: ["d%d" % i for i in range(n)]
_must(H._samples_for(cfg, 3) == ["d0", "d1"], "发现器优先于固定样本")


def _boom(n):
    raise RuntimeError("discoverer down")


H._DISCOVERERS["acfun_rank"] = _boom
_must(H._samples_for(cfg, 3) == ["u1"],
      "发现器异常必须退回固定样本，不能让整轮巡检挂掉")

print("\n✅ test_platform_health_alerts 全部通过")
