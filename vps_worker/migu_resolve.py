#!/usr/bin/env python3
"""咪咕视频（miguvideo.com）解析 —— ECS worker，纯 HTTP 版（无浏览器、无需登录态）。

链路（2026-11-14 实测闭环，见当日日志）：
  1) 从 URL 提取 contId
  2) GET ``webapi.miguvideo.com/gateway/playurl/v3/play/playurl?contId=..``
     带官方头 Appcode/Appid/Channel/x-up-client-channel-id → 返回明文 m3u8
     （``body.urlInfo.url``）。**游客态（urlType=tourist）即可，不需要 Cookie**。
     注：v2 已 404；v3 是当前有效版本。
  3) 明文 URL 直接访问会回咪咕自定义码 **661**（缺签名），必须用咪咕自己的
     ``mgprtcl.wasm`` 算出 ``ddCalcu`` 参数拼上去（wasmtime 在 Python 侧执行，
     盐值 +6e7/UkVh9XuFSTC5k08Qw==、sv=10012）。
  4) 带签名的 gslb URL 会 302 到具体 CDN 节点；跟随重定向得到最终可播 m3u8
     （标准 #EXTM3U + ts 分片）。

为什么弃用 Playwright 抓流方案：
  竖屏页在 headless 下会 about:blank（目标页零网络请求，非跨设备跳转拦截器），
  且 spa 取流依赖 WASM 签名网关，冷启动时序/账号频率都会造成间歇失败。
  纯 HTTP 方案绕开全部浏览器竞态，服务端稳定、毫秒级、无账号风控。

🔴 CDN 不绑来源 IP：同一签名 URL 从 ECS（与生成端完全不同的 IP）实测
   HTTP 200 + 12 个 ts 分片；故服务端解析 + 任意端下载成立。

wasm 文件固化在 ``/opt/vdl-worker/mgprtcl.wasm``
（版本 v_20251124155500_63c16e66，29978 字节；算法底层未变，固定版本比追新版稳）。
文件缺失时自动从咪咕补下。依赖：``wasmtime``（已装在 worker .venv）。

对外 ``resolve(url, timeout=30, cookie="")`` 供 vdl_cookie_daemon.py 复用，
返回 dict 或抛 RuntimeError，契约与 finder/lespark 一致。
"""
import ctypes
import json
import os
import re
import sys
import urllib.parse
import urllib.request

WASM_PATH = "/opt/vdl-worker/mgprtcl.wasm"
WASM_VERSION = "v_20251124155500_63c16e66"
WASM_URL = ("https://www.miguvideo.com/mgs/player/prd/"
            + WASM_VERSION + "/dist/mgprtcl.wasm")
SALT = "+6e7/UkVh9XuFSTC5k08Qw=="
SV = "10012"
CHANNEL_ID = "0132_10010001005"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/89.0.4343.0 Safari/537.36 Edg/89.0.727.0")
REFERER = "https://www.miguvideo.com/"
COOKIE_FILE = "/opt/vdl-worker/cookies/migu.txt"

_HEADERS = {
    "User-Agent": UA,
    "Appcode": "miguvideo_default_www",
    "Appid": "miguvideo",
    "Channel": "H5",
    "x-up-client-channel-id": CHANNEL_ID,
    "Referer": REFERER,
}


def _log(msg: str) -> None:
    print("[migu] " + msg, file=sys.stderr, flush=True)


def _opener():
    # 不读系统代理（WorkBuddy / ECS 上都可能注入 HTTP_PROXY 导致 SSL EOF）
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _vid(url: str) -> str:
    m = re.search(r"/(?:p/(?:vertical|detail)|m/play|p/play)/([0-9a-zA-Z_]+)", url or "")
    if m:
        return m.group(1)
    m = re.search(r"[?&](?:cid|contId|pid)=([0-9a-zA-Z_]+)", url or "")
    return m.group(1) if m else ""


