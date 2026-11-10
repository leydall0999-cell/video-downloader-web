#!/usr/bin/env python3
"""守卫：快手解析的「视频优先 / 图集兜底」路径必须按预期工作。

背景（线上实测，三轮）：
  ① 2026-10-28 —— 图集/图文作品只有幻灯片 HLS 流，没有 progressive mp4 直链，
     需要可读的失败文案（含「图集」），不能白等 30s 再抛通用错误。
  ② 2026-10-30 —— 冷 profile：设备指纹 Cookie 缺失/过期时首访拿不到流，
     reload 带指纹 Cookie 二次访问实测 1.5s 内出直链 ⇒ 需「短轮询 → reload → 再轮询」。
  ③ 2026-11-10 —— **视频分享链接被误判图集**（用户报障）：快手分享短链展开后统一落
     H5 浏览页 `fw/photo/<id>?...&subBiz=BROWSE_SLIDE_PHOTO`，该参数是 H5 浏览页
     业务线标识，**视频分享链接也带它**。旧逻辑一见到参数就立刻抛「图集」⇒ 视频被
     误杀。修正为「先提流（H5 页 → 桌面详情页 → reload），流拿不到且带参数才按图集
     失败」；并补 H5 页 → 桌面详情页（/short-video/<id>）的退化路径。

本守卫钉这些点（缺一即红）：
  ① **不再**用 `BROWSE_SLIDE_PHOTO` 这个 URL 参数当图集判据（视频分享链接也带它，
     旧逻辑据此误杀视频 ⇒ 这是用户报障的根因，必须消除）；
  ② 改由页面数据真实识别图集（`_looks_like_album` 挖 atlas/imageData 等结构），且
     图集 raise 位于**所有轮询之后**——视频先被尽力提流，绝不提前误杀；
  ③ 图集失败文案可读（含「图集」），不复用「APOLLO_STATE 无 mp4 直链」；
  ④ 存在诚实的「解析失败」分支（风控/captcha/IP 被拦导致数据全空时不误导为图集）；
  ⑤ 存在「轮询 → reload → 再轮询」退化路径，轮询总预算 ≤ 30s（5+8+12=25）；
  ⑥ 存在 H5 页 → 桌面详情页（/short-video/<photo_id>）的退化路径；
  ⑦ 正常视频路径没被改坏：video src 选流、mp4 返回结构、元数据字段仍在。

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

    # —— ① 核心修复：不再用 URL 参数误判图集 ——
    m_flag = re.search(r'["\']BROWSE_SLIDE_PHOTO["\']\s+in', src)
    check("不再用 BROWSE_SLIDE_PHOTO 当图集判据（视频分享链接不再被误杀）",
          not m_flag, "仍存在 `\"BROWSE_SLIDE_PHOTO\" in ...` 立即判图集的逻辑")

    # —— ② 真实图集识别 + 轮询/raise 顺序 ——
    m_album_fn = re.search(r"def _looks_like_album\(", src)
    check("存在真实图集检测 _looks_like_album（只认页面数据，不认 URL）",
          bool(m_album_fn), "缺少基于页面数据的图集识别")
    polls = list(re.finditer(r"_poll_streams\(page,\s*(\d+(?:\.\d+)?)\)", src))
    check("存在 _poll_streams 轮询调用（≥2 处）", len(polls) >= 2,
          f"轮询被删/改名/只剩 {len(polls)} 处，守卫失锚")
    m_raise = re.search(r"raise\s+RuntimeError\(\s*\n?\s*\"这是快手图集", src)
    check("存在图集 raise（真图集可读失败）", bool(m_raise), "图集 raise 被删")
    if polls and m_raise:
        check("图集 raise 在所有轮询之后（视频先被尽力提流，不再提前误杀）",
              polls[-1].start() < m_raise.start(),
              "图集 raise 在轮询前 ⇒ 视频分享链接仍会被误判")

    # —— ③ 诚实的「解析失败」分支（风控/数据未加载不误导为图集）——
    m_fail = re.search(r'raise\s+RuntimeError\(\s*\n?\s*\"快手解析失败', src)
    check("存在诚实的「解析失败」分支（风控拦截不误导为图集）",
          bool(m_fail), "缺少非图集的诚实失败文案")

    # —— ④ 文案可读（图集分支）——
    m_msg = re.search(r'"(这是快手图集[^"]*)"', src)
    check("图集文案字符串存在", bool(m_msg), "没找到图集文案字符串")
    if m_msg:
        msg = m_msg.group(1)
        check("失败文案含「图集」二字（用户能看懂）", "图集" in msg,
              "文案没提图集，用户仍看不懂为什么失败")
        check("文案不再复用「APOLLO_STATE 无 mp4 直链」",
              "APOLLO_STATE" not in msg,
              "图集分支仍报通用错误")

    # —— ④ 冷 profile 退化路径 + 轮询预算 ——
    m_reload = re.search(r"page\.reload\(", src)
    check("存在 reload 退化路径（冷 profile 二次访问）", bool(m_reload),
          "没有 reload ⇒ 冷 profile 首访必然失败")
    if m_reload and len(polls) >= 2:
        check("reload 位于首次轮询之后（先短轮询再 reload）",
              polls[0].start() < m_reload.start(),
              "reload 在轮询前 ⇒ 每请求都多一次整页加载，白增延迟")
        check("reload 位于最后一次轮询之前（reload 后才继续等）",
              m_reload.start() < polls[-1].start(),
              "最后一次轮询在 reload 前 ⇒ reload 后没有再等，等于白 reload")
        budget = sum(float(m.group(1)) for m in polls)
        check("轮询总预算 ≤ 30s（H5 页 5s + 桌面 8s + reload 12s）", budget <= 30.0,
              f"当前总预算 {budget}s，用户等待过长")

    # —— ⑤ H5 页 → 桌面详情页退化路径 ——
    m_desktop = re.search(r"short-video/[\s\"']*.*photo_id|photo_id.*short-video", src)
    check("存在桌面详情页退化路径（H5 页拿不到流时切 /short-video/<id>）",
          bool(m_desktop),
          "没有桌面退化 ⇒ 视频分享链接在 H5 壳页上永远拿不到流")

    # —— ⑥ 正常视频路径未被改坏 ——
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
    print(f"🎉 快手解析：视频优先提流 + 图集兜底可读失败 + 桌面退化路径均就位（{CHECKS} 项全过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
