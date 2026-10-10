"""GitHub Releases transport and preparation; never replace a running EXE here."""
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse
import uuid
import zipfile

from . import __version__
from .core import resource_path, user_data_dir

REPOSITORY = "shiknevus/devmemstudio"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
FEED_URL = f"{RELEASES_URL}.atom"   # web feed, not counted against the API quota
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100"   # one call: latest + skipped notes
MAX_PACKAGE = 512 * 1024 * 1024
TIMEOUT = 15


class UpdateCancelled(Exception):
    def __init__(self):
        super().__init__("已取消更新。")


class RateLimited(RuntimeError):
    pass


class NotFound(RuntimeError):
    pass


def version_tuple(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(value))
    if not match:
        raise ValueError(f"不支持的正式版本号：{value}")
    return tuple(map(int, match.groups()))


def _cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise UpdateCancelled()


def _validate_url(url):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
            or host not in ("api.github.com", "github.com", "objects.githubusercontent.com",
                           "release-assets.githubusercontent.com")):
        raise ValueError("更新地址必须是 GitHub 的 HTTPS 地址。")
    return url


def _asset_url(asset):
    url = _validate_url(asset.get("browser_download_url", ""))
    parsed = urlparse(url)
    if parsed.hostname != "github.com" or not parsed.path.startswith(f"/{REPOSITORY}/releases/download/"):
        raise ValueError("发布文件不属于配置的 GitHub 仓库。")
    return url


def _open(url, cancel=None, follow=True, attempts=3):
    # Lazy import keeps window startup light; ssl and its DLLs must be bundled.
    from urllib.request import Request, build_opener, HTTPRedirectHandler
    from urllib.error import HTTPError, URLError

    class Redirect(HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            _validate_url(newurl)
            return super().redirect_request(request, fp, code, msg, headers, newurl) if follow else None

    request = Request(_validate_url(url), headers={"User-Agent": f"DevmemStudio/{__version__}",
                                                   "Accept": "application/vnd.github+json"})
    for attempt in range(attempts):
        _cancelled(cancel)
        try:
            return build_opener(Redirect()).open(request, timeout=TIMEOUT)
        except HTTPError as exc:
            if not follow and exc.code in (301, 302, 303, 307, 308):
                return exc
            if exc.code == 404:
                raise NotFound("未找到正式发布版本；请检查 GitHub Releases 是否已发布。") from exc
            if exc.code in (403, 429):
                raise RateLimited("GitHub 拒绝请求或已达到访问限额，请稍后重试，也可打开发布页下载。") from exc
            raise RuntimeError(f"GitHub 请求失败（HTTP {exc.code}）。") from exc
        except (URLError, TimeoutError, OSError) as exc:
            # Proxies/CDN edges drop TLS handshakes now and then; retry before giving up.
            if attempt + 1 == attempts:
                raise RuntimeError(f"无法连接 GitHub，请检查网络或代理：{exc}") from exc
            if cancel is not None and cancel.wait(attempt + 1):
                raise UpdateCancelled()
            elif cancel is None:
                time.sleep(attempt + 1)


def _read(url, limit, cancel=None):
    _cancelled(cancel)
    with _open(url, cancel) as response:
        data = response.read(limit + 1)
    _cancelled(cancel)
    if len(data) > limit:
        raise ValueError("GitHub 响应超过大小限制。")
    return data


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    name: str
    url: str
    size: int
    digest: str = ""
    checksum_url: str = ""
    notes_html: bool = False


def parse_release(payload, current=__version__):
    if payload.get("draft") or payload.get("prerelease"):
        raise ValueError("更新源返回了非正式发布版本。")
    tag = payload.get("tag_name", "")
    remote = version_tuple(tag)
    if remote <= version_tuple(current):
        return None
    version = ".".join(map(str, remote))
    name = f"DevmemStudio-{version}-win64.zip"
    assets = payload.get("assets", [])
    matching = [item for item in assets if item.get("name") == name]
    if len(matching) != 1:
        raise ValueError(f"发布版本缺少唯一的 Windows 更新包：{name}")
    asset = matching[0]
    size = asset.get("size", 0)
    if type(size) is not int or not 0 < size <= MAX_PACKAGE:
        raise ValueError("更新包大小无效。")
    digest = asset.get("digest") or ""
    digest = digest[7:] if re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest) else ""
    checksum = [item for item in assets if item.get("name") == name + ".sha256"]
    checksum_url = _asset_url(checksum[0]) if len(checksum) == 1 else ""
    if not digest and not checksum_url:
        raise ValueError("发布版本缺少 SHA-256 校验值，请维护者补充更新包校验文件。")
    return Release(version, str(payload.get("body") or "暂无更新说明。")[:100000], name,
                   _asset_url(asset), size, digest.lower(), checksum_url)