def _cookie_header(cookie_text: str = "") -> str:
    """可选：会员/1080P 内容才需要登录态；游客态公开视频不需要。"""
    raw = (cookie_text or "").strip()
    if not raw and os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                raw = f.read().strip()
        except Exception:
            raw = ""
    if not raw:
        return ""
    if "\t" in raw.splitlines()[0]:  # Netscape jar
        pairs = []
        for line in raw.splitlines():
            if not line or line.startswith("#"):
                continue
            p = line.split("\t")
            if len(p) >= 7 and ("miguvideo.com" in p[0].lower() or "cmvideo" in p[0].lower()):
                pairs.append("%s=%s" % (p[5], p[6]))
        return "; ".join(pairs)
    return raw


def _plain_url(vid: str, rate: int = 3, cookie: str = "") -> tuple:
    """调 v3 播放接口，返回 (明文 m3u8 URL, 标题)。"""
    q = urllib.parse.urlencode({
        "contId": vid, "rateType": str(rate), "xh265": "true",
        "chip": "mgwww", "channelId": CHANNEL_ID,
    })
    api = "https://webapi.miguvideo.com/gateway/playurl/v3/play/playurl?" + q
    hdr = dict(_HEADERS)
    ck = _cookie_header(cookie)
    if ck:
        hdr["Cookie"] = ck
    req = urllib.request.Request(api, headers=hdr)
    with _opener().open(req, timeout=20) as r:
        d = json.loads(r.read().decode("utf-8", "ignore"))
    body = d.get("body") or {}
    info = body.get("urlInfo") or {}
    plain = info.get("url") or ""
    if not plain:
        raise RuntimeError("咪咕播放接口未返回地址（%s / %s）；视频可能下架、需会员或仅限 App。"
                           % (d.get("code"), d.get("message")))
    title = ((body.get("content") or {}).get("contName")
             or (body.get("content") or {}).get("detail") or "咪咕视频")
    return plain, title


def _ensure_wasm() -> bytes:
    if not os.path.exists(WASM_PATH):
        _log("wasm 缺失，从咪咕补下 " + WASM_VERSION)
        req = urllib.request.Request(WASM_URL, headers={"User-Agent": UA})
        with _opener().open(req, timeout=25) as r:
            wb = r.read()
        with open(WASM_PATH, "wb") as f:
            f.write(wb)
    with open(WASM_PATH, "rb") as f:
        return f.read()


