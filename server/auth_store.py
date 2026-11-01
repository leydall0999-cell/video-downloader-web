"""VDL 本地账号系统（A1：自建轻量账号）。

零外部依赖（仅标准库 hashlib / hmac / secrets），契合桌面端 LGPL/离线约束。
账号以「邮箱或手机号」为标识，密码用 PBKDF2-HMAC-SHA256 加盐哈希；登录态用
HMAC-SHA256 签名的无状态 token（Bearer），由本机 .auth_secret 校验。

存储：~/.video-downloader/users.json（账号表）+ 同目录 .auth_secret（签名密钥，0600）。
会员状态按用户分文件存于 memberships/{user_id}.json（见 user_membership.py，即 B2）。
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

try:                                    # POSIX（macOS / Linux）
    import fcntl as _fcntl
except ImportError:                     # Windows
    _fcntl = None
try:
    import msvcrt as _msvcrt
except ImportError:
    _msvcrt = None

# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def _base_dir() -> Path:
    # 覆盖位：smoke / E2E 测本进程可能用 PyInstaller 冻结，Path.home() 不一定
    # 跟随 HOME，因此提供 VDL_DATA_DIR 显式隔离数据目录（不设置则走平台默认）。
    override = os.environ.get("VDL_DATA_DIR", "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        base = Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
    else:
        base = Path.home() / ".video-downloader"
    return base


def _users_path() -> Path:
    return _base_dir() / "users.json"


def _secret_path() -> Path:
    return _base_dir() / ".auth_secret"


def _avatar_dir() -> Path:
    return _base_dir() / "avatars"


def _avatar_path(user_id: str) -> Path:
    return _avatar_dir() / f"{user_id}.png"


def _write_atomic(p: Path, payload: bytes, mode: int = 0o600) -> None:
    """原子写盘：唯一临时名（带 pid + 线程 id）→ os.replace 换入。

    临时名必须唯一。固定名（users.json.tmp）在两个写者并发时会发生
    「A 写一半 → B 截断重写 → A 把这张半截文件 replace 上线」，
    结果是 users.json 变成解析不了的半截 JSON（_load_users 只能当空表处理 = 全部账号消失）。
    唯一名让每个写者各写各的临时文件，replace 本身是原子的，读者永远看到完整版本。
    """
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_bytes(payload)
        tmp.replace(p)
    finally:
        if tmp.exists():                    # replace 成功后已不存在；异常路径必须清理
            with contextlib.suppress(OSError):
                tmp.unlink()
    try:
        os.chmod(p, mode)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# users.json 有 8 个写入口（注册 / 改密 / 注销 / 提权 / 头像 / 后台禁用 / 后台重置 /
# 超管引导），散落在 auth_store 与 admin_store 两个模块。它们**全是**
# 「读全量 → 改一处 → 写全量」；不串行时两个入口会各自基于同一份旧快照写回，
# 后写的把先写的整段覆盖 —— 表现是丢账号 / 丢提权 / 丢禁用标记，
# 且不报任何错，只是文件里静静少一条记录（直到登录时才炸出「账号或密码错误」）。
#
# ⚠️ 只锁「落盘」那一步不解决问题：**载入**必须一起进来，
# 否则两个线程照样各自读到同一份旧数据，锁了个寂寞。
#
# 双层锁：
#   · 进程内 RLock —— 请求走线程池，这是主力；可重入（后台重置会回调 auth_store）。
#   · 跨进程 flock —— 多进程/双实例兜底（VPS 推送 / Railway prune 与本进程会同时写）。
_USERS_RLOCK = threading.RLock()
_USERS_LOCK_STATE: dict[str, Any] = {"depth": 0, "owner": None, "fh": None}
_USERS_LOCKFILE = ".users.lock"     # 跨进程锁用的空文件（与 users.json 同目录）
_FILE_LOCK_WAIT = 2.0               # 跨进程锁最多等 2s；超时降级为仅线程锁，绝不卡死用户
_FILE_LOCK_POLL = 0.02


def _users_lock_path() -> Path:
    return _base_dir() / _USERS_LOCKFILE


def _lock_users_file():
    """尝试取跨进程独占锁，成功返回文件句柄。

    任何异常/超时都**降级返回句柄继续走**（此时仅靠进程内锁），
    宁可极端情况下少一层保护，也不能让用户界面卡在登录/注册上。
    """
    try:
        p = _users_lock_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = open(p, "a+")
    except OSError:
        return None
    if _fcntl is None and _msvcrt is None:      # 平台不支持 → 句柄还要拿回去关
        return fh
    deadline = time.monotonic() + _FILE_LOCK_WAIT
    while True:
        try:
            if _fcntl is not None:
                _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            else:
                fh.seek(0)
                _msvcrt.locking(fh.fileno(), _msvcrt.LK_NBLCK, 1)
            return fh
        except OSError:
            if time.monotonic() >= deadline:
                logging.getLogger(__name__).warning(
                    "账号表跨进程锁等待超时（%ss），降级为仅进程内锁", _FILE_LOCK_WAIT)
                return fh
            time.sleep(_FILE_LOCK_POLL)


def _unlock_users_file(fh) -> None:
    if fh is None:
        return
    try:
        if _fcntl is not None:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
        elif _msvcrt is not None:
            fh.seek(0)
            _msvcrt.locking(fh.fileno(), _msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            fh.close()


@contextlib.contextmanager
def users_mutation():
    """账号表临界区：**载入 → 改 → 落盘** 整段包在这里（所有写入口统一用法）。

        with users_mutation():
            data = _load_users()
            ... 改 data ...
            _save_users(data)

    提前 return（例如「用户不存在」）不会落盘 —— 落盘由调用方显式调用，不是自动的。
    """
    me = threading.get_ident()
    _USERS_RLOCK.acquire()
    try:
        st = _USERS_LOCK_STATE
        st["depth"] += 1
        st["owner"] = me
        if st["depth"] == 1:        # 只有最外层拿跨进程锁，否则同进程重入会自我死锁
            st["fh"] = _lock_users_file()
        yield
    finally:
        st = _USERS_LOCK_STATE
        st["depth"] -= 1
        if st["depth"] <= 0:
            st["depth"] = 0
            st["owner"] = None
            _unlock_users_file(st["fh"])
            st["fh"] = None
        _USERS_RLOCK.release()


def users_lock_held() -> bool:
    """当前线程是否正持账号表写锁（守护测试用：断言每次落盘都发生在锁内）。"""
    return _USERS_LOCK_STATE["owner"] == threading.get_ident()


# --------------------------------------------------------------------------- #
# 签名密钥（每机一份，丢失即全部旧 token 失效，但账号仍在）
# --------------------------------------------------------------------------- #
def _load_secret() -> bytes:
    p = _secret_path()
    if p.exists():
        try:
            return p.read_bytes()
        except OSError:
            pass
    # 生成并落盘（仅本机可读）
    secret = secrets.token_bytes(32)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(p, secret)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except OSError:
        pass  # 写失败则退回内存密钥（重启失效，但不阻塞登录）
    return secret


_SECRET = _load_secret()
PBKDF2_ITERS = 200_000
TOKEN_TTL = 60 * 60 * 24 * 30  # 30 天
# 后台专用令牌（2026-11-01）：网页后台走独立登录地址，令牌只活 2 小时。
# 与前台 30 天令牌**同一把签名密钥**（不新增 secret，避免部署新密钥的运维成本），
# 靠 payload 多一段作用域标记 `adm` 区分 —— 见 issue_admin_token / verify_admin_token。
ADMIN_TOKEN_TTL = 2 * 60 * 60  # 2 小时


# --------------------------------------------------------------------------- #
# 密码哈希
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> tuple[str, str]:
    """返回 (salt_hex, hash_hex)。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERS)
    return salt.hex(), dk.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERS)
    return hmac.compare_digest(dk, expected)


