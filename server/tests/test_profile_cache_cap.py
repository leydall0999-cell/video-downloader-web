#!/usr/bin/env python3
"""持久 profile 缓存「容量闸」回归守卫（2026-11-07）。

线上背景
--------
2026-11-06 刚给 **bili_profile** 补了「每轮采集前清纯缓存」，11-07 巡检发现
同类问题在其余 13 个持久 profile 上依旧成立：`weibo_profile` 的
`Default/Cache` 半天涨到 103MB，而守护进程的 `_prune_profile_caches` 只删
**48 小时前**的文件 —— 短命会话（一次解析十几秒）写出来的缓存全是「新文件」，
等不到超龄就已经把 1.6GB 小机器的磁盘 I/O 拖成 11-05 那种 iowait 雪崩。

本文件钉死
----------
A. 超龄闸仍在：mtime > max_age_hours 的缓存文件必须被删。
B. **容量闸存在**：单 profile 缓存总量 > cap 时必须按 mtime 从旧到新削到 trim 水位。
   只有超龄闸 = 缺陷（就是本次要修的那条）。
C. 新鲜文件保护：最近 fresh_sec 秒内写入的不删（避免干扰正在跑的会话）。
D. 登录态零误伤：Default/Cookies、Local Storage、IndexedDB 等一律不动。
E. 常量自洽：cap 合理（<=200MB）、trim < cap、巡检周期 <= 2 小时（12h 太久）。

跑法：cd server && ../.build_venv/bin/python tests/test_profile_cache_cap.py
"""
import importlib.abc
import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DAEMON = REPO / "vps_worker" / "vdl_cookie_daemon.py"

FAILS = []


def _must(cond, msg):
    if cond:
        print("  ok -", msg)
    else:
        print("  FAIL -", msg)
        FAILS.append(msg)