def latest_from_redirect(cancel=None, current=__version__):
    """Resolve releases/latest via its web redirect, which the anonymous API quota does not cover."""
    response = _open(f"{RELEASES_URL}/latest", cancel, follow=False)
    location = response.headers.get("Location", "")
    response.close()
    match = re.fullmatch(rf"/{re.escape(REPOSITORY)}/releases/tag/([^/]+)", urlparse(location).path, re.I)
    if not match:
        raise NotFound("未找到正式发布版本；请检查 GitHub Releases 是否已发布。")
    tag = match.group(1)
    remote = version_tuple(tag)
    if remote <= version_tuple(current):
        return None
    version = ".".join(map(str, remote))
    name = f"DevmemStudio-{version}-win64.zip"
    base = f"https://github.com/{REPOSITORY}/releases/download/{tag}/"
    # Size comes from the API only; download uses Content-Length and the .sha256 asset.
    try:
        notes = feed_notes(_read(FEED_URL, 4 * 1024 * 1024, cancel), current, version)
    except (RuntimeError, ValueError, OSError, SyntaxError):   # SyntaxError covers ElementTree.ParseError
        notes = ""
    return Release(version, notes or "GitHub API 访问次数已达上限，暂未获取更新说明，可点击“打开发布页”查看。",
                   name, base + name, 0, "", base + name + ".sha256", bool(notes))


_PAGE_ONLY = "升级方式|校验|验证"   # release-page sections, redundant inside the updater


def _dialog_markdown(body):
    return re.sub(rf"^###\s*(?:{_PAGE_ONLY})\s*$.*?(?=^#{{1,3}}\s|\Z)", "", body, flags=re.M | re.S).strip()


def _dialog_html(body):
    return re.sub(rf"<h3[^>]*>\s*(?:{_PAGE_ONLY})\s*</h3>.*?(?=<h[1-3][\s>]|\Z)", "", body, flags=re.S).strip()


def feed_notes(data, current, latest):
    """HTML notes from the releases Atom feed for every version in (current, latest], newest first.

    The feed is a web page outside the API quota but lists only the newest 10 releases."""
    from xml.etree import ElementTree   # lazy: only needed when the API quota is exhausted

    atom = "{http://www.w3.org/2005/Atom}"
    low, high = version_tuple(current), version_tuple(latest)
    bodies = {}
    for entry in ElementTree.fromstring(data).iter(f"{atom}entry"):
        link = entry.find(f"{atom}link")
        match = re.search(r"/releases/tag/([^/?#]+)$", link.get("href", "") if link is not None else "")
        try:
            version = version_tuple(match.group(1))
        except (AttributeError, ValueError):
            continue
        bodies.setdefault(version, (entry.findtext(f"{atom}content") or "").strip())
    wanted = sorted((version for version in bodies if low < version <= high), reverse=True)
    if not wanted:
        return ""
    sections = []
    for version in wanted:
        text = ".".join(map(str, version))
        body = _dialog_html(bodies[version]) or "<p>暂无更新说明。</p>"
        if not re.match(rf"\s*<h[1-6][^>]*>[^<]*\b{re.escape(text)}\b", body):
            body = f"<h2>DevmemStudio {text}</h2>{body}"
        sections.append(body)
    if high not in bodies or min(bodies) > low:   # feed lags a new release or stops short of current
        sections.append("<p>部分版本的说明未列出，可点击“打开发布页”查看。</p>")
    return "\n".join(sections)[:100000]


