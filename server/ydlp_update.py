"""yt-dlp 解析器自动更新。

把最新版 yt-dlp 下载到本机用户目录（~/.videodownloader/yt_dlp），下次启动前插入
sys.path 最前优先使用，从而在不重新打包的前提下获得最新站点解析器——新站点 / 改版
站点也能解析，软件「越用越能下」。

合规边界：
- 仅更新公开的站点解析器（yt-dlp 官方 wheel），不涉及任何账号凭证、不共享用户数据。
- 下载内容来自 PyPI 官方，仅替换解析逻辑，不动登录态 / Cookie。
- 生效需重启（已加载的旧版 yt-dlp 无法热替换），前端会提示「重启后生效」。

解释器兼容性（2026-10-30 事故后加固）：
- yt-dlp 2026.10.26 起要求 Python>=3.11，而线上 ECS 的 venv 是 3.10.12。
  旧实现无条件取 PyPI「最新版」，把不兼容的 wheel 解进影子目录 ⇒ 运行中进程无恙、
  下次重启直接 ImportError 崩溃循环（2026-10-30 02:35 实际发生，服务被 systemd 反复拉起）。
- 现在改为「取**当前解释器可用**的最新稳定版」：读 PyPI 的 requires_python 过滤，
  取满足条件的最高版本。这样 3.10 环境自动停在 2026.10.21（最后一个支持 3.10 的版本），
  3.11+ 环境仍能跟到最新。升级解释器后无需改代码即可自动跟进。
"""
from __future__ import annotations

import os
import re
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import requests


def _resolve_ytdlp_dir() -> Path:
    """优先用 Railway 持久卷（部署重启不丢），否则回退本机 home（桌面端）。

    Railway ephemeral 容器每次部署重建，home 下的文件会清空；而自动更新的
    yt-dlp 若随部署丢失就白更新了。持久卷（RAILWAY_VOLUME_MOUNT_PATH）与
    cookie 池共用，重启后 bootstrap 仍能拿到最新解析器。
    """
    mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if mount:
        try:
            p = Path(mount) / "yt_dlp"
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            pass
    return Path.home() / ".videodownloader" / "yt_dlp"


VDL_YTDLP_DIR = _resolve_ytdlp_dir()
_VERSION_FILE = VDL_YTDLP_DIR / "version.txt"
_PYPI_JSON = "https://pypi.org/pypi/yt-dlp/json"
_PYPI_TIMEOUT = 20
# 预发布/开发版：2026.x 的 yt-dlp 每晚发 .dev0，取「稳定版」避免踩未验证改动
_PRERELEASE_RE = re.compile(r"(dev|a|b|rc|alpha|beta|pre)\d*", re.I)


def bootstrap() -> None:
    """必须在 import yt_dlp 之前调用：若本机已存有新版 yt-dlp，插入 sys.path 最前优先使用。"""
    pkg = VDL_YTDLP_DIR / "yt_dlp"
    if pkg.is_dir() and (pkg / "__init__.py").exists():
        sys.path.insert(0, str(VDL_YTDLP_DIR))


def current_version() -> str:
    try:
        from importlib.metadata import version
        return version("yt-dlp")
    except Exception:
        return "未知"


