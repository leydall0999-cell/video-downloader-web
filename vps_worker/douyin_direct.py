#!/usr/bin/env python3
"""抖音纯 HTTP 直连解析（a_bogus 签名），替代/兜底 Playwright 浏览器方案。

背景：douyin_resolve.py 走无头 Chromium，单条 20s+、内存重、且被全局解析锁
串行化。本模块按公开逆向算法（f2 项目，Apache 2.0，见文末出处）本地生成
a_bogus 签名，直接请求抖音 Web 详情接口，免 Cookie、免浏览器：
  1) ttwid：向字节统一注册接口 POST 一次拿到游客 ttwid；
  2) msToken：本地随机生成（游客态详情接口只校验 a_bogus，不校验 msToken 真伪）；
  3) a_bogus：对查询串做 SM3 哈希 + RC4 + 自定义 base64（算法内置，纯 Python）；
  4) GET /aweme/v1/web/aweme/detail/ 拿 aweme_detail JSON；
  5) 播放地址优先 play_addr 的 /aweme/v1/play/ 短链（跟随 302 得 douyinvod
     混合轨直链，自带音轨）；失败再扫 bit_rate 分离轨（video + audio 配对）。

输出契约与 douyin_resolve.resolve() 完全一致（ok/title/duration/video_url/
audio_url/video_has_audio/...），供 vdl_cookie_daemon 透明替换。

SM3 为国标 GB/T 32905-2016 哈希（离线自检见 _selftest）。
算法出处：https://github.com/Johnserf-Seed/f2/blob/main/f2/utils/abogus.py
（Apache License 2.0，本文件为其算法的精简适配实现，未改动签名数学。）
"""
import base64
import json
import random
import re
import time
import urllib.request
import urllib.error

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
_DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/"

_VID_RE = re.compile(r"/video/(\d{15,})")
_NOTE_RE = re.compile(r"/note/(\d{15,})")
_MODAL_RE = re.compile(r"[?&]modal_id=(\d{15,})")


class VideoNotFoundError(RuntimeError):
    """签名有效但视频确证不存在（已删/私密/短链过期）——无需回落浏览器。"""


class ArgusBlockedError(RuntimeError):
    """抖音 Argus 风控确定拦截（平台级，非临时故障）——回落 Playwright 也无效。"""


# ---------------------------------------------------------------- SM3（国标）
class _SM3:
    """GB/T 32905-2016 SM3 哈希，纯 Python（约 5μs/短串，够用）。"""

    _IV = [
        0x7380166F, 0x4914B2B9, 0x172442D7, 0xDA8A0600,
        0xA96F30BC, 0x163138AA, 0xE38DEE4D, 0xB0FB0E4E,
    ]
    _T = [0x79CC4519] * 16 + [0x7A879D8A] * 52

    @classmethod
    def hash_bytes(cls, data: bytes) -> bytes:
        # 填充：1 + k 个 0 + 64bit 长度
        msg = bytearray(data)
        bit_len = len(data) * 8
        msg.append(0x80)
        while len(msg) % 64 != 56:
            msg.append(0)
        msg += bit_len.to_bytes(8, "big")

        v = list(cls._IV)
        for off in range(0, len(msg), 64):
            b = msg[off:off + 64]
            w = [int.from_bytes(b[i * 4:i * 4 + 4], "big") for i in range(16)]
            for j in range(16, 68):
                # W[j] = P1(W[j-16] ^ W[j-9] ^ (W[j-3] <<< 15)) ^ (W[j-13] <<< 7) ^ W[j-6]
                x = w[j - 16] ^ w[j - 9] ^ cls._rot(w[j - 3], 15)
                x = x ^ cls._rot(x, 15) ^ cls._rot(x, 23)
                w.append(x ^ cls._rot(w[j - 13], 7) ^ w[j - 6])
            a, bb, c, d, e, f, g, h = v
            for j in range(64):
                if j < 16:
                    ff, gg = a ^ bb ^ c, e ^ f ^ g
                else:
                    ff = (a & bb) | (a & c) | (bb & c)
                    gg = (e & f) | ((e ^ 0xFFFFFFFF) & g)
                a12 = cls._rot(a, 12)
                ss1 = cls._rot((a12 + e + cls._rot(cls._T[j], j % 32)) & 0xFFFFFFFF, 7)
                ss2 = ss1 ^ a12
                tt1 = (ff + d + ss2 + (w[j] ^ w[j + 4])) & 0xFFFFFFFF
                tt2 = (gg + h + ss1 + w[j]) & 0xFFFFFFFF
                d = c
                c = cls._rot(bb, 9)
                bb = a
                a = tt1
                h = g
                g = cls._rot(f, 19)
                f = e
                e = cls._p0(tt2)
            v = [(x ^ y) & 0xFFFFFFFF for x, y in zip(v, [a, bb, c, d, e, f, g, h])]
        return b"".join(x.to_bytes(4, "big") for x in v)

    @staticmethod
    def _rot(x, n):
        return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF

    @staticmethod
    def _p0(x):
        return x ^ (((x << 9) | (x >> 23)) & 0xFFFFFFFF) ^ (((x << 17) | (x >> 15)) & 0xFFFFFFFF)


