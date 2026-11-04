#!/usr/bin/env python3
"""Cookie 守护进程 / Chromium 卡死拖垮整机的回归守卫（2026-11-05）。

线上事故
--------
2026-11-05 03:29 起，ECS（2 核 / 1.6GB）出现 **iowait 90%、load 12、全站变慢**。
定位结论（证据链）：

1. `vdl_cookie_daemon.py` 的后台线程每 120s 调 `_collect_and_cache()` →
   `_collect_cookies()` 起 Chromium 刷 B站 Cookie。
2. 该 Chromium 启动时带 `--disable-dev-shm-usage`，把共享内存从 `/dev/shm`
   （本机 805MB tmpfs，实测 0% 占用）改放到 `/tmp` 所在的**系统盘**。
3. 内存压力下渲染器反复换入换出这 51MB 磁盘 shm，形成 **51MB/s 读盘死循环**，
   进程卡在 `D`（disk sleep）状态，`SIGKILL` **不生效**（要等 I/O 完成）。
4. `_collect_and_cache()` 当时**没有超时**，且整段持有 `_resolve_lock` ⇒
   - 孤儿 Chromium 存活 12 分钟持续吃盘；
   - 期间所有 `/v1/resolve` 都被判「解析服务正忙」，用户侧解析全线不可用。

本文件钉死（静态 AST 校验，不 import 守护进程、不联网）
----------------------------------------------------
A. `bilibili_ecs_cookie.py` 的 launch 参数**不得**再出现 `--disable-dev-shm-usage`
   （否则 shm 又落回磁盘，同类事故必然复发）。
B. `bilibili_ecs_cookie.py` 必须导出 `push_once`（守护进程 import 它；
   仓库曾长期停在缺失 push_once 的旧版，一旦部署即 ImportError 崩服务）。
C. `_collect_and_cache` 必须通过 `fut.result(timeout=...)` 施加硬超时，
   且超时/异常分支必须调用 `_kill_all_chromium()`，`finally` 必须释放 `_resolve_lock`。
D. `_kill_all_chromium` 必须复核存活并 `logger.error` 上报（D 状态杀不掉要可见）。

跑法：cd server && ../.build_venv/bin/python tests/test_cookie_daemon_chromium_guard.py
"""
import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BILI = REPO / "bilibili_ecs_cookie.py"
DAEMON = REPO / "vps_worker" / "vdl_cookie_daemon.py"

FAILS = []


def _must(cond, msg):
    if cond:
        print("  ok -", msg)
    else:
        print("  FAIL -", msg)
        FAILS.append(msg)


def _parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _calls(node):
    """收集节点内所有 Call 的 (func 文本, keyword 名集合)。"""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                name = f.attr
            elif isinstance(f, ast.Name):
                name = f.id
            else:
                name = ""
            out.append((name, {k.arg for k in n.keywords if k.arg}))
    return out


print("=== 1. B站 Chromium 必须把 shm 留在 /dev/shm（剔除 Playwright 默认开关）===")
btree = _parse(BILI)
launch = _find_func(btree, "launch")
_must(launch is not None, "bilibili_ecs_cookie.py 存在 launch()")

# 关键：该参数在 playwright driver 的默认开关表里，只从 args 里删掉**无效**，
# 必须出现在 ignore_default_args 中才真正生效（2026-11-05 实测踩到）。
ignored = set()
for node in ast.walk(launch):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "launch_persistent_context":
        for kw in node.keywords:
            if kw.arg == "ignore_default_args" and isinstance(kw.value, (ast.List, ast.Tuple)):
                for elt in kw.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        ignored.add(elt.value)
_must("--disable-dev-shm-usage" in ignored,
      "ignore_default_args 显式剔除 --disable-dev-shm-usage（否则 shm 落回 /tmp 系统盘）")

# args 里也不该再重复写（写了反而说明还在靠 args 兜底）
own_args = set()
for node in ast.walk(launch):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "launch_persistent_context":
        for kw in node.keywords:
            if kw.arg == "args" and isinstance(kw.value, (ast.List, ast.Tuple)):
                for elt in kw.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        own_args.add(elt.value)
_must("--disable-dev-shm-usage" not in own_args,
      "args 中不再显式传 --disable-dev-shm-usage")

print("\n=== 2. B站脚本必须导出 push_once（守护进程 import 契约）===")
_must(_find_func(btree, "push_once") is not None,
      "bilibili_ecs_cookie.py 定义 push_once(url, token, sessdata)")
_must(_find_func(btree, "_collect_cookies") is not None,
      "bilibili_ecs_cookie.py 仍提供 _collect_cookies")

print("\n=== 2b. 收集流程不得在视频页查 DOM（会无限挂住）===")
cc = _find_func(btree, "_collect_cookies")
_must(cc is not None, "_collect_cookies 可解析")
dom_calls = [c for c in _calls(cc) if c[0] in ("locator", "is_visible", "wait_for_selector")]
_must(not dom_calls,
      "_collect_cookies 不含 locator/is_visible/wait_for_selector（视频页渲染器会堵死 CDP，实测挂 100s+）")
_must(any(c[0] == "_verify_bili_cookie" for c in _calls(cc)),
      "_collect_cookies 改用 _verify_bili_cookie 做 HTTP 校验")

print("\n=== 3. 后台收集必须有硬超时且超时清理 chromium ===")
dtree = _parse(DAEMON)
collect = _find_func(dtree, "_collect_and_cache")
_must(collect is not None, "vdl_cookie_daemon.py 存在 _collect_and_cache")

calls = _calls(collect)
timeout_calls = [c for c in calls if c[0] == "result" and "timeout" in c[1]]
_must(bool(timeout_calls),
      "_collect_and_cache 通过 fut.result(timeout=...) 施加硬超时（否则卡死会永久占用锁）")
_must(any(c[0] == "_kill_all_chromium" for c in calls),
      "_collect_and_cache 内部调用 _kill_all_chromium 清理卡死浏览器")

# finally 里必须释放 _resolve_lock
released = False
for node in ast.walk(collect):
    if isinstance(node, ast.Try):
        for stmt in node.finalbody:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                    if sub.func.attr == "release":
                        released = True
_must(released, "finally 中释放 _resolve_lock（保证用户解析不被饿死）")

print("\n=== 4. 杀不死的 chromium 必须上报（D 状态 SIGKILL 无效）===")
kill = _find_func(dtree, "_kill_all_chromium")
_must(kill is not None, "vdl_cookie_daemon.py 存在 _kill_all_chromium")
kill_calls = _calls(kill)
_must(any(c[0] == "error" for c in kill_calls),
      "_kill_all_chromium 对 SIGKILL 后仍存活的进程 logger.error 上报")
_must(sum(1 for c in kill_calls if c[0] == "kill") >= 1,
      "_kill_all_chromium 仍执行 os.kill(SIGKILL)")

print("")
if FAILS:
    print("❌ test_cookie_daemon_chromium_guard 失败 %d 项：" % len(FAILS))
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ test_cookie_daemon_chromium_guard 全部通过")
