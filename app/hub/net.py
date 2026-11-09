"""Small HTTPS helpers (standard library only)."""
import hashlib, json, urllib.parse, urllib.request

from . import VERSION

UA = f"TENET-Hub/{VERSION}"


def _req(url, headers=None):
    return urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache", **(headers or {})})


def get(url, timeout=30, limit=50_000_000, headers=None):
    req = _req(url, headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{url} is unexpectedly large")
    return data


def download(url, path, progress=lambda frac: None, timeout=30, limit=300_000_000, headers=None):
    """Saves a large file to `path`. -> its SHA-256 (upper-case hex). progress(0..1) if the size is known."""
    req = _req(url, headers)
    sha, done = hashlib.sha256(), 0
    with urllib.request.urlopen(req, timeout=timeout) as r, open(path, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        while chunk := r.read(1 << 20):
            done += len(chunk)
            if done > limit:
                raise ValueError(f"{url} is unexpectedly large")
            f.write(chunk)
            sha.update(chunk)
            if total:
                progress(min(done / total, 1.0))
    if total and done != total:
        raise ValueError("the download was cut off")
    return sha.hexdigest().upper()


def get_json(url, timeout=30):
    return json.loads(get(url, timeout).decode("utf-8"))


def resolve(base, url):
    """Index entries may use paths relative to the index file."""
    return urllib.parse.urljoin(base, url)
