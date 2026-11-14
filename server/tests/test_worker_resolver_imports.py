#!/usr/bin/env python3
"""Cookie 守护进程 resolver「导入即存在」守卫（2026-11-15）。

线上背景
--------
2026-11-14 在 ECS 上直接加了咪咕（migu）解析，但只改了线上文件、没回灌仓库；
巡检时进一步发现 **lespark_resolve.py / liblib_resolve.py 两个 resolver 压根
不在 git 里**，而 `vps_worker/vdl_cookie_daemon.py` 顶部就有
`from lespark_resolve import resolve as lespark_resolve` —— 也就是说
仓库 HEAD 上「按 git 重新部署」会直接 ImportError，Cookie 守护进程整个起不来，
所有走 worker 的平台（B站/快手/抖音/小红书/短剧…）一起挂。

这类缺陷不会在线上暴露（线上文件是手工补的），只在「换机重装 / 重新部署 /
别人 clone 仓库」时炸，属于最贵的一类：静默、全量、无告警。

本文件钉死
----------
A. 导入即存在：daemon 里 `from X_resolve import ...` 的每个模块，
   `vps_worker/X_resolve.py` 必须真实存在。
B. 映射即导入：RESOLVERS 里引用的名字必须来自上面的 import（否则运行时 NameError）。
C. 服务端配对：`server/downloader.py` 里出现的每个 worker 平台分支
   （`_call_vps_worker("<platform>"`）都必须在 daemon 的 RESOLVERS 里有同名 key，
   反向亦然 —— 防止「后端认这个平台、worker 不认」的半截接线。
D. 负向验证：故意造一个「有 import 没文件」的源，检查必须报错（证明检查有效）。

跑法：cd server && ../.build_venv/bin/python tests/test_worker_resolver_imports.py
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VPS = REPO / "vps_worker"
DAEMON = VPS / "vdl_cookie_daemon.py"
DOWNLOADER = REPO / "server" / "downloader.py"

FAILS = []


def _must(cond, msg):
    if cond:
        print("  ok -", msg)
    else:
        print("  FAIL -", msg)
        FAILS.append(msg)


# 兼容两种写法：`import resolve as X` / `import resolve_album as X`
_IMPORT_RE = re.compile(r"^from\s+(\w+_resolve)\s+import\s+(\w+)\s+as\s+(\w+)", re.M)
_MAP_RE = re.compile(r'^\s*"([\w.-]+)"\s*:\s*(\w+)\s*,', re.M)
_WORKER_CALL_RE = re.compile(r'_call_vps_worker\(\s*"([\w.-]+)"')


def scan(daemon_src, downloader_src, exists):
    """纯函数：返回问题列表。exists(name) 判断 resolver 模块是否存在。"""
    problems = []

    imports = _IMPORT_RE.findall(daemon_src)
    for mod, _sym, _alias in imports:
        if not exists(mod):
            problems.append("resolver 模块缺失：vps_worker/%s.py（daemon 顶部已 import）" % mod)

    imported_aliases = {alias for _mod, _sym, alias in imports}

    # RESOLVERS 字典块：从 "RESOLVERS" 起，到下一个顶层非缩进行结束
    map_block = ""
    idx = daemon_src.find("RESOLVERS")
    if idx >= 0:
        tail = daemon_src[idx:]
        lines = []
        for line in tail.splitlines():
            if lines and line and not line[0].isspace() and not line.startswith("}"):
                break
            lines.append(line)
            if line.startswith("}"):
                break
        map_block = "\n".join(lines)

    mapped = _MAP_RE.findall(map_block)
    if not mapped:
        problems.append("RESOLVERS 映射解析为空（结构被改坏了？）")
    for platform, alias in mapped:
        if alias not in imported_aliases:
            problems.append("RESOLVERS['%s'] 用了未导入的名字 %s" % (platform, alias))

    # C. 服务端 ↔ worker 平台配对
    worker_platforms = {p for p, _a in mapped}
    called = set(_WORKER_CALL_RE.findall(downloader_src))
    for p in sorted(called - worker_platforms):
        problems.append("downloader 调 worker 平台 '%s'，但 daemon RESOLVERS 里没有" % p)
    return problems


def main():
    print("=== A/B. daemon 导入与 RESOLVERS 映射 ===")
    daemon_src = DAEMON.read_text(encoding="utf-8")
    dl_src = DOWNLOADER.read_text(encoding="utf-8")
    problems = scan(daemon_src, dl_src, lambda m: (VPS / (m + ".py")).is_file())
    for p in problems:
        print("  FAIL -", p)
        FAILS.append(p)
    if not problems:
        n_imp = len(_IMPORT_RE.findall(daemon_src))
        n_map = len(_MAP_RE.findall(daemon_src))
        print("  ok - %d 个 resolver 导入全部有对应文件；RESOLVERS %d 项全部已导入"
              % (n_imp, n_map))

    print("\n=== C. 服务端 downloader ↔ worker 平台配对 ===")
    mapped = {p for p, _a in _MAP_RE.findall(daemon_src)}
    called = set(_WORKER_CALL_RE.findall(dl_src))
    _must(bool(mapped), "RESOLVERS 非空（%d 个平台）" % len(mapped))
    _must(bool(called), "downloader 确实有 worker 直调分支（%d 个平台）" % len(called))
    for p in sorted(called - mapped):
        _must(False, "downloader 调 worker 平台 '%s'，daemon 未注册" % p)
    if called <= mapped:
        print("  ok - downloader 直调的 %d 个平台 daemon 全部注册" % len(called))

    print("\n=== D. 负向验证（检查本身必须能发现问题）===")
    bad_src = "from ghost_resolve import resolve as ghost_resolve\nRESOLVERS = {\n    \"ghost\": ghost_resolve,\n}\n"
    neg = scan(bad_src, "", lambda m: False)
    _must(any("ghost_resolve.py" in x for x in neg), "缺文件必须被报出")
    neg2 = scan("from a_resolve import resolve as a_resolve\nRESOLVERS = {\n    \"a\": other_name,\n}\n",
                "", lambda m: True)
    _must(any("other_name" in x for x in neg2), "映射引用未导入名字必须被报出")
    neg3 = scan("RESOLVERS = {\n    \"a\": a,\n}\n",
                '_call_vps_worker("migu", url)', lambda m: True)
    _must(any("migu" in x for x in neg3), "downloader 单边平台必须被报出")

    print("\n" + ("❌ 失败 %d 项" % len(FAILS) if FAILS else "✅ 全部通过"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
