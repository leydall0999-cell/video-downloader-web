"""VDL 会员引擎（V1，照搬 DataTool.vip 三轨结构）。

三轨：
  1) download_member  VIP会员（1/3/7/30/180/365 天订阅；含下载类权益配额表）
  2) ai_member        AI 会员（月订阅 + 一次性积分池；自动捆绑VIP会员权益）
  3) permanent_credits 永久积分包（纯按次计费，不与订阅绑定）

设计要点：
  - 纯标准库、零外部依赖（engine 不 import app / fastapi），可独立单测。
  - 时间与存储路径全部可注入（now_fn / path），测试无需 mock 系统时钟。
  - 激活续费顺延：expire_at = max(now, 当前到期) + 时长，不吞已有天数。
  - AI 会员激活时自动把VIP会员权益覆盖到同一到期日（捆绑，无「纯 AI」档）。
  - 积分消耗顺序：先扣 AI 订阅积分（随会员到期清零），再扣永久积分。
  - 日配额惰性重置：daily_usage.date 非当日时自动清零重计。

V1 明确不做：真实支付、验签、功能门禁接入。仅提供引擎 + 状态机，供
/api/member 路由与后续功能模块调用。

参考文档：video-downloader-app/VDL_会员商业化_V1方案_2026-09-05.md
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------- #
# 套餐与权益常量表（唯一真源：VDL_会员商业化_V1方案_2026-09-05.md）
# --------------------------------------------------------------------------- #

# download member: 时长（天）、折算锚点
# 🔴 2026-10-16 用户定档：下载会员统一改名「VIP会员·X」（6 档全在；3 天档当天
#    一度下架、当天即按用户要求重新上架，云端覆盖层与默认值保持一致）。
DOWNLOAD_PLANS: dict[str, dict[str, Any]] = {
    "download_1day":       {"price_cny": 1.90,   "days": 1,    "label": "VIP会员·1天",   "saving": 0.0},
    "download_3day":       {"price_cny": 4.90,   "days": 3,    "label": "VIP会员·3天",   "saving": 0.0},
    "download_7day":       {"price_cny": 9.90,   "days": 7,    "label": "VIP会员·7天",   "saving": 0.0},
    "download_month":      {"price_cny": 29.80,  "days": 30,   "label": "VIP会员·30天",  "saving": 0.0},
    "download_half_year":  {"price_cny": 99.90,  "days": 180,  "label": "VIP会员·半年",  "saving": 0.44},
    "download_year":       {"price_cny": 179.00, "days": 365,  "label": "VIP会员·1年",   "saving": 0.50, "best": True},
}

# 🔴 2026-10-24 用户定档：开通 VIP 会员赠 300 积分（此前是 0，「会员中心」里永远显示 0 积分）。
#    落「永久积分」池而不是 AI 订阅池：AI 池在 AI 会员到期时会被清零（见 status/apply_cloud），
#    会员档送的积分不该跟着被清。
#    ⚠️ 现在按「每买一次送一次」执行，短档（1 天 ¥1.90）理论上可以反复买来攒积分 —— 已向用户说明；
#    若以后要按档位差异化，给 DOWNLOAD_PLANS 各档加一个 credits 字段即可（本函数读 get 回退）。
VIP_MEMBER_CREDITS = 300

# AI member: 积分池大小
AI_PLANS: dict[str, dict[str, Any]] = {
    "ai_5500":   {"price_cny": 49.90, "days": 30, "credits": 5500,  "label": "AI 积分月会员"},
    "ai_15000":  {"price_cny": 99.90, "days": 30, "credits": 15000, "label": "AI 月会员", "best": True},
}

# 永久积分包
CREDIT_PACKS: dict[str, dict[str, Any]] = {
    "credits_5000":  {"price_cny": 50.00, "credits": 5000,  "label": "5000 积分包"},
    "credits_15000": {"price_cny": 99.00, "credits": 15000, "label": "15000 积分包", "best": True},
}

# 下载类权益的每日配额上限（会员档；免费档见 FREE_DAILY_LIMITS）
# 🔴 2026-10-04 清理：删掉 original / batch_material。二者只有配额条目、没有任何
#    use_daily 拦截点；而且「原画」本身是**清晰度档位门**（>1080P 需会员，见
#    routers/core.py::_quality_gate_error），不按次计费 —— 拿次数配额表达是错的概念。
#    留着会让会员页承诺「原画 100 次/日、批量素材 1000 条/日」两份不存在的权益。
DAILY_QUOTA_LIMITS: dict[str, int] = {
    "download": 1000,         # 下载任务 / 日（会员）—— 2026-09-06 起配额墙在「点清晰度下载」处
    # 🔴 2026-10-06 拆池：每个功能独立「每日次数」，与桌面端同名键共享口径。
    # 网页端没有解说功能，故无 cloud_commentary 键（别承诺不存在的功能）；
    # 旧总池键 "cloud" 已无调用点（routers 全部改传独立键），按 2026-10-04 同样的
    # 清理原则一并移除，避免权益页渲染一条重复的旧文案。
    "cloud_convert": 200,     # 在线转码 / 日（会员）
    # 🔴 2026-10-19：音乐/图片转换从「在线转码」拆出独立计数（与桌面端同键同名，
    # 见 FEATURE_USAGE_DEFS 注释）。不在这张表里 ⇒ quota_state 视为未知资源而放行。
    "convert_audio": 200,     # 音乐转换 / 日（会员）
    "convert_image": 200,     # 图片转换 / 日（会员）
    "cloud_concat": 200,      # 在线拼接 / 日（会员）
    "cloud_dewatermark": 200, # 图片去水印 / 日（会员）
    "cloud_dewatermark_pdf": 200,  # PDF 去水印 / 日（会员）
    "cloud_subtitle": 200,    # 字幕提取 / 日（会员）
    "cloud_subtitle_burn": 200,     # 字幕烧录 / 日（会员）
    "cloud_subtitle_translate": 200,  # 字幕翻译 / 日（会员）
    # 🔴 2026-10-19：「生成二维码 / 生成网页」此前只有 share_token.py 里一份
    # **本地文件计数**（20 次/日，写死、后台改不了、不与 App 关联），个人中心
    # 「使用统计」里根本没有这两行（用户反馈「统计不全」）。现并入账号级每日配额
    # 体系（走 use_daily，按天 + 上云 + 与桌面端同一账号共用）。
    # ⚠️ 刻意**不**走 cloud_quota_gate 的「终身 N 次」口径：分享吃的是分享节点
    # 磁盘不是大模型算力，且中心 _CLOUD_RESOURCES 只认 8 个旧键（未知键会被
    # 回退成 cloud_commentary，等于让分享去消耗「视频解说」的终身额度）。
    "share_qr": 200,          # 生成二维码（分享链接）/ 日（会员）
    "share_page": 200,
    "cloud_drive": 200,         # 网盘下载（服务器中转百度直链）/ 日（会员）        # 生成网页（转在线链接）/ 日（会员）
}
# 免费档每日配额（2026-10-04 定稿：免费下载 10 次/日；2026-10-06 起各功能免费 3 次/日独立计）
FREE_DAILY_LIMITS: dict[str, int] = {
    "download": 10,
    "cloud_convert": 3,       # 在线转码 免费 3 次/日（独立计）
    # 🔴 2026-10-19：与桌面端 convert_audio / convert_image 同档（免费 5 次/日）
    "convert_audio": 5,       # 音乐转换 免费 5 次/日（独立计）
    "convert_image": 5,       # 图片转换 免费 5 次/日（独立计）
    "cloud_concat": 3,        # 在线拼接 免费 3 次/日（独立计）
    "cloud_dewatermark": 3,   # 图片去水印 免费 3 次/日（独立计）
    "cloud_dewatermark_pdf": 3,  # PDF 去水印 免费 3 次/日（独立计）
    "cloud_subtitle": 3,      # 字幕提取 免费 3 次/日（独立计）
    "cloud_subtitle_burn": 3,     # 字幕烧录 免费 3 次/日（独立计）
    "cloud_subtitle_translate": 3,  # 字幕翻译 免费 3 次/日（独立计）
    # 🔴 2026-10-19：沿用受限版分享原有的 20 次/日（此前由 share_token.py 的
    # VDL_WEB_SHARE_DAILY 写死），迁到配额表后后台可改，且与桌面端同账号共用。
    "share_qr": 20,           # 生成二维码 免费 20 次/日
    "share_page": 20,         # 生成网页 免费 20 次/日
    "cloud_drive": 3,          # 网盘下载 免费 3 次/日
}
# 不限次资源：网页端没有「评论/数据/字幕批量」功能（那是从 DataTool 抄来的、V1 未实现），
# 故为空元组。quota_state 遇到不在两张表里的 resource 会走 unknown → fail-open 放行。
UNLIMITED_QUOTA: tuple[str, ...] = ()

# AI 会员权益文案（供 plans().ai_member.features，前端会员中心渲染）
# 🔴 2026-10-04 定档：只写**真实存在**的能力。此前这里写过「AI 字幕识别 / 视频总结 /
#    图片翻译体验」三项，全仓搜不到对应路由 = 拿不存在的功能做卖点，已删。
# ⚠️ **网页端与桌面端的清单必须分开写**：桌面端有自动解说（routers/commentary.py
#    + commentary-pipeline，LLM_API_KEY 硬依赖）与一键抠图（routers/matting.py），
#    网页端两者都没有（已 grep 核实）。把桌面端能力抄到网页端 = 承诺做不到的功能。
# 网页端目前唯一与 AI 相关的真实能力是「赠送积分随购买额到账」+「捆绑VIP会员权益」。
# ⚠️ 措辞守则：不得出现「本地」这类实现口径字眼（用户 2026-10-04 定档）。
AI_FEATURES: list[str] = [
    "赠送积分 5500 / 15000，30 天有效",
    "积分可用于按量计费的高级功能",
    "包含VIP会员全部权益",
]

# 个人中心「今日使用」功能配额表（与前端表格四列对应：功能/体验剩余/权益余额/积分单价）
# - resource: 关联的 daily_usage 资源键
# - free_limit / member_limit: 每日体验配额（-1 表示不限）
# - ai_bonus: AI 会员周期内赠送额度（按 unit 单位）
# - credit_cost: 权益不足时按量扣积分单价（0 表示免费）
# 🔴 2026-10-04 铁律：**只有真有 use_daily/quota_state 拦截点的功能才能进这张表**。
#    本表逐条对应服务器上真实拦得住的配额；守卫 test_feature_usage_gate.mjs 会静态
#    核对「每一行的 resource 都能在业务代码里搜到拦截点」，防止再塞占位行
#    （V1 时从 DataTool 抄了 8 行从未实现的功能，是前车之鉴）。
FEATURE_USAGE_DEFS: list[dict[str, Any]] = [
    {"key": "video_parse",   "name": "下载视频", "resource": "download", "unit": "次", "free_limit": 10, "member_limit": 1000, "ai_bonus": 0, "credit_cost": 0},
    # 🔴 2026-10-06 拆池：旧「云端算力」单行拆为 3 个独立功能行（网页端没有解说功能，
    # 故无 cloud_commentary 行；桌面端 4 行齐全）。resource 与 app.py cloud_quota_gate
    # 的调用点一一对应（routers/convert.py、dewatermark.py、subtitle.py、subtitles.py）。
    {"key": "cloud_convert",     "name": "在线转码",   "resource": "cloud_convert",     "unit": "次", "free_limit": 3, "member_limit": 200, "ai_bonus": 0, "credit_cost": 0},
    # 🔴 2026-10-19 用户反馈「使用统计不全，没有全部统计上去」：网页版此前把
    # 音乐转换 / 图片转换都算进「在线转码」一个计数器（两条前端入口共用
    # /api/upload-convert 与 upload-chunk/finish 的 cloud_convert 闸门），
    # 表里看不到这两项。现按**桌面端口径**拆成独立键（免费 5 次/日、会员 200 次/日），
    # 计数点见 routers/convert.py 的 _convert_resource()：按 target 落在
    # CONVERT_AUDIO / CONVERT_IMAGE 集合里分派，其余仍是 cloud_convert。
    {"key": "convert_audio",     "name": "音乐转换",   "resource": "convert_audio",     "unit": "次", "free_limit": 5, "member_limit": 200, "ai_bonus": 0, "credit_cost": 0},
    {"key": "convert_image",     "name": "图片转换",   "resource": "convert_image",     "unit": "次", "free_limit": 5, "member_limit": 200, "ai_bonus": 0, "credit_cost": 0},
    {"key": "cloud_concat",      "name": "在线拼接",   "resource": "cloud_concat",      "unit": "次", "free_limit": 3, "member_limit": 200, "ai_bonus": 0, "credit_cost": 0},
    {"key": "cloud_dewatermark", "name": "图片去水印", "resource": "cloud_dewatermark", "unit": "次", "free_limit": 3, "member_limit": 200, "ai_bonus": 0, "credit_cost": 0},
    {"key": "cloud_dewatermark_pdf", "name": "PDF 去水印", "resource": "cloud_dewatermark_pdf", "unit": "次", "free_limit": 3, "member_limit": 200, "ai_bonus": 0, "credit_cost": 0},
    {"key": "cloud_subtitle",    "name": "字幕提取",   "resource": "cloud_subtitle",    "unit": "次", "free_limit": 3, "member_limit": 200, "ai_bonus": 0, "credit_cost": 0},
    {"key": "cloud_subtitle_burn", "name": "字幕烧录", "resource": "cloud_subtitle_burn", "unit": "次", "free_limit": 3, "member_limit": 200, "ai_bonus": 0, "credit_cost": 0},
    {"key": "cloud_subtitle_translate", "name": "字幕翻译", "resource": "cloud_subtitle_translate", "unit": "次", "free_limit": 3, "member_limit": 200, "ai_bonus": 0, "credit_cost": 0},
    # 🔴 2026-10-19：分享的两个子页签此前**完全没进统计表**（计数只在
    # share_token.py 的本地文件里，个人中心看不到）。计数点见
    # routers/share_token.py::_share_resource() —— 按子页签分派，
    # 「生成网页」转在线链接计 share_page，其余计 share_qr。
    {"key": "share_qr",    "name": "生成二维码", "resource": "share_qr",    "unit": "次", "free_limit": 20, "member_limit": 200, "ai_bonus": 0, "credit_cost": 0},
    {"key": "share_page",  "name": "生成网页",   "resource": "share_page",  "unit": "次", "free_limit": 20, "member_limit": 200, "ai_bonus": 0, "credit_cost": 0},
    {"key": "baidu_pan",   "name": "网盘下载",   "resource": "cloud_drive", "unit": "次", "free_limit": 3,  "member_limit": 200, "ai_bonus": 0, "credit_cost": 0},
]

# --------------------------------------------------------------------------- #
# AI 积分成本表（2026-10-05 改版：云端算力 + 本机重算力都计费，且可在后台改价）
# --------------------------------------------------------------------------- #
# 旧口径（2026-09-08）：只给「云端抠图」一项计费，本地算力一律免费。
# 2026-10-05 用户在桌面端重新定档并与网页版拉齐，两条原则：
#   1) **云端 + 本机重算力都计费** —— 本机功能不烧平台的钱，但真实占用用户 CPU/内存；
#   2) **按「次」定价** —— 一次操作内部发生多次模型调用只扣一份，重试成本含在单价里。
#
# 🔴 网页版与桌面端的**成本性质并不一样**：桌面端去水印/字幕/抠图跑在用户本机，
#    网页版跑在**服务端**（ECS 真花钱、真占服务器 CPU）。所以这几项在网页版
#    更应该计费，而不是照搬「占的是用户的机器」那套说辞。
#
# 优先级：plans.json 的 `credit_costs` 覆盖层 → 代码默认（见 `credit_cost`）。
# 云端抠图（火山 MediaKit，真实服务端算力）单次
MATTING_CLOUD_CREDIT_COST: int = 50

AI_CREDIT_COSTS: dict[str, dict[str, Any]] = {
    # ── 云端算力（平台真实付钱给上游）────────────────────────────────────
    "commentary_llm": {
        "name": "自动解说（大模型写稿）",
        "where": "commentary-worker/scripts/llm_script.py（由 commentary-worker/process.py:139 调用）",
        "cost": 40,
        "real_cost": "¥0.04~0.19/次（桌面端同口径实测 token × DeepSeek V4-Flash 峰谷价；"
                     "网页版走同一个被执行人管线）",
        "note": "剧情分析 + 脚本生成 + 修复重试，一次任务一份（内部 2~4 次 LLM 调用）。"
                "⚠️ DeepSeek 2026-08-17 已涨价 50%~125%，上调/复查时按 real_cost 重算。",
    },
    "matting_cloud": {
        "name": "云端一键抠图",
        "where": "桌面端 server/routers/matting.py；⚠️ 网页版暂无该路由",
        "cost": 50,
        "real_cost": "¥0.02~0.24/次（火山按次计费，官方未公开图像单价，按实测推的区间）",
        "note": "网页版本身不提供抠图。保留这项只是为了让 plans.json 里已有的历史 "
                "`credit_costs.matting_cloud` 不至于被判成「孤儿项」，定价与桌面端对齐。",
    },
    # ── 服务端算力（网页端专属：跑在 ECS 上，占的是服务器不是用户机器）────
    "dewatermark_ai": {
        "name": "AI 去水印（LaMa）",
        "where": "server/routers/dewatermark.py:113/121（engine=ai，服务端 onnxruntime）",
        "cost": 10,
        "real_cost": "≈¥0（不调用外部付费 API，但**占服务器 CPU/内存**）",
        "note": "🔴 与桌面端不同：网页端的 LaMa 跑在服务端不再是用户的机器上，"
                "每一次都是这台 ECS 在算。opencv 引擎（默认档）不计费，只有 ai 档计费。",
    },
    "subtitle_asr": {
        "name": "字幕提取（Whisper 转写）",
        "where": "server/routers/subtitle.py:219/295（faster-whisper，服务端 CPU）",
        "cost": 5,
        "real_cost": "≈¥0（不外调付费 API，占服务端 CPU，长音频耗时可观）",
        "note": "同样跑在服务端。另有每日云端算力配额（免费 3 次/日、会员 200 次/日）。",
    },
    "subtitle_translate": {
        "name": "字幕翻译 / 多语字幕",
        "where": "server/routers/subtitles.py:38/56（按 chunk 调用 OpenAI 兼容接口）",
        "cost": 5,
        "real_cost": "≈¥0.005/次（按小片段 1200+800 token 估；若上游换成付费模型则重算）",
        "note": "长字幕会分多片，按一次操作一份计。",
    },
}


# --------------------------------------------------------------------------- #
# 套餐 / 成本运行时覆盖层（~/.video-downloader/plans.json）
# 仅作「覆盖层」：未覆盖的键回退到上方代码常量，默认值永不丢失。
# 后台管理面板可经 /api/admin/config/plans 写回。
# --------------------------------------------------------------------------- #
import threading as _threading

_PLAN_OVERRIDE_LOCK = _threading.Lock()
_PLAN_OVERRIDE_CACHE: Optional[tuple[int, dict[str, Any]]] = None


def _base_dir() -> Path:
    """数据目录：VDL_DATA_DIR 优先（离线测试隔离，绝不写真实家目录）。

    与 auth_store._base_dir() 保持同一语义（同目录、同覆盖位）——两处若分叉，
    测试会把真实 plans.json / membership.json 写脏。
    """
    override = os.environ.get("VDL_DATA_DIR", "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
    return Path.home() / ".video-downloader"


def plan_override_path() -> Path:
    return _base_dir() / "plans.json"


def load_plan_overrides() -> dict[str, Any]:
    """读取 plans.json 覆盖（mtime 缓存）。无文件 / 损坏 → 空 dict。"""
    global _PLAN_OVERRIDE_CACHE
    p = plan_override_path()
    try:
        if not p.exists():
            _PLAN_OVERRIDE_CACHE = None
            return {}
        mtime = p.stat().st_mtime_ns
        cached = _PLAN_OVERRIDE_CACHE
        if cached is not None and cached[0] == mtime:
            return cached[1]
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
        if not isinstance(data, dict):
            data = {}
        _PLAN_OVERRIDE_CACHE = (mtime, data)
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def save_plan_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """写入 plans.json（0600）。与现有覆盖做 overlay 合并；传 null 的键视为删除。

    接受结构：
      { "download_plans": {...}, "ai_plans": {...}, "credit_packs": {...},
        "credit_costs": {...}, "free_trial": {...} }
    任意键可缺省；返回写盘后的完整覆盖 dict。失败抛 OSError。

    🔴 `free_trial` 是**扁平**策略字典（enabled/mode/exclude/max_cost…），也必须
    列入 `_SAVE_TABLE_KEYS` —— 否则「只提交 enabled」会被整表替换成只剩 enabled
    一个键，其余字段凭空消失。这一点桌面端 2026-10-05 已踩过，网页端同步补上。

    合并语义（2026-09-30 对齐 app-dev 2026-09-24 修复）：四张表内部按
    「套餐 code / 成本 key」逐条合并——载荷只更新它携带的条目，同表其余条目
    原样保留（此前是整表替换，部分载荷会把未携带的套餐从覆盖层抹掉，
    造成「改一个价、别的全丢」）。条目值为 null 视为删除该条目。
    """
    global _PLAN_OVERRIDE_CACHE
    with _PLAN_OVERRIDE_LOCK:
        existing = load_plan_overrides()
        for k, v in (data or {}).items():
            if v is None:
                existing.pop(k, None)
                continue
            if k in _SAVE_TABLE_KEYS and isinstance(v, dict) and isinstance(existing.get(k), dict):
                merged = dict(existing[k])
                for ik, iv in v.items():
                    if iv is None:
                        merged.pop(ik, None)
                    else:
                        # 🔴 档位内部也要字段级合并（2026-10-03）：原来整条替换，
                        # 只提交一个字段（如限量计数 sold +1）会把该档 price/days/stock 全抹掉。
                        base = merged.get(ik)
                        if isinstance(base, dict) and isinstance(iv, dict):
                            merged[ik] = {**base, **iv}
                        else:
                            merged[ik] = iv
                existing[k] = merged
            else:
                existing[k] = v
        p = plan_override_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        _PLAN_OVERRIDE_CACHE = (p.stat().st_mtime_ns, existing)
        return existing


_SAVE_TABLE_KEYS = ("download_plans", "ai_plans", "credit_packs", "credit_costs", "free_trial")


def _overlay_plans(defaults: dict[str, Any], override: Any) -> dict[str, Any]:
    """把覆盖表逐字段叠加到代码默认套餐上：默认值打底，覆盖字段获胜。

    覆盖表缺整个条目 → 用默认条目；条目里缺某字段（如 days/label）→ 落回默认值。
    只存在于覆盖层的条目原样保留（超管可新增档位）。**顺序保持默认表顺序**，
    新见键追加在末尾，避免后台改价把前端卡片顺序打乱。
    """
    if not isinstance(override, dict) or not override:
        return dict(defaults)
    out: dict[str, Any] = {k: dict(v) if isinstance(v, dict) else v for k, v in defaults.items()}
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            merged = dict(out[k])
            merged.update(v)
            out[k] = merged
        else:
            out[k] = v
    return out


# ── 套餐价格云端真源（2026-10-03）────────────────────────────────────────────
# 授权中心（/api/license/plans）是价格唯一真源：桌面后台改价会下发云端，网页版
# 这里按 TTL 拉取覆盖到本地默认之上。云端不可达时自动回退本机覆盖层 + 代码常量。
_CLOUD_PLANS_CACHE: dict[str, Any] = {"ts": 0.0, "plans": {}, "notice": {}}
# 套餐上架/改价回源节流：原 300s（上架后网页最长 5 分钟才刷新，用户感知「延迟」）。
# 2026-10-04 配套修复：降到 15s，与会员状态节流(AUTHORITY_MIN_INTERVAL)对齐。
# 套餐目录是静态配置、单进程每 15s 一次 POST 完全可忽略，且云端不可达仍回退本机。
_CLOUD_PLANS_TTL = 15.0


def _license_api(path: str) -> str:
    """授权中心接口基址；未配置返回空串（调用方回退本机）。"""
    base = ""
    try:
        import license_client
        base = str(license_client.license_base() or "").rstrip("/")
    except Exception:  # noqa: BLE001 — 云端不可用不该影响网页
        base = str(os.environ.get("VDL_LICENSE_BASE") or "").rstrip("/")
    return f"{base}{path}" if base else ""


def cloud_plan_overrides(force: bool = False) -> dict[str, Any] | None:
    """从授权中心拉价格覆盖表。失败/未配置返回 None → 调用方回退本机。

    设 `VDL_PLANS_CLOUD=0` 可完全关闭云端取价（离线测试/单机部署用）。
    """
    if str(os.environ.get("VDL_PLANS_CLOUD") or "1").strip().lower() in ("0", "false", "off"):
        return None
    url = _license_api("/api/license/plans")
    if not url:
        return None
    now = time.time()
    if not force and (now - float(_CLOUD_PLANS_CACHE.get("ts") or 0.0)) < _CLOUD_PLANS_TTL:
        return _CLOUD_PLANS_CACHE.get("plans") or {}
    try:
        import urllib.request as _rq
        req = _rq.Request(url, data=b"{}", method="POST",
                          headers={"Content-Type": "application/json",
                                   "User-Agent": "VDL-Web/1.0"})
        with _rq.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        plans = data.get("plans") if isinstance(data, dict) else None
        if not isinstance(plans, dict):
            return None
        _CLOUD_PLANS_CACHE["ts"] = now
        _CLOUD_PLANS_CACHE["plans"] = plans
        # 🔴 2026-10-18：会员中心公告复用同一次拉取（不额外增加往返）。
        nt = data.get("notice") if isinstance(data, dict) else None
        _CLOUD_PLANS_CACHE["notice"] = nt if isinstance(nt, dict) else {}
        return plans
    except Exception:  # noqa: BLE001 — 网络/协议异常一律回退本机
        return None


# ── 每日免费/会员额度云端真源（2026-10-17）──────────────────────────────────
# 🔴 背景：后台「免费额度」分栏改的 daily_free_limits 以前**只写桌面端本机
#    plans.json**，网页版压根没有这套概念 ⇒ 同一个账号桌面显示「下载 3/3、各工具
#    1/1」，网页显示「下载 10/10、云端 3/3」，用户实测反馈「免费次数对不上」。
#    这与套餐价格同一套原则：**授权中心是唯一真源**，网页版按 TTL 拉回覆盖层。
# 独立缓存（不复用价格那份）的原因：价格路径失败时是「立即重试」，而这里被
#    quota_state 高频调用，云端不可达必须快速失败 + 退避，否则每次请求等 6s。
_FREEQ_CACHE: dict[str, Any] = {"ts": 0.0, "data": {}, "fail_ts": 0.0}
_FREEQ_TTL = 15.0
_FREEQ_FAIL_BACKOFF = 30.0


def cloud_free_quota_overrides(force: bool = False) -> dict[str, Any]:
    """从授权中心拉免费额度覆盖（`free_quota`）。失败/未配置返回 {}。

    `VDL_PLANS_CLOUD=0` 与价格那条通道一致关闭；`VDL_CLOUD_LINK=0`（run_offline_tests.sh
    与单个测试文件约定的「全局不连云端」开关）同样关闭，离线测试据此保持隔离、不依赖
    真实授权中心里的覆盖值。
    """
    if str(os.environ.get("VDL_PLANS_CLOUD") or "1").strip().lower() in ("0", "false", "off"):
        return {}
    if str(os.environ.get("VDL_CLOUD_LINK") or "1").strip().lower() in ("0", "false", "off"):
        return {}
    url = _license_api("/api/license/plans")
    if not url:
        return {}
    now = time.time()
    if not force:
        if (now - float(_FREEQ_CACHE.get("ts") or 0.0)) < _FREEQ_TTL:
            return dict(_FREEQ_CACHE.get("data") or {})
        if (now - float(_FREEQ_CACHE.get("fail_ts") or 0.0)) < _FREEQ_FAIL_BACKOFF:
            return dict(_FREEQ_CACHE.get("data") or {})
    try:
        import urllib.request as _rq
        req = _rq.Request(url, data=b"{}", method="POST",
                          headers={"Content-Type": "application/json",
                                   "User-Agent": "VDL-Web/1.0"})
        with _rq.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        fq = data.get("free_quota") if isinstance(data, dict) else None
        _FREEQ_CACHE["data"] = fq if isinstance(fq, dict) else {}
        _FREEQ_CACHE["ts"] = now
        _FREEQ_CACHE["fail_ts"] = 0.0
        return dict(_FREEQ_CACHE["data"])
    except Exception:  # noqa: BLE001 — 云端不可达一律回默认常量
        _FREEQ_CACHE["fail_ts"] = now
        return {}


def effective_daily_limits(force: bool = False) -> tuple[dict[str, int], dict[str, int]]:
    """返回 `(会员每日配额, 免费每日配额)`，已叠加授权中心的免费额度覆盖。

    与桌面端 `membership.effective_daily_limits` **同口径**（同一份云端字段、同一套
    白名单收敛规则）：未覆盖的键落回代码常量；未知 resource 忽略；脏值忽略。
    `mode == "off"` ⇒ 免费档全部归 0（会员档不受影响）。
    """
    member = dict(DAILY_QUOTA_LIMITS)
    free = dict(FREE_DAILY_LIMITS)
    fq = cloud_free_quota_overrides(force=force) or {}
    if not isinstance(fq, dict):
        fq = {}
    valid = set(DAILY_QUOTA_LIMITS) | set(FREE_DAILY_LIMITS)
    for src, dst in (("daily_member_limits", member), ("daily_free_limits", free)):
        ov = fq.get(src)
        if not isinstance(ov, dict):
            continue
        for k, v in ov.items():
            key = str(k or "").strip()
            if not key or key not in valid:
                continue
            try:
                dst[key] = int(v)
            except (TypeError, ValueError):
                continue
    if str((fq or {}).get("mode") or "").strip().lower() == "off":
        free = {k: 0 for k in free}
    return member, free


def cloud_member_notice() -> dict[str, Any]:
    """会员中心公告（云端下发，管理员可后台编辑）。返回 {enabled, text}。

    🔴 2026-10-18：与套餐共用 `/api/license/plans` 通道（同一次拉取 + 同一 TTL
    缓存），不额外增加往返。云端不可达 / 未配置 / `VDL_PLANS_CLOUD=0` ⇒ 返回
    空公告（各端隐藏公告栏）——绝不凭空造一条文案出来。
    """
    if str(os.environ.get("VDL_PLANS_CLOUD") or "1").strip().lower() in ("0", "false", "off"):
        return {"enabled": False, "text": ""}
    if not _license_api("/api/license/plans"):
        return {"enabled": False, "text": ""}
    cloud_plan_overrides()          # 触发/复用同一次拉取与缓存
    n = _CLOUD_PLANS_CACHE.get("notice")
    if not isinstance(n, dict):
        return {"enabled": False, "text": ""}
    text = str(n.get("text") or "").strip()
    return {"enabled": bool(n.get("enabled")) and bool(text), "text": text}


def push_member_notice_to_cloud(enabled: bool, text: str) -> dict[str, Any]:
    """把会员中心公告下发授权中心（后台保存后调用）。返回同步状态，绝不抛出。

    🔴 2026-10-18：公告必须走云端——只写本机等于「只有自己那台能看到」。
    保存成功后清本地缓存，让下一次 plans() 立刻读回新值（否则最长等一个 TTL）。
    """
    url = _license_api("/api/license/member_notice_set")
    if not url:
        return {"ok": False, "reason": "no_license_base"}
    try:
        import admin_store
        token = admin_store._license_admin_token()  # noqa: SLF001 — 复用既有令牌读取
    except Exception:  # noqa: BLE001
        token = ""
    if not token:
        return {"ok": False, "reason": "no_admin_token"}
    try:
        import urllib.request as _rq
        body = json.dumps({"token": token, "enabled": bool(enabled),
                           "text": str(text or "")[:500]}, ensure_ascii=False).encode("utf-8")
        req = _rq.Request(url, data=body, method="POST",
                          headers={"Content-Type": "application/json",
                                   "User-Agent": "VDL-Web/1.0"})
        with _rq.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        if isinstance(data, dict) and data.get("ok"):
            nt = data.get("notice") if isinstance(data.get("notice"), dict) else {}
            _CLOUD_PLANS_CACHE["notice"] = nt
            _CLOUD_PLANS_CACHE["ts"] = time.time()
            return {"ok": True, "notice": nt}
        return {"ok": False, "reason": str((data or {}).get("error") or "cloud_rejected")}
    except Exception as e:  # noqa: BLE001 — 云端不可达不能拖垮后台保存
        msg = str(e)
        try:                            # HTTPError 的业务错误码在 body 里
            import urllib.error as _ue
            if isinstance(e, _ue.HTTPError):
                parsed = json.loads(e.read().decode("utf-8", "replace") or "{}")
                msg = str(parsed.get("error") or parsed.get("detail") or msg)[:200]
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason": f"error: {msg}"}


def effective_plans() -> dict[str, dict[str, Any]]:
    """生效套餐表：代码常量 ← 本机 plans.json 覆盖层 ← 授权中心覆盖（云端最高）。"""
    ov = load_plan_overrides()
    cloud = cloud_plan_overrides() or {}
    return {
        "download_plans": _overlay_plans(
            _overlay_plans(DOWNLOAD_PLANS, ov.get("download_plans")), cloud.get("download_plans")),
        "ai_plans": _overlay_plans(
            _overlay_plans(AI_PLANS, ov.get("ai_plans")), cloud.get("ai_plans")),
        "credit_packs": _overlay_plans(
            _overlay_plans(CREDIT_PACKS, ov.get("credit_packs")), cloud.get("credit_packs")),
    }


# ── 档位售卖状态（2026-10-03）：模式 / 秒杀 / 限量 / 活动时间 ──────────────────
# 后台「套餐与积分成本」可给每一档手填这些字段（存在 plans.json 覆盖层里）：
#   on_sale     上架开关（false = 隐藏该档，前端不展示、不可下单）
#   mode        normal 普通 / flash_sale 秒杀 / limited 限量 / event 活动
#   badge       角标文案（前端卡片上显示，如「限时 5 折」）
#   desc        补充说明（前端卡片副文案）
#   flash_price 秒杀价（mode=flash_sale 且在秒杀窗口内时取代 price_cny）
#   flash_start / flash_end  秒杀窗口（Unix 秒）
#   start_at / end_at        活动/售卖窗口（Unix 秒，0 = 不限）
#   stock       限量总份数（0 = 不限）；sold 已售份数
PLAN_MODES = ("normal", "flash_sale", "limited", "event")


def _ts(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def plan_sales_state(plan: dict[str, Any], now: Optional[float] = None) -> dict[str, Any]:
    """算出一档当前的售卖状态与现价（前端置灰、下单校验共用这一份口径）。

    返回：buyable 是否可买、reason 不可买原因（中文）、price 现价、
    original_price 原价、is_flash 是否秒杀中、flash_phase 秒杀窗口三态
    （upcoming 未开始 / active 进行中 / ended 已结束 / none 非秒杀模式）、
    remaining 剩余份数（None=不限）、start_at/end_at 售卖窗口、mode 模式、
    badge/desc 展示字段。
    """
    now = float(now if now is not None else time.time())
    p = dict(plan or {})
    mode = str(p.get("mode") or "normal")
    if mode not in PLAN_MODES:
        mode = "normal"
    on_sale = p.get("on_sale", True)
    on_sale = True if on_sale is None else bool(on_sale)
    base_price = float(p.get("price_cny") or 0)
    start_at = _ts(p.get("start_at"))
    end_at = _ts(p.get("end_at"))
    fs = _ts(p.get("flash_start"))
    fe = _ts(p.get("flash_end"))
    flash_price = float(p.get("flash_price") or 0)
    in_flash = bool(flash_price > 0 and fs and fe and fs <= now <= fe)
    # 秒杀窗口三态：前端据此决定「秒杀价/划线原价/秒杀角标/秒杀至…
    # 要不要出现」。窗口已过必须一切收起 —— 否则价格早已回原价，卡片却还挂着
    # 「限时秒杀」角标和「秒杀至 …」，用户会误以为自己正在享受秒杀价。
    flash_phase = "none"
    if mode == "flash_sale" and flash_price > 0 and fs and fe:
        if now < fs:
            flash_phase = "upcoming"
        elif now <= fe:
            flash_phase = "active"
        else:
            flash_phase = "ended"
    stock = int(p.get("stock") or 0)
    sold = int(p.get("sold") or 0)
    remaining = (stock - sold) if stock > 0 else None

    reason = ""
    if not on_sale:
        reason = "已下架"
    elif start_at and now < start_at:
        reason = "活动未开始"
    elif end_at and now > end_at:
        reason = "活动已结束"
    elif stock > 0 and sold >= stock:
        reason = "已售罄"

    return {
        "mode": mode,
        "on_sale": on_sale,
        "buyable": (reason == ""),
        "reason": reason,
        "price": (flash_price if in_flash else base_price),
        "original_price": base_price,
        "is_flash": in_flash,
        "flash_phase": flash_phase,
        "flash_price": flash_price,
        "flash_start": fs,
        "flash_end": fe,
        "start_at": start_at,
        "end_at": end_at,
        "stock": stock,
        "sold": sold,
        "remaining": remaining,
        "badge": str(p.get("badge") or ""),
        "desc": str(p.get("desc") or ""),
    }


def ensure_plan_buyable(code: str, now: Optional[float] = None) -> dict[str, Any]:
    """下单/激活前的可买校验。不通过时返回 {"ok": False, "error": 中文原因}。"""
    eff = effective_plans()
    for cat in ("download_plans", "ai_plans", "credit_packs"):
        plan = (eff.get(cat) or {}).get(code)
        if not plan:
            continue
        st = plan_sales_state(plan, now=now)
        if not st["buyable"]:
            return {"ok": False, "error": st["reason"] or "该套餐当前不可购买",
                    "state": st, "category": cat}
        return {"ok": True, "state": st, "category": cat}
    return {"ok": False, "error": "套餐不存在或已下架"}


def effective_pay_plans() -> dict[str, dict[str, Any]]:
    """下单用套餐表：{code: {price, name, grant}}，与 payment_core.PAY_PLANS 同构。

    🔴 价格单一真源（2026-09-30）：支付内核若继续读 payment_core 的硬编码
    PAY_PLANS，超管在后台改的价格**不会影响真实扣款金额**。故改由本函数按
    「覆盖层 → 代码常量」实时产出，注入 PaymentService。
    """
    eff = effective_plans()
    out: dict[str, dict[str, Any]] = {}
    for code, p in eff["download_plans"].items():
        out[code] = {"price": float(p.get("price_cny") or 0),
                     "name": p.get("label") or code, "grant": code}
    for code, p in eff["ai_plans"].items():
        out[code] = {"price": float(p.get("price_cny") or 0),
                     "name": p.get("label") or code, "grant": code}
    for code, p in eff["credit_packs"].items():
        out[code] = {"price": float(p.get("price_cny") or 0),
                     "name": p.get("label") or code, "grant": code}
    # 现价走售卖状态（2026-10-03）：秒杀窗口内用秒杀价 + 带可买标记，
    # 避免「页面显示秒杀、下单按原价」或售罄档仍能下单。
    now = time.time()
    for code, item in out.items():
        for cat in ("download_plans", "ai_plans", "credit_packs"):
            src = (eff.get(cat) or {}).get(code)
            if src:
                st = plan_sales_state(src, now=now)
                item["price"] = float(st["price"] or 0)
                item["buyable"] = bool(st["buyable"])
                item["reason"] = st["reason"]
                break
    return out


def credit_cost_table() -> list[dict[str, Any]]:
    """后台展示用：把成本表与运行时生效价合成一张（**不返回任何凭据**）。

    `effective` 是实际会扣的积分（覆盖层优先），`default` 是代码默认，
    两者不等说明管理员改过价，前端据此高亮。
    """
    costs = (load_plan_overrides().get("credit_costs") or {})
    out: list[dict[str, Any]] = []
    for op, row in AI_CREDIT_COSTS.items():
        default = int(row.get("cost", 0))
        try:
            eff = int(costs[op])
        except (KeyError, TypeError, ValueError):
            eff = default
        out.append({
            "op": op,
            "name": str(row.get("name") or op),
            "where": str(row.get("where") or ""),
            "note": str(row.get("note") or ""),
            # 平台真实成本：后台改价前先看它，别只凭感觉调。
            "real_cost": str(row.get("real_cost") or ""),
            "default": default,
            "effective": eff,
            "overridden": eff != default,
        })
    # 表外但覆盖层里有的（历史遗留 / 已下线功能）也列出来，便于清理
    known = set(AI_CREDIT_COSTS)
    for op, v in costs.items():
        if op in known:
            continue
        try:
            eff = int(v)
        except (TypeError, ValueError):
            eff = 0
        out.append({
            "op": op, "name": op, "where": "",
            "note": "⚠️ 覆盖层里有此项，但代码表里没有对应定义（可能已下线，建议清理）",
            "real_cost": "", "default": 0, "effective": eff,
            "overridden": True, "orphan": True,
        })
    return out


def credit_cost(op: str, sub: str | None = None) -> int:
    """查询某次 AI 操作的积分成本。优先级：plans.json 覆盖层 → 代码表里的默认单价。

    🔴 2026-10-05：表外 op **必须打 warning**。旧实现找不到就 `return 0`（免费），
    于是「新功能忘了登记价」会静默变成全站免单 —— 这是财务漏洞的温床，
    排障时只能靠账单异常才发现。改为查表：未登记就留痕。
    """
    costs = load_plan_overrides().get("credit_costs") or {}
    if op in costs:
        try:
            return int(costs[op])
        except (TypeError, ValueError):
            pass
    row = AI_CREDIT_COSTS.get(op)
    if row is None:
        # 与桌面端同口径：表外 op 一律告警（0 本身也是合法单价，只能靠日志区分）
        logging.getLogger("membership").warning(
            "credit_cost: 未知 op=%r —— 未登记单价，按 0 处理（请补 AI_CREDIT_COSTS）", op)
        return 0
    return int(row.get("cost", 0))


# --------------------------------------------------------------------------- #
# 免费用户「首次体验」策略（2026-10-05 用户定档，与桌面端同口径）
# --------------------------------------------------------------------------- #
# 背景：网页版接上 AI 积分墙后，积分池为 0 的新账号**第一次点 AI 功能就撞 402**，
# 连"这东西到底好不好用"都无从判断 —— 等于在漏斗最前面把功能掐死。
# 用户定档：**只要是首次使用的账号，其首次使用免费一次**。
#
# 记录落在会员状态文件自身的 `free_trials` 键（随账号走，不是全局），键名语义：
#   mode=per_op → 每个 op 一个名额，键是 op 名；
#   mode=once   → 全站共用一个名额，键是 "*"。
#
# 🔴 **边界**：这只是账号级控制，换个注册账号仍能重薅。真要收紧得靠用户改不了的
#    维度（强设备指纹 / 授权中心去重），而不是靠本地文件。
#    🔴 另注：网页版数据目录是 ~/.video-downloader，桌面端是 ~/.videodownloader，
#    两边会员状态**不共享** ⇒ 同一个账号在两端各有一次首次免费。要统一得先把
#    积分账户挪到授权中心，那是另一个改动，本次不做。
DEFAULT_FREE_TRIAL_POLICY: dict[str, Any] = {
    "enabled": True,
    # once = 账号首次使用免费一次（默认，用户定档）｜per_op = 每个功能各免一次｜off = 关闭
    "mode": "once",
    # 不参与试用的 op（把最贵的解说排除就用这个）
    "exclude": [],
    # 会员（下载或 AI 任一活跃）是否也享受 —— 默认不给：会员已按套餐拿到积分，
    # 用完请复购，不拿"试用"给会员兜底。
    "members_too": False,
    # 0 = 不限；>0 时单价超过该积分的功能不参与试用
    "max_cost": 0,
}
_TRIAL_MODES = ("per_op", "once", "off")
_TRIAL_ONCE_KEY = "*"


def free_trial_policy() -> dict[str, Any]:
    """运行时生效的试用策略：plans.json 的 `free_trial` 覆盖层 → 代码默认。

    逐个字段合并，管理员只改一项不会把其余字段抹掉。读到的脏值一律收敛到合法
    范围 —— 后台写坏配置不该让整个扣费链路炸掉（fail-open 的方向是「退回默认
    口径」而不是「全站免单」）。
    """
    ov = load_plan_overrides().get("free_trial")
    out = dict(DEFAULT_FREE_TRIAL_POLICY)
    if isinstance(ov, dict) and ov:
        for k, v in ov.items():
            if v is None:
                continue
            out[k] = v
    mode = str(out.get("mode") or "once").strip().lower()
    out["mode"] = mode if mode in _TRIAL_MODES else "once"
    if out["mode"] == "off":
        out["enabled"] = False
    out["enabled"] = bool(out.get("enabled"))
    ex = out.get("exclude")
    out["exclude"] = [str(x) for x in ex] if isinstance(ex, (list, tuple, set)) else []
    try:
        out["max_cost"] = max(0, int(out.get("max_cost") or 0))
    except (TypeError, ValueError):
        out["max_cost"] = 0
    out["members_too"] = bool(out.get("members_too"))
    return out


def _trial_key(op: str, mode: str) -> str:
    return _TRIAL_ONCE_KEY if mode == "once" else str(op)


def spend_for(store: "MembershipStore", op: str, sub: str | None = None,
              reason: str | None = None) -> dict[str, Any]:
    """按成本扣 AI 积分。返回 spend_credits 的结果 dict（ok/error）。

    成本 <=0 视为免费操作，直接返回 ok=True（不污染积分池）。
    积分不足且命中「首次体验」名额时按放行处理（`trial=True`），不扣积分。
    """
    cost = credit_cost(op, sub)
    if cost <= 0:
        return {"ok": True, "spent": 0, "free": True, "credits_left": store.status()["credits_total"]}
    res = store.spend_credits(cost, reason=reason or op)
    if res.get("ok"):
        return res
    # 与 gate_message 同口径：让「不走 gate_message 的调用点」也不会漏。
    if store.trial_available(op, cost):
        if store.trial_consume(op, reason=reason or op):
            return {"ok": True, "spent": 0, "free": True, "trial": True,
                    "credits_left": store.status()["credits_total"]}
        # else: 云端已被另一台领走 → 走正常扣分失败返回
    return res


def gate_message(store: "MembershipStore", op: str, sub: str | None = None,
                 reason: str | None = None) -> str | None:
    """扣积分并产出拦截原因：None=放行；非 None=「积分不足」原因字符串（供 402 detail）。

    成本 <=0 视为免费放行；spend 系统异常时降级放行（记日志），不阻断主流程。

    🔴 2026-10-05「免费用户首次体验」：判定顺序是
        **先扣积分 → 扣不动才动用试用名额 → 都不行才拦**。
       1. 先扣：账户里还有积分就不占用试用名额 —— 名额是一次性资源，有余额时烧掉它
          等于白送，用户本可以用这次名额去试更贵的功能。
       2. 后试用：余额不够时若还有账号名额，标记用掉并返回 None（放行、不扣分）。
       3. 都不行：文案里如实说明「是否已用过试用」，别只丢一句「积分不足」——
          用户上次明明跑通了，会以为是系统出错。
    """
    cost = credit_cost(op, sub)
    if cost <= 0:
        return None
    try:
        res = store.spend_credits(cost, reason=reason or op)
    except Exception as e:  # noqa: BLE001
        logging.getLogger("membership").warning("credit gate error op=%s: %s", op, e)
        return None
    if res.get("ok"):
        return None

    left = res.get("credits_left", 0)
    pol = free_trial_policy()
    hint = ""
    if pol.get("enabled") and store.trial_available(op, cost):
        try:
            if store.trial_consume(op, reason=reason or op):
                logging.getLogger("membership").info(
                    "free trial consumed op=%s cost=%s user=%s", op, cost,
                    getattr(store.path, "name", ""))
                return None
            # 被另一台领走：trial_consume 已把本机标记为已用 → 走下面的 402 提示
        except Exception as e:  # noqa: BLE001
            logging.getLogger("membership").warning("free trial consume failed op=%s: %s", op, e)
            hint = "请开通 AI 会员或购买积分包"
    # 兜底提示：目录已没名额（本机用过 / 云端被另一台领走）
    if not hint:
        # once 口径下名额是账号级的：**不是**"这个功能试过了"，而是"这个账号试过了"。
        # 措辞必须对得上，否则用户第二次点的是另一个功能，却被告知"本功能"已用过，
        # 会当成系统串号报错。
        if str(pol.get("mode")) == "once":
            hint = ("你的账号已用过一次免费体验（每个账号限一次），请开通 AI 会员或购买积分包"
                    if store.trial_used(op, pol) else "请开通 AI 会员或购买积分包")
        else:
            hint = ("本功能的免费体验已用过一次，请开通 AI 会员或购买积分包"
                    if store.trial_used(op, pol) else "请开通 AI 会员或购买积分包")
    return f"AI 积分不足：本次操作需 {cost} 积分，当前 {left}（{hint}）"

# --------------------------------------------------------------------------- #
# 状态文件
# --------------------------------------------------------------------------- #


def default_state_path() -> Path:
    """状态文件默认路径 ~/.video-downloader/membership.json（frozen 兼容）。

    同样受 VDL_DATA_DIR 覆盖（见 _base_dir），保证测试不写真实家目录。
    """
    return _base_dir() / "membership.json"


# 已下线资源的历史计数键（2026-10-04 随占位功能一并移除）
# 加载旧状态文件与跨日重置时都要清掉，否则死键会继续出现在 status() / usage_summary()。
_DEAD_USAGE_KEYS: tuple[str, ...] = (
    "original", "batch_material", "ai_subtitle", "subtitle",
    "subtitle_batch", "image_translate",
)


def _empty_state() -> dict[str, Any]:
    return {
        "download_member": {"active": False, "plan": None, "expire_at": 0.0},
        "ai_member": {"active": False, "plan": None, "expire_at": 0.0,
                      "grant_credits": 0, "credits_left": 0, "feature_credits": {}},
        "permanent_credits": {"total": 0, "packs": []},
        # daily_usage 按功能独立计（2026-10-06 拆池；cloud 为旧键向后兼容保留）
        "daily_usage": {"date": "", "download": 0, "cloud": 0,
                        "cloud_commentary": 0, "cloud_convert": 0,
                        "cloud_dewatermark": 0, "cloud_subtitle": 0},
        "usage_history": {},
        "meta": {"activated_at": 0.0, "history": []},
        # 免费用户「首次体验」已用记录：{op 或 "*": 使用时刻}（见 DEFAULT_FREE_TRIAL_POLICY）
        "free_trials": {},
    }


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return _empty_state()
    st = _empty_state()
    # 逐键合并，容忍旧/缺字段
    for k in ("download_member", "ai_member", "permanent_credits", "daily_usage",
              "usage_history", "meta", "free_trials"):
        if isinstance(data.get(k), dict):
            st[k].update(data[k])
    # 2026-10-04：丢弃已下线资源的历史计数键（下方 _DEAD_USAGE_KEYS）。
    # 放在这里而不是只在跨日重置里清，是为了**读到旧文件那一刻就干净**——
    # 否则用户当天不做任何操作时，死键仍会出现在 status() / usage_summary() 里。
    for dead in _DEAD_USAGE_KEYS:
        st["daily_usage"].pop(dead, None)
    return st


def _save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        # 状态文件写失败不应让业务崩溃（降级为内存态）
        pass


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #

# 会员权益文案（2026-10-03 改为**从配额表自动生成**）
# 此前是手写死的 6 条，加了 matting/cloud/app_compute/subtitle 等配额后忘记补文案，
# 出现「代码里有、页面上没有」。现在按 DAILY_QUOTA_LIMITS + FEATURE_USAGE_DEFS 生成，
# 守卫 test_membership_benefits 钉住「有配额必有文案」，以后不会再漏。
# 🔴 2026-10-04 删掉 original / batch_material / matting / app_compute 四条：
#   前两条对应功能不存在（见 DAILY_QUOTA_LIMITS 上方注释），后两条是**桌面端专属**
#   配额键，网页端这两张表里根本没有 —— 留着会让 v>0 判断失效后文案凭空消失或
#   与实际配额不符。网页版只按自己真有的 download / cloud 两条生成。
# 🔴 2026-10-04 用户定档：**条目全部保留，只把「云端 / 算力 / AI / 本地」这几个字
#    从文案里去掉**（它们是架构与成本口径，不是用户视角的权益）。
#    例：原来「云端算力（转码/拼接/…）200 次/日」→ 现在「在线处理（转码/拼接/…）200 次/日」；
#        「不含 AI 积分：云端抠图…」→「不含积分额度：抠图…」。
#    ⚠️ 不是隐藏条目 —— 每条权益、配额、限流都照旧。
_BENEFIT_FROM_LIMITS: tuple[tuple[str, str], ...] = (
    ("download", "下载任务 {v} 次/日"),
    # 🔴 2026-10-06 拆池：旧「在线处理」单条拆为 3 条独立权益（每个功能各自计次）
    ("cloud_convert", "在线转码 {v} 次/日"),
    ("cloud_concat", "在线拼接 {v} 次/日"),
    ("cloud_dewatermark", "图片去水印 {v} 次/日"),
    ("cloud_dewatermark_pdf", "PDF 去水印 {v} 次/日"),
    ("cloud_subtitle", "字幕提取 {v} 次/日"),
    ("cloud_subtitle_burn", "字幕烧录 {v} 次/日"),
    ("cloud_subtitle_translate", "字幕翻译 {v} 次/日"),
    # 🔴 2026-10-19：音乐/图片转换独立计次（与桌面端同档），有配额就必须有卖点文案
    ("convert_audio", "音乐转换 {v} 次/日"),
    ("convert_image", "图片转换 {v} 次/日"),
    # 🔴 2026-10-19：分享进配额表 ⇒ 必须有对应卖点文案（守卫 test_feature_usage_gate ③）
    ("share_qr", "生成二维码 {v} 次/日"),
    ("share_page", "生成网页 {v} 次/日"),
    ("cloud_drive", "网盘下载 {v} 次/日"),
)
# 不走每日配额、但属于会员权益的说明项
_BENEFIT_EXTRA: tuple[dict[str, str], ...] = (
    {"key": "quality", "text": "清晰度：1080P 及以上全部开放（>1080P 需会员）"},
    {"key": "devices", "text": "同一账号 2 台设备同时在线"},
    {"key": "speed", "text": "高速通道 · 全速不限速"},
    {"key": "no_credits", "text": f"开通即赠 {VIP_MEMBER_CREDITS} 积分：抠图等功能可直接用（永久有效）"},
    {"key": "support", "text": "优先客服支持"},
)


def download_benefits() -> list[dict[str, str]]:
    """VIP会员权益清单：配额项自动跟随 DAILY_QUOTA_LIMITS + 不限项 + 静态说明。

    2026-10-04 起不再有隐藏名单：条目全部保留，只把「云端/算力/AI/本地」几个字
    从文案里去掉了（见 _BENEFIT_FROM_LIMITS 上方注释）。
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for key, tpl in _BENEFIT_FROM_LIMITS:
        v = int(DAILY_QUOTA_LIMITS.get(key) or 0)
        if v > 0:
            out.append({"key": key, "text": tpl.format(v=v)})
            seen.add(key)
    # member_limit = -1 的功能 = 会员不限次（字幕提取等）
    for d in FEATURE_USAGE_DEFS:
        k = str(d.get("key"))
        if int(d.get("member_limit", 0)) == -1 and k not in seen:
            out.append({"key": k, "text": f"{d.get('name') or k}：不限"})
            seen.add(k)
    out.extend(dict(x) for x in _BENEFIT_EXTRA)
    return out


