"""Create the GitHub release for a version and upload its zip + .sha256; the token comes from git's credential helper.

Usage:
  release_github.py <version> <body-file>          create or refresh the release, upload the assets
  release_github.py <version> <body-file> status   print the release and its asset digests only
  release_github.py <version> status               same as above (the body file is optional here)

A new release is created as a draft, gets its assets, and is published only once they are all verified, so
update checks never see a release without its package. Reruns are cheap and non-destructive: an asset whose
size and sha256 already match the local file is left untouched; a changed one is uploaded under a temporary
name, the old one deleted, and the new one renamed.
"""
import hashlib
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "shiknevus/devmemstudio"
API_TIMEOUT = 45          # socket timeout per attempt
CALL_DEADLINE = 60        # hard wall clock per call: urlopen's timeout does not cover a stalled DNS/TLS handshake
TOTAL_DEADLINE = 240      # give up on the whole run instead of hanging; the caller can just rerun it
API_ATTEMPTS = 8
UPLOAD_ATTEMPTS = 5
UPLOAD_TIMEOUT = 300      # the archive itself needs a generous socket timeout
STARTED = time.monotonic()

arguments = sys.argv[1:]
mode = "status" if "status" in arguments else "publish"
arguments = [a for a in arguments if a != "status"]
if not arguments:
    raise SystemExit(__doc__)
version = arguments[0]
body_file = Path(arguments[1]) if len(arguments) > 1 else None
tag = f"v{version}"
archive = ROOT / "dist" / f"DevmemStudio-{version}-win64.zip"
checksum = archive.with_name(archive.name + ".sha256")   # in-app updater falls back to it when the API is rate-limited

# Validate every local input before touching the release: an interrupted run must not be able to
# delete an uploaded asset on its way to failing.
if mode == "publish":
    if body_file is None:
        raise SystemExit(f"publish mode needs <body-file>; add `status` to only inspect the release\n\n{__doc__}")
    if not body_file.is_file():
        raise SystemExit(f"missing release body {body_file}")
    if not archive.is_file():
        raise SystemExit(f"missing {archive}")
    if not checksum.is_file():
        raise SystemExit(f"missing {checksum.name}; run tools/make_release.py first")

try:
    fill = subprocess.run(["git", "credential", "fill"], input=b"protocol=https\nhost=github.com\n\n",
                          capture_output=True, cwd=ROOT, check=True, timeout=60)   # bytes: text mode sends CRLF
except subprocess.TimeoutExpired:
    raise SystemExit("git credential fill timed out after 60s; the credential helper is stuck — retry or `git fetch` first")
credentials = dict(line.split("=", 1) for line in fill.stdout.decode().splitlines() if "=" in line)
token = credentials.get("password") or credentials.get("oauth_token")
if not token:
    raise SystemExit("no GitHub token from git credential fill; configure a credential helper first")


def bounded(call, seconds=CALL_DEADLINE):
    """Run one blocking network call on a worker thread and give up after `seconds` of wall clock."""
    outcome = {}

    def worker():
        try:
            outcome["value"] = call()
        except BaseException as exc:      # surfaced to the caller below
            outcome["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise TimeoutError(f"no response within {seconds}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def api(method, url, data=None, content_type="application/json"):
    """One API call, retried on transport errors so a network blip cannot abandon the release."""
    last = None
    for attempt in range(API_ATTEMPTS):
        if time.monotonic() - STARTED > TOTAL_DEADLINE:
            raise SystemExit(f"{method} {url} gave up after {TOTAL_DEADLINE}s of retries ({last}); rerun the command")
        print(f"{method} {url} attempt {attempt + 1}/{API_ATTEMPTS}", flush=True)
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "Content-Type": content_type, "X-GitHub-Api-Version": "2022-11-28"})
        try:
            return bounded(lambda: json.loads(_read(request)))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise          # the caller decides between "no release yet" and "release is gone"
            last = exc
        except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
        if attempt + 1 < API_ATTEMPTS:
            print(f"{method} {url} attempt {attempt + 1} failed: {last}; retrying", flush=True)
            time.sleep(3)
    raise SystemExit(f"{method} {url} failed after {API_ATTEMPTS} attempts: {last}")


def _read(request):
    with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
        return response.read() or b"{}"