def _load_daemon():
    # daemon 顶层 import 了仓库根的 bilibili_ecs_cookie（push_once 等）以及一批
    # `*_resolve` 平台模块；其中 lespark_resolve 等只存在于线上、不在 git 里。
    # 本测试只关心缓存清理，故：能 import 的真 import，找不到的用桩顶掉，
    # 保证测试在干净 clone 上也能跑（不联网、不启动浏览器）。
    for p in (str(REPO), str(REPO / "vps_worker")):
        if p not in sys.path:
            sys.path.insert(0, p)

    import types

    class _StubLoader(importlib.abc.Loader):
        def create_module(self, spec):
            mod = types.ModuleType(spec.name)

            def _noop(*a, **k):
                raise RuntimeError("stub module: %s" % spec.name)

            mod.resolve = _noop
            mod.resolve_album = _noop
            mod.__getattr__ = lambda name: _noop
            return mod

        def exec_module(self, module):
            pass

    class _StubFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.endswith("_resolve") or name == "douyin_direct":
                return importlib.util.spec_from_loader(name, _StubLoader())
            return None

    if not any(isinstance(f, _StubFinder) for f in sys.meta_path):
        sys.meta_path.append(_StubFinder())

    spec = importlib.util.spec_from_file_location("_vdl_daemon_under_test", DAEMON)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_vdl_daemon_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _mk(path: Path, size: int, age_sec: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    ts = time.time() - age_sec
    os.utime(path, (ts, ts))


def _size_of(root: Path, prof: str) -> int:
    total = 0
    for sub in ("Default/Cache", "Default/Code Cache", "Default/GPUCache"):
        p = root / prof / sub
        if not p.is_dir():
            continue
        for f in p.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    return total


def main() -> int:
    m = _load_daemon()

    # E. 常量自洽
    _must(hasattr(m, "CACHE_CAP_MB"), "存在 CACHE_CAP_MB 容量上限常量")
    _must(hasattr(m, "CACHE_TRIM_TO_MB"), "存在 CACHE_TRIM_TO_MB 削峰水位常量")
    _must(hasattr(m, "CACHE_FRESH_SEC"), "存在 CACHE_FRESH_SEC 新鲜保护常量")
    _must(m.CACHE_CAP_MB <= 200, "CACHE_CAP_MB <= 200MB（小机器可承受）")
    _must(m.CACHE_TRIM_TO_MB < m.CACHE_CAP_MB, "trim 水位严格低于 cap（防抖动）")
    _must(m.CACHE_FRESH_SEC >= 60, "新鲜保护窗口 >= 60s")

    src = DAEMON.read_text(encoding="utf-8")
    _must("time.sleep(3600)" in src, "巡检周期收紧到 1 小时（12h 太久）")

    # A + C + D：超龄删、新鲜留、登录态不动
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _mk(root / "weibo_profile" / "Default" / "Cache" / "old.bin", 1024, 50 * 3600)
        _mk(root / "weibo_profile" / "Default" / "Cache" / "fresh.bin", 1024, 60)
        _mk(root / "weibo_profile" / "Default" / "Cookies", 4096, 100 * 3600)
        _mk(root / "weibo_profile" / "Default" / "Local Storage" / "ls.bin", 4096, 100 * 3600)
        _mk(root / "weibo_profile" / "Default" / "IndexedDB" / "idb.bin", 4096, 100 * 3600)

        m._prune_profile_caches(root=root)

        cache = root / "weibo_profile" / "Default" / "Cache"
        _must(not (cache / "old.bin").exists(), "A: 50h 前的缓存文件被删")
        _must((cache / "fresh.bin").exists(), "C: 1 分钟内的缓存文件保留")
        _must((root / "weibo_profile" / "Default" / "Cookies").exists(),
              "D: Cookies 未被误删")
        _must((root / "weibo_profile" / "Default" / "Local Storage" / "ls.bin").exists(),
              "D: Local Storage 未被误删")
        _must((root / "weibo_profile" / "Default" / "IndexedDB" / "idb.bin").exists(),
              "D: IndexedDB 未被误删")

    # B：容量闸（用极小阈值模拟，避免写几十 MB）
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        prof = "weibo_profile"
        # 20 个 10KB 文件，mtime 从旧到新各差 1 小时（全部在 48h 内，超龄闸不触发）
        for i in range(20):
            _mk(root / prof / "Default" / "Cache" / ("f%02d.bin" % i), 10 * 1024,
                (20 - i) * 3600)
        before = _size_of(root, prof)
        _must(before >= 200 * 1024, "构造：缓存总量 %d KB" % (before // 1024))

        m._prune_profile_caches(root=root, max_age_hours=48, cap_mb=0.1,
                                trim_to_mb=0.05, fresh_sec=600)
        after = _size_of(root, prof)
        _must(after < before, "B: 超 cap 后确实删了文件（%d KB -> %d KB）"
              % (before // 1024, after // 1024))
        _must(after <= int(0.05 * 1024 * 1024) + 10 * 1024,
              "B: 削到 trim 水位附近（%d KB <= ~50KB）" % (after // 1024))
        # 旧文件先走：最旧的 f00 应已消失，最新的 f19 应还在
        cache = root / prof / "Default" / "Cache"
        _must(not (cache / "f00.bin").exists(), "B: 最旧文件优先被删（f00）")
        _must((cache / "f19.bin").exists(), "B: 最新文件最后才动（f19 保留）")

    # C2：容量闸下 fresh 文件必须被跳过（即便超 cap）
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        prof = "bili_profile"
        _mk(root / prof / "Default" / "Cache" / "hot.bin", 200 * 1024, 5)  # 5s 前
        m._prune_profile_caches(root=root, max_age_hours=48, cap_mb=0.1,
                                trim_to_mb=0.05, fresh_sec=600)
        _must((root / prof / "Default" / "Cache" / "hot.bin").exists(),
              "C: 容量闸也跳过刚写入的文件（不干扰运行中会话）")

    print()
    if FAILS:
        print("FAILED: %d 项" % len(FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
