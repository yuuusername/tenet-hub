"""TENET Hub updating itself (the packaged Windows .exe).

Windows won't let a running .exe be overwritten, but it does let one be renamed. So:

  prepare()  downloads the new .exe next to the current one, checks it against the SHA-256 published
             with the release, and test-runs it (--write-version): it has to start here and report
             the version we asked for. Nothing has changed yet if any of this fails.
  install()  renames the running .exe aside, moves the new one to its path (so the start-at-login
             entry and the sensor helper's task still point at it) and starts it with --updated,
             which makes it wait for this copy to exit. The caller then quits.
  cleanup()  run at every start: deletes the renamed copy. The sensor helper may still be running
             from it until the next login, so it can stay locked for a while; that's harmless.

Downloads only ever come from this repo's GitHub releases: the catalogue says which version is
current, but it can't point the app at some other .exe to run.
"""
import glob, os, re, subprocess, sys, tempfile, threading, time, urllib.error

from . import net

RELEASES = "https://github.com/yuuusername/tenet-hub/releases"
API = "https://api.github.com/repos/yuuusername/tenet-hub"   # same files, other route (see _fetch)
ASSET = "TenetHub.exe"
LIMIT = 300_000_000
CHECK_TIMEOUT = 180                # seconds; a one-file .exe unpacks itself first (and antivirus may scan it)
NO_WINDOW = 0x08000000             # CREATE_NO_WINDOW
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


class UpdateError(Exception):
    pass


def supported():
    """Only the packaged .exe can replace itself (from source, git does that)."""
    return sys.platform == "win32" and getattr(sys, "frozen", False)


def child_env():
    """The environment for starting another copy of this .exe. A one-file .exe tells its own child
    process where it unpacked through these variables; a new copy that inherited them would run from
    this copy's temporary folder, which is deleted when this copy exits."""
    env = {k: v for k, v in os.environ.items() if k != "_MEIPASS2" and not k.startswith("_PYI_")}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def _download_path(exe):
    return exe + ".update.exe"


def check_exe(path, version):
    """Runs the downloaded .exe far enough to know it works and is the version it should be."""
    fd, out = tempfile.mkstemp(prefix="tenethub-version-")
    os.close(fd)
    try:
        try:
            r = subprocess.run([path, "--write-version", out], env=child_env(), timeout=CHECK_TIMEOUT,
                               creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            raise UpdateError(f"The new version wouldn't start ({e}). Antivirus software may have blocked it.")
        with open(out, encoding="utf-8") as f:
            got = f.read().strip()
        if r.returncode != 0 or got != version:
            raise UpdateError(f"The new version failed its self-check (exit code {r.returncode}, "
                              f"reported version '{got}', expected '{version}').")
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def _fetch(version, url, name, fetch):
    """fetch(url, headers) from the release's download link; if GitHub fails there (a 5xx or no
    answer, as happens for a while after some releases), the same asset through GitHub's API."""
    try:
        return fetch(url, None)
    except urllib.error.HTTPError as e:
        if e.code < 500:
            raise
        first = e
    except (urllib.error.URLError, TimeoutError) as e:
        first = e
    try:
        rel = net.get_json(f"{API}/releases/tags/v{version}")
        asset = next(a for a in rel.get("assets", []) if a.get("name") == name)
        if not str(asset.get("url", "")).startswith(API + "/releases/assets/"):
            raise first
        return fetch(asset["url"], {"Accept": "application/octet-stream"})
    except (OSError, ValueError, StopIteration):
        raise first


def prepare(version, exe=None, progress=lambda frac: None):
    """Downloads and verifies a release. -> the path of the checked .exe, ready for install()."""
    if not re.fullmatch(r"\d+(\.\d+)*", str(version)):
        raise UpdateError(f"'{version}' isn't a version number.")
    exe = exe or sys.executable
    url = f"{RELEASES}/download/v{version}/{ASSET}"
    new = _download_path(exe)
    try:
        expected = _fetch(version, url + ".sha256", ASSET + ".sha256",
                          lambda u, h: net.get(u, limit=1000, headers=h)).decode("ascii").split()
        expected = expected[0].upper() if expected else ""
        if not re.fullmatch(r"[0-9A-F]{64}", expected):
            raise UpdateError("The checksum published with the release isn't readable.")
        got = _fetch(version, url, ASSET, lambda u, h: net.download(u, new, progress, limit=LIMIT, headers=h))
        if got != expected:
            raise UpdateError("The download doesn't match the release's checksum.")
        check_exe(new, str(version))
    except BaseException as e:
        try:
            os.remove(new)
        except OSError:
            pass
        if isinstance(e, urllib.error.HTTPError) and e.code == 404:
            raise UpdateError(f"The download for version {version} isn't ready yet. GitHub may still be "
                              "building it; try again in a few minutes.") from e
        if isinstance(e, PermissionError):
            raise UpdateError(f"TENET Hub can't write to its folder ({os.path.dirname(exe)}).") from e
        if isinstance(e, (OSError, ValueError)):
            raise UpdateError(f"The download failed: {e}") from e
        raise
    return new


def install(new, args=(), exe=None):
    """Puts the checked .exe in place and starts it. On failure everything goes back as it was.
    On success the caller must quit: the new copy waits for that."""
    exe = exe or sys.executable
    old = f"{exe}.{time.time_ns()}.old"           # unique: an earlier one may still be locked
    try:
        os.replace(exe, old)
    except OSError as e:
        raise UpdateError(f"TENET Hub couldn't replace its .exe: {e}") from e
    try:
        os.replace(new, exe)
        subprocess.Popen([exe, "--updated", *args], env=child_env(), creationflags=DETACHED,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        os.replace(old, exe)
        raise UpdateError(f"The new version wouldn't start: {e}") from e


def cleanup(exe=None, tries=12, every=5.0):
    """Removes what an update left behind. The previous copy takes a moment to finish exiting, so
    this keeps trying for a minute in the background."""
    exe = exe or sys.executable
    try:
        os.remove(_download_path(exe))            # an update that was interrupted
    except OSError:
        pass

    def run():
        for i in range(tries):
            left = 0
            for p in glob.glob(glob.escape(exe) + ".*.old"):
                try:
                    os.remove(p)
                except OSError:
                    left += 1
            if not left:
                return
            if i < tries - 1:
                time.sleep(every)
    t = threading.Thread(target=run, name="update-cleanup", daemon=True)
    t.start()
    return t
