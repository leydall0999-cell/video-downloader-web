#!/usr/bin/env bash
# 离线测试运行器：在沙盒内验证 VideoDownloader 后端，不依赖任何外部网络。
#
# 覆盖：
#   1. test_app_smoke.py         —— FastAPI TestClient 无头冒烟装配
#   3. test_atomic_writes_and_auth.py —— 原子写契约 / 并发锁 / 验证码仅本机回传 回归
#   4. test_worker_proxy_direct.py —— 解析通道直连 daemon 的路由回归
#   5. test_cloud_link.py        —— web 版账号接授权中心（打通 web 与 App 用户数据）
#   6. test_media_proxy_range.py —— 下载用媒体中继 /api/media/proxy 的 Range/防盗链/SSRF 契约
#   7. test_direct_url_hls.py    —— m3u8 不得被当作「可直接下载的文件」透传（否则存下播放列表文本）
#   8. test_web_direct_download.js —— 网页版直链分片下载引擎（前端真源码 + 假源站，需 node）
#   9. test_web_hls_assemble.js  —— 浏览器内 HLS 合成（m3u8 解析/变体选择/初始化段/加密与直播拒绝，需 node）
#   10. test_web_route_retry.js  —— 远端失败自动换路由重试（失败分类/原地重试/换路由/耗尽集合防抖/切换上限，需 node）
#   11. test_youtube_shortlink_scope.py —— youtu.be / shorts / m. 短链必须归一化为 www 长链
#       （yt-dlp 的 http_headers Cookie 按域作用域，短链会把有效 Cookie 挡在 innertube 之外）
#   12. test_upload_body_limit.py —— 网关体积上限守卫：/api/upload-chunk 必须放得下 64MB 分片，
#       前端上传端点只允许同源（VPS 那份 nginx 曾只有 8m，把网页版全部上传 413 打死）
#
# 退出码非 0 表示有测试失败（可在 build_mac.sh 末尾调用以阻断坏构建）。
set -u

# 🔴 离线测验必须与云端解耦：默认关掉账号上云（test_cloud_link.py 自己会重新打开，
#    并把 license_client 全量替换为内存假实现）。否则测试会真的去打 8902。
export VDL_CLOUD_LINK=0

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVER="$REPO/server"

# 优先用项目 build venv（含 requests / fastapi / httpx），否则回退 managed python
PY=""
for cand in "$REPO/.build_venv/bin/python" "/Users/suixindelang/.workbuddy/binaries/python/versions/3.13.12/bin/python3"; do
  if [ -x "$cand" ]; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  echo "❌ 找不到可用的 python（需 requests + fastapi + httpx）"
  exit 1
fi

echo "▶ 使用解释器: $PY"
echo "▶ 运行离线测试（无外部网络依赖）..."
cd "$SERVER" || exit 1

PASS=0
FAIL=0

run_one() {
  local f="$1"
  echo ""
  echo "=== $f ==="
  if "$PY" "tests/$f" 2>&1; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
  fi
}