@dataclass
class MembershipStore:
    """VDL 会员状态机。线程外调用方需自行加锁（见 app 单例）。"""
    path: Optional[Path] = None
    now_fn: Callable[[], float] = field(default=time.time)
    _state: dict[str, Any] = field(default_factory=_empty_state, init=False)
    _loaded: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.path is None:
            self.path = default_state_path()

    # ---- 基础读写 ----
    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._state = _load_state(self.path)
            self._loaded = True

    def _persist(self) -> None:
        _save_state(self.path, self._state)

    def _now(self) -> float:
        return self.now_fn()

    # ---- 云端账号状态（2026-09-26：web 版账号接授权中心）------------------
    # 本机 store 从此同时承载「登录态 + 权益缓存」：账号与会话的权威在授权中心，
    # 这里只做落地与离线兜底；云端 authority 快照一联网即覆盖本地（防篡改）。
    def save_account(self, email: str, token: str, account: Optional[dict] = None,
                     fp: str = "", name: str = "") -> dict[str, Any]:
        """记录登录态（云端 token / 邮箱 / 设备号），清掉 evicted 标记。"""
        self._ensure_loaded()
        meta = self._state.setdefault("meta", {})
        acc = meta.setdefault("account", {})
        acc.update({
            "email": (email or "").strip().lower(),
            "token": token or "",
            "fp": fp or acc.get("fp", ""),
            "name": name or acc.get("name", ""),
            "evicted": False,
            "logged_in": bool(token),
            "last_sync": self._now(),
        })
        if isinstance(account, dict):
            acc["devices"] = account.get("devices") or []
            acc["max_devices"] = int(account.get("max_devices") or 2)
        if "purchases_applied" not in acc:
            acc["purchases_applied"] = []
        self._persist()
        return {"ok": True}

    def clear_account(self) -> dict[str, Any]:
        """登出：只清登录态，**保留已购权益**（换机重装权益要跟着账号走）。"""
        self._ensure_loaded()
        acc = self._state.setdefault("meta", {}).setdefault("account", {})
        acc.update({"token": "", "email": acc.get("email", ""), "logged_in": False,
                    "evicted": False, "devices": [], "last_sync": 0.0})
        self._persist()
        return {"ok": True}

    def set_evicted(self, evicted: bool) -> None:
        """被其他设备挤出 → status() 立即降级免费档（不删数据）。"""
        self._ensure_loaded()
        acc = self._state.setdefault("meta", {}).setdefault("account", {})
        acc["evicted"] = bool(evicted)
        self._persist()

    def account_view(self) -> dict[str, Any]:
        """给前端的账号快照（**不吐 token**）。"""
        self._ensure_loaded()
        acc = (self._state.get("meta") or {}).get("account") or {}
        return {
            "logged_in": bool(acc.get("token")),
            "email": acc.get("email", ""),
            "device_fp": acc.get("fp", ""),
            "device_name": acc.get("name", ""),
            "devices": acc.get("devices") or [],
            "max_devices": int(acc.get("max_devices") or 2),
            "evicted": bool(acc.get("evicted")),
            "banned": bool(acc.get("banned")),
            "last_sync": float(acc.get("last_sync") or 0),
        }

    def cloud_session(self) -> dict[str, Any]:
        """内部用：云端会话（含 token，**不可下发给前端**）。"""
        self._ensure_loaded()
        acc = (self._state.get("meta") or {}).get("account") or {}
        return {"email": acc.get("email", ""), "token": acc.get("token", ""),
                "fp": acc.get("fp", ""), "name": acc.get("name", "")}

    def apply_cloud_purchases(self, purchases: list[dict[str, Any]],
                              via: str = "license") -> dict[str, Any]:
        """按 purchase id **幂等**把云端购买记录落户（老服务端无 authority 快照时的兜底）。"""
        self._ensure_loaded()
        acc = self._state.setdefault("meta", {}).setdefault("account", {})
        applied = acc.setdefault("purchases_applied", [])
        if not isinstance(applied, list):
            applied = acc["purchases_applied"] = []
        out_applied: list[str] = []
        errors: list[dict[str, Any]] = []
        for p in (purchases or []):
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") or "").strip()
            plan_code = str(p.get("plan_code") or "").strip()
            if not pid or not plan_code or pid in applied:
                continue
            res = self.activate(plan_code, via=via)
            if res.get("ok"):
                applied.append(pid)
                out_applied.append(plan_code)
            else:
                errors.append({"id": pid, "plan_code": plan_code,
                               "error": res.get("error") or "未知错误"})
        acc["purchases_applied"] = applied[-500:]
        self._persist()
        return {"ok": not errors, "applied": out_applied, "errors": errors}

    def apply_cloud_authoritative(self, acct: dict[str, Any]) -> dict[str, Any]:
        """云端权威对账：用授权中心的权益快照**覆盖**本地（只认快照 v>=1）。

        与 apply_cloud_purchases（幂等追加、只加不减）的本质区别：本地文件里的
        expire_at / credits_left / permanent_credits 被手改（或走了别的后门）后，
        一联网同步就被云端真值覆盖回滚。封禁标记落 meta.account.banned。
        """
        self._ensure_loaded()
        auth = (acct or {}).get("authority") or {}
        if not isinstance(auth, dict) or int(auth.get("v") or 0) < 1:
            return {"ok": False, "reason": "no_authority"}
        now = self._now()
        st = self._state
        dl = st["download_member"]
        ai = st["ai_member"]

        dl_until = float(auth.get("member_until_dl") or 0)
        dl["expire_at"] = dl_until
        dl["active"] = dl_until > now
        if not dl["active"]:
            dl["plan"] = None

        ai_until = float(auth.get("member_until_ai") or 0)
        ai_active = ai_until > now
        ai["expire_at"] = ai_until
        ai["active"] = ai_active
        if ai_active:
            ai["credits_left"] = max(0, int(auth.get("ai_credits_left") or 0))
            if not ai.get("plan"):
                ai["plan"] = "cloud_ai"
        else:
            ai["plan"] = None
            ai["credits_left"] = 0        # AI 订阅积分随订阅失效清零

        st["permanent_credits"]["total"] = max(0, int(auth.get("perm_credits") or 0))

        # 账号级每日用量同步（同一账号 App/网页共用每日配额）：
        # 取 max(本地, 云端) —— 云端只大不小，既不冲掉本地在途未报的增量，
        # 也让手改本地数字蹭额度的篡改一同步即回滚。仅快照日期=本地今天才合并。
        udate = str(auth.get("usage_date") or "")
        ucounts = auth.get("usage")
        if udate == time.strftime("%Y-%m-%d", time.localtime(now)) and isinstance(ucounts, dict):
            self._roll_daily(now)
            du = st["daily_usage"]
            keys = {str(k) for k in list(du.keys())} | {str(k) for k in ucounts}
            keys.discard("date")
            for k in keys:
                try:
                    cv = int(ucounts.get(k) or 0)
                except (TypeError, ValueError):
                    cv = 0
                try:
                    lv = int(du.get(k, 0) or 0)
                except (TypeError, ValueError):
                    lv = 0
                du[k] = max(lv, cv)

        acc = st.setdefault("meta", {}).setdefault("account", {})
        acc["banned"] = bool(auth.get("banned"))

        # 购买记录落户（2026-10-09）：权威快照（authority）只带会员**字段**、不带流水，
        # 而「购买记录」是读本机 meta.history 的 ⇒ 网页/云端付款后本机 history 永远为空，
        # 用户看到的是「买过但记录里没有」。这里把云端 purchases（id/plan_code/at）
        # 按 id **幂等**并入 history。
        # 🔴 只写展示流水，**绝不**再调 activate() —— 权益效果已由上面的快照字段体现，
        #    再 activate 一次会把会员天数重复叠加（多送时长）。
        # 与 apply_cloud_purchases 共用 meta.account.purchases_applied 去重表，两条路径
        # 互不重复落户。
        cloud_purchases = acct.get("purchases")
        if isinstance(cloud_purchases, list) and cloud_purchases:
            applied = acc.setdefault("purchases_applied", [])
            if not isinstance(applied, list):
                applied = acc["purchases_applied"] = []
            hist = st["meta"].setdefault("history", [])
            if not isinstance(hist, list):
                hist = st["meta"]["history"] = []
            added = 0
            for p in cloud_purchases:
                if not isinstance(p, dict):
                    continue
                pid = str(p.get("id") or "").strip()
                code = str(p.get("plan_code") or "").strip()
                if not pid or not code or pid in applied:
                    continue
                try:
                    at = float(p.get("at") or 0) or now
                except (TypeError, ValueError):
                    at = now
                hist.append({"code": code, "via": "cloud", "at": at,
                             "type": "activate", "purchase_id": pid})
                applied.append(pid)
                added += 1
            if added:
                st["meta"]["history"] = hist[-200:]  # 与 activate 同为只留最近 200 条
                acc["purchases_applied"] = applied[-500:]

        st["meta"]["last_authority_sync"] = now
        self._persist()
        return {"ok": True, "banned": bool(auth.get("banned")),
                "download_until": dl_until, "ai_until": ai_until,
                "perm_credits": int(st["permanent_credits"]["total"]),
                "ai_credits_left": int(ai.get("credits_left", 0))}

    def _account_lock(self) -> Optional[str]:
        """账号级锁（封禁 / 被挤下设备）→ 展示与判定层降级为免费档。"""
        self._ensure_loaded()
        acc = (self._state.get("meta") or {}).get("account") or {}
        if acc.get("banned"):
            return "ACCOUNT_BANNED"
        if acc.get("evicted"):
            return "DEVICE_EVICTED"
        return None

    # ---- 公开查询 ----
    def status(self) -> dict[str, Any]:
        """当前会员状态（惰性判定过期、惰性日切配额）。返回公开 dict。"""
        self._ensure_loaded()
        now = self._now()
        st = self._state
        dl = st["download_member"]
        ai = st["ai_member"]

        # 惰性过期
        if dl.get("active") and now >= float(dl.get("expire_at", 0)):
            dl["active"] = False
            dl["plan"] = None
            dl["expire_at"] = 0.0
        if ai.get("active") and now >= float(ai.get("expire_at", 0)):
            ai["active"] = False
            ai["plan"] = None
            ai["expire_at"] = 0.0
            ai["credits_left"] = 0  # AI 订阅积分到期清零

        # AI 会员捆绑下载权益：激活期内 download 视同可用到同一到期日
        dl_active = bool(dl.get("active")) or bool(ai.get("active"))
        dl_until = max(float(dl.get("expire_at", 0)), float(ai.get("expire_at", 0))) if (dl.get("active") or ai.get("active")) else 0.0

        # 惰性日切
        self._roll_daily(now)

        out = {
            "download_member": {
                "active": dl_active,
                "plan": dl.get("plan") if dl.get("active") else (ai.get("plan") if ai.get("active") else None),
                "expire_at": dl_until,
                "source": "download" if dl.get("active") else ("ai_bundle" if ai.get("active") else None),
            },
            "ai_member": {
                "active": bool(ai.get("active")),
                "plan": ai.get("plan"),
                "expire_at": float(ai.get("expire_at", 0)),
                "credits_left": int(ai.get("credits_left", 0)),
                "grant_credits": int(ai.get("grant_credits", 0)),
            },
            "permanent_credits": int(st["permanent_credits"].get("total", 0)),
            "credits_total": int(ai.get("credits_left", 0)) + int(st["permanent_credits"].get("total", 0)),
            "daily_usage": dict(st["daily_usage"]),
        }

        # 账号级锁：封禁 / 被其他设备挤下 → 整体降级免费档（数据保留）
        lock = self._account_lock()
        if lock:
            out["download_member"]["active"] = False
            out["download_member"]["plan"] = None
            out["download_member"]["source"] = None
            out["ai_member"]["active"] = False
            out["ai_member"]["credits_left"] = 0
            out["account_locked"] = lock
        out["account"] = self.account_view()
        # 免费用户「首次体验」余量（2026-10-05）：让用户**在撞墙之前**知道自己还有没有免费机会
        out["free_trials"] = self.free_trial_view()
        return out

    def plans(self) -> dict[str, Any]:
        """套餐表（价格/时长/权益），供前端购买中心展示。

        优先级：plans.json 覆盖层（逐字段叠加）→ 代码常量默认值。
        合并语义（2026-09-30 对齐 app-dev 2026-09-24 修复）：覆盖层缺整个条目
        → 用默认条目（这样新增档位不会被旧覆盖层「盖掉」）。
        """
        eff = effective_plans()
        # 每档附售卖状态（2026-10-03）：模式/秒杀价/限量/活动时间 → 前端置灰与倒计时
        _now = time.time()
        dl = {c: dict(p, state=plan_sales_state(p, now=_now)) for c, p in eff["download_plans"].items()}
        ai = {c: dict(p, state=plan_sales_state(p, now=_now)) for c, p in eff["ai_plans"].items()}
        cp = {c: dict(p, state=plan_sales_state(p, now=_now)) for c, p in eff["credit_packs"].items()}
        return {
            "download_member": {
                "plans": dl,
                "benefits": download_benefits(),
            },
            "ai_member": {
                "plans": ai,
                "bundle_note": "包含VIP会员全部权益",
                "features": AI_FEATURES,
            },
            "credit_packs": cp,
            "currency": "CNY",
            # 2026-10-18：会员中心公告（套餐卡上方一行运营文案；空则各端隐藏）
            "notice": cloud_member_notice(),
        }

    # ---- 激活 ----
    def activate(self, code: str, via: str = "test",
                 order_id: str = "") -> dict[str, Any]:
        """按套餐/积分包 code 激活。续费顺延，AI 会员自动捆绑下载权益。

        安全审计 2026-10-13（F-01 / 同步 app 侧 P0-3 的幂等）：`order_id` 非空时
        按订单号去重 —— 同一笔支付的重复回调 / 轮询 / 重试**只发放一次**。
        此前仅靠 PaymentService 的 `order["granted"]` 标记防重，而该标记与
        「权益是否真的发过」是两份状态，回调重放或跨实例并发时仍可能重复发放
        （会员天数 / 积分翻倍）。
        """
        self._ensure_loaded()
        now = self._now()
        st = self._state
        # 订单级幂等：已发过就直接返回既有结果，绝不再叠加权益。
        if order_id:
            done = (st.get("meta") or {}).get("granted_orders") or []
            if order_id in done:
                return {"ok": True, "code": code, "via": via, "idempotent": True,
                        "order_id": order_id}
        result: dict[str, Any] = {"ok": True, "code": code, "via": via}

        # 🔴 一律走生效套餐表（覆盖层 → 代码常量）：此前直接用模块常量，
        #    超管在后台把某档天数从 7 改成 5，发放时仍按 7 天算。
        _eff = effective_plans()
        _dl_plans, _ai_plans, _credit_packs = (
            _eff["download_plans"], _eff["ai_plans"], _eff["credit_packs"])

        # 售卖状态校验（2026-10-03）：下架/未开始/已结束/售罄 → 拒发
        _all_plans: dict[str, dict[str, Any]] = {}
        _all_plans.update(_dl_plans)
        _all_plans.update(_ai_plans)
        _all_plans.update(_credit_packs)
        if code in _all_plans:
            _st = plan_sales_state(_all_plans[code], now=now)
            if not _st["buyable"]:
                return {"ok": False, "error": _st["reason"] or "该套餐当前不可购买",
                        "state": _st}

        if code in _dl_plans:
            info = _dl_plans[code]
            cur = float(st["download_member"].get("expire_at", 0) or 0)
            new_exp = max(now, cur) + int(info.get("days") or 0) * 86400
            st["download_member"].update({"active": True, "plan": code, "expire_at": new_exp})
            # 2026-10-24：VIP 会员赠 300 积分（进永久池，不随会员到期清零）
            bonus = int(info.get("credits") or VIP_MEMBER_CREDITS or 0)
            if bonus > 0:
                st["permanent_credits"]["total"] = int(st["permanent_credits"].get("total", 0)) + bonus
                st["permanent_credits"].setdefault("packs", []).append({
                    "pack": f"vip_bonus:{code}", "amount": bonus, "bought_at": now,
                })
            result.update({"kind": "download_member", "expire_at": new_exp,
                           "credits_added": bonus})
        elif code in _ai_plans:
            info = _ai_plans[code]
            cur = float(st["ai_member"].get("expire_at", 0) or 0)
            new_exp = max(now, cur) + info["days"] * 86400
            # AI 会员按功能赠送额度：续费时不足上限则补齐，不浪费已用剩余
            fc = dict(st["ai_member"].get("feature_credits") or {})
            for d in FEATURE_USAGE_DEFS:
                if d.get("ai_bonus", 0) > 0:
                    fc[d["key"]] = max(int(fc.get(d["key"], 0)), int(d["ai_bonus"]))
            st["ai_member"].update({
                "active": True, "plan": code, "expire_at": new_exp,
                "grant_credits": int(info["credits"]), "credits_left": int(info["credits"]),
                "feature_credits": fc,
            })
            # 捆绑：下载权益覆盖到 AI 到期（不置 active 标志，status 负责推导 source）
            dl_exp = float(st["download_member"].get("expire_at", 0) or 0)
            if dl_exp < new_exp:
                st["download_member"]["expire_at"] = new_exp
            result.update({"kind": "ai_member", "expire_at": new_exp,
                           "credits_granted": int(info["credits"])})
        elif code in _credit_packs:
            info = _credit_packs[code]
            amt = int(info.get("credits") or 0)
            st["permanent_credits"]["total"] = int(st["permanent_credits"].get("total", 0)) + amt
            st["permanent_credits"].setdefault("packs", []).append({
                "pack": code, "amount": amt, "bought_at": now,
            })
            result.update({"kind": "credit_pack", "credits_added": amt})
        else:
            return {"ok": False, "error": f"未知套餐 code: {code}"}

        if not st["meta"].get("activated_at"):
            st["meta"]["activated_at"] = now
        st["meta"].setdefault("history", []).append({
            "code": code, "via": via, "at": now, "type": "activate",
        })
        st["meta"]["history"] = st["meta"]["history"][-200:]  # 只留最近 200 条
        # F-01 幂等留痕：发过的订单号记入 meta.granted_orders（只留最近 500 单，
        # 防止无限增长），下次同单号回调直接走上面的幂等短路。
        if order_id:
            _go = st["meta"].setdefault("granted_orders", [])
            if order_id not in _go:
                _go.append(order_id)
            st["meta"]["granted_orders"] = _go[-500:]
        self._persist()
        # 限量档售出 +1（写回覆盖层，让后台「已售」实时反映）
        if code in _all_plans and int(_all_plans[code].get("stock") or 0) > 0:
            try:
                cat = next(k for k in ("download_plans", "ai_plans", "credit_packs")
                           if code in _all_plans)
                save_plan_overrides({cat: {code: {
                    "sold": int(_all_plans[code].get("sold") or 0) + 1}}})
            except Exception:  # noqa: BLE001 — 计数失败不影响发放
                pass
        return result

    # ---- 积分 ----
    def spend_credits(self, amount: int, reason: str = "ai_usage") -> dict[str, Any]:
        """消耗积分：先 AI 订阅积分（快过期），后永久积分。不足则拒绝。

        2026-09-26 打通后：扣减成功即异步上报授权中心记账（cloud_link.report_spend_async），
        否则网页版花掉的积分会在下次云端权威同步时被「涨回来」（本地只是缓存）。
        """
        if amount <= 0:
            return {"ok": False, "error": "amount 必须为正"}
        self._ensure_loaded()
        st = self._state
        ai = st["ai_member"]
        ai_left = int(ai.get("credits_left", 0)) if ai.get("active") else 0
        perm_total = int(st["permanent_credits"].get("total", 0))
        if ai_left + perm_total < amount:
            return {"ok": False, "error": f"积分不足：需要 {amount}，当前 {ai_left + perm_total}"}

        remaining = amount
        ai_taken = 0
        # 1) AI 订阅积分
        if remaining > 0 and ai_left > 0:
            take = min(ai_left, remaining)
            ai["credits_left"] = ai_left - take
            remaining -= take
            ai_taken = take
        # 2) 永久积分
        perm_taken = 0
        if remaining > 0:
            perm_total -= remaining
            st["permanent_credits"]["total"] = perm_total
            perm_taken = amount - ai_taken
            remaining = 0
        st["meta"].setdefault("history", []).append({
            "type": "spend", "amount": amount, "reason": reason, "at": self._now(),
        })
        st["meta"]["history"] = st["meta"]["history"][-200:]
        self._persist()
        # 异步上云记账（fire-and-forget；失败/离线进 pending 队列，下次心跳补报）
        try:
            import cloud_link
            cloud_link.report_spend_async(self, amount=amount, ai_taken=ai_taken,
                                          perm_taken=perm_taken, reason=reason)
        except Exception:  # noqa: BLE001 — 上云失败绝不影响本地扣减结果
            pass
        return {"ok": True, "spent": amount, "reason": reason,
                "ai_taken": ai_taken, "perm_taken": perm_taken,
                "credits_left": self.status()["credits_total"]}

    # ---- 免费用户「首次体验」名额（2026-10-05，与桌面端同口径）----
    def _trials_raw(self) -> dict[str, Any]:
        """已用名额表。惰性建键，旧状态文件没有 `free_trials` 也能正常跑。"""
        self._ensure_loaded()
        tr = self._state.get("free_trials")
        if not isinstance(tr, dict):
            tr = {}
            self._state["free_trials"] = tr
        return tr

    def trial_available(self, op: str, cost: int = 0) -> bool:
        """该 op 现在还能不能走「免费体验」。五道判据缺一不可。

        注意这里**只看本 store 的会员标志，不调 status()**：status() 会连带调用
        `free_trial_view()` → 回到 `trial_available()`，就成了无限递归。
        """
        pol = free_trial_policy()
        if not pol.get("enabled"):
            return False
        if op in (pol.get("exclude") or []):
            return False
        mc = int(pol.get("max_cost") or 0)
        if mc > 0 and int(cost or 0) > mc:
            return False
        if not pol.get("members_too") and self._is_download_active():
            return False          # 会员按付费纪律自己承担，不拿试用兜底
        return _trial_key(op, str(pol.get("mode"))) not in self._trials_raw()

    def trial_used(self, op: str, pol: dict[str, Any] | None = None) -> bool:
        """该 op 的试用名额是否已经用掉（只判状态，不改状态）。"""
        pol = pol or free_trial_policy()
        return _trial_key(op, str(pol.get("mode"))) in self._trials_raw()

    def _trial_claim_remote(self, op: str, mode: str) -> str:
        """跨端原子领取免费名额。返回 'fresh' | 'already' | 'offline'。

        - 'fresh'   : 本端首次成功领取全局唯一名额（放行）
        - 'already' : 另一台设备/网页已领走（本端应拒绝）
        - 'offline' : 授权中心不可达，fail-open 视为本端领取
        不改动本地状态，由调用方按结果落本地盘。
        """
        try:
            import cloud_link
            if not cloud_link.link_enabled():
                return "offline"
            sess = self.cloud_session() or {}
            token = str(sess.get("token") or "")
        except Exception:
            return "offline"
        if not token:
            return "offline"
        try:
            import license_client
            r = license_client.trial_claim_remote(token, op, mode)
            if not r or not r.get("ok"):
                return "offline"
            return "already" if r.get("already") else "fresh"
        except Exception:
            return "offline"

    def trial_consume(self, op: str, reason: str = "") -> bool:
        """标记名额已用；跨端唯一：先去授权中心做原子领取，按结果定夺。

        返回 True=放行（本端首次领取成功 / 离线兜底），False=被另一台领走（应拒绝）。
        与 `spend_credits` 同时机 —— **在任务真正开跑之前**就占掉（防「故意制造失败」白嫖）。
        """
        pol = free_trial_policy()
        mode = str(pol.get("mode"))
        key = _trial_key(op, mode)
        res = self._trial_claim_remote(op, mode)
        if res == "already":
            # 另一台已领走：本机同步标记为已用（UI 一致），但本次拒绝
            self._trials_raw()[key] = self._now()
            self._persist()
            return False
        # fresh 或 offline：本端领取成功（离线兜底放行）
        self._trials_raw()[key] = self._now()
        meta = self._state.setdefault("meta", {})
        meta.setdefault("history", []).append({
            "type": "free_trial", "op": op, "reason": reason, "at": self._now(),
        })
        meta["history"] = meta["history"][-200:]
        self._persist()
        return True

    def free_trial_view(self) -> dict[str, Any]:
        """给 status() / 前端用的试用视图。

        `remaining` 逐个 op 算真实余量，会员中心能直接渲染「还剩几次免费体验」，
        不用前端自己去照着策略推导一遍。
        """
        pol = free_trial_policy()
        mode = str(pol.get("mode"))
        remaining: dict[str, int] = {}
        for op in AI_CREDIT_COSTS:
            remaining[op] = 1 if self.trial_available(op, int(credit_cost(op))) else 0
        # 🔴 once 口径下所有 op 共用同一个名额，`sum(remaining)` 会算出 5 张票，
        #    前端拿去渲染「还剩 5 次」就全错。此时真实余量只有 0/1 两种取值。
        used_any = any(self.trial_used(op, pol) for op in AI_CREDIT_COSTS)
        if mode == "once":
            remaining_count = 0 if self.trial_used(_TRIAL_ONCE_KEY, pol) else 1
        else:
            remaining_count = sum(remaining.values())
        return {
            "enabled": bool(pol.get("enabled")),
            "mode": mode,
            "max_cost": int(pol.get("max_cost") or 0),
            "exclude": list(pol.get("exclude") or []),
            "members_too": bool(pol.get("members_too")),
            "remaining": remaining,
            "remaining_count": remaining_count,
            "used_any": used_any,
        }

    def add_credits(self, delta: int, reason: str = "admin_adjust") -> dict[str, Any]:
        """管理员调整积分：正=充值（永久积分池，不过期），负=扣减（先 AI 订阅后永久）。

        负 delta 直接扣减，不受正常「积分不足拒绝」限制（管理员强扣）。返回最新余额。
        """
        self._ensure_loaded()
        st = self._state
        if delta >= 0:
            st["permanent_credits"]["total"] = int(st["permanent_credits"].get("total", 0)) + delta
        else:
            amount = -delta
            ai = st["ai_member"]
            ai_left = int(ai.get("credits_left", 0)) if ai.get("active") else 0
            perm_total = int(st["permanent_credits"].get("total", 0))
            # 先扣 AI 订阅积分，再扣永久积分（可能扣成负数，管理员强扣允许透支由前端提示）
            remaining = amount
            if remaining > 0 and ai_left > 0:
                take = min(ai_left, remaining)
                ai["credits_left"] = ai_left - take
                remaining -= take
            if remaining > 0:
                perm_total -= remaining
                st["permanent_credits"]["total"] = perm_total
        st["meta"].setdefault("history", []).append({
            "code": f"admin_adjust:{delta}", "via": reason, "at": self._now(), "type": "admin_adjust",
        })
        st["meta"]["history"] = st["meta"]["history"][-200:]
        self._persist()
        return {"ok": True, "delta": delta, "credits_total": self.status()["credits_total"]}

    def credits_balance(self) -> dict[str, int]:
        s = self.status()
        return {"ai_subscription": s["ai_member"]["credits_left"],
                "permanent": s["permanent_credits"],
                "total": s["credits_total"]}

    # ---- 每日配额 ----
    def _is_download_active(self) -> bool:
        """下载权益是否活跃（独立VIP会员或 AI 会员捆绑）。"""
        st = self._state
        return bool(st["download_member"].get("active")) or bool(st["ai_member"].get("active"))

    def _roll_daily(self, now: float) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        du = self._state["daily_usage"]
        old_day = du.get("date")
        if old_day != day:
            # 归档旧一天的数据（如果存在且尚未归档）
            if old_day and old_day != "":
                hist = self._state.setdefault("usage_history", {})
                hist[old_day] = {k: int(v) for k, v in du.items() if k != "date"}
                # 2026-10-19：明细随计数一起归档（与桌面端/云端同构 {资源:[{t,p,at}]}）
                _det = self._state.get("daily_details") or {}
                _clean = {k: v[-50:] for k, v in _det.items()
                          if isinstance(v, list) and v}
                if _clean and isinstance(hist.get(old_day), dict):
                    hist[old_day]["details"] = _clean
                self._state["daily_details"] = {}
                # 只保留最近 90 天，避免无限增长
                cutoff = time.strftime("%Y-%m-%d", time.localtime(now - 90 * 86400))
                for k in list(hist.keys()):
                    if k < cutoff:
                        del hist[k]
                self._persist()
            du["date"] = day
            # 2026-10-25：改为**动态重置**——daily_usage 里除 date 外的键一律归零，
            # 不再维护硬编码列表。原因：FEATURE_USAGE_DEFS 后来新增的资源
            # （cloud_drive / convert_audio / convert_image / cloud_concat /
            #  cloud_dewatermark_pdf / cloud_subtitle_burn / cloud_subtitle_translate /
            #  share_qr / share_page）都没加进旧列表，导致「跨日不清零、配额永久顶满」。
            for _k in list(du.keys()):
                if _k != "date":
                    du[_k] = 0
            for _dead in _DEAD_USAGE_KEYS:      # 清掉历史状态文件里的死键
                du.pop(_dead, None)

    def quota_state(self, resource: str) -> dict[str, Any]:
        """查询某资源的当日用量/上限（按当前档位：免费 or 会员）。unlimited 恒放行。"""
        self._ensure_loaded()
        self._roll_daily(self._now())
        if resource in UNLIMITED_QUOTA:
            return {"resource": resource, "allowed": True, "unlimited": True}
        is_member = self._is_download_active()
        # 2026-10-17：限额取**生效值**（代码常量 ← 授权中心 free_quota 覆盖），
        # 与桌面端 effective_daily_limits 同口径。云端不可达时自动回退常量，
        # 且有 15s 缓存 + 30s 失败退避，不会把每次配额检查拖成网络等待。
        member_limits, free_limits = effective_daily_limits()
        limit_map = member_limits if is_member else free_limits
        limit = limit_map.get(resource)
        if limit is None and member_limits.get(resource) is None:
            # 未知资源：V1 不设卡（保守默认放行，避免误伤功能）
            return {"resource": resource, "allowed": True, "unknown": True}
        if limit is None:
            # 免费表未覆盖但会员表有（原画/批量）→ 免费额度为 0
            limit = 0
        used = int(self._state["daily_usage"].get(resource, 0))
        return {"resource": resource, "limit": limit, "used": used,
                "remaining": max(0, limit - used),
                "allowed": used < limit,
                "tier": "member" if is_member else "free",
                "member_limit": member_limits.get(resource),
                "free_limit": free_limits.get(resource, 0)}

    def use_daily(self, resource: str, n: int = 1, title: str = "",
                  platform: str = "", ref: str = "") -> dict[str, Any]:
        """消耗下载类配额（免费档 resolve 10/日；会员档按表）。超限返回 ok=False。

        title / platform（2026-10-19）：记录「具体做了什么」，随用量上云，
        后台「使用详情」据此显示可定位的明细（如「少帅 第5集 · 抖音」）。

        ref（2026-10-21）：本次消耗的唯一标识（运行时生成，随返回值下发）。
        调用方把它挂到下载任务上，等任务真正**落盘完成**时再调
        `mark_usage_result(ref, ok)` 回写「这次到底下载成功了没有」——
        后台「使用详情」要能区分「点了下载但没下成」，而不只是次数。
        """
        if n <= 0:
            return {"ok": False, "error": "n 必须为正"}
        q = self.quota_state(resource)
        if q.get("unlimited") or q.get("unknown"):
            return {"ok": True, "resource": resource, "unlimited": q.get("unlimited", False)}
        if not q["allowed"]:
            _note_quota_block(resource)      # 会员墙拦截计数（旁路，见函数注释）
            if q.get("tier") == "free":
                if resource == "download":   # 保持下载文案不变（前端/测试依赖）
                    msg = (f"今日免费下载额度已用尽（{q['limit']}/日）"
                           f"— 开通VIP会员可解锁 {q.get('member_limit', 0)} 次/日")
                else:
                    # 🔴 2026-10-06 第二轮：8 个云端功能一一对应独立额度键，
                    # 超限文案必须报**具体功能名**，否则用户不知道是哪一项用完了。
                    label = {"cloud": "该功能",
                             "cloud_commentary": "视频解说",
                             "cloud_convert": "在线转码",
                             "cloud_concat": "在线拼接",
                             "cloud_dewatermark": "图片去水印",
                             "cloud_dewatermark_pdf": "PDF 去水印",
                             "cloud_subtitle": "字幕提取",
                             "cloud_subtitle_burn": "字幕烧录",
                             "cloud_subtitle_translate": "字幕翻译",
                             "cloud_drive": "网盘下载",
                             # 2026-10-19：新键必须有中文名，否则超限文案会直接
                             # 把资源键（convert_audio / share_qr）甩给用户看。
                             "convert_audio": "音乐转换",
                             "convert_image": "图片转换",
                             "share_qr": "生成二维码",
                             "share_page": "生成网页"}.get(resource, resource)
                    msg = f"今日免费{label}次数已用尽（{q['limit']}/日）"
                    if q.get("member_limit"):
                        msg += f"— 开通会员可解锁 {q['member_limit']} 次/日"
                return {"ok": False, "error": msg,
                        "resource": resource, "code": "MEMBER_QUOTA"}
            return {"ok": False, "error": f"{resource} 今日配额已用尽（{q['limit']}/日）", "resource": resource,
                    "code": "MEMBER_QUOTA"}
        used = int(self._state["daily_usage"].get(resource, 0))
        new_used = used + n
        if new_used > q["limit"]:
            _note_quota_block(resource)      # 会员墙拦截计数（并发超限的同一条路径）
            return {"ok": False, "error": f"超出 {resource} 日配额上限 {q['limit']}",
                    "resource": resource, "code": "MEMBER_QUOTA"}
        self._state["daily_usage"][resource] = new_used
        _ref = str(ref or "").strip()[:64] or _new_usage_ref()
        # 2026-10-19：本机也留一份明细（云端才是真源，这里只是本机兜底）
        # 2026-10-21：条目带幂等键 id —— 任务结束后按它回写成功/失败。
        import time as _t
        _title = str(title or "").strip()[:120]
        if _title:
            try:
                _det = self._state.setdefault("daily_details", {})
                _lst = _det.setdefault(str(resource)[:32], [])
                _item = {"t": _title, "p": str(platform or "").strip()[:64],
                         "at": int(_t.time()), "id": _ref}
                _lst.append(_item)
                _det[str(resource)[:32]] = _lst[-50:]
            except Exception:  # noqa: BLE001 — 记账明细绝不能影响配额判定
                pass
        self._persist()
        # 异步上云记账（同一账号 App/网页共用每日配额；失败/离线进队列下次心跳补报）
        try:
            import cloud_link
            cloud_link.report_usage_async(self, resource, n, title=title,
                                          platform=platform, ref=_ref)
        except Exception:  # noqa: BLE001 — 上云失败绝不影响本地结果
            pass
        return {"ok": True, "resource": resource, "used": new_used,
                "remaining": q["limit"] - new_used, "ref": _ref}

    def mark_usage_result(self, ref: str, ok: bool, resource: str = "download",
                          size: int = 0) -> bool:
        """回写「这次用量到底有没有真正落到本机」（成功标准＝下载完成并能保存）。

        成功判据由调用方给（下载器里＝任务状态 completed、成品文件存在），
        本方法只负责记账：把结果标到当天 / 历史归档里同 id 的明细条目上，
        再异步上报授权中心 —— 管理员在**别人的**账号上看「使用详情」时读的是
        云端，所以只写本机不够。

        幂等：同一 ref 重复回写只覆盖为同一结果；不做配额加减（次数早已扣过）。
        """
        r = 1 if ok else 0
        key = str(ref or "").strip()[:64]
        if not key:
            return False
        hit = False
        try:
            self._ensure_loaded()
            for _item in self._iter_detail_items(resource, key):
                _item["r"] = r
                if size:
                    try:
                        _item["sz"] = int(size)
                    except (TypeError, ValueError):
                        pass
                hit = True
            if hit:
                self._persist()
        except Exception:  # noqa: BLE001 — 回写结果绝不能影响下载本身
            pass
        try:
            import cloud_link
            cloud_link.report_usage_result_async(self, resource, key, r)
        except Exception:  # noqa: BLE001
            pass
        return hit

    def _iter_detail_items(self, resource: str, ref: str):
        """遍历当天 + 历史归档里所有同 ref 的明细条目（跨天完成也能标到）。"""
        res = str(resource)[:32]
        buckets = []
        live = self._state.get("daily_details") or {}
        if isinstance(live, dict):
            buckets.append((live.get(res) or []))
        hist = self._state.get("usage_history") or {}
        if isinstance(hist, dict):
            for row in hist.values():
                det = (row or {}).get("details") if isinstance(row, dict) else None
                if isinstance(det, dict):
                    buckets.append(det.get(res) or [])
        for lst in buckets:
            if not isinstance(lst, list):
                continue
            for it in lst:
                if isinstance(it, dict) and str(it.get("id") or "") == ref:
                    yield it


