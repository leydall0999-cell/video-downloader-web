#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端背景音乐曲库离线守卫（2026-11-08）。

不启服务、不联网：把 app.WEB_DIR 指到临时目录直接测曲库工具函数，
再对 admin.py 源码做路由闸门静态扫描（防止后来有人把写接口降级成管理员级）。
登记在 server/tests/run_offline_tests.sh。
"""
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app                      # noqa: E402
import routers.admin as adm     # noqa: E402

PASS = 0
FAIL = 0


def ok(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {extra}")


def make_mp3(seconds=1.0):
    """最小合法 mp3：ID3v2 头 + MPEG1 Layer3 128kbps/44.1kHz 帧。"""
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    n = max(1, int(seconds * 44100 / 1152))
    return b"ID3\x03\x00\x00\x00\x00\x00\x00" + frame * n


def route_block(src, route):
    """按字面路由取「装饰器 → 下一个装饰器」之间的真源码。"""
    i = src.find('"' + route + '"')
    if i < 0:
        return ""
    s = src.rfind("\n@router.", 0, i) + 1
    e = src.find("\n@router.", i)
    return src[s:e if e > 0 else len(src)]


def main():
    blob = make_mp3(1.0)
    with tempfile.TemporaryDirectory(prefix="vdl_bgm_") as td:
        orig = app.WEB_DIR
        app.WEB_DIR = td
        try:
            d = Path(td) / "bgm"
            print("\n【1】目录与空清单")
            ok("首次访问自动建 web/bgm", not d.exists())
            adm._bgm_load()
            ok("_bgm_load 懒创建目录", d.is_dir())
            lib = adm._bgm_load()
            ok("缺清单按空库自愈", lib.get("tracks") == [], repr(lib))
            ok("空库带 base_url/version", lib.get("base_url") == adm.BGM_BASE_URL and lib.get("version") == 1)

            print("\n【2】入库校验（扩展名 / 空文件 / 体积 / 魔数）")
            for ext in (".exe", ".html", ".js", ""):
                try:
                    adm._bgm_add(blob, ext, "x")
                    ok(f"拒绝非法扩展名 {ext!r}", False)
                except ValueError:
                    ok(f"拒绝非法扩展名 {ext!r}", True)
            try:
                adm._bgm_add(b"", ".mp3", "x")
                ok("拒绝空文件", False)
            except ValueError:
                ok("拒绝空文件", True)
            old_max = adm.BGM_MAX_BYTES
            adm.BGM_MAX_BYTES = 100
            try:
                try:
                    adm._bgm_add(blob, ".mp3", "x")
                    ok("超过体积上限被拒", False)
                except ValueError:
                    ok("超过体积上限被拒", True)
            finally:
                adm.BGM_MAX_BYTES = old_max
            try:
                adm._bgm_add(b"this is not an mp3", ".mp3", "x")
                ok("mp3 魔数校验（防改名上传）", False)
            except ValueError:
                ok("mp3 魔数校验（防改名上传）", True)
            ok("非 mp3 时长解析返回 None", adm._bgm_mp3_duration(b"junk") is None)

            print("\n【3】正常入库：文件名重生成 / 落盘 / 清单 / 时长")
            t = adm._bgm_add(blob, ".mp3", "测试曲", "轻柔", "自有版权")
            ok("文件名是 16 位 hex + .mp3（不含原始中文名）",
               bool(re.fullmatch(r"[0-9a-f]{16}\.mp3", t["file"])), t["file"])
            f = d / t["file"]
            ok("音频真落盘且字节一致", f.is_file() and f.read_bytes() == blob)
            ok("sha256 与内容一致", t["sha256"] == hashlib.sha256(blob).hexdigest())
            ok("曲名 / 分类 / 授权说明写入", t["name"] == "测试曲" and t["category"] == "轻柔"
               and t["license"] == "自有版权")
            ok("mp3 时长估算落在合理区间",
               isinstance(t["duration"], (int, float)) and 0.5 < t["duration"] < 2.0,
               repr(t["duration"]))
            lib = adm._bgm_load()
            ok("清单写入 1 首且三键齐全", len(lib["tracks"]) == 1
               and all(k in lib for k in ("version", "base_url", "tracks")))
            ok("清单不含 token/secret/password 等敏感字段",
               not re.search(r"token|secret|password", json.dumps(lib, ensure_ascii=False), re.I))

            t2 = adm._bgm_add(blob, ".wav", "第二首")
            t3 = adm._bgm_add(blob, ".mp3", "第三首")
            ok("连续入库 3 首互不覆盖", len(adm._bgm_load()["tracks"]) == 3
               and len({t["id"], t2["id"], t3["id"]}) == 3)

            print("\n【4】删除与路径穿越防护")
            ok("删除返回 True", adm._bgm_remove(t2["id"]) is True)
            ok("删除后磁盘文件消失", not (d / t2["file"]).exists())
            ok("删除后清单剩 2 首", len(adm._bgm_load()["tracks"]) == 2)
            ok("重复删除幂等返回 False", adm._bgm_remove(t2["id"]) is False)
            ok("非法 id（路径穿越）直接拒绝", adm._bgm_remove("../../etc/passwd") is False)
            lib = adm._bgm_load()
            lib["tracks"][0]["file"] = "../escape.mp3"
            adm._bgm_save(lib)
            victim = Path(td) / "escape.mp3"
            victim.write_bytes(b"keep")
            ok("清单被篡改成 ../ 时拒绝删除", adm._bgm_remove(lib["tracks"][0]["id"]) is False)
            ok("曲库目录外的文件未被误删", victim.read_bytes() == b"keep")
            lib = adm._bgm_load()
            lib["tracks"][0]["file"] = t["file"]
            adm._bgm_save(lib)
            ok("原子写无 .tmp 残留", not list(d.glob("*.tmp")) and not list(d.glob(".*.tmp")))

            print("\n【5】坏清单自愈")
            (d / "library.json").write_text("{broken", encoding="utf-8")
            ok("JSON 损坏时按空库自愈（后台页不 500）", adm._bgm_load()["tracks"] == [])
            (d / "library.json").unlink()
        finally:
            app.WEB_DIR = orig

    print("\n【6】路由闸门静态扫描（防止后来降级成管理员可写）")
    src = (ROOT / "routers" / "admin.py").read_text(encoding="utf-8")
    b_list = route_block(src, "/api/admin/bgm")
    b_up = route_block(src, "/api/admin/bgm/upload")
    b_del = route_block(src, "/api/admin/bgm/delete")
    ok("三条路由都已挂载", all(b_list and b_up and b_del))
    ok("GET 列表用 require_admin（管理员可读）", "require_admin(request)" in b_list)
    ok("上传用 require_super_backend", "require_super_backend(request)" in b_up)
    ok("删除用 require_super_backend", "require_super_backend(request)" in b_del)
    ok("写接口不残留弱闸门 require_admin",
       "require_admin(request)" not in b_up and "require_admin(request)" not in b_del)
    for name, b in (("上传", b_up), ("删除", b_del)):
        g = b.find("require_super_backend(request)")
        w = b.find("_bgm_add") if name == "上传" else b.find("_bgm_remove")
        ok(f"{name}闸门排在写盘动作之前", 0 < g < w, f"gate={g} write={w}")

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