# --- Node 解释器（前端下载引擎测试用）---
# 找不到就只跳过那一条，不让「本机没装 node」把整份套件染红。
NODE=""
for cand in /Users/suixindelang/.workbuddy/binaries/node/versions/*/bin/node \
            "$(command -v node 2>/dev/null)"; do
  if [ -n "$cand" ] && [ -x "$cand" ]; then NODE="$cand"; break; fi
done

run_node() {
  local f="$1"
  echo ""
  echo "=== $f (node) ==="
  if [ -z "$NODE" ]; then
    echo "⏭ 跳过：未找到 node 解释器（前端下载引擎测试需要 node）"
    return 0
  fi
  if "$NODE" "tests/$f" 2>&1; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
  fi
}

run_one test_app_smoke.py
run_one test_index_cache_invalidation.py   # 首页内存缓存失效判据：只改 index.html 也必须重读（2026-09-30 线上吐旧页事故）
run_one test_atomic_writes_and_auth.py
#  账号体系加固（安全审计 2026-10-13，F-02 / F-03，桌面端修复未同步到 web 的补齐）：
#  F-02 token 撤销（禁用/改密/注销都 bump token_version；authenticate 拒 disabled；
#       旧格式 3 段 token 不集体踢下线）；
#  F-03 超管引导仅本机直连（公网首注册不得自动提权；显式名单仍生效）。
run_one test_web_auth_hardening_f02_f03.py
run_one test_worker_proxy_direct.py
run_one test_peer_overseas_fallback.py
run_one test_member_download_quota.py
run_one test_vip_bonus_credits.py         # VIP 会员赠 300 积分：发放/累加/不串池/到期不清/文案同源（2026-10-24 用户定档）
run_one test_youtube_unavailable_vs_bot.py
run_one test_youtube_shortlink_scope.py
run_one test_yt_warp_proxy.py              # YouTube 专属出口代理：VDL_PROXY_YT(WARP) 分流、googlevideo 同代理、国内站不受影响（2026-10-01）
run_one test_yt_socks_preflight.py         # YouTube 解析入口 socks5 出口预检：死端口 0.6s 明确报「线路维护中」，杜绝 120s 干等（2026-10-21）
run_one test_yt_pot_starved.py             # YouTube SABR「PO Token 饥饿」检测 + web_safari/HLS 补救 + 雪碧图假档排除（2026-10-01）
run_one test_quality_options_direct_video.py  # 清晰度档位归类：直链视频（无 height 但非音频）不得被误标「最佳音质」（2026-10-20）
run_one test_yt_quality_lock.py            # YouTube「选什么下什么」：解析阶段 format=None 会选 4K，process_info 前必须按卡片同源重选轨（2026-10-21 事故：选 480P 实下 1.9G 4K AV1）
run_one test_cookie_pool_admin_cloud.py    # 云端 Cookie 池管理入口 POST /api/cookie/pool-admin：令牌门禁/概览口径/脱敏/坏输入/路由已挂载（2026-10-21 桌面后台报「云端不可达 HTTP 405」事故）
run_one test_cookie_probe_static.py        # Cookie 验真·静态兜底：探针不可用时，缺登录字段必须判 invalid、有字段只能 unknown（2026-10-21 17 条全挂「未校验」事故）
run_one test_cookie_probe_douyin_kuaishou.py  # Cookie 验真·真探针：抖音 profile_self(免签名) + 快手 GraphQL visionProfile(免签名) 的 31 项判据，含 passToken 交叉验证与离线反向约束
run_one test_cookie_verify_inherit.py      # Cookie 池「未校验」治理：同会话新快照继承已校验结论、换会话/游客不继承、www. 注入 key 归一（2026-10-21 线上小红书/YouTube 各 3 份全挂未校验）
run_one test_cookie_share_host_map.py      # 分享域/极速版域→主站登录域：chenzhongtech→kuaishou、iesdouyin→douyin、b23.tv→bilibili，含「主站不得被误映射」与 B站 CDN 回归（2026-10-21 极速版只带 did/didv）
run_one test_admin_roles.py             # 后台权限拆两级：超管唯一（名单首项/最早注册兜底）、其余 is_admin 一律 admin（2026-10-22 自桌面端同口径移植）
run_one test_admin_roles_api.py         # 路由层闸门：admin.py 11 写 + admin_cookies 3 写全用 require_super、读接口仍 require_admin、CSRF 兜底不丢、超管不可被降级
run_one test_admin_bgm.py              # 云端背景音乐曲库：文件名重生成/魔数/体积上限/清单原子写/路径穿越/写闸门静态扫描（2026-11-08）
run_one test_admin_roles_e2e.py         # 真 HTTP：超管写 200 / 管理员写 403（不是 401）/ 普通用户 401 / 超管可取消他人管理员但取消不了自己
run_one test_admin_promote_effect.py   # 提权「点了没反应」的根因：手机号账号的标识解析必须命中（否则云端同步被跳过 + 云端权威回写把本机打回）＋权限文案两级口径（2026-10-22）
run_one test_cookie_verify_cooldown.py     # 联网验真冷却：全量 6h/手动 10min 内不重复打平台（云端机房 IP 高频探同一 session 会被风控踢下线，2026-10-21 快手）
run_one test_nmw_extractor.py              # 金牌影院：域名族识别 + 五种链接写法（/sid/ 可省、三段式老域名、maccms、详情页）+ 签名公式锁定（2026-10-21 用户第三次「又不行了」）
run_one test_cookie_pool_nmw_family.py     # 金牌影院一族 Cookie 池链路：hardened 双域名/白名单派生/验真派发按域/测试 URL（2026-10-21 收编 chrqj.com 时发现只认老域名）
run_one test_upload_body_limit.py
run_one test_reconvert_source_reuse.py      # 免重传重转：finish 留源 2h / reconvert 复用 / 设备隔离 / 410 回退 / TTL 清理（2026-09-29）
run_one test_feature_auth_gate.py           # 功能级登录门禁（服务端）：18 个执行类端点未登录 403+引导登录、带 token 放行（2026-09-29）
run_one test_cloud_quota.py                # 云端算力账号级配额：免费 3 次/日（转码/拼接/去水印/字幕），gate/count/relay/fail-open（2026-09-29）
run_one test_cloud_daily_quota_unified.py   # 跨端每日免费额度统一：网页版采纳云端 daily_free_limits + 离线开关切断云端（2026-10-17 用户定档「以后台为准」）
run_one test_cloud_link.py
run_one test_media_proxy_range.py
run_one test_cloud_drive_baidu_web.py         # 网页版百度网盘：登录态存服务器(加密/按用户隔离)/resolve 不外泄直链与 cookie/download 带 Range+登录态中转(2026-10-25 用户拍板「存服务器上」)
run_one test_baidu_pool_server.py         # 服务器端百度登录态进共享池：白名单放行+仅贡献域标记+真问站点验真+入池取回+管理员那份写回池(2026-10-25)
run_one test_direct_url_hls.py
run_node test_web_direct_download.js
run_node test_web_hls_assemble.js
run_node test_web_route_retry.js
run_node test_web_userdata_sync.js           # 网页版云备份引擎：Cookie 端到端加密(真 WebCrypto)/无明文外带/幂等/冲突守卫/摘要取云端实际量（2026-10-17）
run_node test_upload_endpoints.js            # 网页版上传提速（2026-10-22）：端点池红线（只准同后端，禁 hk 那个独立节点）/2MB 分片/6 路并发/轮换选路/连续失败摘除与恢复/5 条上传路径都真用上多端点
run_node test_upload_origin_lease.js         # 网页版上传（2026-10-23）：每 origin 同时只 1 条活跃分片（实测 6路同源 102 / 6源 411 / 12路挤6源 205 KB/s），ucUploadChunk 用租到的 ep + finally 归还 + 5 条路径探测退化重探
run_node test_convert_poll_lifecycle.mjs   # 音乐/图片转换轮询定时器自杀停表回归（2026-09-29 线上卡 30% 事故）
run_node test_dw_preview_zoom.mjs          # 去水印捏合缩放(只放大图片)+结果预览灯箱+重新加工（2026-09-29 用户反馈）
run_node test_view_top_gap.mjs             # 顶层视图卡片与吸顶顶栏间距：main > .panel 3rem，防「顶栏+卡片」粘连（2026-10-04 用户反馈）
run_node test_mobile_header_tabs.mjs       # 手机网页版顶栏功能导航：≤760px 换行让 tabs 不被挤成 0 宽而整排消失（2026-10-07 用户反馈）
run_node test_activate_gate.mjs            # 激活码通道收口：/api/member/activate 仅超管 + 前端入口已移除（2026-10-04 白嫖后门）
run_node test_feature_usage_gate.mjs        # 「今日使用」功能配额表逐行须有真实 use_daily 拦截点（2026-10-04 清占位行）
run_node test_deploy_surface_parity.mjs     # cn/hk 双部署面：activate 超管门禁必须两端同步（2026-10-04 hk 实测被白嫖）
run_node test_benefit_copy_hide.mjs          # 会员页权益文案不得含「云端/算力/AI/本地」字眼，但配额与限流原样（2026-10-04 用户定档）
run_node test_ai_features_honest.mjs         # AI 会员权益：每条须有实现落点 + 前端必须真的渲染（2026-10-04 用户反馈「AI会员补充权益」）
run_node test_concat_flow.mjs              # 视频拼接面板状态机：防重复结果/停表/清源片段/输出分区（2026-09-29 用户反馈）
run_node test_app_intro_entries.mjs        # 网页版「更多功能」页：24 项能力/5 组/标签与入口一致（2026-09-30）
run_node test_platform_count_label.mjs     # 平台数对外口径：两端徽章走 fmtPlatformCount，116→100+（2026-09-30）
run_node test_member_page.mjs              # 网页版会员购买页：免登录可见价目/三轨/登录后自动续单/弹窗在顶层（2026-09-30）
run_node test_home_page.mjs                # 网页版首页（新落地页）：独立 section/默认落地/22 项能力全列/文案一字不改/hp-* 类全有样式（2026-10-24）
run_node test_profile_login_single.mjs     # 个人中心未登录：页内登录表单优先，禁止与全局登录弹窗同屏（2026-10-08 用户反馈）
run_node test_auth_modal_width.mjs         # 登录弹窗定宽 400 无右侧留白 + 登录方式文案「电话号码/QQ邮箱」（2026-10-08 用户反馈）
run_node test_notice_modal.mjs             # 下单失败/活动限购改用弹窗：禁回退成「只写页面底部状态行」（2026-10-09 用户反馈）
run_node test_cookie_contribute_hidden.mjs # 「贡献 Cookie 到公共池」勾选框隐藏：只 display:none 不删元素/仍默认勾选/不留悬空指引（2026-10-09 用户反馈）
run_node test_placeholder_contrast.mjs     # 输入框 placeholder 灰度统一：全局兜底须与 #urlInput 基准 #a2acbd 同值，防个别框吃浏览器默认深灰 #757575（2026-10-09 用户反馈）
run_one test_member_plans_source.py        # 1/3/7 天档 + 套餐价格单一真源：超管改价/改天数在展示/下单/发放三处同步（2026-09-30）
run_one test_resolve_demand.py             # 解析需求登记（Downie 机制本土化）：落库字段/同 host 60s 节流/「站点×周」聚合口径/坏行跳过/2MB 截断（2026-10-17，10-23 改按周）
run_one test_member_notice.py              # 会员中心公告：云端下发生效/关闭即隐藏/空文本不显示/下发失败不抛/前端 textContent（2026-10-18）
run_one test_resolve_demand_merge.py       # 需求榜跨节点合并棘轮：hmac 常量时间/内部请求防递归/CF UA/_json 别名/合并口径/peer_ok 明示（2026-10-17）
run_one test_pay_gateway_web.py            # 网页版支付真通道：后端带云端令牌转发 VPS 支付服务 / 无令牌必拒 / PAID 拉权益 / 离线回落 mock / 前端按通道切文案（2026-10-09）
run_one test_purchase_history_sync.py       # 购买记录落户：apply_cloud_authoritative 幂等并入云端 purchases（按 id 去重） + 显示端 1/3/7 天档 + 到期精确到分 + 来源映射（cloud/ui_test/admin_direct）（2026-10-09）
run_one test_flash_phase.py                # 秒杀窗口三态：窗口过了必须自动收起「限时秒杀」角标与「秒杀至…」（2026-10-03）
run_one test_share_token.py                # 网页版分享凭据端点：登录门禁/SHARE_UNAVAILABLE/每日限次/计数/挂载（2026-09-30 受限版分享）
run_one test_web_stats.py                  # 网页访客统计：PV/UV 去重、不落 IP 明文、跨天哈希不可关联、事件白名单、来源归一、90 天裁剪（2026-10-01）
#  任务落盘持久化（2026-10-02 B2 移植）：重启后未完成任务恢复为可续传、completed 只在成品文件仍在时恢复。
run_one test_task_persistence.py
#  下载预检与 PO Token 会话保持（2026-10-02 B1/A1 移植）：体积估算/上限 20GB/磁盘预检、
#  YouTube 403 降级链必须带回 visitor_data（丢会话=照样 403）。
run_one test_download_preflight.py
run_node test_share_web.mjs                # 网页版「生成二维码/生成网页」：登录门禁/凭据三头/计数/受限上限/合成器红线/本地二维码（2026-09-30）
#  下载失败矩阵重试与 aria2c 默认化（2026-10-02 A2/B4 移植）：可重试失败换策略重试
#  （切换下载器→切 HLS 链路，最多额外 2 次）、auto 默认启用 aria2c、硬上限到点仍在推进则延长等待（B3）。
run_one test_download_matrix.py
#  安装包端点「默认 DMG + ZIP 兜底」（2026-10-15）：p=macos→dmg 类型/文件名、
#  p=macos-zip→zip、统计按平台归并不拆格式、默认映射源码级回归、未知平台 404。
run_one test_installer_dmg_default.py
#  可行动化报错 / 完成任务历史（2026-10-02 A3/B5 移植）：停滞与硬超时不再伪装成
#  「用户取消」；到期完成任务降级为历史条目（保留 50 条元数据、可重新下载）。
#  （A4 的 proxy.json 分流：web 端验证见 test_yt_warp_proxy.py 的 env 分支。）
run_one test_batch3_history.py
#  渗透自查（2026-10-12）修复守卫：/api/tasks 严格归属（无 device → 空队列、
#  带 device 不含无归属/他人任务）、/api/version 不外泄内部信息、
#  /api/media/proxy 域名分散度限流。改动任一处都会立刻红。
run_one test_pentest_fixes_20261012.py
run_one test_subtitle_minimal.py              # I8：web AI 字幕路径护栏 + _fmt_ts 最小回归（零覆盖补盲）
#  test_cookie_pull_diag_auth.py —— /api/cookie/pull-diag 必须校验令牌，
#  且校验必须在「经隧道拉 Cookie 写公共池」的重操作之前（2026-10-12 安全盘点实测：
#  该端点经 nginx/CF 公网可达，未带令牌即 200，默认 mode=full 是状态变更重操作）。
run_one test_cookie_pull_diag_auth.py
#  test_kuaishou_slide_photo.py —— 快手图集/图文作品必须**快速失败 + 可读文案**
#  （2026-10-28 巡检实测：短链展开后 final URL 带 subBiz=BROWSE_SLIDE_PHOTO，
#  只有幻灯片 HLS 流、无 mp4 直链 ⇒ 旧逻辑白等 25s 再抛「APOLLO_STATE 无 mp4」）。
run_one test_kuaishou_slide_photo.py
run_one test_ai_free_trial_web.py
run_one e2e_ai_trial_check.py

#  test_cloud_quota_unified.py —— 两端云端额度口径统一（2026-06-06 用户定档
#    「两端统一成终身 3 次」）：此前桌面记 quota.json 的终身 3 次、网页记
#    use_daily("cloud") 的每日 3 次/日 ⇒ 换端就白拿。现网页版也走授权中心原子
#    扣减（App / 网页共享一份）。守卫钉住：中心扣减是主路径、本地每日配额仅作
#    fail-open 兜底（AST 判「不许出现在 try 体的无条件单条语句里」）、上限读
#    quota.py 常量、取 token 走 store.cloud_session()、会员判定用网页版**实际
#    存在**的接口（它没有桌面端的 is_download_active / _cloud_token）。
run_one test_cloud_quota_unified.py
#  全端账号统一（2026-10-16 用户「每个端数据要都统一到一起」）：超管后台用户列表
#  以云端授权中心为唯一真源（Mac/Windows 桌面 + 网页版 cn/hk 四端账号全部可见），
#  本机表仅兜底附列；写操作双写云端、失败只出 notice 不阻断。本树无
#  deploy/license_server.py，授权中心侧断言自动跳过，只跑列表合并 + 路由双写。
run_one test_cloud_account_unified.py

#  操作引导「怎么用」守卫（2026-10-09 自 app-dev 移植适配）：9 个功能入口
#  每个有且仅有一条引导、落点不串台、文案引用真实控件、首次展开/已读收起/
#  三态「?」/关闭整条消失/子面板切换同步，任一被改坏即红。
run_node test_howto_guide.mjs

#  下载两端点 HEAD 支持（2026-10-13 接续待办 #1）：/api/extension/package 与
#  /download/installer 补 HEAD（下载管理器预检取 Content-Length/Type）。
#  守卫钉住：HEAD→200 空 body 但头部齐全、HEAD 不计入下载统计（否则预检+下载虚高一倍）、
#  未提供安装包的平台仍 404、GET body 不丢。改回 @router.get 会立刻红。
run_one test_download_head_support.py

# ---------------------------------------------------------------------------
#  根 tests/ 安全测试段（F-13/I2）：网页版根 tests/ 此前 0 登记，crypto/torrent/
#  auth 等安全测试常年不跑。这些测试是脚本式（直跑 python）或 pytest 式，且部分
#  需要 PYTHONPATH=server:tests 或功能开关（VDL_CRYPTO_ENABLED 等）才能启用。
#  下面对「已验证可离线通过」的安全测试单独接线；需要功能开关的依赖测试若在当前
#  默认环境下失败，先不纳入（避免把门禁染红），改由专项复跑确认。
# ---------------------------------------------------------------------------
run_root() {
  local f="$1"
  echo ""
  echo "=== (root) $f ==="
  if PYTHONPATH="$SERVER:$REPO/tests" VDL_DATA_DIR="/tmp/vdl_root_${f}" \
        "$PY" "$REPO/tests/$f" 2>&1; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
  fi
}

run_pytest_root() {
  local f="$1" path=""
  echo ""
  echo "=== (root pytest) $f ==="
  # 🔴 2026-10-14：用例同时存在于 $REPO/tests/ 与 $SERVER/tests/ 两处，
  #    原实现写死 "$REPO/tests/$f" ⇒ server/tests/ 的用例会「文件不存在」被误判
  #    FAIL（web-dev 侧漏改：app-dev 已于同日加固，本树仍为旧实现）。
  for cand in "$REPO/tests/$f" "$SERVER/tests/$f"; do
    if [ -f "$cand" ]; then path="$cand"; break; fi
  done
  if [ -z "$path" ]; then
    echo "❌ 找不到用例文件：$f"
    FAIL=$((FAIL+1)); return
  fi
  local d
  d=$(mktemp -d "/tmp/vdl_approot_XXXXXX")
  if PYTHONPATH="$SERVER:$REPO/tests" VDL_LOGIN_GATE=0 VDL_PLANS_CLOUD=0 \
        VDL_DATA_DIR="$d" env -u HTTP_PROXY -u HTTPS_PROXY "$PY" -m pytest "$path" -q 2>&1; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
  fi
  /bin/rm -rf "$d"
}

# crypto 保险箱：单元（密文头/加解密/跨端标识）+ 路由集成（status/set-pass/unlock/加解密）
run_root test_crypto_unit.py
# torrent：URI 校验 / SSRF 护栏 / 穿越防护 / 状态映射 等纯逻辑单测
run_root test_torrent_unit.py
# API Token 鉴权中间件：/api/* 拦截逻辑（pytest 式）
run_pytest_root test_auth_routes.py
run_pytest_root test_dl_stats_browser.py # 下载统计的浏览器判定：顺序敏感(Edge≠chrome/iOS Firefox≠safari)+非浏览器落 unknown 而非 chromium（2026-10-14 接续待办 #7）
# 下载页安装包按钮：跨域 302 的链接不得带 download 属性（Safari/微信「点了没反应」根因）+ 直连兜底与服务端对象名一致
run_pytest_root test_download_page_installer_links.py

# 公共 Cookie 池：按账号身份去重 + 入池验真（2026-10-18）
#   B 站池 1905 条其实是同 1 个账号的 1905 次快照（1902 条未登录），而 get_cookie
#   取最新一条 ⇒ 解析恒 412。守：未登录不入池 / 同账号留最新 3 条 / 剔除按会话验一次。
run_pytest_root test_cookie_pool_identity.py
# B 站 requests 补丁：cookie/proxy/ua 必须按每次请求刷新（冻结首次参数 ⇒ 永久 412），
# 且无 Cookie 时用池兜底、脚本 UA 换 Chrome。
run_pytest_root test_bilibili_patch_state.py
# B站 API 兜底：api 域强制要 buvid3（缺则 412 验证页 ⇒ 兜底静默全失效）+ 稿件不可播放要可读原因（2026-11-01）
run_pytest_root test_bilibili_api_buvid3.py
# 平台健康巡检：稿件级不可播（删稿/地区/会员）必须从分母剔除，否则热门榜一条死稿就误报整平台 degraded（2026-11-01）
run_pytest_root test_platform_health_sample.py
run_pytest_root test_convert_quota_split.py # 音乐转换/图片转换独立计次：按目标格式分派 + 使用统计补全（2026-10-19）
run_pytest_root test_share_quota_split.py   # 生成二维码/生成网页 进统计表：子页签分派 + 免费20/会员200 + 不走终身额度 + 前端带 src（2026-10-19）
# 对象存储直链（方案③）：OSS V1/SigV4 签名 + 假 OSS 端到端 + 内网上传 endpoint（2026-10-21 移植进 web-dev）
run_pytest_root test_object_store.py
# ⚠️ 必须写在 run_pytest_root 定义（第 ~206 行）之后：写在前面会报
#    「run_pytest_root: command not found」而静默跳过（2026-10-19 真踩过）。
# Cookie 加固：跨站读写 / 属性防护
run_root test_cookie_hardened.py
# 路由隔离：静态/API 边界、不得越权
run_root test_router_isolation.py
# 加工队列：并发/出队/状态（零测试覆盖补充，I9）
run_root test_process_queue.py

# ---------------------------------------------------------------------------
#  根测试大收编（2026-10-14，F-13/I2 终章）：39 个未登记根测试逐一盲测甄别后
#  36 个通过并纳入门禁（34 个直过 + 2 个修复：cjk 适配设备严格归属、
#  crypto_routes 裁掉桌面专属 library 节）；2 个 archive 死测试归档；
#  kuaishou_e2e 设计内自跳过（需 VDL_RUN_KS_E2E=1 真网络）不纳入。
# ---------------------------------------------------------------------------
run_root test_commentary_routes.py          # 解说桥接路由（http 模式 mock 算力）
run_root test_douyin_direct.py
run_root test_merged_modules_verify.py
run_root test_upload_convert.py
run_root test_web_contract.py
run_root test_b23tv_cookie_alias.py
run_root test_b23tv_expand.py
run_root test_bilibili_cookie_fix.py        # B站 Cookie 容错
run_root test_bilibili_url_normalize.py     # B站 URL 归一化（8 项）
run_root test_chrqj_extractor.py
run_root test_cookie_cache.py
run_root test_cookie_pool_save.py
run_root test_cookie_sync_ratelimit.py      # Cookie 同步限速
run_root test_dewatermark.py                # 去水印路由
run_root test_download_file_cjk.py          # CJK 文件名下载（适配设备严格归属）
run_root test_download_phases.py
run_root test_download_watchdog.py
run_root test_error_category.py
run_root test_extract_text_unit.py
run_root test_ffmpeg_tools_unit.py
run_root test_friendly_error_403.py         # 403 诊断分层
run_root test_iqiyi_short_url.py
run_root test_iqiyi_worker.py
run_root test_kuaishou_extractor.py
run_root test_new_platforms.py
run_root test_probe_youtube_log_passthrough.py
run_root test_proxy_cn_routing.py
run_root test_share_url_normalize.py        # 分享 URL 归一化（15 项）
run_root test_summarize_title.py
run_root test_tasks.py
run_root test_worker_direct_play.py
run_root test_ydlp_update.py
run_root test_youku_series_title.py
run_root test_youtube_autofallback.py
run_root test_youtube_player_skip.py
#  YouTube「无 PO Token ⇒ ≤1080p 优先 HLS 混合流」+ VDL_YT_JS_RUNTIME=off 开关
#  接线守卫（2026-10-19，HK 小机 43s deno / DASH 按连接限速两病灶的回归）：
#  档位矩阵 / HLS 选择器回落 / _download_options 接线 / _pot_usable 代理感知 /
#  off 必须写空 js_runtimes（yt-dlp 库默认 deno，不写=没关）/ fetch_pot 源码接线。
run_pytest_root test_youtube_hls_prefer.py
#  YouTube 403 降级链「不许超过用户选的清晰度」守卫（2026-10-21 事故：
#  选 1080P 显示 398MB、实际下到 641MiB 的 1440P AV1 —— 末环 bv*+ba 无 height 上限，
#  还把「PRO·原画需会员」档白送出去。详见 test_yt_fallback_cap.py 头部说明）。
run_pytest_root test_yt_fallback_cap.py
# 2026-10-21「选什么清晰度就下什么清晰度」：卡片只列真实档位 + 体积取首选编码组合、
# 下载/降级/HLS 对具体档一律 `=N` 精确锁定、完成后核对请求档 vs 实得档（详见文件头）。
run_pytest_root test_quality_exact.py
# 2026-10-22 金牌影院「选择清晰度里只有音频/WebM/M4A」：自研提取器不填 vcodec，
# 老判据 `vcodec in (None,"none")` 把「未知」当「没有视频轨」⇒ 分辨率档全被滤掉。
# 守：vcodec 缺失仍按 height 平铺真实档位 / 显式 "none" 依旧排除 / 纯音频不受影响。
run_pytest_root test_quality_vcodec_missing.py
run_root test_crypto_routes.py              # crypto 路由（web 适用节，自设开关）
#  用户信息类数据云备份 · 网页版接入（2026-10-17）：与桌面端同一账号资料包通道但
#  scope="web" 分文件（互不覆盖）、无登录/无云端身份必拒、parts 空必拒、scope 必须
#  进请求体、app.py 路由已注册 + 前端两处登录成功分支都接了自动同步。
run_one test_userdata_web.py

#  网页版「这次下载到底成功了没有」结果回写（2026-10-21）
#  实测证据：云端 18 条明细 0 条带结果，其中 7 条 id 是随机 hex（网页版上报），
#  桌面 App 的 ref 形如 u<16hex> —— 网页版原先压根没有回写链路：use_daily 不接
#  ref、上云 id 随机、下载完成不回报，所以面板永远显示「未上报」。
#  守卫钉住：ref 落明细 / mark 只改 r 不改次数 / 跨天归档仍可回写 /
#  两侧幂等键逐字一致 / 终态才回写且要求成品真实存在 / usage_ref 持久化 /
#  浏览器直存由前端回报 /api/usage/result。
run_one test_usage_result_flag_web.py

#  平台健康巡检 · 异常提醒链路（2026-11-03）：只报「比上一轮变差」、去重不堆叠、
#  已读不被空跑冲掉、发现器崩了回退固定样本（详见 test_platform_health_alerts.py 头）。
run_one test_platform_health_alerts.py

#  Cookie 守护进程 / Chromium 卡死守卫（2026-11-05）：B站 Chromium 不得把共享内存
#  放到 /tmp 系统盘（--disable-dev-shm-usage），后台收集必须有硬超时并在超时后清理
#  chromium，否则一个 D 状态渲染器就能把 2 核 VPS 拖到 iowait 90%、用户解析全线
#  「解析服务正忙」（详见 test_cookie_daemon_chromium_guard.py 头）。
run_one test_cookie_daemon_chromium_guard.py

#  持久 profile 缓存「容量闸」守卫（2026-11-07）：只删 48h 前文件不够 —— 短命会话
#  一天就能写出上百 MB（weibo_profile 实测 103MB、bili_profile 曾 768MB/24h），
#  等不到超龄就把 1.6GB 小机器拖成 iowait 雪崩。必须按 cap 削峰且不误伤登录态
#  （详见 test_profile_cache_cap.py 头）。
run_one test_profile_cache_cap.py

echo ""
echo "========================================="
echo "  通过: $PASS   失败: $FAIL"
echo "========================================="
if [ "$FAIL" -gt 0 ]; then
  echo "❌ 存在失败用例，构建不应发布"
  exit 1
fi
echo "✅ 全部离线测试通过"