def _note_quota_block(resource: str) -> None:
    """记一次「会员墙拦截」（2026-11-01）。

    这是全链路里**唯一既不进需求库、也不进授权中心**的信号：用户被配额拦住时
    只在前端弹个提示就消失了，后台看不到「到底多少人卡在配额上」——而这恰恰
    是转化瓶颈最关键的一项（谁被拦、被哪个功能拦）。

    落本机 stats.json（后台 `/api/admin/stats` 直接可读），零新增通道、
    零上报成本。旁路统计，任何异常一律静默：绝不能因为记账失败而改变拦截结果。
    """
    try:
        import stats
        stats.record_event("quota_block_" + str(resource or "unknown")[:24])
    except Exception:  # noqa: BLE001
        pass


def _new_usage_ref() -> str:
    """生成一次用量明细的唯一标识（2026-10-21）。

    用途：把「下单时扣的那 1 次」与「下载结束后回写的成功/失败」对上号。
    形状 `u<16 hex>`，≤64 字符，天然可作为授权中心的幂等键。
    """
    import uuid
    return "u" + uuid.uuid4().hex[:16]


def _date_range_days(period: str, now: float) -> list[str]:
    """根据周期返回应包含的 YYYY-MM-DD 日期列表（含今天）。"""
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if period in ("3d", "7d"):
        days = 3 if period == "3d" else 7
        return [time.strftime("%Y-%m-%d", time.localtime(now - i * 86400)) for i in range(days - 1, -1, -1)]
    if period == "month":
        # 本月 1 日到今天
        tm = time.localtime(now)
        year, month = tm.tm_year, tm.tm_mon
        start = time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1))
        dates: list[str] = []
        cur = start
        while time.strftime("%Y-%m-%d", time.localtime(cur)) <= today:
            dates.append(time.strftime("%Y-%m-%d", time.localtime(cur)))
            cur += 86400
        return dates
    return [today]