def _sign(plain: str) -> str:
    """用咪咕 mgprtcl.wasm 计算 ddCalcu，返回带签名的 gslb URL。"""
    from wasmtime import Engine, Store, Module, Linker

    engine = Engine()
    module = Module(engine, _ensure_wasm())
    store = Store(engine)
    linker = Linker(engine)
    st = {"mem": None, "u8": None, "u32": None}

    def view():
        mem = st["mem"]
        if mem is None:
            return
        ptr = mem.data_ptr(store)
        size = mem.data_len(store)
        addr = ctypes.addressof(ptr.contents)
        st["u8"] = (ctypes.c_uint8 * size).from_address(addr)
        st["u32"] = (ctypes.c_uint32 * (size // 4)).from_address(addr)

    # wasm 导入函数（签名与模块声明一致：a/b 四参数，c 单参数，d 零参数）
    def imp_a(e, t, r, n):
        view()
        s = 0
        for _ in range(r):
            s += st["u32"][(t + 4) >> 2]
            t += 8
        st["u32"][n >> 2] = s
        return 0

    def imp_b(e, t, r, n):
        return None

    def imp_c(e):
        return 0

    def imp_d():
        return 1

    fns = {"a": imp_a, "b": imp_b, "c": imp_c, "d": imp_d}
    for imp in module.imports:
        linker.define_func(imp.module, imp.name, imp.type, fns[imp.name])

    inst = linker.instantiate(store, module)
    ex = inst.exports(store)
    st["mem"] = ex["e"]
    view()

    def s2u(s: str, off: int) -> None:
        view()
        b = s.encode("utf-8")
        for i, ch in enumerate(b):
            st["u8"][off + i] = ch
        st["u8"][off + len(b)] = 0

    def u2s(off: int) -> str:
        view()
        out = bytearray()
        while st["u8"][off]:
            out.append(st["u8"][off])
            off += 1
        return out.decode("utf-8", "ignore")

    q = urllib.parse.parse_qs(urllib.parse.urlparse(plain).query)
    get = lambda k: (q.get(k) or [""])[0]
    vals = [get("userid"), get("timestamp"), get("ProgramID"),
            get("Channel_ID"), get("puData"), SALT]

    # 新版 wasmtime 的导出函数签名是 func(store, *args)
    def call(name, *args):
        return ex[name](store, *args)

    ptrs = [call("v", len(v) + 1) for v in vals]
    call("v", 64)
    t_ptr = call("v", 128)
    w_ptr = call("v", 128)
    for v, off in zip(vals, ptrs):
        s2u(v, off)

    S = call("n")
    call("i", S, ptrs[2], len(vals[2]))   # ProgramID
    call("r", S, ptrs[1], len(vals[1]))   # timestamp
    call("q", S, ptrs[0], len(vals[0]))   # userid
    call("k", S, 0, 0)
    call("s", S, 0, 0)
    call("p", S, ptrs[4], len(vals[4]))   # puData
    call("j", S, ptrs[3], len(vals[3]))   # Channel_ID
    call("u", S, ptrs[5], len(vals[5]), w_ptr, 128)

    inter = u2s(w_ptr)
    i_ptr = call("v", len(inter) + 1)
    s2u(inter, i_ptr)
    call("o", S, i_ptr, len(inter))
    call("l", S, t_ptr, 128)
    dd = u2s(t_ptr)
    if not dd:
        raise RuntimeError("咪咕 WASM 签名返回空（wasm 版本或盐值可能已更新）")
    return plain + "&ddCalcu=" + urllib.parse.quote(dd, safe="") + "&sv=" + SV


def _final_url(signed: str) -> tuple:
    """跟随 gslb 302，返回最终 200 的节点 URL；失败则退回带签名的 gslb URL。"""
    try:
        req = urllib.request.Request(signed, headers={"User-Agent": UA, "Referer": REFERER})
        with _opener().open(req, timeout=20) as r:
            body = r.read(4096).decode("utf-8", "ignore")
            final = r.geturl()
        if "#EXTM3U" in body or ".ts" in body:
            _log("GSLB -> %s" % urllib.parse.urlparse(final).netloc)
            return final, True
        _log("GSLB 返回非清单(%s)，退回 gslb URL" % r.status)
    except Exception as e:  # noqa: BLE001
        _log("GSLB 跟随失败(退回 gslb URL): %s" % str(e)[:120])
    return signed, False


def resolve(url: str, timeout: int = 30, cookie: str = "") -> dict:
    vid = _vid(url)
    if not vid:
        raise RuntimeError("无法从链接识别咪咕视频 ID（支持 /p/vertical/<id>、/p/detail/<id>、?cid=<id>）。")

    plain, title = _plain_url(vid, rate=3, cookie=cookie)
    _log("contId=%s title=%s plain=%d bytes" % (vid, title[:30], len(plain)))
    signed = _sign(plain)
    _log("ddCalcu 已生成")
    final, resolved = _final_url(signed)

    return {
        "ok": True,
        "title": title,
        "duration": None,
        "video_id": vid,
        "video_url": final,
        "webpage_url": url,
        "thumbnail": "",
        "ext": "mp4",
        "protocol": "m3u8",
        "gslb_url": signed,          # 备用：未跟随 302 的官方调度入口
        "gslb_resolved": resolved,
        "http_headers": {"Referer": REFERER, "User-Agent": UA},
    }


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else ""
    ck = sys.argv[2] if len(sys.argv) > 2 else ""
    if not u:
        print("用法: python migu_resolve.py <咪咕链接> [cookie文本]", file=sys.stderr)
        sys.exit(1)
    try:
        print(json.dumps(resolve(u, cookie=ck), ensure_ascii=False, indent=2))
    except Exception as e:  # noqa: BLE001
        print("FAIL:", str(e), file=sys.stderr)
        sys.exit(2)