def _fetch_meta() -> dict | None:
    """拉一次 PyPI JSON（info + releases），失败返回 None。"""
    try:
        r = requests.get(_PYPI_JSON, timeout=_PYPI_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def latest_version() -> str | None:
    """PyPI 上的绝对最新版（不看解释器兼容性，仅作信息展示/诊断）。"""
    meta = _fetch_meta()
    if not meta:
        return None
    try:
        return meta["info"]["version"]
    except Exception:
        return None


# ---------------------------------------------------------------- 兼容性判定

def _py_tuple(py=None) -> tuple[int, int]:
    v = py or sys.version_info[:2]
    return int(v[0]), int(v[1])


def _spec_allows(requires_python: str | None, py=None) -> bool:
    """判断 requires_python 是否允许当前解释器。

    优先用 packaging（pip 生态标准实现）；缺失时退回内置极简解析，
    只支持 >=, >, <=, <, ==, != 加逗号组合（PyPI 上 yt-dlp 只用到这几种）。
    解析不了的一律按「不兼容」处理：宁可少更新，也不能把不能 import 的包装进去。
    """
    if not requires_python or not str(requires_python).strip():
        return True  # 未声明视为无限制（老版本常见）
    major, minor = _py_tuple(py)
    cur = (major, minor, 0)
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version
        return SpecifierSet(requires_python).contains(
            Version("%d.%d.0" % (major, minor)), prereleases=True)
    except Exception:
        pass
    try:
        for part in str(requires_python).split(","):
            part = part.strip()
            if not part:
                continue
            m = re.match(r"^(>=|<=|==|!=|>|<)\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?", part)
            if not m:
                return False
            op = m.group(1)
            ver = tuple(int(x) for x in m.groups()[1:] if x is not None)
            ver = ver + (0,) * (3 - len(ver))
            if op == ">=" and not cur >= ver:
                return False
            if op == "<=" and not cur <= ver:
                return False
            if op == ">" and not cur > ver:
                return False
            if op == "<" and not cur < ver:
                return False
            if op == "==" and cur != ver:
                return False
            if op == "!=" and cur == ver:
                return False
        return True
    except Exception:
        return False


def _ver_key(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def _is_stable(v: str) -> bool:
    return not _PRERELEASE_RE.search(v)


def _has_wheel(files: list) -> bool:
    return any(str(f.get("filename", "")).endswith(".whl") for f in files or [])


def latest_compatible_version(py=None) -> str | None:
    """当前解释器可安装的最新**稳定**版 yt-dlp；无可用版本返回 None。"""
    meta = _fetch_meta()
    if not meta:
        return None
    releases = meta.get("releases") or {}
    cands: list[str] = []
    for ver, files in releases.items():
        if not files or not _is_stable(ver) or not _has_wheel(files):
            continue
        if not _spec_allows((files[0] or {}).get("requires_python"), py):
            continue
        cands.append(ver)
    if not cands:
        # 仅当 releases 缺失（异常/极简响应）时才退回 info.version；若 releases 明确
        # 存在但**无一兼容**，绝不能退回 info.version —— 那正是「最新但装不上」的那个版本。
        if releases:
            return None
        info = meta.get("info") or {}
        info_ver = info.get("version")
        if info_ver and _spec_allows(info.get("requires_python"), py):
            return info_ver
        return None
    return max(cands, key=_ver_key)


def version_report() -> dict:
    """给 /api/ydlp/version 用的聚合视图（只打一次 PyPI，避免双请求拖慢接口）。

    latest = 本机解释器可用的最新版（客户端据此判断「是否需要更新」才有意义）；
    latest_upstream = PyPI 绝对最新版（仅诊断用，可能因解释器过旧而不可安装）。
    """
    cur = current_version()
    meta = _fetch_meta()
    if not meta:
        return {"current": cur, "latest": None, "latest_upstream": None}
    upstream = (meta.get("info") or {}).get("version")
    # 复用同一份 meta 做兼容性筛选，避免二次请求
    releases = meta.get("releases") or {}
    cands = [
        v for v, files in releases.items()
        if files and _is_stable(v) and _has_wheel(files)
        and _spec_allows((files[0] or {}).get("requires_python"))
    ]
    latest = max(cands, key=_ver_key) if cands else None
    if latest is None and upstream and _spec_allows(
            (meta.get("info") or {}).get("requires_python")):
        latest = upstream
    return {"current": cur, "latest": latest or upstream, "latest_upstream": upstream}


# ---------------------------------------------------------------- 下载与安装

def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """解压并防 Zip Slip：任何解析后落在 dest 之外的成员一律拒绝（跨 Python 版本稳健）。"""
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    for name in zf.namelist():
        target = (dest / name).resolve()
        if target != dest and dest not in target.parents:
            raise RuntimeError(f"wheel 含越界路径，拒绝解压：{name}")
    zf.extractall(dest)


def _install_wheel(meta: dict, ver: str) -> dict:
    """下载指定版本的 wheel 并原子替换影子目录。"""
    import hashlib
    import shutil

    releases = meta.get("releases") or {}
    files = releases.get(ver) or []
    if not files:
        files = (meta.get("urls") or []) if (meta.get("info") or {}).get("version") == ver else []
    wheel = next((u for u in files if str(u.get("filename", "")).endswith(".whl")), None)
    if not wheel:
        return {"ok": False, "error": f"未找到 {ver} 的可用 yt-dlp wheel"}
    # 供应链校验：核对 PyPI 公布的 sha256，防投毒 / 中间人
    expected_sha = (wheel.get("digests") or {}).get("sha256")
    VDL_YTDLP_DIR.mkdir(parents=True, exist_ok=True)
    dl = requests.get(wheel["url"], timeout=180, stream=True)
    dl.raise_for_status()
    raw = dl.content
    if expected_sha and hashlib.sha256(raw).hexdigest() != expected_sha:
        return {"ok": False, "error": "校验和不匹配，疑似下载被篡改，已终止更新"}
    # 先解压到临时目录，成功后再原子替换，避免半解压导致下次启动崩溃
    tmp = VDL_YTDLP_DIR.with_name("yt_dlp_tmp")
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    _safe_extract(zipfile.ZipFile(BytesIO(raw)), tmp)
    # 装机前最后一道闸：解出来的包必须能被**当前解释器** import（只做语法/依赖层校验，
    # 不依赖 app.py 的异步护栏，避免「装完才发现在别的进程里崩」）
    import subprocess
    chk = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'%s'); import yt_dlp" % tmp],
        capture_output=True, timeout=120)
    if chk.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": False,
                "error": "新版与当前 Python %d.%d 不兼容，已放弃安装" % _py_tuple()}
    old = VDL_YTDLP_DIR.with_name("yt_dlp_old")
    if VDL_YTDLP_DIR.exists():
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        VDL_YTDLP_DIR.rename(old)
    tmp.rename(VDL_YTDLP_DIR)
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    _VERSION_FILE.write_text(ver)
    return {"ok": True, "updated": True, "version": ver, "restart_required": True}