def usage_summary(store: MembershipStore, period: str = "today") -> dict[str, int]:
    """汇总指定周期内各 resource 的累计用量。

    period: today | 3d | 7d | month
    """
    st = store.status()
    now = store._now()
    dates = _date_range_days(period, now)
    # 🔴 历史用量只能从 _state 读：status() 是「对外公开视图」，刻意不暴露
    # usage_history（体积大、含逐日明细）。原来这里读 st["usage_history"]，
    # 恒为 {} → 个人中心「使用统计」表的近三日/近七日/本月**永远显示 0**，
    # 而后台「用户使用详情」读 _state 所以有数据，两边对不上（2026-10-03 实测）。
    du = st.get("daily_usage", {})
    hist = dict(getattr(store, "_state", None) or {}).get("usage_history") or {}
    totals: dict[str, int] = {}
    for d in dates:
        if d == du.get("date"):
            src = du
        else:
            src = hist.get(d, {})
        for k, v in src.items():
            if k == "date":
                continue
            try:
                totals[k] = totals.get(k, 0) + int(v)
            except (TypeError, ValueError):
                continue
    return totals


def feature_usage_status(store: MembershipStore, period: str = "today") -> list[dict[str, Any]]:
    """生成个人中心「使用统计」表格所需的每行数据。

    period: today | 3d | 7d | month
    返回字段：key, name, unit, daily_used, daily_limit, daily_remaining,
              period_used, unlimited, balance, credit_cost。
    """
    st = store.status()
    ai = st["ai_member"]
    fc = ai.get("feature_credits") or {} if ai.get("active") else {}
    daily = st.get("daily_usage", {})
    period_totals = usage_summary(store, period)
    is_member = st["download_member"].get("active", False)
    # 2026-10-17：表格里的「体验剩余」必须与闸门用同一套生效值（授权中心覆盖），
    # 否则会出现「表格写 3 次、实际放行 10 次」这种自相矛盾。
    member_limits, free_limits = effective_daily_limits()
    rows: list[dict[str, Any]] = []
    for d in FEATURE_USAGE_DEFS:
        key = d["key"]
        unlimited = bool(d.get("unlimited", False))
        if unlimited:
            rows.append({
                "key": key,
                "name": d["name"],
                "unit": d["unit"],
                "daily_used": 0,
                "daily_limit": -1,
                "daily_remaining": -1,
                "period_used": 0,
                "unlimited": True,
                "balance": None,
                "credit_cost": int(d.get("credit_cost", 0)),
            })
            continue
        res = d["resource"]
        if is_member:
            limit = int(member_limits.get(res, d.get("member_limit", 0)))
        else:
            limit = int(free_limits.get(res, d.get("free_limit", 0)))
        used = int(daily.get(d["resource"], 0)) if limit >= 0 else 0
        remaining = -1 if limit < 0 else max(0, limit - used)
        bonus = int(d.get("ai_bonus", 0))
        balance = int(fc.get(key, 0)) if bonus > 0 and ai.get("active") else None
        rows.append({
            "key": key,
            "name": d["name"],
            "unit": d["unit"],
            "daily_used": used,
            "daily_limit": limit,
            "daily_remaining": remaining,
            "period_used": period_totals.get(d["resource"], 0),
            "unlimited": False,
            "balance": balance,
            "credit_cost": int(d.get("credit_cost", 0)),
        })
    return rows
