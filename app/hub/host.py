"""The background engine: owns the keyboard connection and runs packages.

Each tick it reads which screen the keyboard shows (REVIVER v6+). If an enabled screen package owns
that screen it gets frame() calls at its fps and its images are drawn. Background packages get
events. The host also does the screen-off rules (PC asleep / monitor off, idle, nothing to show),
nudges package screens sideways against burn-in, and reconnects when the keyboard is replugged.
"""
import ctypes, datetime, os, sys, threading, time, traceback

from PIL import Image

from . import screens as screenlist, api, gif, power, stats as pcstats
from .config import data_dir, windows_uses_12h
from .device import Keyboard, enumerate_keyboards
from .screen import Screen

SHIFT_EVERY = 120
SHIFT_PATTERN = [0, 1, 2, 1, 0, -1, -2, -1]
FULL_REFRESH = 10.0
MAX_ERRORS = 5


def idle_seconds():
    if sys.platform != "win32":
        return 0.0
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    li = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
        return 0.0
    return ((ctypes.windll.kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000.0


class Running:
    def __init__(self, manifest, obj, ctx):
        self.m, self.obj, self.ctx = manifest, obj, ctx
        self.errors = 0
        self.next_frame = 0.0
        self.none_since = None
        self.shown = False


class Host:
    def __init__(self, cfg, registry, log=print, notify=lambda title, msg: None):
        self.cfg, self.reg, self.log, self.notify = cfg, registry, log, notify
        self.kb = self.fw = self.screen = None
        self.status = "Starting..."
        self.mode = None
        self.slots, self.want = {}, None             # v21: package id -> screen code; list to keep
        self._sent_list, self._sent_at = None, 0.0
        self._clock_at, self._clock_fmt = 0.0, None   # last clock sync; 12h flag sent this connection
        self.stats = pcstats.Sampler(cfg, log)           # PC stats screen (v24)
        self.brightness, self._bright_at, self._bright_want = None, 0.0, None
        self._units_sent, self._stats_at, self._labels_sent = None, 0.0, {}
        self._kb_slots = None
        self.storing = None                           # (package id, fraction, message) while storing one
        self._store_failed = {}                       # package id -> (version, why): not retried until turned off and on
        self.running = {}                 # id -> Running
        self.problems = {}                # id -> why it isn't running
        self.paused = threading.Event()   # set while flashing: the host lets go of the keyboard
        self.stopped = threading.Event()
        self.lock = threading.RLock()
        self.asleep = False
        self.watcher = power.PowerWatcher(on_off=self._sleep, on_on=self._wake,
                                          with_display=cfg["off_with_monitor"], log=log)
        self._last_full = 0.0
        self._requests = []

    # --- lifecycle ---------------------------------------------------------------------
    def start(self):
        if self.cfg["off_when_pc_sleeps"]:
            self.watcher.start()
        threading.Thread(target=self._loop, name="host", daemon=True).start()

    def stop(self):
        self.stopped.set()
        self.stats.stop()
        with self.lock:
            for pid in list(self.running):
                self._stop_pkg(pid)

    def release_keyboard(self):
        """Pause and disconnect (before flashing). resume() reconnects."""
        self.paused.set()
        with self.lock:
            self._disconnect("Paused for a firmware update")

    def resume(self):
        self.paused.clear()

    def reload(self):
        """Settings or installed packages changed: restart what's affected on the next tick."""
        with self.lock:
            for pid in list(self.running):
                self._stop_pkg(pid)
            self.problems.clear()

    # --- power ----------------------------------------------------------------------------
    def _sleep(self):
        self.asleep = True
        with self.lock:
            if self.screen:
                self.screen.hold_off = True
                try:
                    self.screen.power(False)
                except Exception:
                    pass
        self._event("sleep")

    def _wake(self):
        self.asleep = False
        self._clock_at = 0.0                         # the PC slept: set the keyboard's clock again
        with self.lock:
            if self.screen:
                self.screen.hold_off = False
        self._event("wake")

    # --- packages ---------------------------------------------------------------------------
    def _v21(self):
        return self.fw is not None and self.fw.at_least(screenlist.MIN_PATCH)

    def _mode_for(self, m):
        if self.fw is None or self._v21():
            return 5                                 # v21: every screen package has a screen of its own
        mode = self.cfg.pkg(m["id"]).get("mode") or m.get("default_mode")
        return mode if mode in api.PACKAGE_MODES else None

    def _request(self, kind, value, pid=None):
        self._requests.append((kind, value, pid))
        return True

    def _start_pkg(self, m):
        pid = m["id"]
        need = int(m.get("requires_firmware") or 0)
        if need and self.fw is None:
            return                                   # waiting for the keyboard
        if need and not self.fw.at_least(need):
            self.problems[pid] = f"Needs REVIVER v{need} or newer"
            return
        if m["type"] == "screen" and self._mode_for(m) is None:
            self.problems[pid] = "No screen chosen"
            return
        try:
            cls = self.reg.load_class(m)
            obj = cls()
            settings = dict(m.defaults())
            settings.update(self.cfg.pkg(pid).get("settings") or {})
            ctx = api.Context(pid, settings, data_dir("package-data", pid),
                              lambda *a, pid=pid: self.log(f"[{pid}]", *a), self.fw,
                              lambda kind, value, pid=pid: self._request(kind, value, pid))
            ctx.screen_mode = self._mode_for(m) if m["type"] == "screen" else None
            obj.start(ctx)
            self.running[pid] = Running(m, obj, ctx)
            self.problems.pop(pid, None)
            self.log(f"started {m['name']} {m['version']}")
        except Exception as e:
            self.problems[pid] = f"Failed to start: {e}"
            self.log(f"{pid} failed to start:\n{traceback.format_exc()}")

    def _stop_pkg(self, pid):
        r = self.running.pop(pid, None)
        if r:
            try:
                r.obj.stop()
            except Exception:
                pass

    def _sync_packages(self):
        wanted = {pid: m for pid, m in self.reg.installed.items()
                  if self.cfg.pkg(pid).get("enabled") and m["type"] != "animation"}
        for pid in list(self.running):
            if pid not in wanted or self.running[pid].m is not wanted[pid]:
                self._stop_pkg(pid)
        claimed = {}
        for pid, m in wanted.items():
            if m["type"] == "screen" and not self._v21() and self.fw is not None:
                mode = self._mode_for(m)
                if mode in claimed and claimed[mode] != pid:
                    self.problems[pid] = f"{api.PACKAGE_MODES[mode]} is used by {claimed[mode]}"
                    self._stop_pkg(pid)
                    continue
                claimed[mode] = pid
            if pid not in self.running and pid not in self.problems:
                self._start_pkg(m)
        for pid in list(self.problems):
            if pid not in wanted and self.reg.installed.get(pid, {}).get("type") != "animation":
                self.problems.pop(pid)
        patch = self.fw.patch if self.fw is not None and self.fw.kind == "patch" else 99
        self.want, self.slots = screenlist.keyboard_list(screenlist.entries(self.cfg, self.reg), patch,
                                                         {p: v["slot"] for p, v in self.stored().items()})
        if self.want and screenlist.STATS in self.want:
            self.stats.start()
        else:
            self.stats.stop()

    def _fail(self, r, e):
        r.errors += 1
        self.log(f"{r.m['id']} error:\n{traceback.format_exc()}")
        if r.errors >= MAX_ERRORS:
            self._stop_pkg(r.m["id"])
            self.problems[r.m["id"]] = f"Stopped after repeated errors: {e}"
            self.notify(r.m["name"], "Stopped after repeated errors (see the log).")

    def _event(self, name, data=None):
        for r in list(self.running.values()):
            if r.m["type"] == "background":
                try:
                    r.obj.on_event(name, data)
                except Exception as e:
                    self._fail(r, e)

    # --- keyboard -----------------------------------------------------------------------
    def _disconnect(self, status):
        if self.kb:
            self.kb.close()
            self._event("disconnected")
        self.kb = self.screen = None
        self.fw = None
        self.status = status

    def _connect(self):
        kind, path = enumerate_keyboards()
        if kind == "iap":
            self.status = "Keyboard is in update mode (install firmware from the Firmware tab)"
            return False
        if kind is None:
            self.status = "Keyboard not found (plugged in? Hexcore Link closed?)"
            return False
        kb = Keyboard(path)
        fw = kb.probe()
        if fw is None:
            kb.close()
            self.status = "Keyboard found but not answering yet - retrying"
            return False
        self.kb, self.fw = kb, fw
        self._sent_list, self._sent_at = None, 0.0
        self._clock_at, self._clock_fmt = 0.0, None   # last clock sync; 12h flag sent this connection
        self._units_sent, self._stats_at, self._labels_sent = None, 0.0, {}
        self._kb_slots = None                         # v26 image slots as read from this keyboard
        self._lights_sent = None
        self.brightness, self._bright_at = None, 0.0  # key lights 0-100, as the keyboard last said
        self.screen = Screen(kb, fw, self.cfg["contrast"]) if fw.at_least(1) else None
        if self.screen:
            self.screen.hold_off = self.asleep
            if self.asleep:
                self.screen.power(False)
        self.status = f"Connected - {fw}"
        self.log("keyboard connected:", fw)
        if str(fw) != getattr(self, "_last_fw", None):    # different firmware: re-check packages
            for pid in list(self.running):
                self._stop_pkg(pid)
            self._last_fw = str(fw)
        for r in self.running.values():
            r.ctx.firmware = fw
        self.problems.clear()                      # firmware may have changed: re-check
        self._event("connected", fw)
        return True

    # --- stored images: animation packages on screens of their own (v26) ------------------
    def stored(self):
        """{package id: {"slot", "version"}} stored on this keyboard by TENET Hub."""
        return dict(self.cfg.get("stored_images") or {})

    def _set_stored(self, d):
        self.cfg.set("stored_images", d)

    def _sync_stored(self):
        """Store each enabled animation package in an image slot of its own (one per tick: it takes
        a while), and free the slots of packages turned off or removed."""
        if self._kb_slots is None:
            self._kb_slots = gif.read_slots(self.kb)
        stored = self.stored()
        wanted = {pid: m for pid, m in self.reg.installed.items()
                  if m.get("type") == "animation" and self.cfg.pkg(pid).get("enabled")}
        for pid in list(self.problems):
            if pid not in wanted and self.reg.installed.get(pid, {}).get("type") == "animation":
                self.problems.pop(pid)
        for pid in list(self._store_failed):                      # turned off or removed: may try again
            if pid not in wanted:
                self._store_failed.pop(pid)
        for pid in list(stored):                                # turned off / removed: free the slot
            if pid not in wanted:
                s = stored.pop(pid)["slot"]
                try:
                    gif.clear(self.kb, s)
                except gif.GifError as e:
                    self.log("couldn't clear image slot", s, e)
                self._kb_slots.pop(s, None)
                self._set_stored(stored)
                self.log(f"removed {pid} from image slot {s}")
                return
        for pid, m in sorted(wanted.items()):
            have = stored.get(pid)
            if have and have.get("version") == m["version"] and have["slot"] in self._kb_slots:
                self.problems.pop(pid, None)
                continue
            failed = self._store_failed.get(pid)
            if failed and failed[0] == m["version"]:              # failed for this version: wait for a change
                self.problems[pid] = failed[1]                    # (reload() clears problems: keep it showing)
                continue
            used = {v["slot"] for p, v in stored.items() if p != pid}
            slot = have["slot"] if have else next((s for s in range(1, gif.SLOTS) if s not in used), None)
            if slot is None:
                self.problems[pid] = f"No free stored screen (the keyboard holds {gif.SLOTS - 1}); turn another off"
                continue
            try:
                s = dict(gif.DEFAULTS)
                s.update(m.get("gif_settings") or {})
                frames = gif.convert(m.animation_path(), s)
                self.storing = (pid, 0.0, "Preparing...")
                self.log(f"storing {pid} {m['version']} in image slot {slot} ({len(frames)} frames)")
                gif.upload(self.kb, frames, s["burst"], lambda frac, msg, pid=pid: setattr(self, "storing", (pid, frac, msg)),
                           slot=slot)
            except gif.GifError as e:
                why = f"Couldn't store it on the keyboard: {e} Turn it off and on to try again."
                self.log(f"couldn't store {pid} in image slot {slot}: {e}")
                self.problems[pid] = why
                self._store_failed[pid] = (m["version"], why)
                return
            finally:
                self.storing = None
            stored[pid] = {"slot": slot, "version": m["version"]}
            self._set_stored(stored)
            self._kb_slots = gif.read_slots(self.kb)
            self.problems.pop(pid, None)
            self.log(f"stored {m.get('name', pid)} in image slot {slot}")
            return

    # --- PC stats screen (v24) -----------------------------------------------------------
    def _feed_stats(self, current):
        units = pcstats.units_bits(pcstats.kinds(self.cfg))
        if units != self._units_sent:                     # °C or % per slot (RAM only: resend per connection)
            r = self.kb.cmd(0x23, bytes([units]))
            if r is not None and r[:1] == b"\x00":
                self._units_sent = units
        if self.fw is not None and self.fw.at_least(pcstats.LABELS_PATCH):
            for slot, text in pcstats.labels(self.cfg).items():   # the labels (v25+, RAM only too)
                if self._labels_sent.get(slot) != text:
                    r = self.kb.cmd(0x24, pcstats.label_payload(slot, text))
                    if r is not None and r[:1] == b"\x00":
                        self._labels_sent[slot] = text
                    else:
                        self._labels_sent[slot] = text           # don't retry every tick (each one redraws)
                        self.log("PC stats label refused:", slot, text, r.hex() if r else r)
        if current == screenlist.STATS and time.time() - self._stats_at >= 1.0:
            first = time.time() - self._stats_at > 3
            self._stats_at = time.time()
            vals = self.stats.values()
            r = self.kb.cmd(0x09, vals)                  # stock 70 09: CPU, GPU, DISK (0-99)
            if first or r != getattr(self, "_stats_reply", b"\x00"):
                self.log("PC stats values sent:", list(vals), "reply", r.hex() if r else r)
            self._stats_reply = r

    # --- keyboard lights (stock 20 03; any firmware) ---------------------------------------
    def lighting_rgb(self):
        """What the lights should be: an (r, g, b), or None to leave the keyboard's own lighting."""
        mode = self.cfg.get("lighting", "keep")
        if mode == "off":
            return (0, 0, 0)
        if mode == "solid":
            c = str(self.cfg.get("lighting_color") or "#FFFFFF").lstrip("#")
            try:
                return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
            except ValueError:
                return (255, 255, 255)
        return None

    def request_brightness(self, level):
        """From the UI: set the key light brightness on the next tick."""
        self._bright_want = max(0, min(100, int(level)))

    def _sync_brightness(self):
        want, self._bright_want = self._bright_want, None
        if want is not None and want != self.brightness:
            if self.kb.set_brightness(want):
                self.brightness, self._bright_at = want, time.time()
                self.log("key light brightness:", want)
        elif time.time() - self._bright_at > 10:         # also catches Fn-key changes
            self._bright_at = time.time()
            b = self.kb.brightness()
            if b is not None:
                self.brightness = b

    def _sync_lighting(self):
        want = self.lighting_rgb()
        if want is None or want == self._lights_sent:
            return
        if self.kb.lights(want):
            self._lights_sent = want
            self.log("keyboard lights:", "off" if want == (0, 0, 0) else "#%02X%02X%02X" % want)
        else:
            self._lights_sent = want                   # don't retry every tick
            self.log("the keyboard didn't accept the lighting command")

    # --- the keyboard's clock (Dashboard / Clock screens) -------------------------------
    CLOCK_EVERY = 60

    def want_12h(self):
        f = self.cfg.get("clock_format", "auto")
        if f in ("12", "24"):
            return f == "12"
        w = windows_uses_12h()
        return bool(w) if w is not None else False

    def _sync_clock(self):
        if not self.cfg.get("sync_clock", True):
            return
        now = time.time()
        twelve = self.want_12h()
        if twelve != self._clock_fmt:                # once per connection or when changed: it's a flash write
            if self.kb.set_clock_12h(twelve):
                self._clock_fmt = twelve
        # every minute, just after the minute turns (the keyboard counts seconds by itself in between)
        if now - self._clock_at >= self.CLOCK_EVERY or (int(now // 60) != int(self._clock_at // 60) and now % 60 < 2):
            t = datetime.datetime.now()
            if self.kb.set_time(t):
                self._clock_at = now
                if not getattr(self, "_clock_logged", False):
                    self.log("keyboard clock set:", t.strftime("%a %d %b %H:%M:%S"))
                    self._clock_logged = True

    # --- main loop ------------------------------------------------------------------------
    def _loop(self):
        while not self.stopped.is_set():
            if self.paused.is_set():
                time.sleep(0.3)
                continue
            try:
                delay = self._tick()
            except Exception as e:
                self.log("keyboard connection lost:", e)
                with self.lock:
                    self._disconnect("Reconnecting...")
                delay = 2.0
            time.sleep(delay)

    def _tick(self):
        with self.lock:
            if self.kb is None and not self._connect():
                self._sync_packages()
                return 2.0
            self._sync_packages()
            self._sync_clock()
            self._sync_lighting()
            self._sync_brightness()
            if self.fw.at_least(gif.SLOTS_PATCH) and not self.asleep:
                self._sync_stored()
            v21 = self._v21()
            while self._requests:
                kind, value, pid = self._requests.pop(0)
                if v21 and kind == "mode" and value == 5 and pid in self.slots:
                    self.kb.show_screen(self.slots[pid])           # a package asking for its own screen
                elif v21 and kind == "show" and value is not None:
                    self.kb.show_screen(value)
                elif kind == "mode" and self.fw.at_least(15) and value in range(1, 8):   # v15: safe from the app
                    if v21 and value != 5:
                        self.kb.show_screen(value)
                    elif not v21:
                        self.kb.set_mode(value)
            if v21:
                st = self.kb.screens(heartbeat=True)               # also keeps package screens in the cycle
                if st is None:
                    raise IOError("no reply")
                _, mode, _, on_kb = st                             # mode = screen code (0x80+k = package)
                # keep the keyboard's list in step with ours: send it whenever it differs (the
                # keyboard may have lost it, e.g. v21 without a boot animation), at most every 3 s
                now = time.time()
                if self.want and on_kb != self.want and (self._sent_list != self.want or now - self._sent_at > 3):
                    self._sent_list, self._sent_at = list(self.want), now
                    if self.kb.set_screens(self.want):
                        self.log("screen list sent:", [hex(x) for x in self.want])
                    else:
                        self.log("the keyboard refused the screen list:", [hex(x) for x in self.want])
                if self.fw.at_least(screenlist.STATS_PATCH):
                    self._feed_stats(mode)
            if self.asleep or self.screen is None:
                return 0.5
            if not v21:
                mode = self.kb.mode() if self.fw.at_least(6) else None
                if mode is None and self.fw.at_least(6):
                    raise IOError("no reply")
            if mode != self.mode:
                self.log("keyboard screen:", hex(mode) if mode is not None else mode)
                self.mode = mode
                self.screen.forget()
                self._event("mode", mode)
            active = None
            for r in self.running.values():
                if r.m["type"] == "screen":
                    show = (self.slots.get(r.m["id"]) == mode) if v21 else self._mode_for(r.m) == mode
                    if show != r.shown:
                        r.shown = show
                        try:
                            r.obj.visible(show)
                        except Exception as e:
                            self._fail(r, e)
                    if show:
                        active = r
            if active is None:
                self.screen.power(True)            # the keyboard's own screens need it on
                return 0.25
            return self._draw(active)

    def _draw(self, r):
        now = time.time()
        if now < r.next_frame:
            return min(0.25, r.next_frame - now)
        fps = max(0.2, min(30.0, float(getattr(r.obj, "fps", 2) or 2)))
        r.next_frame = now + 1.0 / fps
        try:
            img = r.obj.frame(now)
        except Exception as e:
            self._fail(r, e)
            return 0.25
        idle_min = self.cfg["idle_off_min"]
        off = bool(idle_min and idle_seconds() > idle_min * 60)
        if img is None:
            r.none_since = r.none_since or now
            limit = getattr(r.obj, "nothing_off_sec", 0)
            off = off or bool(limit and now - r.none_since > limit)
            img = Image.new("L", (api.WIDTH, api.HEIGHT), 0)
        else:
            r.none_since = None
        if off:
            self.screen.power(False)
            return 0.5
        if self.screen.power(True):
            self._last_full = 0
        if self.cfg["burn_in_shift"]:
            dx = SHIFT_PATTERN[int(now // SHIFT_EVERY) % len(SHIFT_PATTERN)]
            if dx:
                shifted = Image.new("L", img.size, 0)
                shifted.paste(img, (dx, 0))
                img = shifted
        full = now - self._last_full >= FULL_REFRESH
        if not self.screen.show(img, getattr(r.obj, "crisp_rows", 0), full):
            raise IOError("keyboard stopped answering")
        if full:
            self._last_full = now
        return max(0.01, r.next_frame - time.time())
