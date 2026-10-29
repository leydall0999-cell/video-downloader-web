#!/usr/bin/env python3
"""守卫：快手解析的两条失败/退化路径必须按预期工作。

背景（线上实测，两轮）：
  ① 2026-10-28 —— **图集/图文**作品：短链展开后 final URL 带 `subBiz=BROWSE_SLIDE_PHOTO`，
     页面只有幻灯片 HLS 流（`vod-rt-remux/hls-ts/*.m3u8`），没有 progressive mp4 直链。
     此前无该分支 ⇒ 一律进长轮询 → 必然超时 → 白等 30s 再抛通用错误。
  ② 2026-10-30 —— **冷 profile**：设备指纹 Cookie 缺失/过期时，首访 `<video>` 存在但
     src 为空、`__APOLLO_STATE__` 里零 kwaicdn，且该 SPA 不会自己重取数据 ⇒ 单页轮询
     无论多久都拿不到流（实测 25s 全程 len 不变）。二次访问（reload）带指纹 Cookie
     才会返回数据，实测 1.5s 内出直链。

本守卫钉这些点（缺一即红）：
  ① 源码里有 `BROWSE_SLIDE_PHOTO` 判据，且位于 `page.goto(...)` 之后；
  ② 判据在**首次轮询调用之前**（放到后面等于没加）；
  ③ 图集失败文案可读（含「图集」），不复用「APOLLO_STATE 无 mp4 直链」；
  ④ 存在「短轮询 → reload → 再轮询」的退化路径，且轮询总预算不超过原先的 25s；
  ⑤ 正常视频路径没被改坏：video src 选流、mp4 返回结构、元数据字段仍在。

跑法：`python3 server/tests/test_kuaishou_slide_photo.py`
"""
from __future__ import annotations

import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
# 可传入被测文件路径：变异测试在**副本**上注入缺陷后再跑本守卫（绝不动真源码）
SRC = (pathlib.Path(sys.argv[1]) if len(sys.argv) > 1
       else HERE.parent.parent / "vps_worker" / "kuaishou_resolve.py")

FAILS = 0
CHECKS = 0


def check(desc: str, cond: bool, extra: str = "") -> None:
    global FAILS, CHECKS
    CHECKS += 1
    if cond:
        print(f"  ✅ {desc}")
    else:
        FAILS += 1
        print(f"  ❌ {desc} | {extra}")


def main() -> int:
    print(f"被测文件：{SRC}")
    if not SRC.is_file():
        print(f"❌ 找不到被测文件：{SRC}")
        return 1
    src = SRC.read_text(encoding="utf-8")

    # —— ① 判据存在 ——
    m_flag = re.search(r'["\']BROWSE_SLIDE_PHOTO["\']\s+in', src)
    check("存在 BROWSE_SLIDE_PHOTO 判据", bool(m_flag),
          "没找到 `\"BROWSE_SLIDE_PHOTO\" in ...` 判断")

    # —— ② 位置：goto 之后、首次轮询之前 ——
    m_goto = re.search(r"page\.goto\(", src)
    polls = list(re.finditer(r"_poll_streams\(page,\s*(\d+(?:\.\d+)?)\)", src))
    check("存在 _poll_streams 轮询调用", bool(polls), "轮询被删/改名，守卫失锚")
    if m_flag and m_goto and polls:
        check("判据在 page.goto 之后（URL 已展开）", m_goto.start() < m_flag.start(),
              "判据早于 goto ⇒ 短链还没展开，永远判不出图集")
        check("判据在首次轮询之前（才算提前失败）", m_flag.start() < polls[0].start(),
              "判据晚于轮询 ⇒ 仍然白等，等于没修")

    # —— ③ 文案可读 ——
    if m_flag:
        window = src[m_flag.start(): m_flag.start() + 400]
        check("判据命中后立即 raise", "raise RuntimeError" in window,
              "没有 raise ⇒ 判据不生效")
        check("失败文案含「图集」二字（用户能看懂）", "图集" in window,
              "文案没提图集，用户仍看不懂为什么失败")
        check("文案不再复用「APOLLO_STATE 无 mp4 直链」", "APOLLO_STATE 无 mp4" not in window,
              "图集分支仍报通用错误")

    # —— ④ 冷 profile 退化路径：短轮询 → reload → 再轮询，且总预算不膨胀 ——
    m_reload = re.search(r"page\.reload\(", src)
    check("存在 reload 退化路径（冷 profile 二次访问）", bool(m_reload),
          "没有 reload ⇒ 冷 profile 首访必然 30s 后失败")
    if m_reload and len(polls) >= 2:
        check("reload 位于首次轮询之后（先短轮询再 reload）",
              polls[0].start() < m_reload.start(),
              "reload 在轮询前 ⇒ 每请求都多一次整页加载，白增延迟")
        check("reload 位于第二次轮询之前（reload 后才继续等）",
              m_reload.start() < polls[1].start(),
              "第二次轮询在 reload 前 ⇒ 拿到的仍是旧 DOM")
        budget = sum(float(m.group(1)) for m in polls)
        check("轮询总预算 ≤ 25s（不比旧实现更慢）", budget <= 25.0,
              f"当前总预算 {budget}s，超过旧实现 25s，用户等待变长")
    elif m_reload:
        check("reload 后仍有轮询", False, "reload 之后没有再次 _poll_streams，等于白 reload")

    # —— ⑤ 正常视频路径未被改坏 ——
    check("仍按 video 标签 src 优先选流", "document.querySelector('video')" in src,
          "选流逻辑被删")
    check("仍返回 ext=mp4 结构", re.search(r'"ext":\s*"mp4"', src) is not None,
          "返回结构被改")
    check("仍提取标题/作者/时长", all(k in src for k in ('"title":', '"uploader":', '"duration":')),
          "元数据字段缺失")

    print()
    if FAILS:
        print(f"❌ 快手解析守卫未通过（{FAILS}/{CHECKS} 项失败）")
        return 1
    print(f"🎉 快手解析：图集快失败 + 冷 profile reload 退化路径均就位（{CHECKS} 项全过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