def _formal_version(payload):
    """Version tuple of a published formal release, or None for drafts/prereleases/odd tags."""
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        return None
    try:
        return version_tuple(payload.get("tag_name", ""))
    except ValueError:
        return None


def combined_notes(payloads, current, latest):
    """Notes of every formal release in (current, latest], newest first, one section each."""
    low, high = version_tuple(current), version_tuple(latest)
    bodies = {}
    for payload in payloads:
        version = _formal_version(payload)
        if version is not None and low < version <= high:
            bodies[version] = _dialog_markdown(str(payload.get("body") or "")) or "暂无更新说明。"
    sections = []
    for version in sorted(bodies, reverse=True):
        text = ".".join(map(str, version))
        body = bodies[version]
        first = body.splitlines()[0]
        sections.append(body if first.startswith("#") and text in first else f"## DevmemStudio {text}\n\n{body}")
    return "\n\n".join(sections)


def check_release(cancel=None, current=__version__):
    try:
        payloads = json.loads(_read(API_URL, 8 * 1024 * 1024, cancel).decode("utf-8"))
    except RateLimited:
        # Anonymous API allows 60 requests/hour per IP, shared by everyone behind an office NAT.
        return latest_from_redirect(cancel, current)
    if not isinstance(payloads, list):
        raise ValueError("更新源返回的发布列表格式无效。")
    formal = [payload for payload in payloads if _formal_version(payload) is not None]
    if not formal:
        raise NotFound("未找到正式发布版本；请检查 GitHub Releases 是否已发布。")
    release = parse_release(max(formal, key=_formal_version), current)
    if release is None:
        return None
    return replace(release, notes=combined_notes(formal, current, release.version)[:100000])


def parse_checksum(data, name):
    text = data.decode("utf-8-sig").strip()
    match = re.fullmatch(r"([a-fA-F0-9]{64})(?:\s+\*?([^\r\n]+))?", text)
    if not match or (match.group(2) is not None and match.group(2) != name):
        raise ValueError("更新包校验文件格式或文件名不正确。")
    return match.group(1).lower()


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def extract_executable(archive, target, cancel=None):
    with zipfile.ZipFile(archive) as package:
        entries = [item for item in package.infolist() if not item.is_dir()]
        if len(entries) != 1 or entries[0].filename != "DevmemStudio/DevmemStudio.exe":
            raise ValueError("更新包必须只包含 DevmemStudio/DevmemStudio.exe。")
        entry = entries[0]
        if not 0 < entry.file_size <= MAX_PACKAGE or entry.flag_bits & 1:
            raise ValueError("更新包内的程序大小无效或已加密。")
        with package.open(entry) as source, Path(target).open("wb") as output:
            total = 0
            while block := source.read(1024 * 1024):
                _cancelled(cancel)
                total += len(block)
                if total > MAX_PACKAGE:
                    raise ValueError("解压后的程序超过大小限制。")
                output.write(block)
    with Path(target).open("rb") as executable:
        if executable.read(2) != b"MZ":
            raise ValueError("更新包内不是 Windows EXE。")


def _content_length(response):
    headers = getattr(response, "headers", None)
    value = headers.get("Content-Length", "") if headers is not None else ""
    return int(value) if str(value).isdigit() and 0 < int(value) <= MAX_PACKAGE else 0


@dataclass(frozen=True)
class Download:
    version: str
    executable: Path
    digest: str