def sha256_of(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


commit = subprocess.run(["git", "rev-parse", f"{tag}^{{commit}}"], capture_output=True, text=True, cwd=ROOT, check=True).stdout.strip()
try:
    release = api("GET", f"https://api.github.com/repos/{REPO}/releases/tags/{tag}")   # rerun: keep the release
    if mode == "publish" and body_file is not None:
        release = api("PATCH", f"https://api.github.com/repos/{REPO}/releases/{release['id']}", json.dumps({
            "target_commitish": commit, "name": f"DevmemStudio {version}",
            "body": body_file.read_text(encoding="utf-8")}).encode())
except urllib.error.HTTPError as exc:
    if exc.code != 404:
        raise
    # The tag lookup never returns drafts: a draft left by an interrupted run is found in the list instead.
    release = next((item for item in api("GET", f"https://api.github.com/repos/{REPO}/releases?per_page=100")
                    if item.get("draft") and item.get("tag_name") == tag), None)
    if release is None and mode == "status":
        raise SystemExit(f"{tag} has no GitHub release yet")
    if release is None:   # a draft until its assets are all up: clients checking for updates never see a release without a package
        release = api("POST", f"https://api.github.com/repos/{REPO}/releases", json.dumps({
            "tag_name": tag, "target_commitish": commit, "name": f"DevmemStudio {version}",
            "body": body_file.read_text(encoding="utf-8"), "draft": True, "prerelease": False}).encode())
    elif mode == "publish":
        release = api("PATCH", f"https://api.github.com/repos/{REPO}/releases/{release['id']}", json.dumps({
            "target_commitish": commit, "name": f"DevmemStudio {version}",
            "body": body_file.read_text(encoding="utf-8")}).encode())

if mode == "status":
    for asset in release["assets"]:
        print(f"{asset['name']} {asset['size']} {asset['state']} {asset.get('digest')}")
    print(f"{release['html_url']} target={release['target_commitish']} assets={len(release['assets'])}")
    raise SystemExit

uploaded = []


def upload_asset(path: Path, content_type: str, name: str):
    """Upload one asset with retries; the name is URL encoded independently."""
    asset = None
    for attempt in range(UPLOAD_ATTEMPTS):
        request = urllib.request.Request(
            f"https://uploads.github.com/repos/{REPO}/releases/{release['id']}/assets?name="
            f"{urllib.parse.quote(name, safe='')}",
            data=path.read_bytes(), method="POST",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "Content-Type": content_type, "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(request, timeout=UPLOAD_TIMEOUT) as response:
                asset = json.loads(response.read() or b"{}")
            break
        except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"{name} upload attempt {attempt + 1} failed: {exc}", flush=True)
            if attempt + 1 < UPLOAD_ATTEMPTS:
                time.sleep(5)
    if asset is None:
        raise SystemExit(f"{name} upload failed after {UPLOAD_ATTEMPTS} attempts; "
                         "the release may need this command rerun")
    return asset


for path, content_type in ((archive, "application/zip"), (checksum, "text/plain")):
    digest = sha256_of(path)
    stale = next((asset for asset in release.get("assets", []) if asset["name"] == path.name), None)
    if stale and stale.get("size") == path.stat().st_size and (stale.get("digest") or "") == f"sha256:{digest}":
        print(f"{path.name} already uploaded and identical; skipped", flush=True)
        uploaded.append({"asset": stale["name"], "size": stale["size"], "state": stale["state"],
                         "digest": stale.get("digest")})
        continue
    # Drop any dangling replacement assets left by interrupted runs
    for dangling in [a for a in release.get("assets", []) if a["name"].startswith(f"{path.name}.replacement-")]:
        try:
            api("DELETE", dangling["url"])
        except (SystemExit, urllib.error.HTTPError):
            pass   # api() gives up with SystemExit, which `except Exception` misses
    if stale:
        # GitHub rejects an upload while an asset has the same name.  Upload the *new* bytes under a temporary
        # name first, drop the old asset, then rename: no second upload, and the gap without the package is two calls.
        staged = upload_asset(path, content_type, f"{path.name}.replacement-{digest[:12]}")
        if staged.get("state") not in (None, "uploaded"):
            raise SystemExit(f"{staged.get('name')} ended in state {staged.get('state')}; rerun the command")
        try:
            api("DELETE", stale["url"])
        except urllib.error.HTTPError as exc:
            if exc.code != 404:   # already gone is fine
                raise
        try:
            asset = api("PATCH", staged["url"], json.dumps({"name": path.name}).encode())
        except SystemExit as exc:
            raise SystemExit(f"renaming {staged.get('name')} to {path.name} failed. "
                             "Rerun this command to finish the publish.") from exc
    else:
        try:
            asset = upload_asset(path, content_type, path.name)   # retries internally
        except SystemExit as exc:
            raise SystemExit(f"{path.name} upload failed. Rerun this command to finish the publish.") from exc
    if asset.get("state") not in (None, "uploaded"):
        # starter = half uploaded: a rerun replaces this asset
        raise SystemExit(f"{path.name} ended in state {asset.get('state')}; rerun the command")
    uploaded.append({"asset": asset.get("name"), "size": asset.get("size"), "state": asset.get("state"),
                     "digest": asset.get("digest")})

# Every asset up and matching the local files before the release becomes visible to update checks.
final = api("GET", f"https://api.github.com/repos/{REPO}/releases/{release['id']}")
problems = []
for path in (archive, checksum):
    found = [asset for asset in final.get("assets", []) if asset["name"] == path.name]
    if len(found) != 1:
        problems.append(f"{path.name}: {len(found)} assets by that name")
    elif found[0].get("state") != "uploaded" or found[0].get("size") != path.stat().st_size:
        problems.append(f"{path.name}: state {found[0].get('state')}, size {found[0].get('size')} (local {path.stat().st_size})")
    elif found[0].get("digest") and found[0]["digest"] != f"sha256:{sha256_of(path)}":
        problems.append(f"{path.name}: digest {found[0]['digest']} differs from the local file")
if problems:
    raise SystemExit("release not published, assets do not match: " + "; ".join(problems) + ". Rerun the command.")
if final.get("draft"):
    final = api("PATCH", f"https://api.github.com/repos/{REPO}/releases/{release['id']}",
                json.dumps({"draft": False, "prerelease": False}).encode())
print(json.dumps({"release": final["html_url"], "tag": tag, "commit": commit, "draft": bool(final.get("draft")),
                  "assets": uploaded}, ensure_ascii=False))