#!/usr/bin/env python3
"""平台失效「自动修复」层：判出 broken → 自动升 yt-dlp → 子进程复测 → 仍失败则出派单。

## 为什么升版要配「子进程复测」
`ydlp_update.update()` 只把新版 yt-dlp 解到影子目录，**运行中进程零效果**
（返回 `restart_required: True`：模块早已在内存里）。所以「升版到底有没有用」
只能由**全新解释器**回答 —— 本模块起 `platform_repair_runner.py` 子进程，
它 `bootstrap()` 后 import 到的一定是新版。

## 修复分级（诚实边界）
- **自动**：`extractor_stale` 类（上游改版/新站点，yt-dlp 已跟进）⇒ 升版即愈。
- **半自动**：升版有效但需重启才在线上生效 ⇒ 标 `pending_restart`，**绝不自动
  重启生产服务**（重启失败=全站不可用，风险不对等）。由人或运维确认后执行。
- **人工**：登录墙 / 付费 / 地区限制 ⇒ 升版永远修不了，直接出派单。

## 回归防护
升版是**全局**动作（整进程共用一套 yt-dlp）。新版修好 A 却弄坏 B 是真实风险，
所以升版成功后额外复测「原本健康的其它平台」，一旦由好变坏 ⇒ 判 `regressed`
并建议回滚（删影子目录即回退到 venv 内置版）。

用法（库调用）：
    import platform_autorepair
    rep = platform_autorepair.repair_platform("bilibili", "哔哩哔哩", urls)
命令行（干跑，供人工验证）：
    python platform_autorepair.py --platform bilibili --dry-run
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
TICKET_PATH = os.environ.get(
    "VDL_REPAIR_TICKETS", os.path.join(_HERE, "data", "repair_tickets.json"))
RUNNER = os.path.join(_HERE, "platform_repair_runner.py")
# 单个平台复测的总超时（样本 3 条 × 每条最长 90s + 进程启动余量）
REPAIR_TIMEOUT = int(os.environ.get("VDL_REPAIR_TIMEOUT", "420"))

# 根因归类：(category 精确匹配 / 关键词子串, 根因码, 人话解释, 建议动作)
_ROOT_RULES = [
    (("cookie_required",), "login_wall", "需要登录态才能拿到直链",
     "登录墙：需要该站账号 Cookie/token 才能开发与验证，自动修复无法完成"),
    (("restricted",), "geo_or_paywall", "内容受限（会员/付费/地区）",
     "受限内容：升版不会解决，需账号权益或地区出口，通常不值得投入"),
    (("unsupported_platform", "unsupported_url_form"), "no_extractor",
     "没有对应提取器（或链接形态不支持）",
     "缺提取器：需按 vdl-vps-worker-add 流程新写解析器，属开发任务"),
    (("pending_extractor",), "no_extractor", "提取器待补",
     "缺提取器：需新写解析器"),
    (("network", "timeout", "stalled"), "network",
     "网络/超时（可能是机房 IP 被限速或对端抖动）",
     "网络类：先重试；持续失败再查机房出口与代理链路，不属平台改版"),
    (("parse_failed",), "extractor_stale",
     "解析失败（多为上游改版、提取器老化）",
     "提取器老化：升版应能修复；若已是最新仍失败，说明上游尚未跟进，需等更新或自行改提取器"),
]
_KEYWORD_RULES = [
    # 风控优先于「内容没了」：412/403 常被误读成链接失效，其实是机房 IP 被拦
    (("412", "429", "403 forbidden", "captcha", "风控", "验证码", "too many requests",
      "precondition failed"), "anti_bot",
     "被平台风控/反爬拦截（数据中心 IP 常见）",
     "反爬/风控：升版通常无效，需换出口 IP、补 Cookie 或过签名校验，属人工排查"),
    (("登录", "login", "sign in", "cookie", "凭证"), "login_wall",
     "需要登录态才能拿到直链", "登录墙：需要账号 Cookie/token，自动修复无法完成"),
    (("会员", "付费", "购买", "vip", "premium", "paywall"), "geo_or_paywall",
     "内容受限（会员/付费）", "受限内容：升版不会解决，需账号权益"),
    (("地区", "geo", "region", "unsupported country", "your country"), "geo_or_paywall",
     "地区限制", "地区限制：需对应地区出口，通常不值得投入"),
    (("410", "404", "not found", "不存在", "已删除", "removed"), "content_gone",
     "内容已删除/不存在", "内容本身不在了：换样本链接，不是平台失效"),
]


# ---------------------------------------------------------------- 环境探测

def _find_dir_with(filename: str) -> str:
    """在自身目录 / 其 server 子目录里找模块所在目录（兼容平铺与分层两种部署）。"""
    for cand in (_HERE, os.path.join(_HERE, "server")):
        if os.path.isfile(os.path.join(cand, filename)):
            return cand
    return _HERE


def _load_ydlp():
    d = _find_dir_with("ydlp_update.py")
    if d not in sys.path:
        sys.path.insert(0, d)
    try:
        import ydlp_update  # noqa: PLC0415
        return ydlp_update
    except Exception:  # noqa: BLE001
        return None


def ytdlp_state() -> dict:
    """当前「本机实际可用」的 yt-dlp 版本（影子目录优先，因为 bootstrap 会盖住 venv）。"""
    ydlp = _load_ydlp()
    shadow = ""
    if ydlp is not None:
        try:
            vf = ydlp.VDL_YTDLP_DIR / "version.txt"
            if vf.exists():
                shadow = vf.read_text().strip()
        except Exception:  # noqa: BLE001
            pass
    installed = "未知"
    try:
        from importlib.metadata import version
        installed = version("yt-dlp")
    except Exception:  # noqa: BLE001
        pass
    return {"shadow": shadow, "venv": installed,
            "effective": shadow or installed,
            "shadow_dir": str(getattr(ydlp, "VDL_YTDLP_DIR", "")) if ydlp else ""}


# ---------------------------------------------------------------- 子进程复测

def run_probe_subprocess(urls: list[str], timeout: int | None = None) -> dict:
    """起子进程跑真实 probe（会用影子目录里的最新版 yt-dlp）。"""
    if not urls:
        return {"ok": False, "results": [], "error": "无样本 URL"}
    if not os.path.isfile(RUNNER):
        return {"ok": False, "results": [], "error": "runner 缺失: %s" % RUNNER}
    ts = "%d-%d" % (os.getpid(), int(time.time() * 1000))
    uf = os.path.join("/tmp" if os.name == "posix" else ".", "vdl_repair_urls_%s.json" % ts)
    rf = os.path.join("/tmp" if os.name == "posix" else ".", "vdl_repair_res_%s.json" % ts)
    try:
        with open(uf, "w", encoding="utf-8") as f:
            json.dump(urls, f, ensure_ascii=False)
        proc = subprocess.run(
            [sys.executable, RUNNER, "--urls-file", uf, "--out", rf],
            capture_output=True, timeout=timeout or REPAIR_TIMEOUT)
        if not os.path.isfile(rf):
            return {"ok": False, "results": [],
                    "error": "子进程无输出 (rc=%s) %s" % (
                        proc.returncode,
                        (proc.stderr or b"").decode("utf-8", "ignore")[-300:])}
        with open(rf, "r", encoding="utf-8") as f:
            return json.load(f)
    except subprocess.TimeoutExpired:
        return {"ok": False, "results": [], "error": "复测超时（>%ss）" % (timeout or REPAIR_TIMEOUT)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "results": [], "error": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        for p in (uf, rf):
            try:
                os.remove(p)
            except OSError:
                pass


# ---------------------------------------------------------------- 根因归类

def classify_root_cause(results: list[dict]) -> dict:
    """从失败样本里归纳根因（取出现最多的那类），给出人话解释与建议。"""
    fails = [r for r in results if not r.get("ok")]
    if not fails:
        return {"code": "none", "label": "无失败样本", "action": "", "samples": 0}
    tally: dict[str, int] = {}
    for r in fails:
        cat = str(r.get("category") or "").lower()
        msg = (str(r.get("message") or "") + " " + str(r.get("hint") or "")).lower()
        code = "unknown"
        for cats, c, _label, _act in _ROOT_RULES:
            if cat in cats:
                code = c
                break
        else:
            for kws, c, _label, _act in _KEYWORD_RULES:
                if any(k in msg for k in kws):
                    code = c
                    break
        tally[code] = tally.get(code, 0) + 1
    code = max(tally, key=lambda k: tally[k])
    label = action = ""
    for cats, c, lb, act in _ROOT_RULES:
        if c == code:
            label, action = lb, act
            break
    if not label:
        for kws, c, lb, act in _KEYWORD_RULES:
            if c == code:
                label, action = lb, act
                break
    if not label:
        label = "未归类（需人工看样本报错）"
        action = "人工排查：看 evidence 里的原始报错，判断是改版、反爬还是缺提取器"
    return {"code": code, "label": label, "action": action,
            "samples": len(fails), "breakdown": tally}


# ---------------------------------------------------------------- 派单

def _load_tickets() -> list:
    try:
        with open(TICKET_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:  # noqa: BLE001
        return []


def write_ticket(ticket: dict) -> str:
    """派单落盘（按平台+根因去重，只保留最新一条，避免每天刷屏）。"""
    os.makedirs(os.path.dirname(TICKET_PATH), exist_ok=True)
    tickets = _load_tickets()
    tickets = [t for t in tickets
               if not (t.get("platform") == ticket.get("platform")
                       and t.get("root_cause") == ticket.get("root_cause"))]
    tickets.append(ticket)
    # 只留最近 200 条，防止无限增长
    tickets = tickets[-200:]
    try:
        with open(TICKET_PATH, "w", encoding="utf-8") as f:
            json.dump(tickets, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass
    return TICKET_PATH


# ---------------------------------------------------------------- 主流程

def repair_platform(key: str, name: str, urls: list[str],
                    baseline_ok: int = 0, regression_urls: dict | None = None,
                    do_update: bool = True, log=None) -> dict:
    """对一个 broken 平台尝试自动修复。

    baseline_ok     巡检时该平台的通过样本数（仅用于对比，不重复跑）
    regression_urls {其它平台key: [urls]} —— 升版成功后做回归校验用
    """
    say = log or (lambda *a: None)
    t0 = time.time()
    state_before = ytdlp_state()
    rep = {"platform": key, "name": name, "urls": urls,
           "baseline_ok": baseline_ok, "state_before": state_before,
           "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    # ---- 1. 先取一份「升版前」的子进程基线（失败分类是派单的核心证据）
    say("[%s] 升版前子进程基线复测…" % key)
    before = run_probe_subprocess(urls)
    rep["before"] = before
    before_ok = sum(1 for r in before.get("results") or [] if r.get("ok"))

    # ---- 2. 升版
    upd = {"ok": False, "error": "未执行"}
    if not do_update:
        # 干跑：只复测取证，不升版、不出派单（人工排查时用，避免误写工单）
        rep.update(status="dry_run", after=before,
                   root_cause=classify_root_cause(before.get("results") or []),
                   restart_required=False,
                   note="干跑：仅子进程复测取证，未升级 yt-dlp，未生成派单")
        return _finish(rep, say, t0)
    if do_update:
        ydlp = _load_ydlp()
        if ydlp is None:
            upd = {"ok": False, "error": "ydlp_update 模块不可用"}
        else:
            say("[%s] 升级 yt-dlp…" % key)
            try:
                upd = ydlp.update()
            except Exception as exc:  # noqa: BLE001
                upd = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    rep["update"] = upd
    state_after = ytdlp_state()
    rep["state_after"] = state_after

    # 升版没成功 / 已是最新 ⇒ 升版这条路走不通，直接出派单
    if not upd.get("ok"):
        rep.update(status="update_failed", after=None,
                   root_cause=classify_root_cause(before.get("results") or []),
                   restart_required=False,
                   note="yt-dlp 升级未成功（%s），未产生新证据" % upd.get("error"))
        return _finish(rep, say, t0)

    if not upd.get("updated"):
        # 已是最新仍坏 ⇒ 不是「提取器老化」能解释的，根因看失败分类
        rc = classify_root_cause(before.get("results") or [])
        status = "already_latest"
        rep.update(status=status, after=before, root_cause=rc,
                   restart_required=False,
                   note="yt-dlp 已是当前解释器可用最新版（%s），升版无效"
                        % upd.get("version"))
        return _finish(rep, say, t0)

    # ---- 3. 升版成功 ⇒ 新进程复测
    say("[%s] 已升到 %s，子进程复测…" % (key, upd.get("version")))
    after = run_probe_subprocess(urls)
    rep["after"] = after
    after_ok = sum(1 for r in after.get("results") or [] if r.get("ok"))
    total = max(len(urls), 1)
    rc = classify_root_cause((after.get("results") or []) or (before.get("results") or []))

    if after_ok == total:
        status = "fixed"
        note = "升版后样本全通：%d/%d" % (after_ok, total)
    elif after_ok > before_ok:
        status = "improved"
        note = "升版后部分转好：%d → %d / %d（未全通，可能仍有内容级限制）" % (
            before_ok, after_ok, total)
    elif after_ok == before_ok:
        status = "not_fixed"
        note = "升版后无变化（%d/%d），根因不在提取器版本" % (after_ok, total)
    else:
        status = "regressed"
        note = "升版后反而更差（%d → %d / %d），建议回滚" % (before_ok, after_ok, total)

    # ---- 4. 回归校验：新版别把原本好的平台弄坏
    regression = None
    if regression_urls and status in ("fixed", "improved"):
        say("[%s] 升版后回归校验（其它平台）…" % key)
        rr = {}
        for pk, purls in list(regression_urls.items())[:6]:
            if not purls:
                continue
            r = run_probe_subprocess(purls)
            ok_n = sum(1 for x in r.get("results") or [] if x.get("ok"))
            rr[pk] = {"ok": ok_n, "total": len(purls),
                      "passed": ok_n == len(purls)}
        regression = rr
        broken = [k for k, v in rr.items() if not v["passed"]]
        if broken:
            status = "regressed_other"
            note += "；但回归校验发现其它平台受损：%s" % "、".join(broken)

    rep.update(status=status, after=after, root_cause=rc,
               after_ok=after_ok, before_ok=before_ok, regression=regression,
               # 修复有效但线上仍在用旧版 ⇒ 需要重启才生效
               restart_required=status in ("fixed", "improved"),
               rollback_suggested=status.startswith("regressed"),
               note=note)
    return _finish(rep, say, t0)


def _finish(rep: dict, say, t0: float) -> dict:
    rep["elapsed_s"] = round(time.time() - t0, 1)
    rep["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    st = rep.get("status")
    # 只有「需要人动手」的才落派单：修好的、需要重启的不算派单
    if st in ("not_fixed", "already_latest", "update_failed", "regressed",
              "regressed_other"):
        rc = rep.get("root_cause") or {}
        ev = []
        src = (rep.get("after") or rep.get("before") or {}).get("results") or []
        for r in src:
            if not r.get("ok"):
                ev.append({"url": r.get("url"), "category": r.get("category"),
                           "message": r.get("message"), "hint": r.get("hint"),
                           "elapsed_s": r.get("elapsed_s")})
        ticket = {
            "id": "%s-%s-%s" % (rep["platform"], rc.get("code", "unknown"),
                                time.strftime("%Y%m%d")),
            "platform": rep["platform"], "name": rep.get("name"),
            "status": st, "root_cause": rc.get("code"),
            "root_cause_label": rc.get("label"),
            "suggested_action": rc.get("action"),
            "ytdlp_before": (rep.get("state_before") or {}).get("effective"),
            "ytdlp_after": (rep.get("state_after") or {}).get("effective"),
            "update_result": rep.get("update"),
            "evidence": ev[:6],
            "note": rep.get("note"),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        rep["ticket_path"] = write_ticket(ticket)
        rep["ticket"] = ticket
    say("[%s] 修复结论：%s（%s）" % (rep["platform"], st, rep.get("note", "")))
    return rep


# ---------------------------------------------------------------- CLI

def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="平台自动修复（升 yt-dlp + 子进程复测）")
    ap.add_argument("--platform", required=True)
    ap.add_argument("--urls", nargs="*", default=[])
    ap.add_argument("--dry-run", action="store_true", help="只复测，不升版")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    urls = a.urls
    if not urls:
        # 从样本库取
        sp = os.path.join(_HERE, "platform_samples.json")
        try:
            with open(sp, "r", encoding="utf-8") as f:
                cfg = json.load(f).get(a.platform) or {}
            urls = list(cfg.get("urls") or [])
        except Exception:  # noqa: BLE001
            urls = []
    if not urls and a.platform == "bilibili":
        sys.path.insert(0, _HERE)
        try:
            import platform_health_check as phc  # noqa: PLC0415
            urls = phc._discover_bilibili(3)
        except Exception:  # noqa: BLE001
            urls = []
    if not urls:
        print("该平台无样本 URL，无法复测（请先在 platform_samples.json 补 urls）")
        return 2

    rep = repair_platform(a.platform, a.platform, urls,
                          do_update=not a.dry_run,
                          log=lambda m: print(m, file=sys.stderr))
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print("状态: %s" % rep.get("status"))
        print("说明: %s" % rep.get("note"))
        print("yt-dlp: %s → %s" % ((rep.get("state_before") or {}).get("effective"),
                                    (rep.get("state_after") or {}).get("effective")))
        rc = rep.get("root_cause") or {}
        print("根因: %s（%s）" % (rc.get("code"), rc.get("label")))
        print("建议: %s" % rc.get("action"))
        if rep.get("restart_required"):
            print("⚠️ 修复有效但需重启 vdl-web 才在线上生效")
        for r in (rep.get("after") or rep.get("before") or {}).get("results") or []:
            print("  %s %s %s" % ("OK  " if r.get("ok") else "FAIL",
                                  r.get("url", "")[:56],
                                  (r.get("message") or r.get("title") or "")[:70]))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