def update() -> dict:
    """下载并解压「当前解释器可用」的最新 yt-dlp 到本机目录，返回结果（需重启生效）。

    安全加固：
    - 只装 requires_python 允许当前解释器的版本（防装进不能 import 的包）；
    - 校验 PyPI 公布的 sha256（防投毒 / 中间人篡改）；
    - 解压前逐成员校验路径（防 Zip Slip）；
    - 先解到临时目录，import 自检通过再原子替换，避免半解压导致下次启动崩溃。
    """
    meta = _fetch_meta()
    if not meta:
        return {"ok": False, "error": "无法获取最新版本（请检查网络）"}
    ver = latest_compatible_version()
    if not ver:
        upstream = (meta.get("info") or {}).get("version") or "未知"
        return {"ok": False,
                "error": "无与当前 Python %d.%d 兼容的 yt-dlp 版本（上游最新 %s）"
                         % (_py_tuple() + (upstream,))}
    pkg = VDL_YTDLP_DIR / "yt_dlp" / "__init__.py"
    # 已是最新：版本标记一致**且**影子包真实存在（防止标记残留但目录被清）
    if _VERSION_FILE.exists() and _VERSION_FILE.read_text().strip() == ver and pkg.exists():
        return {"ok": True, "updated": False, "version": ver, "restart_required": False}
    try:
        return _install_wheel(meta, ver)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"下载失败：{exc}"}