# --------------------------------------------------------------------------- #
# Token（HMAC 签名，无状态）
# --------------------------------------------------------------------------- #
def _b64url(b: bytes) -> str:
    return b64e(b)


def b64e(b: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


# --------------------------------------------------------------------------- #
# 账号鉴权状态（安全审计 2026-10-13，F-02）：token_version / disabled / deleted_at
# --------------------------------------------------------------------------- #
# verify_token 每个请求都要过 ⇒ 不能每次都读盘。这里做 1 秒 TTL 的进程内快照，
# 并在**每次账号表落盘**时（_save_users）立刻失效 —— 所以本进程内改密/禁用是
# **即时**生效的（含测试），只有「双实例窗口」的另一个进程最多滞后 1 秒。
# （与桌面端 app 仓 auth_store 同语义。）
_STATUS_CACHE: dict[str, Any] = {"ts": 0.0, "map": {}}
_STATUS_CACHE_TTL = 1.0


def _invalidate_status_cache() -> None:
    _STATUS_CACHE["ts"] = 0.0
    _STATUS_CACHE["map"] = {}


def _user_auth_state(user_id: str) -> Optional[tuple]:
    """返回 (token_version, disabled, deleted)；账号不存在 / 账号表不可读 → None。

    读不到账号表一律返回 None（调用方 fail-closed），绝不「当作正常账号放行」。
    """
    now = time.time()
    if (now - float(_STATUS_CACHE.get("ts") or 0.0)) < _STATUS_CACHE_TTL:
        return _STATUS_CACHE["map"].get(user_id)
    try:
        data = _load_users()
        m = {}
        for u in data.get("users", []):
            m[u.get("user_id")] = (int(u.get("token_version", 0) or 0),
                                   bool(u.get("disabled")),
                                   bool(u.get("deleted_at")))
    except Exception:  # noqa: BLE001 — 读不到账号表按 fail-closed
        return None
    _STATUS_CACHE["ts"] = now
    _STATUS_CACHE["map"] = m
    return m.get(user_id)


def _current_token_version(user_id: str) -> int:
    st = _user_auth_state(user_id)
    return st[0] if st else 0


def bump_token_version(user: dict) -> int:
    """递增账号的 token_version ⇒ 该账号所有已签发 token 立即失效。

    调用方必须已在 `users_mutation()` 临界区内，且随后调用 `_save_users(data)`
    （落盘时会使状态缓存失效，本进程内即时生效）。
    """
    ver = int(user.get("token_version", 0) or 0) + 1
    user["token_version"] = ver
    return ver


def b64d(s: str) -> bytes:
    import base64
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def issue_token(user_id: str) -> str:
    """签发 bearer。payload = `<uid>.<iat>.<exp>.<token_version>`（base64url）。

    安全审计 2026-10-13（F-02）：payload 第 4 段写入该账号当前 token_version，
    改密/注销/禁用时 +1 ⇒ 旧 token 立即失效（见 verify_token）。此前只验签名与
    exp，于是这些操作都不会让已签发 token 失效（TTL 长达 30 天），token 一旦
    泄漏等于长期后门；且账号被后台禁用后旧 token 仍可用。
    """
    iat = int(time.time())
    exp = iat + TOKEN_TTL
    ver = _current_token_version(user_id)
    payload = _b64url(f"{user_id}.{iat}.{exp}.{ver}".encode("utf-8"))
    # 防篡改：对 user_id + payload 签名
    sig = hmac.new(_SECRET, f"{user_id}.{payload}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{user_id}.{payload}.{sig}"


def verify_token(token: str) -> Optional[str]:
    """校验 token：HMAC 签名 → exp → **账号状态 + token_version**。

    安全审计 2026-10-13（F-02）：
      · 账号不存在 / 已注销（deleted_at）/ 已被后台禁用（disabled）→ 一律 None；
      · payload 里的 token_version ≠ 账号当前 token_version → None（已撤销）。
    兼容：旧 token 的 payload 只有 3 段（无版本号），按版本 0 处理 —— 账号表里
    历史账号的 token_version 缺省也是 0，故升级后**不会**把在线用户集体踢下线。
    """
    if not token or token.count(".") != 2:
        return None
    user_id, payload, sig = token.split(".")
    exp = None
    ver = 0
    try:
        decoded = b64d(payload).decode("utf-8")
        # decoded = f"{uid}.{iat}.{exp}" 或 f"{uid}.{iat}.{exp}.{ver}"
        # 或后台令牌 f"{uid}.{iat}.{exp}.{ver}.adm"
        parts = decoded.split(".")
        if len(parts) >= 3:
            exp = int(parts[2])
        if len(parts) >= 4:
            ver = int(parts[3])
        if len(parts) >= 5:
            return None                  # 后台令牌（adm 作用域）不得当普通会话用
    except Exception:
        return None
    expected = hmac.new(_SECRET, f"{user_id}.{payload}".encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    if exp is not None and int(time.time()) > exp:
        return None
    state = _user_auth_state(user_id)
    if state is None:
        return None                      # 账号不存在 / 账号表不可读 → fail-closed
    cur_ver, disabled, deleted = state
    if disabled or deleted:
        return None                      # 已注销 / 已被后台禁用
    if ver != cur_ver:
        return None                      # 密码/状态已变更，旧 token 作废
    return user_id


def issue_admin_token(user_id: str) -> str:
    """签发**后台专用**令牌（2026-11-01）：payload 多一段作用域 `adm`，只活 2 小时。

    为什么单独一套：网页后台原来复用前台 30 天登录令牌，导致
      ① 必须先在前台登录超管账号，才能进后台（入口混在一起）；
      ② 后台一旦登进去，30 天都算"在线"，管理员笔记本丢了的风险窗口太大。
    现在后台有自己的地址 /admin/login + 自己的短效令牌，两者互不牵连：
      · 前台登录 → 拿不到后台令牌，进不去 /admin/；
      · 后台退出 → 不影响前台正在用的会话。

    签名密钥仍用 _SECRET（不新增 secret），`adm` 段由 verify_admin_token 严格校验，
    同时 verify_token 对 5 段 payload 一律拒绝 ⇒ 后台令牌不能冒充普通会话。
    """
    iat = int(time.time())
    exp = iat + ADMIN_TOKEN_TTL
    ver = _current_token_version(user_id)
    payload = _b64url(f"{user_id}.{iat}.{exp}.{ver}.adm".encode("utf-8"))
    sig = hmac.new(_SECRET, f"{user_id}.{payload}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{user_id}.{payload}.{sig}"


def verify_admin_token(token: str) -> Optional[str]:
    """校验后台令牌：签名 → exp → 必须带 `adm` 作用域 → 账号状态 + token_version。

    与 verify_token 的检查项一致，但**额外要求第 5 段 == "adm"**：
    普通登录令牌在这里一律 None（反向由 verify_token 拒绝 5 段），
    因此两个作用域严格互斥，前台令牌永远换不到后台权限。
    """
    if not token or token.count(".") != 2:
        return None
    user_id, payload, sig = token.split(".")
    exp = None
    ver = 0
    try:
        decoded = b64d(payload).decode("utf-8")
        parts = decoded.split(".")
        if len(parts) < 5 or parts[4] != "adm":
            return None                  # 不是后台令牌（含前台 30 天令牌）
        exp = int(parts[2])
        ver = int(parts[3])
    except Exception:
        return None
    expected = hmac.new(_SECRET, f"{user_id}.{payload}".encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    if exp is not None and int(time.time()) > exp:
        return None                      # 后台令牌过期（2 小时），需重新登录
    state = _user_auth_state(user_id)
    if state is None:
        return None
    cur_ver, disabled, deleted = state
    if disabled or deleted:
        return None
    if ver != cur_ver:
        return None                      # 改密 / 禁用后后台令牌一并作废
    return user_id


def token_from_header(authorization: Optional[str]) -> Optional[str]:
    """从 Authorization 头解析 uid，**两种作用域都认**。

    普通令牌 → verify_token；后台令牌 → verify_admin_token（2 小时）。
    后台令牌也认，是因为 /api/admin/* 端点内部大量直接用 get_current_user_id()
    取「我是谁」（标记当前账号、写审计 actor）。若这里只认前台令牌，后台登录后
    那些位置会拿到 None，「自己那行」会被渲染成「取消管理员 / 禁用」。

    两个作用域仍是**签发端互斥**：前台令牌换不到后台页面，后台令牌 2 小时即失效。
    """
    if not authorization:
        return None
    if authorization.startswith("Bearer "):
        raw = authorization[7:].strip()
        return verify_token(raw) or verify_admin_token(raw)
    return None


# --------------------------------------------------------------------------- #
# 账号表
# --------------------------------------------------------------------------- #
def _rebuild_index(data: dict) -> dict:
    """由 users 列表重建 by_identifier 索引，避免索引与列表不同步导致登录失败。

    历史上曾因手动改文件 / 部分写入，使 by_identifier 少于 users，造成
    '账号或密码错误'（authenticate 反查不到 user_id）。此处做自愈。
    """
    bi: dict[str, str] = {}
    for u in data.get("users", []):
        ident = u.get("identifier")
        uid = u.get("user_id")
        if ident and uid and not u.get("deleted_at"):  # 已删除账号不进索引（注销/删除后该号不可登录）
            bi[ident] = uid
    data["by_identifier"] = bi
    return data


def _load_users() -> dict:
    p = _users_path()
    if not p.exists():
        return {"users": [], "by_identifier": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return {"users": [], "by_identifier": {}}
    data.setdefault("users", [])
    data.setdefault("by_identifier", {})
    # 自愈：补齐缺失/陈旧的 identifier 索引
    if len(data["by_identifier"]) != len(data["users"]):
        _rebuild_index(data)
    return data


def _save_users(data: dict) -> None:
    """落盘账号表。**调用方必须已在 users_mutation() 临界区内**（见该函数说明）。"""
    _invalidate_status_cache()   # F-02：账号状态刚变，token 校验快照立即失效
    p = _users_path()
    # 写前重建索引，保证 by_identifier 与 users 始终一致
    _rebuild_index(data)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(p, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
    except OSError:
        pass


def _normalize(identifier: str) -> str:
    return identifier.strip().lower()


def create_user(identifier: str, password: str, is_admin: bool = False) -> Optional[str]:
    """注册。成功返回 user_id，账号已存在或已注销返回 None。

    is_admin 仅经后台显式提权（set_user_admin / ensure_superusers），常规自注册恒为 False。
    """
    ident = _normalize(identifier)
    with users_mutation():          # 载入→查重→写回 必须整段串行（并发注册同名只应成功一个）
        data = _load_users()
        if ident in data["by_identifier"]:
            return None
        # 已注销账号不可重新注册
        if any(u.get("identifier") == ident and u.get("deleted_at") for u in data["users"]):
            return None
        import uuid
        user_id = "u_" + uuid.uuid4().hex[:16]
        salt, pw_hash = hash_password(password)
        data["users"].append({
            "user_id": user_id,
            "identifier": ident,
            "salt": salt,
            "pw_hash": pw_hash,
            "created_at": int(time.time()),
            "is_admin": bool(is_admin),
        })
        data["by_identifier"][ident] = user_id
        _save_users(data)
        return user_id


def authenticate(identifier: str, password: str) -> Optional[str]:
    """校验凭证，成功返回 user_id，否则 None。"""
    ident = _normalize(identifier)
    data = _load_users()
    user_id = data["by_identifier"].get(ident)
    if not user_id:
        return None
    user = next((u for u in data["users"] if u["user_id"] == user_id), None)
    if not user or user.get("deleted_at"):
        return None
    # 安全审计 2026-10-13（F-02）：被后台禁用的账号不得再登录换新 token。
    # 此前只拦 deleted_at ⇒ 禁用账号仍可用正确密码登录拿到 bearer，与「禁用」语义相悖。
    if user.get("disabled"):
        return None
    if verify_password(password, user["salt"], user["pw_hash"]):
        return user_id
    return None


def user_exists(identifier: str) -> bool:
    """账号是否存在于本机账号表（已注销的不算）。

    用途：web 版登录/镜像时判断「本机有没有这个号」，以及本机回环调用方的错误提示
    区分（「密码不正确」vs「账号不存在」）。公网调用方**不得**据此区分（账号枚举）。
    """
    ident = _normalize(identifier)
    if not ident:
        return False
    data = _load_users()
    uid = data["by_identifier"].get(ident)
    if not uid:
        return False
    return any(u.get("user_id") == uid and not u.get("deleted_at")
               for u in data.get("users", []))


def user_identifier(user_id: str) -> Optional[str]:
    data = _load_users()
    user = next((u for u in data["users"] if u["user_id"] == user_id), None)
    return user["identifier"] if user else None


def user_avatar_url(user_id: str) -> Optional[str]:
    """返回用户头像相对/绝对 URL；未设置返回 None。"""
    p = _avatar_path(user_id)
    if not p.exists():
        return None
    return f"/api/account/avatar/{user_id}?t={int(p.stat().st_mtime)}"


def set_user_avatar(user_id: str, image_bytes: bytes) -> dict[str, Any]:
    """保存用户头像（bytes 应为 PNG/JPG 等图片二进制）。成功返回 ok=True 与 url。"""
    if len(image_bytes) > 2 * 1024 * 1024:
        return {"ok": False, "error": "头像文件过大，请压缩至 2MB 以内"}
    # 简单魔数校验：至少识别 PNG/JPEG/WebP 三种常见格式
    header = image_bytes[:12]
    is_png = header.startswith(b"\x89PNG\r\n\x1a\n")
    is_jpg = header[:2] == b"\xff\xd8"
    is_webp = header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    if not (is_png or is_jpg or is_webp):
        return {"ok": False, "error": "仅支持 PNG、JPG、WebP 格式图片"}
    try:
        _avatar_dir().mkdir(parents=True, exist_ok=True)
        p = _avatar_path(user_id)
        _write_atomic(p, image_bytes)
        # 记录更新时间在 users.json 中，便于前端感知变更
        with users_mutation():
            data = _load_users()
            user = next((u for u in data["users"] if u["user_id"] == user_id), None)
            if user:
                user["avatar_updated_at"] = int(time.time())
                _save_users(data)
        return {"ok": True, "url": user_avatar_url(user_id)}
    except OSError as e:
        return {"ok": False, "error": f"保存头像失败: {e}"}


def reset_password(identifier: str, new_password: str) -> bool:
    """本地重置密码（无邮箱验证，本机自助）。成功返回 True，账号不存在/密码过短返回 False。"""
    if not new_password or len(new_password) < 6:
        return False
    ident = _normalize(identifier)
    with users_mutation():
        data = _load_users()
        user_id = data["by_identifier"].get(ident)
        if not user_id:
            return False
        user = next((u for u in data["users"] if u["user_id"] == user_id), None)
        if not user:
            return False
        salt, pw_hash = hash_password(new_password)
        user["salt"] = salt
        user["pw_hash"] = pw_hash
        user["updated_at"] = int(time.time())
        # 安全审计 2026-10-13（F-02）：改密即撤销该账号所有已签发 token
        # （此前改密不 bump ⇒ 旧 bearer 在 TTL 内继续可用）。
        bump_token_version(user)
        _save_users(data)
        return True


def deactivate_user(user_id: str) -> dict[str, Any]:
    """注销当前账号（软删除）：标记 deleted_at，清除索引，使该 identifier 不可再登录/注册。"""
    with users_mutation():
        data = _load_users()
        user = next((u for u in data["users"] if u.get("user_id") == user_id), None)
        if not user:
            return {"ok": False, "error": "用户不存在"}
        if user.get("deleted_at"):
            return {"ok": False, "error": "账号已注销"}
        ident = user.get("identifier", "")
        user["deleted_at"] = int(time.time())
        user["updated_at"] = int(time.time())
        # 安全审计 2026-10-13（F-02）：注销即撤销该账号所有已签发 token。
        bump_token_version(user)
        # 清除索引，防止再次登录/注册
        if ident and ident in data["by_identifier"]:
            del data["by_identifier"][ident]
        _save_users(data)
        return {"ok": True}


# --------------------------------------------------------------------------- #
# 超级用户（is_admin）：后台管理面板访问授权
# --------------------------------------------------------------------------- #
_SUPERUSER_CACHE: dict[str, Any] = {"ts": 0.0}


def user_is_admin(user_id: str) -> bool:
    """该账号是否被标记为超级用户（后台管理权限）。"""
    data = _load_users()
    user = next((u for u in data["users"] if u["user_id"] == user_id), None)
    return bool(user.get("is_admin", False)) if user else False


def _admin_identifiers() -> list[str]:
    """显式超管名单：环境变量 VDL_ADMIN_IDENTIFIER + admin.json 的 admin_identifiers。

    2026-10-22 从 ensure_superusers() 里抽出来复用：判断「谁是那唯一的超管」
    与本函数必须读同一份名单，否则会出现「按名单提了权、却判定成只读管理员」。
    """
    idents: list[str] = []
    import json as _json
    for x in (os.environ.get("VDL_ADMIN_IDENTIFIER", "") or "").split(","):
        x = x.strip().lower()
        if x:
            idents.append(x)
    try:
        p = _base_dir() / "admin.json"
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                cfg = _json.load(f)
            if isinstance(cfg, dict):
                for x in (cfg.get("admin_identifiers") or []):
                    x = str(x).strip().lower()
                    if x:
                        idents.append(x)
    except Exception:
        pass
    return idents


def super_admin_identifier() -> str:
    """**唯一**超级管理员的账号标识（小写）。空串＝当前没有任何超管（仅全新空库才可能）。

    🔴 产品硬约束（2026-10-22）：「超管永远只有一个，管理员可以有多个」。
    在此之前的实现只有一个 is_admin 布尔 ⇒ 所有被提权的人都顶着「超管」标签、
    都能改数据。现在拆成两级，判定规则就落在这一个函数里：

      优先级 1：显式名单（VDL_ADMIN_IDENTIFIER / admin.json 的 admin_identifiers）
                的**第一项**。名单即便配了多个也只认第一个 —— 其余账号
                自动降级为「管理员」（只读），这是"唯一性"的硬保证。
      优先级 2：名单为空时兜底取 is_admin 账号里**注册最早**的那个。与
                ensure_superusers 的 bootstrap 语义一致（最早注册＝机主本人），
                也保证桌面端没配名单时后台不会变成"人人只读、谁都改不了"。

    ⚠️ 不要改成「名单里所有账号都是超管」——那就退回多超管了。
    """
    idents = _admin_identifiers()
    if idents:
        return idents[0]
    data = _load_users()
    admins = [u for u in data["users"] if u.get("is_admin")]
    if not admins:
        return ""
    earliest = min(admins, key=lambda u: u.get("created_at", 0))
    return str(earliest.get("identifier") or "").strip().lower()


def is_super_identifier(identifier: str) -> bool:
    """该账号标识是否为那唯一的超级管理员。"""
    ident = (identifier or "").strip().lower()
    if not ident:
        return False
    return ident == super_admin_identifier()


def user_role(user_id: str) -> str:
    """角色三分：super（唯一超管）/ admin（管理员·只读后台）/ ""（普通用户）。

    前端据此渲染「超管 / 管理员」标签并决定写操作按钮是否可用。
    """
    if not user_is_admin(user_id):
        return ""
    ident = user_identifier(user_id) or ""
    return "super" if is_super_identifier(ident) else "admin"


def set_user_admin(user_id: str, flag: bool) -> dict[str, Any]:
    """后台面板提权/降权。成功返回 ok=True 与当前 is_admin。"""
    with users_mutation():
        data = _load_users()
        user = next((u for u in data["users"] if u["user_id"] == user_id), None)
        if not user:
            return {"ok": False, "error": "用户不存在"}
        user["is_admin"] = bool(flag)
        user["updated_at"] = int(time.time())
        _save_users(data)
        return {"ok": True, "is_admin": bool(flag)}


def ensure_superusers(allow_bootstrap: bool = False) -> None:
    """Bootstrap 超级用户（幂等，带 60s 缓存避免频繁扫盘）。

    - 显式名单：~/.video-downloader/admin.json 的 admin_identifiers（列表），或环境变量
      VDL_ADMIN_IDENTIFIER（逗号分隔）。名单内账号登录后标记为 is_admin=True。
    - 无显式名单且当前无任何 is_admin 账号时，**仅当调用方是本机直连**
      （`allow_bootstrap=True`）才把最早注册的账号提升为超级用户。

    提权为单向：本函数永不降级，降级须经后台面板 /api/admin/users/{id}/set-admin。

    安全审计 2026-10-13（F-03，实测确认）：此前无条件兜底提升「最早注册账号」——
    公网部署上等于「谁先注册谁就是超管」，可直接拿 `/api/admin/*` 全部权限
    （含 system_config 返回 SMTP 明文口令）。现与桌面端 app 仓同口径：兜底提升
    **只允许本机直连**，公网必须靠显式名单（VDL_ADMIN_IDENTIFIER / admin.json）。
    """
    import json as _json
    with users_mutation():
        now = time.time()
        data = _load_users()
        has_admin = any(u.get("is_admin", False) for u in data["users"])
        # 已有超管：60s 内不重复扫盘（显式名单变更最迟 60s 后生效，可接受）
        if has_admin and (now - _SUPERUSER_CACHE["ts"] < 60):
            return

        # 2026-10-22：名单读取抽到 _admin_identifiers()，本函数与 super_admin_identifier()
        # 共用同一份实现，避免「这里配了多个、那里只认第一个」的口径漂移。
        idents: list[str] = _admin_identifiers()

        explicit = bool(idents)
        changed = False
        if explicit:
            for u in data["users"]:
                if u.get("identifier", "").strip().lower() in idents and not u.get("is_admin", False):
                    u["is_admin"] = True
                    changed = True
        else:
            # 无显式名单：尚无任何超管时，**仅本机直连**才兜底提升最早注册账号。
            # 安全审计 2026-10-13（F-03）：公网一律不提升 —— 否则「首注册即超管」
            # 等于把后台双手送给最先注册的那个人。
            if allow_bootstrap and data["users"] and not has_admin:
                earliest = min(data["users"], key=lambda u: u.get("created_at", 0))
                earliest["is_admin"] = True
                changed = True
        if changed:
            _save_users(data)
        _SUPERUSER_CACHE["ts"] = now


# --------------------------------------------------------------------------- #
# 找回密码：邮箱/手机验证码（V1 用 dev 模式本地投递，后续可切真实网关）
# --------------------------------------------------------------------------- #
RESET_CODE_TTL = 5 * 60  # 5 分钟有效
_RESEND_COOLDOWN = 60    # 重发冷却（秒）
_RESET_CODES: dict[str, dict] = {}   # ident -> {"code": str, "exp": float, "last": float}
_RESET_LOCK = threading.Lock()


def _send_mode() -> str:
    """验证码投递模式：显式 VDL_SEND_MODE 优先；未设置时存在 smtp.json 即自动切 smtp，否则 dev。

    - dev：本地测试（日志 + 返回 dev_code）
    - smtp：真实邮件投递（已接入，依赖 ~/.video-downloader/smtp.json）
    """
    env = os.environ.get("VDL_SEND_MODE")
    if env:
        return env.lower()
    if _smtp_accounts() is not None:
        return "smtp"
    return "dev"


def generate_reset_code(identifier: str) -> Optional[str]:
    """为存在的账号生成 6 位验证码；账号不存在返回 None。V1 不暴露账号是否真实存在。"""
    ident = _normalize(identifier)
    data = _load_users()
    if ident not in data["by_identifier"]:
        return None
    import secrets as _s
    code = f"{_s.randbelow(1_000_000):06d}"
    now = time.time()
    with _RESET_LOCK:
        _RESET_CODES[ident] = {"code": code, "exp": now + RESET_CODE_TTL, "last": now}
    return code


def reset_code_cooldown_ok(identifier: str) -> bool:
    """是否可发送（冷却期内不可）。"""
    ident = _normalize(identifier)
    with _RESET_LOCK:
        rec = _RESET_CODES.get(ident)
        if not rec:
            return True
        return (time.time() - rec.get("last", 0)) >= _RESEND_COOLDOWN


def verify_reset_code(identifier: str, code: str) -> bool:
    """校验验证码；正确则一次性作废（防重放）。"""
    ident = _normalize(identifier)
    with _RESET_LOCK:
        rec = _RESET_CODES.get(ident)
        if not rec:
            return False
        if time.time() > rec["exp"]:
            _RESET_CODES.pop(ident, None)
            return False
        ok = hmac.compare_digest(rec["code"], (code or "").strip())
        if ok:
            _RESET_CODES.pop(ident, None)  # 一次性
        return ok


def _smtp_accounts() -> Optional[list]:
    """读取 SMTP 账户配置（~/.video-downloader/smtp.json，0600）。缺文件返回 None。

    支持两种格式（自动兼容）：
    1. 单账户（旧）：直接是 {host, user, ...} 对象 → 当作唯一账户（default）。
    2. 多账户（新）：{"accounts": [ {...}, {...} ]}，每个账户可选
       match_domains（收件域路由，如 ["qq.com","foxmail.com"]）与 default（兜底）。
    非 dict 或无可解析账户返回 None。
    """
    p = _base_dir() / "smtp.json"
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if "accounts" in data and isinstance(data["accounts"], list):
        return [a for a in data["accounts"]
                if isinstance(a, dict) and a.get("host") and a.get("user")]
    # 单账户兼容
    if data.get("host") and data.get("user"):
        return [data]
    return None


def _select_smtp_account(to_email: str) -> Optional[dict]:
    """按收件人域名选择 SMTP 账户。优先 match_domains 命中，其次 default，否则第一个。"""
    accounts = _smtp_accounts()
    if not accounts:
        return None
    domain = ""
    if to_email and "@" in to_email:
        domain = to_email.split("@", 1)[1].strip().lower()
    for acc in accounts:
        doms = acc.get("match_domains") or []
        doms = [d.strip().lower() for d in doms]
        if domain and domain in doms:
            return acc
    for acc in accounts:
        if acc.get("default"):
            return acc
    return accounts[0]


# 发信配额：QQ 邮箱免费版每日约 50 封上限，被刷爆会触发发件人风控，连正常收信
# 都可能受影响（是整个邮件通道一起挂，不是只丢那几封验证码）。因此做进程级自我
# 保护：宁可对刷量的人说「太频繁」，也不要把当天额度烧光。
# 计数在内存里，服务重启清零 —— 这条限制的目的是挡住「有人猛刷」，不是精确计费。
_SEND_LOG: list[float] = []
_SEND_LOCK = threading.Lock()
SEND_PER_HOUR = 20
SEND_PER_DAY = 40


def smtp_quota_ok() -> tuple[bool, str]:
    """返回 (是否可发, 超出时的用户可读文案)。"""
    now = time.time()
    with _SEND_LOCK:
        kept = [t for t in _SEND_LOG if now - t < 86400]
        _SEND_LOG[:] = kept
        if len(kept) >= SEND_PER_DAY:
            return False, "今日邮件发送量已达上限，请明天再试或联系客服"
        if sum(1 for t in kept if now - t < 3600) >= SEND_PER_HOUR:
            return False, "发送过于频繁，请稍后再试"
    return True, ""


def _record_smtp_send() -> None:
    with _SEND_LOCK:
        _SEND_LOG.append(time.time())


def _send_smtp(to_email: str, code: str) -> None:
    """通过 SMTP 发送验证码邮件（仅标准库，无第三方依赖）。

    按收件人域名自动选择对应 SMTP 账户（QQ 收件→QQ 发件、Gmail 收件→Gmail 发件等）。
    多账户配置见 ~/.video-downloader/smtp.json（{"accounts": [...]}，每个账户可带 match_domains）。
    """
    import smtplib
    from email.mime.text import MIMEText
    from email.utils import formataddr, formatdate

    cfg = _select_smtp_account(to_email)
    if not cfg or not cfg.get("host") or not cfg.get("user"):
        raise RuntimeError("SMTP 未配置：缺少 ~/.video-downloader/smtp.json 或对应账户")
    host = cfg["host"]
    use_ssl = bool(cfg.get("use_ssl", int(cfg.get("port", 0)) == 465))
    port = int(cfg.get("port", 465 if use_ssl else 587))
    user = cfg["user"]
    password = cfg.get("pass", "")
    from_addr = cfg.get("from") or user
    from_name = cfg.get("from_name") or "VideoDownloader"
    use_tls = bool(cfg.get("use_tls", not use_ssl))

    subject = "VideoDownloader 密码重置验证码"
    body = (
        "您正在重置 VideoDownloader 账号密码。\n\n"
        f"验证码：{code}\n"
        "该验证码 5 分钟内有效，且仅可使用一次。\n\n"
        "若非本人操作，请忽略此邮件。"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=15) as s:
                s.login(user, password)
                s.sendmail(from_addr, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                if use_tls:
                    s.starttls()
                s.login(user, password)
                s.sendmail(from_addr, [to_email], msg.as_string())
    except Exception as e:  # noqa: BLE001
        logging.getLogger("vdl.auth").error("SMTP 发送失败 to=%s: %s", to_email, e)
        raise
    _record_smtp_send()


def deliver_reset_code(identifier: str, code: str) -> None:
    """投递验证码。dev=本地调试展示（日志 + 写文件）；smtp=真实邮件投递。"""
    mode = _send_mode()
    if mode == "dev":
        logging.getLogger("vdl.auth").info("V1 dev 验证码 for %s: %s", identifier, code)
        try:
            with open(_base_dir() / "reset_code.dev.log", "a", encoding="utf-8") as f:
                f.write(f"{int(time.time())} {identifier} {code}\n")
        except OSError:
            pass
    elif mode == "smtp":
        _send_smtp(identifier, code)
    elif mode == "sms":
        # TODO: 真实短信投递（Twilio / 阿里云短信 / 火山短信）
        raise NotImplementedError("SMS 投递未接入")
    else:
        raise NotImplementedError(f"未知投递模式: {mode}")
