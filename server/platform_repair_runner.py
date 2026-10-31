#!/usr/bin/env python3
"""平台修复复测执行器（**子进程**专用，不要被 web 服务 import）。

为什么必须另起进程：`ydlp_update.update()` 只是把新版 yt-dlp 解到影子目录，
运行中的进程 `sys.path` 早已定型、`yt_dlp` 模块也已在内存里 ⇒ **升版对当前进程
零效果**（`ydlp_update.update()` 自己就返回 `restart_required: True`）。
所以「升版有没有效」只能靠**全新解释器 + 重新 bootstrap** 来回答——这正是本文件
的存在意义：它跑在子进程里，`bootstrap()` 后 import 到的一定是新版影子包。

输出契约（给 platform_autorepair 消费）：
  `--out` 指定的 JSON 文件写入 {"yt_dlp": "<版本>", "results": [...]}
  每个 result: {"url", "ok", "title"/"category"/"message"/"hint", "elapsed_s"}
stdout 只打印一行状态，**不打印装载日志**（避免污染调用方解析）。

用法：
    python platform_repair_runner.py --urls-file /tmp/u.json --out /tmp/r.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))


def _server_dir() -> str:
    """server 目录：优先 `server/`（vdl-web 真正加载的那份），再退回平铺形态。

    注意 ECS 上历史上存在两份 downloader.py（顶层 + server/），uvicorn 在 server/
    目录跑，import 到的是 **server/downloader.py**。复测必须复现线上那条路径，
    否则可能「复测通过了线上还是坏的」。
    """
    for cand in (os.path.join(_HERE, "server"), _HERE):
        if os.path.isfile(os.path.join(cand, "downloader.py")):
            return cand
    return _HERE


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls-file", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.urls_file, "r", encoding="utf-8") as f:
        urls = json.load(f)
    if not isinstance(urls, list):
        urls = []

    srv = _server_dir()
    if srv not in sys.path:
        sys.path.insert(0, srv)
    os.chdir(srv)

    out = {"ok": False, "yt_dlp": "未知", "results": [], "error": ""}
    try:
        import ydlp_update
        ydlp_update.bootstrap()
        import yt_dlp
        out["yt_dlp"] = str(getattr(getattr(yt_dlp, "version", None),
                                    "__version__", "未知"))
        out["yt_dlp_path"] = str(getattr(yt_dlp, "__file__", ""))
        import downloader
    except Exception as exc:  # noqa: BLE001
        out["error"] = "import 失败: %s: %s" % (type(exc).__name__, exc)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        print("IMPORT_FAILED")
        return 1

    # 逐个 URL 真跑 probe——这就是线上用户走的那条路，不是另写一套简化探测。
    for u in urls:
        t0 = time.time()
        rec = {"url": u, "ok": False, "elapsed_s": 0.0}
        try:
            info = downloader.probe(u)
            rec["ok"] = True
            rec["title"] = str((info or {}).get("title") or "")[:100]
        except Exception as exc:  # noqa: BLE001
            rec["category"] = str(getattr(exc, "category", "") or "unknown")
            rec["message"] = str(getattr(exc, "message", "") or exc)[:200]
            # hint 才是真证据：downloader 把原始异常与 [diag] 诊断写在这里
            # （message 多是笼统的「视频解析失败」），截断太短会让派单失去根因。
            rec["hint"] = str(getattr(exc, "hint", "") or "")[:600]
            _ctx = getattr(exc, "context", None)
            if isinstance(_ctx, dict) and _ctx:
                rec["context"] = {str(k): str(v)[:200] for k, v in list(_ctx.items())[:12]}
            rec["exc_type"] = type(exc).__name__
        rec["elapsed_s"] = round(time.time() - t0, 1)
        out["results"].append(rec)

    out["ok"] = True
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print("OK %d/%d" % (sum(1 for r in out["results"] if r["ok"]),
                        len(out["results"])))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(3)