def download_release(release, cancel=None, progress=lambda value: None, directory=None):
    base = Path(directory) if directory is not None else user_data_dir() / "updates"
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="download-", dir=base))
    started = time.monotonic()
    try:
        expected = release.digest
        if not expected:
            try:
                expected = parse_checksum(_read(release.checksum_url, 4096, cancel), release.name)
            except NotFound as exc:
                raise ValueError("发布版本缺少 SHA-256 校验文件，无法安全升级；请打开发布页手动下载。") from exc
        archive = work / "package.zip"
        digest = hashlib.sha256()
        received = 0
        _cancelled(cancel)
        with _open(release.url, cancel) as response, archive.open("wb") as output:
            size = release.size or _content_length(response)
            while True:
                _cancelled(cancel)
                if time.monotonic() - started > 1800:
                    raise TimeoutError("下载超过 30 分钟，请重试。")
                block = response.read(256 * 1024)
                if not block:
                    break
                received += len(block)
                if received > (size or MAX_PACKAGE) or received > MAX_PACKAGE:
                    raise ValueError("下载内容超过发布版本声明的大小。")
                output.write(block)
                digest.update(block)
                progress((received, size))
        _cancelled(cancel)
        if (size and received != size) or digest.hexdigest() != expected:
            raise ValueError("更新包不完整或 SHA-256 校验失败，原程序未修改。")
        executable = work / "DevmemStudio.exe"
        extract_executable(archive, executable, cancel)
        _cancelled(cancel)
        archive.unlink()
        return Download(release.version, executable, file_digest(executable))
    except Exception:
        shutil.rmtree(work)
        raise


def launcher_path():
    origin = os.environ.get("DEVMEMSTUDIO_LAUNCHER")
    if sys.platform != "win32" or not getattr(sys, "frozen", False) or not origin:
        raise ValueError("源码运行仅支持检查和下载更新；请使用单文件发布版执行重启升级。")
    target = Path(origin)
    if not target.is_absolute() or not target.is_file() or target.suffix.lower() != ".exe":
        raise ValueError("无法确认外层启动器位置，已停止升级。")
    return target


def backup_path(target, identifier):
    # Keep ".exe": office DLP encrypts *.bak written by PowerShell, leaving a backup that cannot run.
    # The old version then also starts with a plain double-click.
    backup = target.with_name(f"{target.stem}-{__version__}-backup{target.suffix}")
    return backup if not backup.exists() else target.with_name(f"{target.stem}-{__version__}-backup-{identifier[:8]}{target.suffix}")


def prepare_install(download, target=None):
    target = Path(target) if target is not None else launcher_path()
    identifier = uuid.uuid4().hex
    staged = target.parent / f".DevmemStudio-update-{identifier}.exe"
    work = download.executable.parent
    try:
        # Try the destination's permissions before shutting down. Staging beside
        # the target also lets Windows replace the EXE atomically on one volume.
        with download.executable.open("rb") as source, staged.open("xb") as output:
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
        if file_digest(staged) != download.digest:
            raise ValueError("安装前程序校验失败。")
        helper = work / "apply_update.ps1"
        shutil.copyfile(resource_path("assets/updater/apply_update.ps1"), helper)
        job = {"target": str(target), "staged": str(staged), "sha256": download.digest,
               "backup": str(backup_path(target, identifier)),
               "pids": [os.getpid()], "version": download.version,
               "result": str(work / "result.json")}
        launcher_pid = os.environ.get("DEVMEMSTUDIO_LAUNCHER_PID", "")
        if launcher_pid.isdigit() and int(launcher_pid) > 0:
            job["pids"].append(int(launcher_pid))
        path = work / "install.json"
        path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        return path
    except Exception:
        staged.unlink(missing_ok=True)
        raise


def start_install(job_path):
    job_path = Path(job_path)
    powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("_PYI") and key != "_MEIPASS2" and not key.startswith("DEVMEMSTUDIO_LAUNCHER")}
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return subprocess.Popen([str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-File", str(job_path.parent / "apply_update.ps1"), "-JobPath", str(job_path)],
                            env=environment, creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)


def discard_install(job_path):
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    Path(job["staged"]).unlink(missing_ok=True)