# ---------------------------------------------------------------- ABogus
class ABogus:
    """a_bogus 签名生成（f2 算法精简适配，签名数学未改动）。"""

    SALT = "cus"
    UA_KEY = b"\x00\x01\x0e"
    CHAR = "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe"
    CHAR2 = "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe"

    def __init__(self, user_agent: str = UA):
        self.ua = user_agent or UA
        self.big_array = list(range(256))
        # f2 big_array（原样保留，改动会破坏签名）
        self.big_array = [
            121, 243, 55, 234, 103, 36, 47, 228, 30, 231, 106, 6, 115, 95, 78, 101, 250, 207, 198, 50,
            139, 227, 220, 105, 97, 143, 34, 28, 194, 215, 18, 100, 159, 160, 43, 8, 169, 217, 180, 120,
            247, 45, 90, 11, 27, 197, 46, 3, 84, 72, 5, 68, 62, 56, 221, 75, 144, 79, 73, 161,
            178, 81, 64, 187, 134, 117, 186, 118, 16, 241, 130, 71, 89, 147, 122, 129, 65, 40, 88, 150,
            110, 219, 199, 255, 181, 254, 48, 4, 195, 248, 208, 32, 116, 167, 69, 201, 17, 124, 125, 104,
            96, 83, 80, 127, 236, 108, 154, 126, 204, 15, 20, 135, 112, 158, 13, 1, 188, 164, 210, 237,
            222, 98, 212, 77, 253, 42, 170, 202, 26, 22, 29, 182, 251, 10, 173, 152, 58, 138, 54, 141,
            185, 33, 157, 31, 252, 132, 233, 235, 102, 196, 191, 223, 240, 148, 39, 123, 92, 82, 128, 109,
            57, 24, 38, 113, 209, 245, 2, 119, 153, 229, 189, 214, 230, 174, 232, 63, 52, 205, 86, 140,
            66, 175, 111, 171, 246, 133, 238, 193, 99, 60, 74, 91, 225, 51, 76, 37, 145, 211, 166, 151,
            213, 206, 0, 200, 244, 176, 218, 44, 184, 172, 49, 216, 93, 168, 53, 21, 183, 41, 67, 85,
            224, 155, 226, 242, 87, 177, 146, 70, 190, 12, 162, 19, 137, 114, 25, 165, 163, 192, 23, 59,
            9, 94, 179, 107, 35, 7, 142, 131, 239, 203, 149, 136, 61, 249, 14, 156,
        ]

    # --- 基础件 ---
    @staticmethod
    def _sm3_arr(data) -> list:
        if isinstance(data, str):
            data = data.encode("utf-8")
        return list(_SM3.hash_bytes(bytes(data)))

    def _params_hash(self, param, add_salt=True):
        if isinstance(param, str) and add_salt:
            param = param + self.SALT
        return self._sm3_arr(param)

    def _b64(self, s: str, alphabet: str = CHAR) -> str:
        binary = "".join("{:08b}".format(ord(c)) for c in s)
        pad = (6 - len(binary) % 6) % 6
        binary += "0" * pad
        idx = [int(binary[i:i + 6], 2) for i in range(0, len(binary), 6)]
        return "".join(alphabet[i] for i in idx) + "=" * (pad // 2)

    def _ab64(self, s: str, alphabet: str = CHAR) -> str:
        out = []
        for i in range(0, len(s), 3):
            if i + 2 < len(s):
                n = (ord(s[i]) << 16) | (ord(s[i + 1]) << 8) | ord(s[i + 2])
            elif i + 1 < len(s):
                n = (ord(s[i]) << 16) | (ord(s[i + 1]) << 8)
            else:
                n = ord(s[i]) << 16
            for j, k in zip(range(18, -1, -6), (0xFC0000, 0x03F000, 0x0FC0, 0x3F)):
                if j == 6 and i + 1 >= len(s):
                    break
                if j == 0 and i + 2 >= len(s):
                    break
                out.append(alphabet[(n & k) >> j])
        out.append("=" * ((4 - len(out) % 4) % 4))
        return "".join(out)

    @staticmethod
    def _rc4(key: bytes, text: str) -> bytes:
        sbox = list(range(256))
        j = 0
        for i in range(256):
            j = (j + sbox[i] + key[i % len(key)]) % 256
            sbox[i], sbox[j] = sbox[j], sbox[i]
        i = j = 0
        out = bytearray()
        for c in text:
            i = (i + 1) % 256
            j = (j + sbox[i]) % 256
            sbox[i], sbox[j] = sbox[j], sbox[i]
            out.append(ord(c) ^ sbox[(sbox[i] + sbox[j]) % 256])
        return bytes(out)

    def _transform(self, byte_list) -> str:
        arr = self.big_array
        s = "".join(chr(c) for c in byte_list)
        res = []
        idx_b = arr[1]
        initial = 0
        value_e = 0
        for i, ch in enumerate(s):
            if i == 0:
                initial = arr[idx_b]
                s0 = idx_b + initial
                arr[1] = initial
                arr[idx_b] = idx_b
            else:
                s0 = initial + value_e
            s0 %= 256
            vf = arr[s0]
            res.append(chr(ord(ch) ^ vf))
            value_e = arr[(i + 2) % 256]
            s0 = (idx_b + value_e) % 256
            initial = arr[s0]
            arr[s0] = arr[(i + 2) % 256]
            arr[(i + 2) % 256] = initial
            idx_b = s0
        return "".join(res)

    @staticmethod
    def _rand_bytes() -> str:
        out = []
        for _ in range(3):
            rd = int(random.random() * 10000)
            out += [
                chr(((rd & 255) & 170) | 1),
                chr(((rd & 255) & 85) | 2),
                chr((((rd >> 8) & 255) & 170) | 5),
                chr((((rd >> 8) & 255) & 85) | 40),
            ]
        return "".join(out)

    # 对外：GET 请求生成 a_bogus（options=[0,1,8]）
    def generate(self, params: str, body: str = "") -> str:
        self.big_array = __class__.big_array_backup if hasattr(__class__, "big_array_backup") else list(self.big_array)
        sort_index = [18, 20, 52, 26, 30, 34, 58, 38, 40, 53, 42, 21, 27, 54, 55, 31, 35, 57, 39,
                      41, 43, 22, 28, 32, 60, 36, 23, 29, 33, 37, 44, 45, 59, 46, 47, 48, 49, 50,
                      24, 25, 65, 66, 70, 71]
        sort_index2 = [18, 20, 26, 30, 34, 38, 40, 42, 21, 27, 31, 35, 39, 41, 43, 22, 28, 32, 36,
                       23, 29, 33, 37, 44, 45, 46, 47, 48, 49, 50, 24, 25, 52, 53, 54, 55, 57, 58,
                       59, 60, 65, 66, 70, 71]
        fp = "%d|%d|%d|%d|0|%d|0|0|%d|%d|%d|%d|%d|%d|24|24|Win32" % (
            (iw := random.randint(1024, 1920)), (ih := random.randint(768, 1080)),
            iw + random.randint(24, 32), ih + random.randint(75, 90),
            random.choice([0, 30]),
            (sw := random.randint(1024, 1920)), (sh := random.randint(768, 1080)),
            random.randint(1280, 1920), random.randint(800, 1080), iw, ih)
        start = int(time.time() * 1000)
        array1 = self._params_hash(self._params_hash(params))
        array2 = self._params_hash(self._params_hash(body))
        array3 = self._params_hash(self._b64(self._rc4(self.UA_KEY, self.ua).decode("latin-1"), self.CHAR2),
                                   add_salt=False)
        end = int(time.time() * 1000)
        d = {8: 3, 18: 44, 19: [1, 0, 1, 0, 1], 66: 0, 69: 0, 70: 0, 71: 0,
             20: (start >> 24) & 255, 21: (start >> 16) & 255, 22: (start >> 8) & 255, 23: start & 255,
             24: int(start / 256 / 256 / 256 / 256), 25: int(start / 256 / 256 / 256 / 256 / 256),
             26: 0, 27: 0, 28: 0, 29: 0,                      # options[0]=0
             30: 0, 31: 1, 32: 0, 33: 0,                      # options[1]=1（GET）
             34: 0, 35: 0, 36: 0, 37: 8,                      # options[2]=8（GET）
             38: array1[21], 39: array1[22], 40: array2[21], 41: array2[22],
             42: array3[23], 43: array3[24],
             44: (end >> 24) & 255, 45: (end >> 16) & 255, 46: (end >> 8) & 255, 47: end & 255,
             48: 3, 49: int(end / 256 / 256 / 256 / 256), 50: int(end / 256 / 256 / 256 / 256 / 256),
             51: 0, 52: 0, 53: 0, 54: 0, 55: 0,               # pageId=0
             56: 6383, 57: 6383 & 255, 58: (6383 >> 8) & 255, 59: (6383 >> 16) & 255, 60: (6383 >> 24) & 255,
             64: len(fp), 65: len(fp)}
        values = [d.get(i, 0) for i in sort_index]
        values += [ord(c) for c in fp]
        ab_xor = 0
        for i in range(len(sort_index2) - 1):
            if i == 0:
                ab_xor = d.get(sort_index2[i], 0)
            ab_xor ^= d.get(sort_index2[i + 1], 0)
        values.append(ab_xor)
        raw = self._rand_bytes() + self._transform(values)
        return self._ab64(raw, self.CHAR)


# ---------------------------------------------------------------- 解析
def _ms_token() -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(random.choice(alphabet) for _ in range(116))


def _ttwid(timeout: int = 10) -> str:
    body = json.dumps({
        "region": "cn", "aid": 1768, "needFid": False, "service": "www.ixigua.com",
        "migrate_info": {"ticket": "", "source": "node"}, "cbUrlProtocol": "https", "union": True,
    }).encode()
    req = urllib.request.Request(
        "https://ttwid.bytedance.com/ttwid/union/register/", data=body,
        headers={"Content-Type": "application/json", "User-Agent": UA}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for sc in r.headers.get_all("Set-Cookie") or []:
            if sc.startswith("ttwid="):
                return sc.split(";", 1)[0].split("=", 1)[1]
    raise RuntimeError("获取 ttwid 失败（无 Set-Cookie）")


def normalize_url(url: str) -> str:
    m = (re.search(r"(?:iesdouyin\.com/(?:xg|share)/video/|douyin\.com/(?:video|note)/|ixigua\.com/(?:video/|i)?|modal_id=)(\d{15,})", url))
    if m:
        return m.group(1)
    if "v.douyin.com" in url or "iesdouyin.com/share" in url:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
                "Accept": "text/html,*/*"})
            final = urllib.request.urlopen(req, timeout=15).geturl()
            m2 = re.search(r"(?:iesdouyin\.com/(?:xg|share)/video/|douyin\.com/(?:video|note)/|modal_id=)(\d{15,})", final)
            if m2:
                return m2.group(1)
        except Exception:
            pass
    return ""


