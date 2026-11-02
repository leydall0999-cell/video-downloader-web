#!/usr/bin/env python3
"""网关体积上限守卫：网页版分片上传不许再被 nginx 413 拦死（2026-09-29）。

背景（用户截图为证，已公网复现）
--------------------------------
用户网页版「视频处理 → 拼接」添加两个片段，两个都变成
    「失败：分片 2/8 上传失败：分片上传失败 HTTP 413」

复现与根因
----------
    $ curl -X POST https://hanyuxz.top/api/upload-chunk -F file=@33MB.bin
    http=413  <html><head><title>413 Request Entity Too Large</title></head>…
              <hr><center>nginx/1.18.0 (Ubuntu)</center>            ← 注意这是 nginx 的 HTML 页

- 2026-09-26 域名 `hanyuxz.top` 从「CF 回源 Railway」切成「CF 命名隧道 → ECS nginx:8888」；
- ECS 上 `vdl-gateway` 的 **server 级 `client_max_body_size` 是 8m**，而只有
  `= /su` 和 `= /api/upload`（扫码分享）两条精确 location 放宽到了 2048m；
- 网页版**所有**大文件上传都走 `/api/upload-chunk`（格式转换 / 视频拼接 / 音乐转换 /
  字幕识别共用），前端每片 32MB（>2GB 文件 64MB）→ 命中 `location /` 继承的 8m
  → **每一片都在到达应用之前被 413**，整条上传链路全废。

为什么之前没暴露：切隧道前 hanyuxz.top 直连 Railway，压根没有这层 nginx。

本测试钉住的契约
----------------
1. `deploy/nginx-vdl-gateway.cn.conf`（VPS `/etc/nginx/sites-enabled/vdl-gateway` 的
   版本化副本）里，`/api/upload-chunk` 及其子路径的有效体积上限 ≥ 后端单块上限
   `app.UPLOAD_CHUNK_MAX`（64MB），且该 location 必须 `proxy_request_buffering off`
   —— 但 `location /` 保持默认缓冲（应用有大量「读 body 前就早退」的 401/403 端点，
     全局关缓冲会让这些早退变成上游提前关连接）；
2. server 级上限 ≥ 256MB：给「整文件上传」类端点（/api/commentary、/api/convert、
   /api/dewatermark）兜底，避免同类事故换个端点再犯一次；
3. `/gw/`（LLM 网关）必须显式保留较紧上限，不能因 server 级放宽而顺手放开；
4. `/su`、`/api/upload`（扫码分享，2048m）与精确匹配语义不得被破坏；
5. 前端 `web/app.js` 的 `UC_UPLOAD_ENDPOINTS` **只允许同源**：曾经的第二条
   `web-production-b9993.up.railway.app` 自 2026-09-11 已死，而选路在样本不足时按
   奇偶分流 ⇒ 每个奇数下标分片必然发到死主机（用户看到的「分片 2/8」就是 i=1）。
   新增端点必须与主站同后端、同分片存储，否则分片会落到别的节点磁盘、finish 必报
   「分片不完整」——宁可单端，也不要不同源的多端。
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(SERVER_DIR)

CONF_PATH = os.path.join(REPO, "deploy", "nginx-vdl-gateway.cn.conf")
APP_JS = os.path.join(REPO, "web", "app.js")

MB = 1024 * 1024
INF = float("inf")


# --------------------------------------------------------------------------- #
# 极简 nginx 配置解析：只取「server 级指令」与「location 块 + 块内指令」
# --------------------------------------------------------------------------- #
def _parse_directives(block_lines):
    """把一段 nginx 配置行解析成 [(name, args)]，跳过注释与空行。"""
    out = []
    for raw in block_lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.rstrip(";").split()
        if parts:
            out.append((parts[0], parts[1:]))
    return out


def parse_conf(path):
    """→ {'server': [(name,args)], 'locations': [(modifier, pattern, [(name,args)])]}

    用花括号栈逐行解析：深度 1 且不在 location 内的指令 = server 级；
    每个 location 块单独收集。按「行值去重」的做法是错的（多条 location 里
    有字面相同的指令行，会把 server 级行也一起删掉）。
    """
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    server_dirs = []
    locations = []
    cur = None            # 当前 location 块 {modifier, pattern, directives}
    depth = 0             # server 块内深度（server { 之后为 1）

    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("location") and line.endswith("{"):
            rest = line[:-1].strip().split()[1:]
            if rest and rest[0] in ("=", "^~", "~", "~*"):
                modifier, pattern = rest[0], (rest[1] if len(rest) > 1 else "")
            else:
                modifier, pattern = "", (rest[0] if rest else "")
            cur = {"modifier": modifier, "pattern": pattern.strip('"'), "directives": []}
            depth += 1
            continue
        if line.endswith("{"):
            depth += 1            # server { / http { 等
            continue
        if line == "}":
            if cur is not None and depth == 2:
                locations.append((cur["modifier"], cur["pattern"], cur["directives"]))
                cur = None
            depth -= 1
            continue
        directive = _parse_directives([line])
        if cur is not None:
            cur["directives"] += directive
        elif depth == 1:
            server_dirs += directive

    return {"server": server_dirs, "locations": locations}


def _limit_bytes(dirvalue):
    """client_max_body_size 的值 → 字节数（0 = 不限制 = inf）。"""
    v = dirvalue.strip().lower()
    if v == "0":
        return INF
    m = re.fullmatch(r"(\d+)([kmg]?)", v)
    assert m, "无法解析 client_max_body_size=%r" % dirvalue
    n = int(m.group(1))
    return n * {"": 1, "k": 1024, "m": MB, "g": 1024 * MB}[m.group(2)]


def pick_location(conf, path):
    """按 nginx 匹配规则挑出处理 `path` 的 location。

    nginx 顺序：① `=` 精确匹配最高优先；② `^~` 前缀匹配命中即用（不再看正则）；
    ③ 正则 location 按书写顺序；④ 无正则命中则用最长前缀匹配。
    本文件里正则只做 404/代理分享路径，够用。
    """
    locs = conf["locations"]
    for modifier, pattern, directives in locs:            # ① 精确
        if modifier == "=" and pattern == path:
            return pattern, directives
    best = None
    for modifier, pattern, directives in locs:            # ② ^~ 前缀（最长优先）
        if modifier == "^~" and path.startswith(pattern):
            if best is None or len(pattern) > len(best[0]):
                best = (pattern, directives)
    if best:
        return best
    for modifier, pattern, directives in locs:            # ③ 正则（按书写顺序）
        if modifier in ("~", "~*"):
            flags = re.I if modifier == "~*" else 0
            if re.search(pattern, path, flags):
                return pattern, directives
    best = None
    for modifier, pattern, directives in locs:            # ④ 最长前缀
        if modifier in ("", "^~") and path.startswith(pattern):
            if best is None or len(pattern) > len(best[0]):
                best = (pattern, directives)
    return best or ("", conf["server"])


def effective_limit(conf, path):
    """→ (上限字节数, 命中的 location pattern, 该块指令)。块内没写就回退 server 级。"""
    pattern, directives = pick_location(conf, path)
    for name, args in directives:
        if name == "client_max_body_size":
            return _limit_bytes(args[0]), pattern, directives
    for name, args in conf["server"]:
        if name == "client_max_body_size":
            return _limit_bytes(args[0]), pattern, directives
    return INF, pattern, directives


def _has_directive(directives, name, value=None):
    for n, args in directives:
        if n == name and (value is None or (args and args[0] == value)):
            return True
    return False


# --------------------------------------------------------------------------- #
# 测试 1：解析器本身要对（挑 location 的规则错了，后面全是假的）
# --------------------------------------------------------------------------- #
def test_resolver_rules():
    conf = parse_conf(CONF_PATH)

    # 精确匹配优先于前缀（/api/upload 有专属块，不能被 location / 吞掉）
    limit, pattern, _ = effective_limit(conf, "/api/upload")
    assert pattern == "/api/upload", "精确匹配失效：/api/upload 落到 %r" % pattern
    assert limit >= 2048 * MB, "/api/upload 上限被改小：%s" % limit

    # 前缀匹配：/api/upload-chunk 及其子路径都归它
    for p in ("/api/upload-chunk", "/api/upload-chunk/finish", "/api/upload-chunk/abort"):
        _, pat, _ = effective_limit(conf, p)
        assert pat == "/api/upload-chunk", "%s 落到 %r（应归 /api/upload-chunk）" % (p, pat)

    # 普通网页版路径仍走 location /
    _, pat, _ = effective_limit(conf, "/api/version")
    assert pat == "/", "/api/version 落到 %r" % pat

    # 分享相关的正则 location 没被新前缀块抢走（正则以 ^ 开头，用 search 判可匹配）
    _, pat, _ = effective_limit(conf, "/f/abc123.mp4")
    assert pat == "/f/", "/f/ 落到 %r" % pat
    _, pat, _ = effective_limit(conf, "/api/share/abc123")
    assert pat.startswith("^/api/share/"), "/api/share 落到 %r" % pat
    assert re.search(pat, "/api/share/abc123"), "正则 %r 居然匹配不上样例路径" % pat
    print("✅ 解析器：精确 / ^~ 前缀 / 正则 / 最长前缀 四档规则都正确")


# --------------------------------------------------------------------------- #
# 测试 2：分片上传端点必须放得下 64MB 分片（本次事故的正面防线）
# --------------------------------------------------------------------------- #
def test_chunk_upload_limit_allows_max_chunk():
    conf = parse_conf(CONF_PATH)
    # 后端单块上限（app.UPLOAD_CHUNK_MAX）；前端 >2GB 文件正是按 64MB 切
    chunk_max = 64 * MB
    for path in ("/api/upload-chunk", "/api/upload-chunk/finish"):
        limit, pattern, directives = effective_limit(conf, path)
        assert limit >= chunk_max, (
            "%s 的有效上限只有 %s，小于后端单块上限 64MB —— 分片会在到达应用前被 413\n"
            "（命中的 location：%r）" % (path, limit, pattern))
        assert _has_directive(directives, "proxy_request_buffering", "off"), (
            "%s 必须 proxy_request_buffering off（32MB 先落临时盘再转发是白白翻倍磁盘 I/O）"
            % path)

    # 反向断言：不能靠「全局关掉请求缓冲」蒙过去 —— location / 上大量端点会先早退
    # （401/403/402），关缓冲会让这些早退变成上游提前关连接
    _, pattern, directives = effective_limit(conf, "/api/version")
    assert pattern == "/", "/api/version 不该命中分片块：%r" % pattern
    assert not _has_directive(directives, "proxy_request_buffering", "off"), (
        "location / 上不应开 proxy_request_buffering off —— 早退端点会受影响；"
        "大文件上传请单独放行一条 location")
    print("✅ /api/upload-chunk 有效上限 ≥ 64MB，且流式转发；location / 保持原样")


# --------------------------------------------------------------------------- #
# 测试 3：server 级上限要给「整文件上传」类端点兜底，但 /gw/ 必须保持紧
# --------------------------------------------------------------------------- #
def test_server_level_limit_and_gateway_stays_tight():
    conf = parse_conf(CONF_PATH)
    limit, pattern, _ = effective_limit(conf, "/api/commentary")
    assert pattern == "/", " /api/commentary 应走 location /，实际 %r" % pattern
    assert limit >= 256 * MB, (
        "server 级上限只有 %s：/api/commentary、/api/convert、/api/dewatermark 这类"
        "整文件上传端点会在 nginx 层被 413（当年 8m 就是这么坑的）" % limit)

    gw, gw_pattern, _ = effective_limit(conf, "/gw/v1/chat/completions")
    assert gw >= 1 * MB, "/gw/ 上限异常小：%s" % gw
    # 上限 64MB → 128MB（2026-11-03 同步）：2026-11-01 `93b5b6d` 为「抠图要传图」
    # 把 /gw/ 从 16m 显式抬到 128m（线上 /etc/nginx/sites-enabled/vdl-gateway 同值）。
    # 这条断言真正要守的是「/gw/ 有**显式**紧上限、不继承 server 级 1024m」，
    # 所以保留硬上限、只把数字对齐现状；再被抬到与 server 级同量级时仍会报红。
    assert gw <= 128 * MB, (
        "/gw/（LLM 网关）上限被放开到 %s —— 超过 128MB 说明 server 级放宽时忘了给它写回紧上限" % gw)
    print("✅ server 级上限给整文件上传兜底，/gw/ 仍保持紧上限")


# --------------------------------------------------------------------------- #
# 测试 4：前端上传端点必须同源（死端点 + 奇偶分流 = 一半分片必失败）
# --------------------------------------------------------------------------- #
def test_frontend_upload_endpoints_same_origin_only():
    """前端上传端点必须**同后端**（死端点 / 跨节点端点 = 一半分片必失败）。

    🔴 2026-10-22 契约已变更：上传提速改造后 `UC_UPLOAD_ENDPOINTS` 从 `const`
    改成 `let`（多端口探测结果会回填），候选由 `ucEndpointCandidates()` 动态生成。
    原断言写死 `const UC_UPLOAD_ENDPOINTS = [...]` 已失效 —— 留着就是永远红的
    陈旧测试。**不变量没变**，变的是形态，所以这里按新结构重写：

      旧：端点数组里每一项都必须是 location.origin（只能单同源）
      新：① 池子初值仍是 [location.origin]；
          ② 动态候选必须全部满足「同协议 + 同主机名或白名单备用主机名」，只允许换端口；
          ③ 绝不能出现**另一个物理节点**（hk.hanyuxz.top）或裸 IP ——
             那会把分片写到别的机器磁盘上，finish 必报「分片不完整」
             （2026-09-11 Railway 事故的根因）。
    """
    with open(APP_JS, encoding="utf-8") as fh:
        src = fh.read()

    # ① 池子初值仍是单一同源
    m = re.search(r"let UC_UPLOAD_ENDPOINTS = \[([^\]]*)\]", src)
    assert m, "web/app.js 里找不到 let UC_UPLOAD_ENDPOINTS（多端口改造后应为 let）"
    entries = [e.strip() for e in m.group(1).split(",") if e.strip()]
    assert entries, "UC_UPLOAD_ENDPOINTS 为空，分片没有可用的上传端点"
    for e in entries:
        assert e == "location.origin", (
            "端点池初值出现了非同源项 %r。初值必须只有同源，其余端点由探测后回填。" % e)

    # ② 取候选生成函数体，逐个校验它产出的端点形态
    fn = re.search(r"const ucEndpointCandidates = \(\) => \{(.*?)\n  \};", src, re.S)
    assert fn, "web/app.js 里找不到 ucEndpointCandidates()（多端口提速的核心）"
    body = fn.group(1)
    # 注释里恰恰写着「🔴 不要放宽到 hk.hanyuxz.top」这类禁令，若不剥掉行注释，
    # 禁令本身会被当成违规命中。先剥注释，再校验真实代码。
    code = "\n".join(re.sub(r"//.*$", "", ln) for ln in body.split("\n"))

    # 主机白名单：只能是同主机名或显式声明的备用主机名
    allowed = set(re.findall(r"const (?:host|secondHost) = '([^']+)'", code))
    allowed.add(re.findall(r"location\.hostname !== '([^']+)'", code)[0])
    assert allowed, "解析不出主机白名单，无法校验候选端点"

    # 🔴 跨物理节点禁令
    for banned in ("hk.hanyuxz.top", "hk_", "47.82.101.79"):
        assert banned not in code, (
            "候选端点里出现了 %r —— 那是**另一个物理节点**，分片会落到另一台机器的"
            "磁盘上，finish 必报「分片不完整」（2026-09-11 Railway 事故同款）。" % banned)

    # 候选端口必须来自写死的 CF 边缘 HTTPS 端口表，不得出现任意端口
    ports = re.search(r"for \(const p of \[([0-9,\s]+)\]\)", code)
    assert ports, "候选端点没有走写死的端口表（不得随意拼端口）"
    port_list = {int(x) for x in ports.group(1).replace(" ", "").split(",") if x}
    assert port_list and port_list <= {443, 2053, 2083, 2087, 2096, 8443}, (
        "候选端口 %s 不在 CF 边缘已验证的 HTTPS 端口集合内" % sorted(port_list))

    # 协议必须是 https（明文端口会让分片裸奔，且 CF 不转发）
    assert "location.protocol !== 'https:'" in code, (
        "候选端点没有 https 前置判断 —— 非 https 环境也去试非 443 端口会必然失败")

    print("✅ 前端上传端点仅同后端（初值单同源 + 候选同主机换端口，无跨节点端点）")


if __name__ == "__main__":
    test_resolver_rules()
    test_chunk_upload_limit_allows_max_chunk()
    test_server_level_limit_and_gateway_stays_tight()
    test_frontend_upload_endpoints_same_origin_only()
    print("\n🎉 网关体积上限守卫全部通过")
