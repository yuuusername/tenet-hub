"""TENET Hub updating itself, against a local copy of a GitHub release. (No keyboard needed.)

    python tests/test_updater.py
"""
import functools, hashlib, http.server, json, os, subprocess, sys, tempfile, threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="tenethub-update-")
sys.path.insert(0, os.path.join(ROOT, "app"))
from hub import power, updater                             # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name)


def read(p):
    with open(p, "rb") as f:
        return f.read()


def write(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


# --- a local "GitHub Releases" -------------------------------------------------------------------
WEB = os.path.join(TMP, "web")
os.makedirs(WEB)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(
    type("Q", (http.server.SimpleHTTPRequestHandler,), {
        "log_message": lambda *a: None,
        "do_GET": lambda self: self.send_error(504) if self.path.startswith("/down/")   # GitHub having a bad day
        else http.server.SimpleHTTPRequestHandler.do_GET(self)}), directory=WEB))
threading.Thread(target=srv.serve_forever, daemon=True).start()
updater.RELEASES = f"http://127.0.0.1:{srv.server_port}/releases"
NEW = os.urandom(3_000_000)


def release(version, data=NEW, sha=None):
    d = os.path.join(WEB, "releases", "download", f"v{version}")
    write(os.path.join(d, updater.ASSET), data)
    write(os.path.join(d, updater.ASSET + ".sha256"), (sha or hashlib.sha256(data).hexdigest().upper()).encode())


def app_folder():
    exe = os.path.join(tempfile.mkdtemp(dir=TMP), "TenetHub.exe")
    write(exe, b"old version")
    return exe


def failure(fn):
    try:
        fn()
    except updater.UpdateError as e:
        return str(e)
    return None


checked = []
updater.check_exe = lambda path, version: checked.append((read(path), version))
# --- prepare: nothing changes unless the download is right ----------------------------------------
release("2.0.0")
exe = app_folder()
fracs = []
new = updater.prepare("2.0.0", exe, fracs.append)
check("a good release downloads next to the .exe", read(new) == NEW and os.path.dirname(new) == os.path.dirname(exe))
check("it's self-checked before anything changes", checked == [(NEW, "2.0.0")] and read(exe) == b"old version")
check(f"progress reaches 100% ({len(fracs)} steps)", fracs and fracs[-1] == 1.0)

release("2.0.1", sha="0" * 64)
exe = app_folder()
err = failure(lambda: updater.prepare("2.0.1", exe))
check(f"wrong checksum refused: {err}", err and "checksum" in err)
check("...nothing left behind, the .exe unchanged", os.listdir(os.path.dirname(exe)) == ["TenetHub.exe"] and read(exe) == b"old version")

release("2.0.2", sha="not a checksum")
err = failure(lambda: updater.prepare("2.0.2", exe))
check(f"unreadable checksum refused: {err}", err and "checksum" in err)

err = failure(lambda: updater.prepare("3.0.0", exe))
check(f"a release that isn't built yet: {err}", err and "isn't ready" in err)

err = failure(lambda: updater.prepare("../../evil", exe))
check(f"a version that isn't a version: {err}", err and "version number" in err)

# GitHub's download link failing (504) for a release: the same files through its API instead
base = f"http://127.0.0.1:{srv.server_port}"
updater.RELEASES, updater.API = f"{base}/down", f"{base}/api"
assets = os.path.join(WEB, "api", "releases", "assets")
write(os.path.join(assets, "1"), NEW)
write(os.path.join(assets, "2"), hashlib.sha256(NEW).hexdigest().upper().encode())
write(os.path.join(WEB, "api", "releases", "tags", "v2.0.4"), json.dumps({"assets": [
    {"name": updater.ASSET, "url": f"{updater.API}/releases/assets/1"},
    {"name": updater.ASSET + ".sha256", "url": f"{updater.API}/releases/assets/2"}]}).encode())
exe = app_folder()
new = updater.prepare("2.0.4", exe)
check("a 504 on the download link falls back to GitHub's API", read(new) == NEW)
err = failure(lambda: updater.prepare("2.0.5", exe))
check(f"...and says the download failed when that has nothing either: {err}", err and "504" in err)
updater.RELEASES = f"{base}/releases"


def refuses(path, version):
    raise updater.UpdateError("failed its self-check")


updater.check_exe = refuses
release("2.0.3")
err = failure(lambda: updater.prepare("2.0.3", exe))
check(f"a release that fails its self-check: {err}", err and "self-check" in err)
check("...nothing left behind", os.listdir(os.path.dirname(exe)) == ["TenetHub.exe"])

# --- install -------------------------------------------------------------------------------------
started = []
real_popen = subprocess.Popen


def fake_popen(args, **k):
    started.append((args, k))


subprocess.Popen = fake_popen
exe = app_folder()
new = updater._download_path(exe)
write(new, b"new version")
os.environ["_MEIPASS2"] = "inherited"
os.environ["_PYI_APPLICATION_HOME_DIR"] = "inherited"
updater.install(new, ["--tray"], exe)
check("the new version is at the same path", read(exe) == b"new version" and not os.path.exists(new))
args, kw = started[0]
check(f"it's started with --updated: {args}", args == [exe, "--updated", "--tray"])
env = kw["env"]
check("it won't reuse this copy's temp folder",
      env["PYINSTALLER_RESET_ENVIRONMENT"] == "1" and "_MEIPASS2" not in env and "_PYI_APPLICATION_HOME_DIR" not in env)
olds = [f for f in os.listdir(os.path.dirname(exe)) if f.endswith(".old")]
check(f"the old version is set aside: {olds}", len(olds) == 1)

write(new, b"newer version")                               # a second update while the first .old is locked
updater.install(new, [], exe)
olds = [f for f in os.listdir(os.path.dirname(exe)) if f.endswith(".old")]
check(f"a second update doesn't need the first .old gone: {len(olds)}", len(olds) == 2 and read(exe) == b"newer version")

# cleanup, with one copy still locked by something (open = can't be deleted on Windows)
d = os.path.dirname(exe)
locked = os.path.join(d, olds[0])
write(new, b"interrupted download")
held = open(locked, "rb")
updater.cleanup(exe, tries=1).join()
left = sorted(f for f in os.listdir(d) if f != "TenetHub.exe")
if sys.platform == "win32":
    check(f"cleanup survives a locked copy: {left}", left == [olds[0]])
held.close()
updater.cleanup(exe, tries=1).join()
check("cleanup removes the old copies and interrupted downloads", os.listdir(d) == ["TenetHub.exe"] and read(exe) == b"newer version")


def cant_start(args, **k):
    raise OSError("blocked by antivirus")


subprocess.Popen = cant_start
exe = app_folder()
new = updater._download_path(exe)
write(new, b"new version")
err = failure(lambda: updater.install(new, [], exe))
check(f"if it won't start, the old version goes back: {err}",
      err and read(exe) == b"old version" and os.listdir(os.path.dirname(exe)) == ["TenetHub.exe"])
subprocess.Popen = real_popen

# --- the new copy waits for the old one to exit ----------------------------------------------------
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    name = f"TenetHubTest{os.getpid()}"
    check("first copy gets the lock", power.single_instance(name))
    first = power.single_instance._h
    check("another copy doesn't, without waiting", not power.single_instance(name))
    check("...nor after a short wait", not power.single_instance(name, wait=0.3))
    k32 = ctypes.WinDLL("kernel32")
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    threading.Timer(0.5, k32.CloseHandle, [first]).start()
    check("a waiting copy gets it when the first one exits", power.single_instance(name, wait=5))

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