def _scan_audio_urls(obj, out):
    """递归扫描 JSON，收集分离音频轨 URL（media-audio / mime_type=audio）。"""
    if isinstance(obj, dict):
        for v in obj.values():
            _scan_audio_urls(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _scan_audio_urls(v, out)
    elif isinstance(obj, str):
        if "douyinvod.com" in obj and ("media-audio" in obj or "mime_type=audio" in obj):
            if obj.startswith("http") and obj not in out:
                out.append(obj)


def resolve(url: str, timeout: int = 15, cookie_header: str | None = None) -> dict:
    """a_bogus 直连解析。返回与 douyin_resolve.resolve() 同构的 dict。

    cookie_header：可选登录态 Cookie（来自云端公共池的有效抖音登录态）。
        提供时优先用它（含 sessionid/sid_tt/UIFID 等），跳过游客 ttwid 注册，
        用于突破抖音 Argus 风控（游客态被确定性拦截）。缺失/为空则走原有游客态。
    """
    aweme_id = normalize_url(url)
    if not aweme_id:
        raise RuntimeError("无法从链接提取视频 ID：%s" % url[:100])

    if cookie_header:
        # 登录态：直接用，跳过游客 ttwid 注册；msToken 优先取 Cookie 内的值（与签名一致）
        ms_token = _ms_token()
        m = re.search(r"msToken=([^;]+)", cookie_header)
        if m:
            ms_token = m.group(1)
        cookie_str = cookie_header
    else:
        ttwid = _ttwid(timeout)
        ms_token = _ms_token()
        cookie_str = "ttwid=%s; msToken=%s" % (ttwid, ms_token)
    params = (
        "device_platform=webapp&aid=6383&channel=channel_pc_web&aweme_id=%s"
        "&version_code=170400&version_name=17.4.0&cookie_enabled=true"
        "&screen_width=1920&screen_height=1080&browser_language=zh-CN"
        "&browser_platform=Win32&browser_name=Chrome&browser_version=130.0.0.0"
        "&browser_online=true&engine_name=Blink&engine_version=130.0.0.0"
        "&os_name=Windows&os_version=10&cpu_core_num=12&device_memory=8"
        "&platform=PC&downlink=10&effective_type=4g&round_trip_time=50"
        "&msToken=%s" % (aweme_id, ms_token)
    )
    a_bogus = ABogus(UA).generate(params)
    full = "%s?%s&a_bogus=%s" % (_DETAIL_API, params, a_bogus)
    req = urllib.request.Request(full, headers={
        "User-Agent": UA,
        "Referer": "https://www.douyin.com/",
        "Accept": "application/json, text/plain, */*",
        "Cookie": cookie_str,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # 2026-10-27：抖音上线 Argus 安全插件，匿名直连详情接口被确定性拦截
        # （响应体 "Blocked by ArgusSecurityPlugin Uifid/Signature Not Found"，
        #   本机/机房 IP、有无浏览器 cookie 均一样）。这是平台级风控而非临时
        #   故障，抛 ArgusBlockedError 让 daemon 直接返回、不再回落 Playwright
        #   白耗 35s（实测 Playwright 页面同样拿不到视频流）。
        _body = b""
        try:
            _body = e.read(300) or b""
        except Exception:
            pass
        if b"ArgusSecurityPlugin" in _body:
            raise ArgusBlockedError(
                "抖音解析暂不可用：抖音近期升级风控（Argus 安全插件），网页端匿名解析被拦截"
                "（与链接是否有效无关）。请稍后重试")
        raise RuntimeError("详情接口 HTTP %s" % e.code)
    except json.JSONDecodeError:
        raise RuntimeError("详情接口返回非 JSON（可能被风控）")

    detail = data.get("aweme_detail") or {}
    if not detail:
        # status_code=0 说明签名被正常接受、请求成功处理，只是没有这个视频
        # ——确定性结论，调用方无需再回落 Playwright 白耗 20s
        raise VideoNotFoundError(
            "抖音链接无效或视频不存在（status_code=%s）。"
            "可能原因：视频已删除/作者设为私密/短链已过期" % data.get("status_code"))

    video = detail.get("video") or {}
    title = (detail.get("desc") or aweme_id).strip() or aweme_id
    duration = None
    try:
        duration = int(video.get("duration") or 0) // 1000 or None
    except (TypeError, ValueError):
        pass

    width = int((video.get("width") or 0))
    height = int((video.get("height") or 0))

    # 播放地址：优先 play_addr 的 /aweme/v1/play/ 链接（跟随 302 得混合轨 douyinvod 直链）
    video_url = ""
    play_list = ((video.get("play_addr") or {}).get("url_list")) or []
    play_list = [u for u in play_list if u.startswith("http")]
    for cand in play_list:
        try:
            creq = urllib.request.Request(cand, headers={
                "User-Agent": UA, "Referer": "https://www.douyin.com/"})
            with urllib.request.urlopen(creq, timeout=timeout) as r:
                final_url = r.geturl()
                ctype = r.headers.get("Content-Type", "")
            if "douyinvod.com" in final_url and "video" in ctype:
                video_url = final_url
                break
        except Exception:
            continue

    # 分离轨兜底：bit_rate 里逐档找 video-only；音频全局扫
    audio_url = ""
    video_has_audio = bool(video_url)
    if not video_url:
        for br in video.get("bit_rate") or []:
            urls = ((br.get("play_addr") or {}).get("url_list")) or []
            v = next((u for u in urls if "media-video" in u or "douyinvod" in u), "")
            if v:
                video_url = v
                break
    if not video_url:
        raise RuntimeError("详情拿到了但提取不到视频流（play_addr=%d, bit_rate=%d）"
                           % (len(play_list), len(video.get("bit_rate") or [])))
    # 轨道判定：分离视频轨特征是 media-video-avc1 或 mime_type=video（纯视频，
    # 注意 mime_type=video_mp4 是混合轨、自带音轨，别被子串误伤）
    is_video_only = ("media-video-" in video_url
                     or ("mime_type=video" in video_url and "mime_type=video_mp4" not in video_url))
    if is_video_only:
        # 分离轨：需要配对音频
        audios = []
        _scan_audio_urls(detail, audios)
        audio_url = audios[0] if audios else ""
        video_has_audio = False

    return {
        "ok": True,
        "title": title,
        "duration": duration,
        "video_id": aweme_id,
        "video_url": video_url,
        "audio_url": audio_url,
        "video_has_audio": video_has_audio,
        "width": width,
        "height": height,
        "webpage_url": "https://www.douyin.com/video/%s" % aweme_id,
        "thumbnail": (detail.get("video") or {}).get("cover", {}).get("url_list", [""])[0]
        if isinstance((detail.get("video") or {}).get("cover"), dict) else "",
        "ext": "mp4",
    }


def _selftest():
    """离线自检：SM3 标准向量 + a_bogus 格式。"""
    assert _SM3.hash_bytes(b"abc").hex() == (
        "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0"), "SM3 标准向量失败"
    assert _SM3.hash_bytes(b"").hex() == (
        "1ab21d8355cfa17f8e61194831e81a8f22bec8c728fefb747ed035eb5082aa2b"), "SM3 空串向量失败"
    sig = ABogus(UA).generate("device_platform=webapp&aid=6383&aweme_id=7380308675841297704")
    assert re.fullmatch(r"[A-Za-z0-9=/+_-]{100,300}", sig), "a_bogus 格式异常: %r" % sig[:50]
    return True


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
        print("selftest OK")
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "--vectors":
        # 调试：打印一条真实解析的中间产物
        pass
    u = sys.argv[1] if len(sys.argv) > 1 else ""
    if not u:
        print("用法: python douyin_direct.py <抖音链接|--selftest>")
        sys.exit(1)
    try:
        print(json.dumps(resolve(u), ensure_ascii=False, indent=2))
    except Exception as e:
        print("FAIL:", str(e))
        sys.exit(2)
