#!/usr/bin/env python3
"""平台健康巡检（最小闭环）：样本实测 + 域名级成功遥测 + 需求榜，三源合成健康表。

三个证据源，互为补充：
  1. **样本实测**：对该平台样本链接真实打一次 /api/resolve（最准，但要维护样本库）；
  2. **成功遥测**（2026-10-31 新增，主力）：解析成功只落 host + 时间戳（**不记 URL**），
     谁被真实用户解析成功了就证明谁活着。**不需要样本库**，也就没有「链接烂掉」
     的维护成本，且天然只覆盖有真实流量的平台——那正是「坏了才值得修」的那些；
  3. **需求榜**（失败侧）：用户要但没拿到（pending_extractor / unsupported_platform）。

设计要点（均为实测修正，勿回退）：
  * **多样本多数判定**：单样本会误报。实测 B站 3 条样本中仅 1 条 412 失败
    （该视频**内容级**限制：地区/会员），若只测 1 条就会误判「B站挂了」。
  * **区分「平台失效」与「样本失效」**：解析失败时回查样本链接是否还活着
    （HTTP 4xx/5xx 或页面含「删除/失效/不存在」）→ 样本失效属**维护问题**，
    不算平台坏了，需换样本重测。
  * **无样本不装数据**：没有样本也没有遥测/需求证据的平台，显示 `unknown`
    （诚实的未知），而不是假装健康。

状态分档：
  healthy        样本全通，或（无样本但）近期有成功遥测
  degraded       样本部分通
  sample_invalid 样本全挂且样本自身也死了（维护问题，非平台坏）
  broken         样本全挂且样本仍活着 ⇒ 平台真的坏了（会触发自动修复）
  repaired_pending_restart  自动升 yt-dlp 后复测已通过，但线上进程仍在跑旧版，
                 需重启 vdl-web 才真正生效
  needs_manual   升版无效且根因明确（登录墙/付费/地区/缺提取器）⇒ 人力才可能解决
  demand_only    无样本、无成功遥测，但有用户需求 ⇒ 有人要却从没成功过，值得排查
  unknown        三源皆无证据
  no_sample      无样本（保留，供样本库补齐进度用）

自动修复（2026-11-01 接入，见 platform_autorepair）：
  仅对 `broken` 触发 —— 升 yt-dlp（`ydlp_update.update()`）→ **子进程**复测
  （新进程 bootstrap 才用得到新版；运行中进程装完仍是旧的）→ 有效则标
  `repaired_pending_restart`（**不自动重启生产服务**），无效则按根因落派单
  `data/repair_tickets.json`。升版是全局动作，故成功后会回归校验其它平台样本，
  一旦「修好 A 弄坏 B」判 `regressed_other` 并建议回滚。
  `degraded` 不触发：实测多为内容级限制（B站某视频 412），升版无用且白担风险。
  环境变量 VDL_HEALTH_AUTOREPAIR=0 可关闭自动修复。

用法：
    python platform_health_check.py                 # 全量巡检
    python platform_health_check.py bilibili douyin # 只测指定平台 key
    VDL_HEALTH_TELEMETRY_DAYS=7 python platform_health_check.py
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
ENDPOINT = os.environ.get("VDL_HEALTH_ENDPOINT", "http://127.0.0.1:18890/api/resolve")
_HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES_PATH = os.environ.get(
    "VDL_PLATFORM_SAMPLES", os.path.join(_HERE, "platform_samples.json"))
OUT_PATH = os.environ.get("VDL_HEALTH_OUT", os.path.join(_HERE, "data", "platform_health.json"))
PER_URL_TIMEOUT = int(os.environ.get("VDL_HEALTH_TIMEOUT", "90"))
TELEMETRY_DAYS = int(os.environ.get("VDL_HEALTH_TELEMETRY_DAYS", "30"))
DEMAND_DAYS = int(os.environ.get("VDL_HEALTH_DEMAND_DAYS", "84"))
DATA_DIR = os.environ.get("VDL_DATA_DIR") or str(Path.home() / ".video-downloader")
_DEAD_MARKERS = ("删除", "失效", "不存在", "已下线", "404", "not found", "removed", "deleted")

# 复用 server 侧模块（拿遥测/需求聚合），找不到就降级为自读 JSONL。
for _p in (_HERE, os.path.join(_HERE, "server"), os.path.join(os.path.dirname(_HERE), "server")):
    if os.path.isfile(os.path.join(_p, "platform_telemetry.py")) and _p not in sys.path:
        sys.path.insert(0, _p)
        break
try:
    import platform_telemetry
    import resolve_demand
except Exception:  # noqa: BLE001
    platform_telemetry = None
    resolve_demand = None

# 修复层（2026-11-01）：判出 broken 后自动升 yt-dlp + 子进程复测。
# 导入失败只降级为「只检测不修复」，绝不让巡检整体挂掉。
try:
    for _p in (_HERE, os.path.join(_HERE, "server")):
        if os.path.isfile(os.path.join(_p, "platform_autorepair.py")) and _p not in sys.path:
            sys.path.insert(0, _p)
            break
    import platform_autorepair
except Exception:  # noqa: BLE001
    platform_autorepair = None

# 自动修复开关：默认开启（用户要求接上），可 VDL_HEALTH_AUTOREPAIR=0 关闭
AUTOREPAIR = os.environ.get("VDL_HEALTH_AUTOREPAIR", "1").strip() not in ("0", "false", "no")


def _http(url: str, timeout: int = 20, data: bytes | None = None) -> tuple[int, str]:
    """返回 (status, body)。data 非空则 POST JSON。"""
    headers = {"User-Agent": UA}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(getattr(r, "status", 200)), r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return int(e.code), e.read().decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        return 0, "%s: %s" % (type(e).__name__, e)


def _resolve(url: str) -> tuple[bool, str, str]:
    """打一次解析。返回 (ok, category, msg)。"""
    status, body = _http(ENDPOINT, timeout=PER_URL_TIMEOUT,
                         data=json.dumps({"url": url}).encode())
    try:
        d = json.loads(body)
    except Exception:  # noqa: BLE001
        return False, "bad_response", "HTTP %s %s" % (status, body[:160])
    if isinstance(d, dict) and d.get("error"):
        return False, str(d.get("category") or "unknown"), str(d.get("error"))[:200]
    if isinstance(d, dict) and d.get("detail"):
        return False, "http_error", str(d.get("detail"))[:200]
    if isinstance(d, dict) and (d.get("url") or d.get("title") or d.get("formats")):
        return True, "ok", str(d.get("title") or "")[:80]
    return False, "no_payload", body[:160]


def _sample_alive(url: str) -> bool:
    """样本链接本身是否还活着（区分「样本失效」与「平台失效」）。"""
    status, body = _http(url, timeout=20)
    if status == 0 or status >= 400:
        return False
    low = body.lower()
    return not any(m.lower() in low for m in _DEAD_MARKERS)


def _discover_bilibili(n: int) -> list[str]:
    """B站公开热门 API 取当前真实 bvid（无需登录）。"""
    _, body = _http("https://api.bilibili.com/x/web-interface/popular?ps=%d&pn=1" % max(n, 1))
    try:
        items = (json.loads(body).get("data") or {}).get("list") or []
    except Exception:  # noqa: BLE001
        return []
    return ["https://www.bilibili.com/video/%s" % i["bvid"]
            for i in items if i.get("bvid")][:n]


_DISCOVERERS = {"bilibili_popular": _discover_bilibili}


def _samples_for(cfg: dict, n: int) -> list[str]:
    disc = cfg.get("discoverer")
    if disc and disc in _DISCOVERERS:
        got = _DISCOVERERS[disc](n)
        if got:
            return got
    return list(cfg.get("urls") or [])[:n]


def _host_matches(host: str, domains: list[str]) -> bool:
    """与 platforms.py 同口径：host == domain 或 host 以 .domain 结尾。"""
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in (domains or []))


def _load_evidence() -> tuple[dict, dict]:
    """读成功遥测与失败需求，返回 (success_by_host, demand_by_host)。

    两个源都在 VDL_DATA_DIR 下（与 web 服务写入位置一致）。任一不可读则为空，
    绝不因此中断巡检——退化为只有样本实测的健康表，仍然有用。
    """
    succ: dict[str, dict] = {}
    dem: dict[str, dict] = {}
    try:
        if platform_telemetry is not None:
            succ = platform_telemetry.host_success_map(days=TELEMETRY_DAYS,
                                                       base_dir=Path(DATA_DIR))
    except Exception:  # noqa: BLE001
        succ = {}
    try:
        if resolve_demand is not None:
            dem = {r["host"]: r for r in resolve_demand.demand_summary(
                days=DEMAND_DAYS, base_dir=Path(DATA_DIR))}
    except Exception:  # noqa: BLE001
        dem = {}
    return succ, dem


def _evidence_for(domains: list[str], succ: dict, dem: dict) -> dict:
    """把该平台名下所有域的成功/需求证据合并。"""
    days = total = last_ts = 0
    hosts_hit: list[str] = []
    for host, r in succ.items():
        if _host_matches(host, domains):
            days += int(r.get("days_count") or 0)
            total += int(r.get("total") or 0)
            last_ts = max(last_ts, int(r.get("last_ts") or 0))
            hosts_hit.append(host)
    d_weeks = d_total = d_last = 0
    d_hosts: list[str] = []
    for host, r in dem.items():
        if _host_matches(host, domains):
            d_weeks += int(r.get("weeks_count") or 0)
            d_total += int(r.get("total") or 0)
            d_last = max(d_last, int(r.get("last_ts") or 0))
            d_hosts.append(host)
    return {
        "success_days": days,
        "success_total": total,
        "last_success_ts": last_ts,
        "success_hosts": sorted(set(hosts_hit))[:5],
        "demand_weeks": d_weeks,
        "demand_total": d_total,
        "last_demand_ts": d_last,
        "demand_hosts": sorted(set(d_hosts))[:5],
    }


def main() -> int:
    try:
        with open(SAMPLES_PATH, "r", encoding="utf-8") as f:
            samples = json.load(f)
    except Exception as e:  # noqa: BLE001
        print("样本库读取失败: %s (%s)" % (SAMPLES_PATH, e))
        return 2

    succ, dem = _load_evidence()
    only = set(sys.argv[1:])
    per_platform = int(os.environ.get("VDL_HEALTH_SAMPLE_N", "3"))
    items = []
    for key, cfg in samples.items():
        # 样本库里 `_comment` 等非平台条目是说明文字，不是平台配置，必须跳过
        # （早期版本全量跑会拿到 str 直接崩）。
        if not isinstance(cfg, dict):
            continue
        if only and key not in only:
            continue
        ev = _evidence_for(cfg.get("hosts") or [], succ, dem)
        urls = _samples_for(cfg, per_platform)
        if not urls:
            # 无样本 ⇒ 靠证据判定，不编造结论
            if ev["success_days"] > 0:
                status, source = "healthy", "telemetry"
            elif ev["demand_weeks"] > 0:
                status, source = "demand_only", "demand"
            else:
                status, source = "unknown", "none"
            items.append({"platform": key, "name": cfg.get("name", key),
                          "status": status, "source": source,
                          "ok": 0, "total": 0, "evidence": ev,
                          "detail": "无样本，按遥测/需求证据判定"})
            continue

        results = []
        for u in urls:
            t0 = time.time()
            ok, cat, msg = _resolve(u)
            rec = {"url": u, "ok": ok, "category": cat, "msg": msg,
                   "elapsed_s": round(time.time() - t0, 1)}
            if not ok:
                rec["sample_alive"] = _sample_alive(u)
            results.append(rec)
        ok_n = sum(1 for r in results if r["ok"])
        alive_n = sum(1 for r in results if r.get("sample_alive", True))
        if ok_n == len(results):
            status = "healthy"
        elif ok_n > 0:
            status = "degraded"
        elif alive_n == 0:
            status = "sample_invalid"
        else:
            status = "broken"
        items.append({"platform": key, "name": cfg.get("name", key), "status": status,
                      "source": "sample", "ok": ok_n, "total": len(results),
                      "evidence": ev, "results": results})

    # ---- 自动修复层（2026-11-01）----
    # 只对 broken 触发：degraded 实测多为**内容级**限制（如 B站某视频 412 地区/会员），
    # 升版对它无效，却要承担「新版弄坏别的平台」的全局风险 —— 不值得。
    # broken = 样本仍活着但全挂，最符合「提取器老化」特征，正是升版能治的那类。
    repair_log: list[dict] = []
    if AUTOREPAIR and platform_autorepair is not None:
        broken = [it for it in items if it["status"] == "broken"]
        if broken:
            # 回归校验池：其它平台的样本（升版是整进程共用，必须防「修好 A 弄坏 B」）
            reg: dict[str, list[str]] = {}
            broken_keys = {b["platform"] for b in broken}
            for k, c in samples.items():
                if not isinstance(c, dict) or k in broken_keys:
                    continue
                u = _samples_for(c, 1)
                if u:
                    reg[k] = u[:1]
            for it in broken:
                urls = [r["url"] for r in (it.get("results") or [])]
                if not urls:
                    continue
                rep = platform_autorepair.repair_platform(
                    it["platform"], it.get("name") or it["platform"], urls,
                    baseline_ok=int(it.get("ok") or 0), regression_urls=reg,
                    log=lambda m: print(m))
                it["repair"] = rep
                repair_log.append(rep)
                rs = rep.get("status")
                if rs in ("fixed", "improved"):
                    # 修好了，但线上进程还在跑旧 yt-dlp ⇒ 必须重启才生效
                    it["status"] = "repaired_pending_restart"
                elif rs == "already_latest" and (
                        (rep.get("root_cause") or {}).get("code")
                        in ("login_wall", "geo_or_paywall", "no_extractor",
                            "content_gone", "anti_bot")):
                    # 升版修不了、且根因明确不在版本 ⇒ 归「需人工」，不再天天当 broken 报
                    it["status"] = "needs_manual"
                # 其余（not_fixed / regressed / update_failed）保持 broken，且已落派单

    # 排序：**需要行动的排前面**，无证据（=无真实流量）的沉底。
    # 不排序的话，unknown 会淹没真正坏掉的平台——健康表的价值在于「先看什么」。
    _RANK = {"broken": 0, "needs_manual": 1, "sample_invalid": 2, "degraded": 3,
             "repaired_pending_restart": 4, "demand_only": 5, "healthy": 6,
             "no_sample": 7, "unknown": 8}
    items.sort(key=lambda r: (_RANK.get(r["status"], 9), r["platform"]))
    counts: dict[str, int] = {}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    actionable = sum(counts.get(s, 0) for s in
                     ("broken", "needs_manual", "sample_invalid", "degraded",
                      "repaired_pending_restart", "demand_only"))
    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "endpoint": ENDPOINT, "telemetry_days": TELEMETRY_DAYS,
           "demand_days": DEMAND_DAYS, "data_dir": DATA_DIR,
           "telemetry_available": bool(succ), "demand_available": bool(dem),
           "autorepair_enabled": bool(AUTOREPAIR and platform_autorepair is not None),
           "summary": {"total": len(items), "actionable": actionable,
                       "counts": counts,
                       "no_evidence": counts.get("unknown", 0),
                       "repaired_pending_restart": counts.get(
                           "repaired_pending_restart", 0),
                       "needs_manual": counts.get("needs_manual", 0)},
           "repairs": repair_log,
           "items": items}
    try:
        os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        print("健康表写入失败: %s" % e)

    # 「修好了但还没重启」必须显眼，否则等于白修：自动修复只把新版 yt-dlp 解到
    # 影子目录，线上 vdl-web 仍在跑旧模块（我们不自动重启生产服务）。落一个标记
    # 文件，任何巡检/人一眼就能看到该重启了；恢复后自动清掉。
    _pending_path = os.path.join(os.path.dirname(OUT_PATH), "PENDING_RESTART.json")
    _pending = [{"platform": it["platform"], "name": it.get("name"),
                 "note": (it.get("repair") or {}).get("note")}
                for it in items if it["status"] == "repaired_pending_restart"]
    try:
        if _pending:
            with open(_pending_path, "w", encoding="utf-8") as f:
                json.dump({"since": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "action": "restart vdl-web to apply upgraded yt-dlp",
                           "platforms": _pending}, f, ensure_ascii=False, indent=2)
        elif os.path.isfile(_pending_path):
            os.remove(_pending_path)
    except Exception:  # noqa: BLE001
        pass

    print("== 平台健康表 %s ==" % out["generated_at"])
    print("遥测可用=%s 需求可用=%s（窗口 成功%d天 / 需求%d天）"
          % (out["telemetry_available"], out["demand_available"],
             TELEMETRY_DAYS, DEMAND_DAYS))
    s = out.get("summary") or {}
    print("共 %d 个平台：待处理 %d（%s）｜无证据（无真实流量）%d"
          % (s.get("total", 0), s.get("actionable", 0),
             " ".join("%s=%d" % (k, v) for k, v in sorted(
                 (s.get("counts") or {}).items(),
                 key=lambda kv: kv[0]) if k in
                 ("broken", "needs_manual", "sample_invalid", "degraded",
                  "repaired_pending_restart", "demand_only")) or "无",
             s.get("no_evidence", 0)))
    if out.get("autorepair_enabled") and repair_log:
        print("自动修复：尝试 %d 个平台，需要重启生效 %d 个"
              % (len(repair_log), s.get("repaired_pending_restart", 0)))
        if s.get("repaired_pending_restart"):
            print("⚠️  有平台已修好但未生效：请重启 vdl-web "
                  "（systemctl restart vdl-web）后复测；标记见 %s"
                  % _pending_path)
    for it in items:
        ev = it.get("evidence") or {}
        extra = ""
        if ev.get("success_days"):
            extra = " 成功%d天/最后%s" % (
                ev["success_days"],
                time.strftime("%m-%d %H:%M", time.localtime(ev["last_success_ts"]))
                if ev.get("last_success_ts") else "?")
        if ev.get("demand_weeks"):
            extra += " 需求%d周" % ev["demand_weeks"]
        print("%-12s %-12s %-14s %s(%d/%d)%s"
              % (it["platform"], it["name"], it["status"], it.get("source", ""),
                 it["ok"], it.get("total", 0), extra))
        for r in it.get("results") or []:
            if not r["ok"]:
                print("      FAIL %s alive=%s %s"
                      % (r["url"][:60], r.get("sample_alive"), r["msg"][:90]))
        rp = it.get("repair") or {}
        if rp:
            _rc = rp.get("root_cause") or {}
            print("      REPAIR %s / 根因 %s：%s%s"
                  % (rp.get("status"), _rc.get("code"), rp.get("note", ""),
                     "  ⚠️需重启 vdl-web 才在线上生效"
                     if rp.get("restart_required") else ""))
    print("-> %s" % OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
