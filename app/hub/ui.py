"""The window (tkinter) and tray icon (pystray)."""
import os, queue, subprocess, sys, threading, time, traceback, webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw

from . import APP_NAME, VERSION, api, bootlogo, firmware, flasher, gif, packages, screens, sensors, stats as pcstats, updater
from .device import Keyboard, enumerate_keyboards
from .config import data_dir, set_start_at_login

RECOVERY = ("If the keyboard ever stops responding after an update: unplug it, hold Esc while "
            "plugging it back in (it starts in update mode), then install the official firmware "
            "from this tab. Typing keeps working on every REVIVER version.")


def tray_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 14, 62, 50), 8, fill=(20, 20, 20, 255), outline=(200, 200, 200, 255), width=3)
    for i, x in enumerate(range(10, 56, 6)):
        h = (5, 9, 14, 8, 12, 6, 10, 13)[i % 8]
        d.rectangle((x, 44 - h * 2, x + 3, 44), fill=(220, 220, 220, 255))
    return img


class App:
    def __init__(self, cfg, reg, host, log, log_path, hidden=False):
        self.cfg, self.reg, self.host, self.log, self.log_path = cfg, reg, host, log, log_path
        self.q = queue.Queue()
        self.busy = False
        self.local_official = None
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("900x660")
        self.root.minsize(700, 480)
        try:
            from PIL import ImageTk
            self._icon = ImageTk.PhotoImage(tray_image())
            self.root.iconphoto(True, self._icon)
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.tray = None
        self._build()
        self._start_tray()
        if hidden and self.tray:
            self.root.withdraw()
        self.root.after(300, self._pump)
        threading.Thread(target=self._update_loop, name="updates", daemon=True).start()

    # --- plumbing --------------------------------------------------------------------------
    def run(self):
        self.root.mainloop()

    def ui(self, fn, *a):
        """Run fn on the UI thread."""
        self.q.put((fn, a))

    def _pump(self):
        try:
            while True:
                fn, a = self.q.get_nowait()
                try:
                    fn(*a)
                except Exception:
                    self.log(traceback.format_exc())
        except queue.Empty:
            pass
        self.status_var.set(self.host.status)
        now = time.time()
        if now - getattr(self, "_gif_info_at", 0) > 5 and not self.busy and self.host.fw and self.host.fw.at_least(gif.MIN_PATCH):
            self._gif_info_at = now
            if self.host.lock.acquire(blocking=False):     # not while the host is busy (e.g. storing an image)
                try:
                    if self.host.kb:
                        self.gif_kbinfo = gif.info(self.host.kb) or self.gif_kbinfo
                except Exception:
                    pass
                finally:
                    self.host.lock.release()
        if not self.busy:
            self.fw_status.set(self._fw_text())
        if now - getattr(self, "_stats_ui_at", 0) >= 1:
            self._stats_ui_at = now
            self._stats_live()
            st = (self.host.storing, tuple(sorted(self.host.stored())), str(self.host.fw),
                  tuple(sorted(self.host.running)), tuple(sorted(self.host.problems.items())))
            if st != getattr(self, "_storing_seen", None):          # stored-screen progress in the Packages tab
                self._storing_seen = st
                self.refresh_packages()
        self.root.after(500, self._pump)

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.refresh_packages()

    def hide(self):
        if self.tray:
            self.root.withdraw()
        else:
            self.quit()

    def quit(self):
        if self.busy and not messagebox.askyesno(APP_NAME, "A firmware update is running. Quit anyway?"):
            return
        self.host.stop()
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    def notify(self, title, msg):
        if self.tray:
            try:
                self.tray.notify(msg, title)
            except Exception:
                pass

    def _start_tray(self):
        try:
            import pystray
            menu = pystray.Menu(
                pystray.MenuItem("Open TENET Hub", lambda: self.ui(self.show), default=True),
                pystray.MenuItem("Check for updates", lambda: self.ui(self.check_updates)),
                pystray.MenuItem("Quit", lambda: self.ui(self.quit)))
            self.tray = pystray.Icon("tenet-hub", tray_image(), APP_NAME, menu)
            self.tray.run_detached()
        except Exception as e:
            self.log("no tray icon:", e)
            self.tray = None

    def _thread(self, fn, done=None):
        def run():
            try:
                r = fn()
                if done:
                    self.ui(done, r, None)
            except Exception as e:
                self.log(traceback.format_exc())
                if done:
                    self.ui(done, None, e)
        threading.Thread(target=run, daemon=True).start()

    # --- layout -------------------------------------------------------------------------------
    def _build(self):
        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text=APP_NAME, font=("Segoe UI", 14, "bold")).pack(side="left")
        self.status_var = tk.StringVar()
        ttk.Label(top, textvariable=self.status_var, foreground="#555").pack(side="left", padx=14)
        self.banner = ttk.Label(self.root, foreground="#0a5", cursor="hand2")
        self.banner.bind("<Button-1>", lambda e: self.update_hub())
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.nb = nb
        self._build_packages(nb)
        self._build_screens(nb)
        self._build_stats(nb)
        self._build_gif(nb)
        self._build_boot(nb)
        self._build_lighting(nb)
        self._build_firmware(nb)
        self._build_settings(nb)
        nb.bind("<<NotebookTabChanged>>", lambda e: self._tab_changed())

    # --- packages tab ---------------------------------------------------------------------
    def _build_packages(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Packages")
        cols = ("source", "installed", "latest", "screen", "status")
        tv = ttk.Treeview(f, columns=cols, show="tree headings", height=9, selectmode="browse")
        tv.heading("#0", text="Package")
        for c, w, label in zip(cols, (130, 70, 70, 130, 230), ("Source", "Installed", "Latest", "Screen", "Status")):
            tv.heading(c, text=label)
            tv.column(c, width=w, anchor="w")
        tv.column("#0", width=180)
        tv.tag_configure("new", foreground="#0a5")
        tv.tag_configure("problem", foreground="#b40")
        tv.pack(fill="x")
        tv.bind("<<TreeviewSelect>>", lambda e: self._pkg_selected())
        self.pkg_tv = tv
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=6)
        self.b_install = ttk.Button(bar, text="Install", command=self.pkg_install)
        self.b_toggle = ttk.Button(bar, text="Turn on", command=self.pkg_toggle)
        self.b_settings = ttk.Button(bar, text="Settings...", command=self.pkg_settings)
        self.b_remove = ttk.Button(bar, text="Remove", command=self.pkg_remove)
        for b in (self.b_install, self.b_toggle, self.b_settings, self.b_remove):
            b.pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Check for updates", command=self.check_updates).pack(side="right")
        ttk.Button(bar, text="Community sources...", command=lambda: CommunitySources(self)).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="Open packages folder",
                   command=lambda: self._open(self.reg.root)).pack(side="right", padx=6)
        self.pkg_info = tk.Text(f, height=8, wrap="word", relief="flat", background=self.root.cget("bg"))
        self.pkg_info.pack(fill="both", expand=True)
        self.pkg_info.configure(state="disabled")
        self.refresh_packages()

    def _pkg_rows(self):
        av, inst = self.reg.available(), self.reg.installed
        new = set(self.reg.new_ids())
        upd = set(self.reg.updatable())
        for pid in sorted(set(av) | set(inst), key=lambda p: (inst.get(p) or av[p]).get("name", p).lower()):
            m, a = inst.get(pid), av.get(pid)
            info = m or a
            pc = self.cfg.pkg(pid)
            screen = ""
            if info.get("type") == "screen":
                mode = pc.get("mode") or info.get("default_mode")
                fw = self.host.fw
                screen = "Its own screen" if fw is None or fw.at_least(screens.MIN_PATCH) else \
                    api.PACKAGE_MODES.get(mode, "-")
            elif info.get("type") == "animation":
                screen = "Its own screen (stored)" if self._slots_fw() else "Image screen"
            else:
                screen = "(background)"
            if not m:
                status, tag = ("New - not installed" if pid in new else "Not installed"), ("new" if pid in new else "")
            elif m["type"] == "animation" and self._slots_fw():
                st = self.host.storing
                if st and st[0] == pid:
                    status, tag = f"Storing on the keyboard: {st[2]}", ""
                elif pid in self.host.problems:
                    status, tag = self.host.problems[pid], "problem"
                elif pid in self.host.stored() and pc.get("enabled"):
                    status, tag = "On the keyboard", ""
                elif pc.get("enabled"):
                    status, tag = "Waiting to be stored", ""
                else:
                    status, tag = "Off", ""
            elif m["type"] == "animation":
                status, tag = "Animation (use it from the Image screen tab)", ""
            elif pid in self.host.problems:
                status, tag = self.host.problems[pid], "problem"
            elif pid in self.host.running:
                status, tag = "Running", ""
            elif pc.get("enabled"):
                status, tag = "Waiting for the keyboard", ""
            else:
                status, tag = "Off", ""
            if pid in upd:
                status += "  (update available)"
                tag = tag or "new"
            src = self.reg.source_of(pid) if m else (a or {}).get("_source")
            source = packages.source_label(src) if src else (
                "Official" if a or pid in self.reg.official() or pid in self.reg.bundled_ids else "Local")
            yield pid, info.get("name", pid), source, m["version"] if m else "", a["version"] if a else "", screen, status, tag

    def refresh_packages(self):
        tv = self.pkg_tv
        sel = tv.selection()
        tv.delete(*tv.get_children())
        for pid, name, source, iv, av, screen, status, tag in self._pkg_rows():
            tv.insert("", "end", iid=pid, text=name, values=(source, iv, av, screen, status),
                      tags=(tag,) if tag else ())
        if sel and tv.exists(sel[0]):
            tv.selection_set(sel[0])
        self._pkg_selected()
        if hasattr(self, "gif_combo"):
            self.refresh_gif_sources()
        if hasattr(self, "scr_tv"):
            self.refresh_screens()

    def _slots_fw(self):
        """Animation packages get stored screens of their own (REVIVER v26+)."""
        fw = self.host.fw
        return fw is not None and fw.at_least(gif.SLOTS_PATCH)

    def _sel(self):
        s = self.pkg_tv.selection()
        return s[0] if s else None

    def _pkg_selected(self):
        pid = self._sel()
        m, a = (self.reg.installed.get(pid), self.reg.available().get(pid)) if pid else (None, None)
        info = m or a or {}
        self.b_install.configure(state="normal" if a and (not m or pid in self.reg.updatable()) else "disabled",
                                 text="Update" if m else "Install")
        anim = bool(info) and info.get("type") == "animation" and not self._slots_fw()
        self.b_toggle.configure(state="normal" if m and not anim else "disabled",
                                text="Turn off" if m and self.cfg.pkg(pid).get("enabled") else "Turn on")
        self.b_settings.configure(state="normal" if m and info.get("type") != "animation" else "disabled")
        self.b_remove.configure(state="normal" if m else "disabled")
        lines = []
        if info:
            lines.append(f"{info.get('name', pid)}  {info.get('version', '')}"
                         + (f"  by {info['author']}" if info.get("author") else ""))
            lines.append("")
            lines.append(info.get("description", ""))
            src = self.reg.source_of(pid) if m else (a or {}).get("_source")
            if src:
                auto = self.cfg["auto_update_community"]
                lines += ["", f"Community package from {packages.source_page(src)} - not made or checked by "
                              "TENET Hub. " + ("It updates automatically (Settings)." if auto else
                                                 "Updates install when you click Update.")]
            need = int(info.get("requires_firmware") or 0)
            if need:
                lines.append("")
                lines.append(f"Needs REVIVER v{need} or newer (Firmware tab).")
            if a and a.get("changelog") and (not m or pid in self.reg.updatable()):
                lines += ["", "What's new: " + a["changelog"]]
            if pid in self.reg.new_ids():
                self.reg.mark_seen([pid])
        self.pkg_info.configure(state="normal")
        self.pkg_info.delete("1.0", "end")
        self.pkg_info.insert("1.0", "\n".join(lines))
        self.pkg_info.configure(state="disabled")

    def pkg_install(self):
        pid = self._sel()
        if not pid:
            return
        entry = self.reg.available().get(pid) or {}
        name = entry.get("name", pid)
        first = pid not in self.reg.installed
        if entry.get("_source") and not messagebox.askyesno(
                APP_NAME, f"{'Install' if first else 'Update'} {name} from {packages.source_page(entry['_source'])}?\n\n"
                          "This is a community package. It runs as a program on your PC with your permissions, "
                          "and TENET Hub hasn't checked it. Only install packages from people you trust.",
                icon="warning"):
            return

        def done(m, err):
            if err:
                messagebox.showerror(APP_NAME, f"Couldn't install {name}:\n{err}")
            else:
                if first:
                    self.cfg.pkg(pid)["enabled"] = True
                    self.cfg.save()
                self.host.reload()
            self.refresh_packages()
        self._thread(lambda: self.reg.install(pid), done)

    def pkg_toggle(self):
        pid = self._sel()
        if not pid:
            return
        p = self.cfg.pkg(pid)
        p["enabled"] = not p.get("enabled")
        self.cfg.save()
        self.host.reload()
        self.root.after(800, self.refresh_packages)

    def pkg_remove(self):
        pid = self._sel()
        m = self.reg.installed.get(pid)
        if not m or not messagebox.askyesno(APP_NAME, f"Remove {m['name']}?"):
            return
        self.cfg.pkg(pid)["enabled"] = False
        self.cfg.save()
        self.host.reload()
        self.reg.remove(pid)
        self.refresh_packages()

    def pkg_settings(self):
        pid = self._sel()
        m = self.reg.installed.get(pid)
        if not m:
            return
        PackageSettings(self, m)


    # --- screens tab ----------------------------------------------------------------------
    def _build_screens(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Screens")
        ttk.Label(f, text="The keyboard's screen button goes through these screens, top to bottom. Turn screens "
                          "on or off and move them around. Every screen package gets a screen of its own; those "
                          "and PC stats only come up while TENET Hub is running, so the keyboard never stops on "
                          "a blank one.",
                  wraplength=820, justify="left").pack(anchor="w")
        body = ttk.Frame(f)
        body.pack(fill="both", expand=True, pady=6)
        cols = ("on", "kind")
        tv = ttk.Treeview(body, columns=cols, show="tree headings", height=12, selectmode="browse")
        tv.heading("#0", text="Screen")
        tv.heading("on", text="In the cycle")
        tv.heading("kind", text="Type")
        tv.column("#0", width=330)
        tv.column("on", width=110, anchor="center")
        tv.column("kind", width=110)
        tv.pack(side="left", fill="both", expand=True)
        tv.bind("<Double-1>", lambda e: self.scr_toggle())
        tv.bind("<<TreeviewSelect>>", lambda e: self._scr_selected())
        self.scr_tv = tv
        side = ttk.Frame(body)
        side.pack(side="left", fill="y", padx=(8, 0))
        self.b_scr_up = ttk.Button(side, text="Move up", command=lambda: self.scr_move(-1))
        self.b_scr_up.pack(fill="x")
        self.b_scr_down = ttk.Button(side, text="Move down", command=lambda: self.scr_move(1))
        self.b_scr_down.pack(fill="x", pady=4)
        self.b_scr_toggle = ttk.Button(side, text="Turn off", command=self.scr_toggle)
        self.b_scr_toggle.pack(fill="x")
        self.b_scr_show = ttk.Button(side, text="Show it now", command=self.scr_show)
        self.b_scr_show.pack(fill="x", pady=(16, 0))
        ttk.Button(side, text="Reset to default", command=self.scr_reset).pack(fill="x", pady=(4, 0))
        self.scr_msg = tk.StringVar()
        ttk.Label(f, textvariable=self.scr_msg, foreground="#555", wraplength=820, justify="left").pack(anchor="w")
        self.refresh_screens()

    # --- PC stats tab ---------------------------------------------------------------------
    def _build_stats(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="PC stats")
        ttk.Label(f, text="The PC stats screen shows three numbers. Pick what each one measures. TENET Hub "
                          "sends the values, so the screen only comes up while TENET Hub is running (turn it on "
                          "or off and move it in the Screens tab).",
                  wraplength=820, justify="left").pack(anchor="w")
        box = ttk.LabelFrame(f, text="PC stats screen", padding=8)
        box.pack(fill="x", pady=(8, 0))
        src = pcstats.sources(self.cfg)
        labels = [v[0] for v in pcstats.SOURCES.values()]
        self.stat_vars, self.stat_now, self.stat_label = {}, {}, {}
        names = pcstats.labels(self.cfg)
        for i, s in enumerate(pcstats.SLOTS):
            ttk.Label(box, text=pcstats.SLOT_NAMES[s]).grid(row=i, column=0, sticky="w", padx=(0, 8))
            v = tk.StringVar(value=pcstats.SOURCES[src[s]][0])
            cb = ttk.Combobox(box, textvariable=v, values=labels, state="readonly", width=24)
            cb.grid(row=i, column=1, sticky="w", pady=1)
            cb.bind("<<ComboboxSelected>>", lambda e: self._stats_sources_changed())
            lab = tk.StringVar(value=f"label {names[s]}")
            ttk.Label(box, textvariable=lab, width=11).grid(row=i, column=2, sticky="w", padx=(10, 0))
            now = tk.StringVar(value="")
            ttk.Label(box, textvariable=now, foreground="#555").grid(row=i, column=3, sticky="w", padx=10)
            self.stat_vars[s], self.stat_now[s], self.stat_label[s] = v, now, lab
        ttk.Label(box, text="Temperatures come from LibreHardwareMonitor (run it as administrator and turn on "
                            "Options > Remote Web Server) or, for NVIDIA graphics cards, the NVIDIA driver. "
                            "Usage and memory come from Windows. The keyboard shows 0-99. The label under each "
                            f"number follows what it measures on REVIVER v{pcstats.LABELS_PATCH} or newer "
                            "(older firmware always shows CPU / GPU / DISK).",
                  foreground="#555", wraplength=800, justify="left").grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self._build_sensors(f)

    # temperature sensors (hub/sensors.py): one admin prompt sets up a helper that starts at login
    def _build_sensors(self, f):
        box = ttk.LabelFrame(f, text="Temperature sensors", padding=8)
        box.pack(fill="x", pady=(8, 0))
        self.sens_msg = tk.StringVar()
        ttk.Label(box, textvariable=self.sens_msg, wraplength=640, justify="left").pack(side="left", fill="x", expand=True)
        self.b_sens = ttk.Button(box, text="Set up", command=self.sensors_toggle)
        self.b_sens.pack(side="right")
        self._sens_alive, self._sens_checked, self._sens_busy = None, 0.0, False
        self._sensors_status()

    def _sensors_status(self):
        if not sensors.available():
            self.sens_msg.set("CPU and disk temperatures need LibreHardwareMonitor running as administrator with "
                              "its web server on (this copy of TENET Hub doesn't include the built-in sensors).")
            self.b_sens.pack_forget()
            return
        if self._sens_busy:
            return
        alive, set_up = self._sens_alive, self.cfg.get("sensors_helper")
        if alive:
            self.sens_msg.set("Built-in temperature sensors are running (they start with Windows).")
            self.b_sens.configure(text="Remove")
        elif set_up:
            self.sens_msg.set("Built-in temperature sensors are set up but not running. If you moved "
                              "TenetHub.exe, set them up again.")
            self.b_sens.configure(text="Set up again")
        else:
            self.sens_msg.set("CPU and disk temperatures need TENET Hub's built-in sensors. Setting them up "
                              "asks Windows for administrator permission once; after that they start with "
                              "Windows by themselves. (GPU temperature on NVIDIA cards works without them.)")
            self.b_sens.configure(text="Set up")

    def _sensors_poll(self):
        """Every few seconds, off the UI thread: is the helper answering?"""
        if not sensors.available() or self._sens_busy or time.time() - self._sens_checked < 3:
            return
        self._sens_checked = time.time()

        def done(alive, err):
            self._sens_alive = bool(alive)
            self._sensors_status()
        self._thread(sensors.alive, done)

    def sensors_toggle(self):
        if self._sens_busy:
            return
        removing = bool(self._sens_alive) and self.cfg.get("sensors_helper")
        if removing and not messagebox.askyesno(APP_NAME, "Remove the built-in temperature sensors?\n\n"
                                                          "CPU and disk temperatures will stop working "
                                                          "unless LibreHardwareMonitor runs instead."):
            return
        self._sens_busy = True
        self.b_sens.configure(state="disabled")
        self.sens_msg.set("Waiting for Windows... (approve the administrator prompt)")

        def work():
            if removing:
                return sensors.run_as_admin("--sensors-remove")
            return sensors.run_as_admin("--sensors-setup", f'"{sensors.current_user()}"')

        def done(code, err):
            self._sens_busy = False
            self.b_sens.configure(state="normal")
            if err or code is None:
                messagebox.showinfo(APP_NAME, "Nothing was changed." if code is None and not err else
                                    f"That didn't work: {err}")
            elif code != 0:
                why = {3: "the PawnIO driver didn't install", 4: "Windows didn't accept the scheduled task",
                       5: "it didn't get administrator rights"}.get(code, f"error {code}")
                messagebox.showerror(APP_NAME, f"The temperature sensors weren't set up: {why}.\n\n"
                                               f"Details are in sensors.log in {data_dir()}.")
            else:
                self.cfg.set("sensors_helper", not removing)
                self.host.stats._fail_until.clear()       # look for the helper straight away
            self._sens_checked = 0.0
            self._sensors_status()
            self._sensors_poll()
        self._thread(work, done)

    def _stats_sources_changed(self):
        rev = {v[0]: k for k, v in pcstats.SOURCES.items()}
        self.cfg.set("stats_sources", {s: rev[v.get()] for s, v in self.stat_vars.items()})
        names = pcstats.labels(self.cfg)
        for s in pcstats.SLOTS:
            self.stat_now[s].set("")
            self.stat_label[s].set(f"label {names[s]}")

    def _stats_live(self):
        self._brightness_live()
        if not hasattr(self, "stat_now"):
            return
        self._sensors_poll()
        if not self.host.stats.running():
            for s in pcstats.SLOTS:
                self.stat_now[s].set("(turn the PC stats screen on to see live values)" if s == "cpu" else "")
            return
        for s in pcstats.SLOTS:
            v, unit, src = self.host.stats.latest.get(s) or (None, "", None)
            if v is None:
                hint = ""
                if unit == "°C":
                    hint = (" - set up the temperature sensors below" if sensors.available() and not self._sens_alive
                            else " - the sensors don't report this one" if self._sens_alive
                            else " - is LibreHardwareMonitor running with its web server on?")
                self.stat_now[s].set("not available" + hint)
            else:
                self.stat_now[s].set(f"now {pcstats.to_byte(v)}{unit}  ({src})")

    def refresh_screens(self):
        tv = self.scr_tv
        sel = tv.selection()
        tv.delete(*tv.get_children())
        self.scr_items = screens.entries(self.cfg, self.reg)
        for e in self.scr_items:
            tv.insert("", "end", iid=e["id"], text=e["name"], values=("On" if e["on"] else "-", e["kind"]),
                      tags=() if e["on"] else ("off",))
        tv.tag_configure("off", foreground="#888")
        if sel and tv.exists(sel[0]):
            tv.selection_set(sel[0])
        fw = self.host.fw
        codes, _ = screens.keyboard_list(self.scr_items, fw.patch if fw is not None and fw.kind == "patch" else 99)
        msg = f"{len(codes)} screens in the cycle (the keyboard holds up to {screens.MAX})."
        if fw is not None and fw.at_least(screens.MIN_PATCH) and not fw.at_least(screens.STATS_PATCH):
            msg += f" PC stats needs REVIVER v{screens.STATS_PATCH} (Firmware tab)."
        if fw is not None and not fw.at_least(screens.MIN_PATCH):
            msg = (f"Changing the screens needs REVIVER v{screens.MIN_PATCH} or newer (Firmware tab). "
                   "Your choices here are kept and sent to the keyboard once it has it.")
        self.scr_msg.set(msg)
        self._scr_selected()

    def _scr_sel(self):
        s = self.scr_tv.selection()
        return s[0] if s else None

    def _scr_selected(self):
        i = self._scr_sel()
        e = next((x for x in self.scr_items if x["id"] == i), None)
        n = [x["id"] for x in self.scr_items].index(i) if e else -1
        self.b_scr_up.configure(state="normal" if e and n > 0 else "disabled")
        self.b_scr_down.configure(state="normal" if e and n < len(self.scr_items) - 1 else "disabled")
        self.b_scr_toggle.configure(state="normal" if e else "disabled", text="Turn off" if e and e["on"] else "Turn on")
        self.b_scr_show.configure(state="normal" if e and e["on"] else "disabled")

    def _scr_save(self):
        screens.save(self.cfg, self.scr_items)          # the host sends the new list on its next tick
        self.refresh_screens()

    def scr_move(self, d):
        i = self._scr_sel()
        ids = [x["id"] for x in self.scr_items]
        if i not in ids:
            return
        n = ids.index(i)
        if 0 <= n + d < len(ids):
            self.scr_items.insert(n + d, self.scr_items.pop(n))
            self._scr_save()
            self.scr_tv.selection_set(i)

    def scr_toggle(self):
        i = self._scr_sel()
        e = next((x for x in self.scr_items if x["id"] == i), None)
        if not e:
            return
        if e["id"].startswith("stock:") and e["on"] and \
                not any(x["on"] for x in self.scr_items if x["id"].startswith("stock:") and x is not e):
            messagebox.showinfo(APP_NAME, "Keep at least one built-in screen on: it's what the keyboard shows "
                                          "when TENET Hub isn't running.")
            return
        if e["id"].startswith("pkg:"):
            self.cfg.pkg(e["id"][4:])["enabled"] = not e["on"]     # same switch as on the Packages tab
            self.cfg.save()
        else:
            e["on"] = not e["on"]
        self._scr_save()
        self.refresh_packages()

    def scr_show(self):
        i = self._scr_sel()
        fw = self.host.fw
        if not (fw and fw.at_least(screens.MIN_PATCH)):
            messagebox.showinfo(APP_NAME, f"This needs REVIVER v{screens.MIN_PATCH} or newer (Firmware tab).")
            return
        codes, slots = screens.keyboard_list(self.scr_items)
        code = int(i[6:]) if i.startswith("stock:") else slots.get(i[4:])
        if code is not None:
            self.host._request("show", code)

    def scr_reset(self):
        if not messagebox.askyesno(APP_NAME, "Put the screens back in the default order (built-in screens: "
                                             "Dashboard, Image screen and Typing animation on)?"):
            return
        self.cfg.set("screens", [])
        self._scr_save()

    # --- Image screen tab ----------------------------------------------------------------------
    GIF_CONTROLS = [
        ("fps_cap", "Frame-rate cap (fps)", 1, 30),
        ("brightness", "Brightness", -100, 100),
        ("contrast", "Contrast (%)", 25, 300),
        ("gamma", "Gamma (%)", 30, 300),
        ("zoom", "Zoom (%)", 10, 400),
        ("rotate", "Rotate (degrees)", 0, 360),
        ("x", "Move left / right", -256, 256),
        ("y", "Move up / down", -32, 32),
        ("trim_start", "Skip frames at the start", 0, 300),
        ("trim_end", "Skip frames at the end", 0, 300),
        ("burst", "Smoothness vs typing", 64, 1024),
    ]

    def _build_gif(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Image screen")
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="Image or GIF:").pack(side="left")
        self.gif_src = tk.StringVar()
        self.gif_combo = ttk.Combobox(top, textvariable=self.gif_src, state="readonly", width=36)
        self.gif_combo.pack(side="left", padx=6)
        self.gif_combo.bind("<<ComboboxSelected>>", lambda e: self._gif_source_changed())
        ttk.Button(top, text="Open an image or GIF...", command=self.gif_open).pack(side="left")
        self.gif_view = tk.Label(f, background="black")
        self.gif_view.pack(pady=8)
        self.gif_info = tk.StringVar(value="Open an image or a GIF (or pick one of your animation packages).")
        ttk.Label(f, textvariable=self.gif_info).pack(anchor="w")
        grid = ttk.Frame(f)
        grid.pack(fill="x", pady=6)
        self.gif_vars = {}
        for i, (key, label, lo, hi) in enumerate(self.GIF_CONTROLS):
            r, c = i // 2, (i % 2) * 3
            ttk.Label(grid, text=label).grid(row=r, column=c, sticky="w", padx=(0, 4))
            v = tk.IntVar(value=gif.DEFAULTS[key])
            val = ttk.Label(grid, width=5)
            sc = ttk.Scale(grid, from_=lo, to=hi, length=170, variable=v,
                           command=lambda x, v=v, val=val: (val.configure(text=str(int(float(x)))),
                                                            self._gif_changed()))
            sc.grid(row=r, column=c + 1, sticky="w")
            val.grid(row=r, column=c + 2, sticky="w", padx=(2, 16))
            val.configure(text=str(gif.DEFAULTS[key]))
            self.gif_vars[key] = (v, val)
        ttk.Label(f, text="Smoothness vs typing: higher plays big changes faster; lower keeps the keyboard "
                          "snappier while a busy GIF plays. The frame-rate cap is the most it will ever play.",
                  foreground="#555", wraplength=820, justify="left").pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, text="Fit:").pack(side="left")
        self.gif_fit = tk.StringVar(value=gif.FITS["contain"])
        cb = ttk.Combobox(row, textvariable=self.gif_fit, values=list(gif.FITS.values()), state="readonly", width=16)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._gif_changed())
        self.gif_invert = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Invert", variable=self.gif_invert, command=self._gif_changed).pack(side="left", padx=10)
        ttk.Button(row, text="Reset adjustments", command=self.gif_reset).pack(side="left")
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(10, 2))
        self.b_gif_upload = ttk.Button(bar, text="Put it on the keyboard", command=self.gif_upload)
        self.b_gif_upload.pack(side="left")
        ttk.Button(bar, text="Show the image screen", command=self.gif_show).pack(side="left", padx=6)
        ttk.Button(bar, text="Remove from keyboard", command=self.gif_clear).pack(side="left")
        self.gif_progress = ttk.Progressbar(f, mode="determinate", maximum=1.0)
        self.gif_progress.pack(fill="x", pady=(6, 0))
        self.gif_msg = tk.StringVar()
        ttk.Label(f, textvariable=self.gif_msg, foreground="#555").pack(anchor="w")
        self.gif_frames, self.gif_gen, self.gif_job, self.gif_anim = [], 0, None, None
        self.gif_sources, self.gif_kbinfo = {}, None
        self.refresh_gif_sources()

    def refresh_gif_sources(self):
        src = {}
        for pid, m in sorted(self.reg.installed.items()):
            if m["type"] == "animation":
                src[f"{m['name']} (package)"] = ("pkg", pid)
        for p in self.cfg.get("gif_files", [])[-5:]:
            if os.path.exists(p):
                src[os.path.basename(p)] = ("file", p)
        self.gif_sources = src
        self.gif_combo.configure(values=list(src))
        cur = self.cfg.get("gif_source")
        if cur in src and not self.gif_src.get():
            self.gif_src.set(cur)
            self._gif_source_changed()

    def _gif_key(self):
        s = self.gif_sources.get(self.gif_src.get())
        return f"{s[0]}:{s[1]}" if s else None

    def _gif_source_changed(self):
        key = self._gif_key()
        if not key:
            return
        self.cfg.set("gif_source", self.gif_src.get())
        saved = (self.cfg.get("gif_settings") or {}).get(key)
        kind, ref = self.gif_sources[self.gif_src.get()]
        if saved is None and kind == "pkg":
            saved = self.reg.installed[ref].get("gif_settings")
        self._gif_apply(dict(gif.DEFAULTS, **(saved or {})))
        self._gif_changed(save=False)

    def _gif_apply(self, s):
        for key, (v, val) in self.gif_vars.items():
            v.set(int(s[key]))
            val.configure(text=str(int(s[key])))
        self.gif_fit.set(gif.FITS.get(s["fit"], gif.FITS["contain"]))
        self.gif_invert.set(bool(s["invert"]))

    def gif_settings(self):
        s = {k: int(v.get()) for k, (v, _) in self.gif_vars.items()}
        s["fit"] = next((k for k, n in gif.FITS.items() if n == self.gif_fit.get()), "contain")
        s["invert"] = bool(self.gif_invert.get())
        return s

    def gif_reset(self):
        self._gif_apply(dict(gif.DEFAULTS))
        self._gif_changed()

    def gif_open(self):
        p = filedialog.askopenfilename(title="Choose an image or GIF", filetypes=[
            ("Images and GIFs", "*.gif *.png *.apng *.webp *.jpg *.jpeg *.bmp *.tif *.tiff"), ("All files", "*.*")])
        if not p:
            return
        files = [x for x in self.cfg.get("gif_files", []) if x != p] + [p]
        self.cfg.set("gif_files", files[-5:])
        self.refresh_gif_sources()
        self.gif_src.set(os.path.basename(p))
        self._gif_source_changed()

    def _gif_changed(self, save=True):
        key = self._gif_key()
        if not key:
            return
        if save:
            allset = dict(self.cfg.get("gif_settings") or {})
            allset[key] = self.gif_settings()
            self.cfg.set("gif_settings", allset)
        if self.gif_job:
            self.root.after_cancel(self.gif_job)
        self.gif_job = self.root.after(250, self._gif_convert)

    def _gif_convert(self):
        self.gif_job = None
        kind, ref = self.gif_sources[self.gif_src.get()]
        path = self.reg.installed[ref].animation_path() if kind == "pkg" else ref
        s = self.gif_settings()
        self.gif_gen += 1
        gen = self.gif_gen
        self.gif_info.set("Working...")

        def done(frames, err):
            if gen != self.gif_gen:
                return
            if err:
                self.gif_frames = []
                self.gif_info.set(f"Can't use that file: {err}")
                return
            self.gif_frames = frames
            secs = sum(d for _, d in frames) / 100
            cap = self.gif_kbinfo["capacity"] if self.gif_kbinfo else None
            room = f"uses {len(frames)} of the keyboard's {cap} frames" if cap else \
                f"{len(frames)} frames"
            warn = "  - too many: lower the frame-rate cap or trim it" if cap and len(frames) > cap else ""
            if len(frames) == 1:
                self.gif_info.set(f"Still image (1 frame){', ' + room if cap else ''}")
            else:
                self.gif_info.set(f"{len(frames)} frames, {secs:.1f} s loop, {room}{warn}")
            self._gif_play(0)
        self._thread(lambda: gif.convert(path, s), done)

    def _gif_play(self, i):
        if self.gif_anim:
            self.root.after_cancel(self.gif_anim)
            self.gif_anim = None
        if not self.gif_frames:
            return
        from PIL import ImageTk
        img, d = self.gif_frames[i % len(self.gif_frames)]
        self._gif_photo = ImageTk.PhotoImage(img.resize((768, 96), Image.NEAREST))
        self.gif_view.configure(image=self._gif_photo)
        self.gif_anim = self.root.after(d * 10, self._gif_play, i + 1)

    def _with_keyboard(self, fn, done):
        """Run fn(kb) with the keyboard to ourselves (packages pause meanwhile)."""
        def work():
            self.host.release_keyboard()
            time.sleep(0.3)
            kb = None
            try:
                kind, path = enumerate_keyboards()
                if kind != "app":
                    raise gif.GifError("Keyboard not found (plugged in? Hexcore Link closed?)")
                kb = Keyboard(path)
                return fn(kb)
            finally:
                if kb:
                    kb.close()
                self.host.resume()
        self.busy = True
        def finish(r, err):
            self.busy = False
            done(r, err)
        self._thread(work, finish)

    def gif_upload(self):
        if not self.gif_frames or self.busy:
            return
        fw = self.host.fw
        if fw and not fw.at_least(gif.MIN_PATCH):
            messagebox.showinfo(APP_NAME, f"The image screen needs REVIVER v{gif.MIN_PATCH} or newer. "
                                          "Install it from the Firmware tab first.")
            return
        frames, burst = list(self.gif_frames), self.gif_settings()["burst"]
        self.b_gif_upload.configure(state="disabled")

        def progress(frac, msg):
            self.ui(lambda: (self.gif_progress.configure(value=frac), self.gif_msg.set(msg)))

        def fn(kb):
            gif.upload(kb, frames, burst, progress)
            kb.set_mode(6)
            return gif.info(kb)

        def done(inf, err):
            self.b_gif_upload.configure(state="normal")
            if err:
                self.gif_msg.set(f"Not stored: {err}")
                messagebox.showerror(APP_NAME, str(err))
            else:
                self.gif_kbinfo = inf
                self.gif_msg.set(f"Stored {inf['frames']} frame{'s' if inf['frames'] != 1 else ''}. It shows on the image screen, even with the PC off.")
        self._with_keyboard(fn, done)

    def gif_show(self):
        fw = self.host.fw
        if not (fw and fw.at_least(gif.MIN_PATCH)):
            messagebox.showinfo(APP_NAME, f"The image screen needs REVIVER v{gif.MIN_PATCH} or newer (Firmware tab).")
            return
        self.host._request("mode", 6)

    def gif_clear(self):
        if self.busy or not messagebox.askyesno(APP_NAME, "Remove the animation stored on the keyboard?"):
            return
        self._with_keyboard(gif.clear, lambda r, err: self.gif_msg.set(
            f"Couldn't remove it: {err}" if err else "Removed. The image screen is blank now."))

    # --- boot logo tab ---------------------------------------------------------------------
    BOOT_CONTROLS = [
        ("brightness", "Brightness", -100, 100),
        ("contrast", "Contrast (%)", 25, 300),
        ("gamma", "Gamma (%)", 30, 300),
        ("zoom", "Zoom (%)", 10, 400),
        ("rotate", "Rotate (degrees)", 0, 360),
        ("x", "Move left / right", -256, 256),
        ("y", "Move up / down", -32, 32),
    ]

    def _build_boot(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Boot logo")
        ttk.Label(f, text="The boot logo shows for 3 seconds when the keyboard powers on. REVIVER shows the "
                          "TENET 70 logo unless you put your own image here (the first frame of a GIF is used).",
                  wraplength=820, justify="left").pack(anchor="w")
        top = ttk.Frame(f)
        top.pack(fill="x", pady=(6, 0))
        ttk.Button(top, text="Open an image...", command=self.boot_open).pack(side="left")
        ttk.Button(top, text="Preview the TENET 70 logo", command=self.boot_preview_default).pack(side="left", padx=6)
        self.boot_src_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.boot_src_var, foreground="#555").pack(side="left", padx=6)
        self.boot_view = tk.Label(f, background="black")
        self.boot_view.pack(pady=8)
        grid = ttk.Frame(f)
        grid.pack(fill="x", pady=6)
        self.boot_vars = {}
        for i, (key, label, lo, hi) in enumerate(self.BOOT_CONTROLS):
            r, col = i // 2, (i % 2) * 3
            ttk.Label(grid, text=label).grid(row=r, column=col, sticky="w", padx=(0, 4))
            v = tk.IntVar(value=bootlogo.DEFAULTS[key])
            val = ttk.Label(grid, width=5, text=str(bootlogo.DEFAULTS[key]))
            ttk.Scale(grid, from_=lo, to=hi, length=170, variable=v,
                      command=lambda x, v=v, val=val: (val.configure(text=str(int(float(x)))),
                                                       self._boot_changed())).grid(row=r, column=col + 1, sticky="w")
            val.grid(row=r, column=col + 2, sticky="w", padx=(2, 16))
            self.boot_vars[key] = (v, val)
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, text="Fit:").pack(side="left")
        self.boot_fit = tk.StringVar(value=gif.FITS["contain"])
        cb = ttk.Combobox(row, textvariable=self.boot_fit, values=list(gif.FITS.values()), state="readonly", width=16)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._boot_changed())
        self.boot_invert = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Invert", variable=self.boot_invert, command=self._boot_changed).pack(side="left", padx=10)
        ttk.Button(row, text="Reset adjustments", command=self.boot_reset).pack(side="left")
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(10, 0))
        self.b_boot_upload = ttk.Button(bar, text="Put it on the keyboard", command=self.boot_upload, state="disabled")
        self.b_boot_upload.pack(side="left")
        ttk.Button(bar, text="Restore the TENET 70 logo", command=self.boot_restore).pack(side="left", padx=6)
        self.boot_progress = ttk.Progressbar(f, mode="determinate", maximum=1.0)
        self.boot_progress.pack(fill="x", pady=(6, 0))
        self.boot_msg = tk.StringVar()
        ttk.Label(f, textvariable=self.boot_msg, foreground="#555").pack(anchor="w")
        self.boot_img, self.boot_job = None, None
        self.boot_path = self.cfg.get("boot_file")
        saved = self.cfg.get("boot_settings") or {}
        self._boot_apply(dict(bootlogo.DEFAULTS, **saved))
        if self.boot_path and os.path.exists(self.boot_path):
            self._boot_changed(save=False)
        else:
            self.boot_path = None
            self.boot_preview_default()

    def _boot_apply(self, s):
        for key, (v, val) in self.boot_vars.items():
            v.set(int(s[key]))
            val.configure(text=str(int(s[key])))
        self.boot_fit.set(gif.FITS.get(s["fit"], gif.FITS["contain"]))
        self.boot_invert.set(bool(s["invert"]))

    def boot_settings(self):
        s = {k: int(v.get()) for k, (v, _) in self.boot_vars.items()}
        s["fit"] = next((k for k, n in gif.FITS.items() if n == self.boot_fit.get()), "contain")
        s["invert"] = bool(self.boot_invert.get())
        return s

    def _boot_show(self, img):
        from PIL import ImageTk
        self._boot_photo = ImageTk.PhotoImage(img.resize((768, 96), Image.NEAREST))
        self.boot_view.configure(image=self._boot_photo)

    def boot_preview_default(self):
        self.boot_img = None
        self.b_boot_upload.configure(state="disabled")
        self.boot_src_var.set("Built-in TENET 70 logo (what the keyboard shows by default)")
        self._boot_show(bootlogo.default_image())

    def boot_open(self):
        p = filedialog.askopenfilename(title="Choose an image for the boot logo", filetypes=[
            ("Images", "*.png *.gif *.jpg *.jpeg *.bmp *.webp *.tif *.tiff"), ("All files", "*.*")])
        if not p:
            return
        self.boot_path = p
        self.cfg.set("boot_file", p)
        self._boot_changed(save=False)

    def boot_reset(self):
        self._boot_apply(dict(bootlogo.DEFAULTS))
        self._boot_changed()

    def _boot_changed(self, save=True):
        if save:
            self.cfg.set("boot_settings", self.boot_settings())
        if not self.boot_path:
            return
        if self.boot_job:
            self.root.after_cancel(self.boot_job)
        self.boot_job = self.root.after(200, self._boot_convert)

    def _boot_convert(self):
        self.boot_job = None
        path, s = self.boot_path, self.boot_settings()

        def done(img, err):
            if path != self.boot_path:
                return
            if err:
                self.boot_img = None
                self.b_boot_upload.configure(state="disabled")
                self.boot_src_var.set(f"Can't use that file: {err}")
                return
            self.boot_img = img
            self.b_boot_upload.configure(state="normal")
            self.boot_src_var.set(os.path.basename(path))
            self._boot_show(img)
        self._thread(lambda: bootlogo.convert(path, s), done)

    def _boot_fw_ok(self):
        fw = self.host.fw
        if fw and not fw.at_least(bootlogo.MIN_PATCH):
            messagebox.showinfo(APP_NAME, f"Changing the boot logo needs REVIVER v{bootlogo.MIN_PATCH} or newer. "
                                          "Install it from the Firmware tab first.")
            return False
        return True

    def boot_upload(self):
        if self.boot_img is None or self.busy or not self._boot_fw_ok():
            return
        img = self.boot_img
        self.b_boot_upload.configure(state="disabled")

        def progress(frac, msg):
            self.ui(lambda: (self.boot_progress.configure(value=frac), self.boot_msg.set(msg)))

        def done(r, err):
            self.b_boot_upload.configure(state="normal")
            if err:
                self.boot_msg.set(f"Not stored: {err}")
                messagebox.showerror(APP_NAME, str(err))
        self._with_keyboard(lambda kb: bootlogo.upload(kb, img, progress), done)

    def boot_restore(self):
        if self.busy or not self._boot_fw_ok():
            return
        self._with_keyboard(bootlogo.restore, lambda r, err: (
            self.boot_msg.set(f"Couldn't restore it: {err}" if err else
                              "Restored. The keyboard shows the TENET 70 logo when it powers on."),
            self.boot_progress.configure(value=0)))

    # --- lighting tab ----------------------------------------------------------------------
    LIGHTING = {"keep": "The keyboard's own lighting", "solid": "One colour", "off": "Off"}

    def _build_lighting(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Lighting")
        ttk.Label(f, text="The keyboard's key lights. TENET Hub sets them each time the keyboard connects; "
                          "it works on every firmware, official or REVIVER.",
                  wraplength=820, justify="left").pack(anchor="w")
        box = ttk.LabelFrame(f, text="Key lights", padding=8)
        box.pack(fill="x", pady=(8, 0))
        self.light_mode = tk.StringVar(value=self.cfg.get("lighting", "keep"))
        for key, label in self.LIGHTING.items():
            row = ttk.Frame(box)
            row.pack(anchor="w", fill="x", pady=2)
            ttk.Radiobutton(row, text=label, value=key, variable=self.light_mode,
                            command=self._lighting_changed).pack(side="left")
            if key == "solid":
                self.light_swatch = tk.Label(row, width=4, relief="solid", borderwidth=1)
                self.light_swatch.pack(side="left", padx=(12, 6))
                ttk.Button(row, text="Choose colour...", command=self.lighting_pick).pack(side="left")
        self.light_msg = tk.StringVar()
        ttk.Label(box, textvariable=self.light_msg, foreground="#555", wraplength=800,
                  justify="left").pack(anchor="w", pady=(8, 0))
        row = ttk.Frame(box)
        row.pack(anchor="w", fill="x", pady=(10, 0))
        ttk.Label(row, text="Brightness").pack(side="left")
        self.light_bright = tk.IntVar(value=0)
        self.light_bright_val = ttk.Label(row, width=6)
        self.light_bright_scale = ttk.Scale(row, from_=0, to=100, length=300, variable=self.light_bright,
                                            command=lambda x: self.light_bright_val.configure(
                                                text=f"{int(float(x))}%"))
        self.light_bright_scale.pack(side="left", padx=8)
        self.light_bright_val.pack(side="left")
        self.light_bright_scale.bind("<ButtonRelease-1>", lambda e: self._brightness_set())
        self.light_bright_scale.bind("<KeyRelease>", lambda e: self._brightness_set())
        self.light_bright_scale.state(["disabled"])
        self._bright_touched = 0.0
        ttk.Label(box, text="The keyboard keeps this itself (the Fn brightness keys change it too).",
                  foreground="#555").pack(anchor="w", pady=(4, 0))
        self._lighting_show()

    def _brightness_set(self):
        self._bright_touched = time.time()
        self.host.request_brightness(int(self.light_bright.get()))

    def _brightness_live(self):
        """Every second: follow the keyboard's brightness (unless the user just moved the slider)."""
        if not hasattr(self, "light_bright_scale"):
            return
        b = self.host.brightness if self.host.kb is not None else None
        if b is None:
            self.light_bright_scale.state(["disabled"])
            self.light_bright_val.configure(text="-")
            return
        self.light_bright_scale.state(["!disabled"])
        if time.time() - self._bright_touched > 3 and int(self.light_bright.get()) != b:
            self.light_bright.set(b)
            self.light_bright_val.configure(text=f"{b}%")

    def _lighting_show(self):
        c = self.cfg.get("lighting_color") or "#FFFFFF"
        self.light_swatch.configure(background=c)
        mode = self.light_mode.get()
        self.light_msg.set({
            "keep": "TENET Hub leaves the lights alone. If it changed them, the keyboard's own lighting comes "
                    "back the next time the keyboard restarts (unplug it and plug it back in).",
            "solid": f"Every key lit in {c.upper()}. Hexcore Link, if it's running, may change them again.",
            "off": "All key lights off. Hexcore Link, if it's running, may change them again.",
        }[mode])

    def _lighting_changed(self):
        self.cfg.set("lighting", self.light_mode.get())
        self._lighting_show()

    def lighting_pick(self):
        from tkinter import colorchooser
        rgb, hexc = colorchooser.askcolor(color=self.cfg.get("lighting_color") or "#FFFFFF",
                                          title="Key light colour", parent=self.root)
        if not hexc:
            return
        self.cfg.set("lighting_color", hexc.upper())
        self.light_mode.set("solid")
        self._lighting_changed()

    # --- firmware tab ----------------------------------------------------------------------
    def _build_firmware(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="Firmware")
        self.fw_status = tk.StringVar()
        head = ttk.Frame(f)
        head.pack(fill="x")
        ttk.Label(head, textvariable=self.fw_status, font=("Segoe UI", 10, "bold")).pack(side="left")
        self.b_fw_check = ttk.Button(head, text="Check for updates", command=lambda: self.check_updates(manual=True))
        self.b_fw_check.pack(side="right")
        self.fw_upd_msg = tk.StringVar(value="")
        self.fw_upd_label = ttk.Label(f, textvariable=self.fw_upd_msg, wraplength=820, justify="left")
        self.fw_upd_label.pack(anchor="w", pady=(4, 0))
        cols = ("version", "notes")
        tv = ttk.Treeview(f, columns=cols, show="tree headings", height=6, selectmode="browse")
        tv.heading("#0", text="Firmware")
        tv.heading("version", text="Patch")
        tv.heading("notes", text="What it adds")
        tv.column("#0", width=230)
        tv.column("version", width=60)
        tv.column("notes", width=460)
        tv.pack(fill="x", pady=6)
        tv.bind("<<TreeviewSelect>>", lambda e: self._fw_selected())
        self.fw_tv = tv
        self.fw_tab = f
        self.fw_notes = tk.StringVar()
        ttk.Label(f, textvariable=self.fw_notes, wraplength=760, justify="left").pack(anchor="w", pady=(0, 6))
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        self.b_flash = ttk.Button(bar, text="Install selected firmware", command=self.fw_install)
        self.b_flash.pack(side="left")
        ttk.Button(bar, text="Use a downloaded official firmware file...",
                   command=self.fw_pick_local).pack(side="left", padx=6)
        self.fw_progress = ttk.Progressbar(f, mode="determinate", maximum=1.0)
        self.fw_progress.pack(fill="x", pady=(10, 2))
        self.fw_msg = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.fw_msg).pack(anchor="w")
        ttk.Label(f, text=RECOVERY, wraplength=760, foreground="#555", justify="left").pack(anchor="w", pady=10)
        ttk.Label(f, text="REVIVER is built on your PC: TENET Hub downloads the official "
                          "firmware from Hexcore's own server, checks it, adds REVIVER's changes and checks the "
                          "result before installing anything.",
                  wraplength=760, foreground="#555", justify="left").pack(anchor="w")
        self.refresh_firmware()

    def _fw_text(self):
        fw = self.host.fw
        if fw is None:
            return self.host.status
        return f"Your keyboard: {fw}"

    def refresh_firmware(self):
        tv = self.fw_tv
        sel = tv.selection()
        tv.delete(*tv.get_children())
        for r in self.reg.firmware_releases():
            tv.insert("", "end", iid="fw:" + r["id"], text=r.get("name", r["id"]),
                      values=(r.get("patch_version", ""), r.get("notes", "")))
        o = firmware.OFFICIAL_307
        tv.insert("", "end", iid="official", text=f"Official Hexcore firmware {o['version']}",
                  values=("-", "Removes REVIVER (packages that need it stop working)."))
        kids = tv.get_children()
        if sel and tv.exists(sel[0]):
            tv.selection_set(sel[0])
        elif kids:
            tv.selection_set(kids[0])
        self._fw_selected()
        self._fw_update_text()

    def _fw_update_text(self):
        """The line under the list: is there newer firmware for this keyboard?"""
        if not hasattr(self, "fw_upd_msg"):
            return
        if self.update_checking:
            self.fw_upd_msg.set("Checking for updates...")
            return
        rel = self.reg.firmware_releases()
        when = self._checked_text()
        if self.reg.index_error and not rel:
            self.fw_upd_msg.set(f"Couldn't check for updates: {self.reg.index_error}")
            return
        if not rel:
            self.fw_upd_msg.set(f"No REVIVER releases found.{when}")
            return
        latest = rel[0]
        name, n = latest.get("name", latest["id"]), latest.get("patch_version", 0)
        fw = self.host.fw
        if fw is None:
            text = f"Latest: {name}. Connect the keyboard to compare."
        elif fw.kind != "patch":
            text = f"{name} is available (your keyboard has the official firmware)."
        elif fw.at_least(n) and (fw.exact or fw.patch > n):
            text = f"Your keyboard has the latest firmware ({fw})."
        else:
            text = f"Update available: {name}. Select it and click Install selected firmware."
            if not getattr(self, "_fw_offered", None) == latest["id"]:   # select it once, not on every redraw
                self._fw_offered = latest["id"]
                if self.fw_tv.exists("fw:" + latest["id"]):
                    self.fw_tv.selection_set("fw:" + latest["id"])
        self.fw_upd_label.configure(foreground="#0a5" if text.startswith("Update available") else "")
        self.fw_upd_msg.set(text + when)

    def _checked_text(self):
        t = self.cfg.get("last_check") or 0
        return f"  Last checked {time.strftime('%H:%M', time.localtime(t))}." if t else ""

    def _fw_selected(self):
        s = self.fw_tv.selection()
        if not s:
            self.fw_notes.set("")
        elif s[0] == "official":
            self.fw_notes.set("Hexcore's own firmware, exactly as Hexcore Link installs it.")
        else:
            r = next((r for r in self.reg.firmware_releases() if "fw:" + r["id"] == s[0]), {})
            self.fw_notes.set(r.get("notes", ""))

    def fw_pick_local(self):
        p = filedialog.askopenfilename(title="Official TENET 70 firmware (apx_tenet_KEY_APP.bin)",
                                       filetypes=[("Firmware", "*.bin"), ("All files", "*.*")])
        if p:
            data = open(p, "rb").read()
            if firmware.sha256(data) != firmware.OFFICIAL_307["sha256"]:
                messagebox.showerror(APP_NAME, "That file isn't the official 3.0.7 firmware.")
                return
            self.local_official = p
            self.fw_msg.set("Using the official firmware file you picked (no download needed).")

    def fw_install(self):
        s = self.fw_tv.selection()
        if not s or self.busy:
            return
        sel = s[0]
        release = None if sel == "official" else next(
            r for r in self.reg.firmware_releases() if "fw:" + r["id"] == sel)
        name = "the official firmware" if release is None else release.get("name", release["id"])
        if not messagebox.askokcancel(
                APP_NAME, f"Install {name}?\n\nKeep the keyboard plugged in for about a minute. "
                          f"Close Hexcore Link first.\n\n{RECOVERY}"):
            return
        self.busy = True
        self.b_flash.configure(state="disabled")
        self.fw_progress["value"] = 0

        def progress(frac, msg):
            self.ui(self._fw_progress, frac, msg)

        def work():
            self.host.release_keyboard()
            time.sleep(0.5)
            try:
                progress(0, "Getting the firmware...")
                if release is None:
                    data = firmware.official(local_path=self.local_official)
                    allowed = {firmware.OFFICIAL_307["sha256"]}
                else:
                    data = firmware.build(release, self.reg.index.get("_url", self.cfg["index_url"]),
                                          self.local_official)
                    allowed = {release["result_sha256"]}
                flasher.flash(data, allowed, progress)
                time.sleep(4)
            finally:
                self.host.resume()

        def done(_, err):
            self.busy = False
            self.b_flash.configure(state="normal")
            if err:
                self.fw_msg.set(f"Not installed: {err}")
                messagebox.showerror(APP_NAME, f"Firmware not installed:\n{err}\n\n{RECOVERY}")
            else:
                self.fw_msg.set("Installed. The keyboard has restarted.")
                self.host.reload()
        self._thread(work, done)

    def _fw_progress(self, frac, msg):
        self.fw_progress["value"] = frac
        self.fw_msg.set(msg)
        self.fw_status.set(msg)

    # --- settings tab ---------------------------------------------------------------------
    def _build_settings(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="Settings")
        self.vars = {}

        def check(key, label):
            v = tk.BooleanVar(value=bool(self.cfg[key]))
            self.vars[key] = v
            ttk.Checkbutton(f, text=label, variable=v, command=self.save_settings).pack(anchor="w", pady=2)
        check("start_at_login", "Start TENET Hub when I log in")
        check("auto_update_packages", "Update official packages automatically (firmware is never updated without asking)")
        check("auto_update_community", "Update community packages automatically too (they aren't checked by TENET Hub)")
        check("off_when_pc_sleeps", "Turn the keyboard screen off when the PC sleeps or shuts down")
        check("off_with_monitor", "...and when Windows turns the monitor off")
        check("burn_in_shift", "Burn-in protection: nudge package screens sideways every 2 minutes")
        check("sync_clock", "Set the keyboard's clock from this PC (Dashboard and Clock screens: time, date, day)")
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=(2, 0))
        ttk.Label(row, text="Clock format on the keyboard:").pack(side="left")
        self.CLOCK_FORMATS = {"auto": "Same as Windows", "12": "12-hour", "24": "24-hour"}
        self.clock_var = tk.StringVar(value=self.CLOCK_FORMATS.get(self.cfg.get("clock_format", "auto")))
        cb = ttk.Combobox(row, textvariable=self.clock_var, values=list(self.CLOCK_FORMATS.values()),
                          state="readonly", width=16)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.save_settings())
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=6)
        ttk.Label(row, text="Package screens turn off after").pack(side="left")
        self.idle_var = tk.IntVar(value=int(self.cfg["idle_off_min"]))
        ttk.Spinbox(row, from_=0, to=120, width=5, textvariable=self.idle_var,
                    command=self.save_settings).pack(side="left", padx=4)
        ttk.Label(row, text="minutes without keyboard/mouse use (0 = never)").pack(side="left")
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=6)
        ttk.Label(row, text="Package screen brightness").pack(side="left")
        self.contrast_var = tk.IntVar(value=int(self.cfg["contrast"]))
        ttk.Scale(row, from_=8, to=255, length=220, variable=self.contrast_var,
                  command=lambda v: None).pack(side="left", padx=6)
        ttk.Button(row, text="Apply", command=self.save_settings).pack(side="left")
        box = ttk.LabelFrame(f, text="Updates", padding=8)
        box.pack(fill="x", pady=(10, 0))
        top = ttk.Frame(box)
        top.pack(fill="x")
        ttk.Label(top, text=f"{APP_NAME} {VERSION}", font=("Segoe UI", 10, "bold")).pack(side="left")
        self.b_hub_check = ttk.Button(top, text="Check for updates", command=lambda: self.check_updates(manual=True))
        self.b_hub_check.pack(side="right")
        self.b_hub_update = ttk.Button(top, text="Update", command=self.update_hub)
        self.hub_upd_msg = tk.StringVar(value="")
        ttk.Label(box, textvariable=self.hub_upd_msg).pack(anchor="w", pady=(4, 0))
        v = tk.BooleanVar(value=bool(self.cfg["check_updates"]))
        self.vars["check_updates"] = v
        ttk.Checkbutton(box, text="Check GitHub for new packages and updates automatically", variable=v,
                        command=self.save_settings).pack(anchor="w", pady=(4, 0))
        self._hub_update_text()
        row = ttk.Frame(f)
        row.pack(fill="x", pady=10)
        ttk.Label(row, text="Catalogue (index.json) URL:").pack(anchor="w")
        self.index_var = tk.StringVar(value=self.cfg["index_url"])
        ttk.Entry(row, textvariable=self.index_var).pack(fill="x")
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=10)
        ttk.Button(bar, text="Save", command=self.save_settings).pack(side="left")
        ttk.Button(bar, text="Open log", command=lambda: self._open(self.log_path)).pack(side="left", padx=6)
        ttk.Button(bar, text="Quit TENET Hub", command=self.quit).pack(side="right")

    def save_settings(self):
        for k, v in self.vars.items():
            self.cfg.data[k] = bool(v.get())
        try:
            self.cfg.data["idle_off_min"] = max(0, int(self.idle_var.get()))
        except (tk.TclError, ValueError):
            pass
        self.cfg.data["contrast"] = int(self.contrast_var.get())
        self.cfg.data["clock_format"] = next((k for k, v in self.CLOCK_FORMATS.items() if v == self.clock_var.get()), "auto")
        self.host._clock_at = 0.0                    # apply clock changes right away
        url = self.index_var.get().strip()
        changed_url = url and url != self.cfg["index_url"]
        if url:
            self.cfg.data["index_url"] = url
        self.cfg.save()
        try:
            set_start_at_login(self.cfg["start_at_login"])
        except Exception as e:
            self.log("couldn't change start-at-login:", e)
        self.host.watcher.with_display = self.cfg["off_with_monitor"]
        if self.host.screen:
            self.host.screen.contrast = self.cfg["contrast"]
            try:
                with self.host.lock:
                    self.host.kb.oled(0x81, self.cfg["contrast"])
            except Exception:
                pass
        if changed_url:
            self.check_updates()

    # --- updates ----------------------------------------------------------------------------
    update_checking = False
    hub_updating = None                  # text saying what an update of TENET Hub itself is doing

    def check_updates(self, quiet=False, manual=False):
        """quiet: no error box. manual: from a "Check for updates" button (shows the result)."""
        if self.update_checking:
            return
        self.update_checking = True
        self._check_started = time.time()
        for b in ("b_hub_check", "b_fw_check"):
            if hasattr(self, b):
                getattr(self, b).configure(state="disabled")
        self._hub_update_text()
        self._fw_update_text()

        def done(r, err):
            self.update_checking = False
            for b in ("b_hub_check", "b_fw_check"):
                if hasattr(self, b):
                    getattr(self, b).configure(state="normal")
            self.refresh_packages()
            self.refresh_firmware()
            self._hub_banner()
            self._hub_update_text()
            err = err or self.reg.index_error
            if err and not quiet:
                messagebox.showwarning(APP_NAME, f"Couldn't check for updates:\n{err}")
        self._thread(self._do_check, done)

    def _hub_update_text(self):
        if not hasattr(self, "hub_upd_msg"):
            return
        if self.hub_updating:
            self.hub_upd_msg.set(self.hub_updating)
            self.b_hub_update.pack_forget()
            return
        if self.update_checking:
            self.hub_upd_msg.set("Checking for updates...")
            self.b_hub_update.pack_forget()
            return
        h = self.reg.hub_update()
        if h:
            self.hub_upd_msg.set(f"TENET Hub {h['version']} is available.{self._checked_text()}")
            self.b_hub_update.configure(text=f"Update to {h['version']} and restart" if updater.supported()
                                        else f"Download {h['version']}")
            self.b_hub_update.pack(side="right", padx=6)
        else:
            self.b_hub_update.pack_forget()
            if self.reg.index_error:
                self.hub_upd_msg.set(f"Couldn't check for updates: {self.reg.index_error}")
            elif self.reg.index:
                self.hub_upd_msg.set(f"You have the latest version.{self._checked_text()}")
            else:
                self.hub_upd_msg.set("Not checked yet.")

    FW_RECHECK_SEC = 60                  # switching to the Firmware tab checks again after this long

    def _tab_changed(self):
        try:
            on_fw = self.nb.nametowidget(self.nb.select()) is self.fw_tab
        except Exception:
            return
        if on_fw:
            self._fw_update_text()
            last = max(self.cfg.get("last_check") or 0, getattr(self, "_check_started", 0))
            if time.time() - last > self.FW_RECHECK_SEC and not self.busy:
                self.check_updates(quiet=True)

    def _do_check(self):
        new, upd = self.reg.refresh()
        if self.reg.index_error:
            return
        auto = self.reg.auto_updatable(official=self.cfg["auto_update_packages"],
                                       community=self.cfg["auto_update_community"])
        if auto:
            for pid in auto:
                old = self.reg.installed[pid]["version"]
                try:
                    m = self.reg.install(pid)
                    self.log(f"package auto-updated: {pid} {old} -> {m['version']}")
                    self.notify("Package updated", f"{m['name']} {m['version']}")
                except Exception as e:
                    self.log(f"auto-update of {pid} failed:", e)
            self.host.reload()
        ask = [p for p in upd if p not in auto]           # community packages (or auto-update off)
        if ask:
            self.notify("Updates available", ", ".join(self.reg.available()[p]["name"] for p in ask)
                        + " (Packages tab)")
        notified = set(self.cfg.get("notified", []))
        fresh = [p for p in new if p not in notified]
        if fresh:
            self.notify("New packages", ", ".join(self.reg.available()[p]["name"] for p in fresh))
        fw = self.reg.firmware_releases()
        cur = self.host.fw
        if fw and cur and not cur.at_least(fw[0].get("patch_version", 0)) and ("fw:" + fw[0]["id"]) not in notified:
            self.notify("Firmware update", f"{fw[0].get('name', fw[0]['id'])} is available (Firmware tab).")
            fresh.append("fw:" + fw[0]["id"])
        if fresh:
            self.cfg.set("notified", sorted(notified | set(fresh)))

    def _update_loop(self):
        time.sleep(5)
        while True:
            if self.cfg["check_updates"]:
                self.ui(self.check_updates, True)
            time.sleep(max(1, self.cfg["check_every_hours"]) * 3600)

    def _hub_banner(self):
        h = self.reg.hub_update()
        if self.hub_updating:
            self.banner.configure(text=self.hub_updating)
        elif h:
            action = "update and restart" if updater.supported() else "download"
            self.banner.configure(text=f"TENET Hub {h['version']} is available - click to {action}")
        if h:
            self.banner.pack(fill="x", padx=10, before=self.nb)
        else:
            self.banner.pack_forget()

    def _open_hub_release(self):
        h = self.reg.hub_update()
        if h and h.get("url"):
            webbrowser.open(h["url"])

    def _hub_update_state(self, text):
        """What the update is doing (None: nothing), shown in the banner and the Settings tab."""
        self.hub_updating = text
        self._hub_banner()
        self._hub_update_text()

    def update_hub(self):
        """The "update and restart" button: download and check on a thread, then swap and restart."""
        h = self.reg.hub_update()
        if not h or self.hub_updating:
            return
        if not updater.supported():
            self._open_hub_release()
            return
        version = h["version"]
        self._hub_update_state(f"Downloading TENET Hub {version}...")
        seen = [-1]

        def progress(frac):
            pct = int(frac * 100)
            if pct != seen[0]:
                seen[0] = pct
                self.ui(self._hub_update_state, f"Downloading TENET Hub {version}... {pct}%"
                        if pct < 100 else f"Checking TENET Hub {version}...")

        def done(path, err):
            if err:
                self._hub_update_failed(err)
            else:
                self._hub_restart(path, version)
        self._thread(lambda: updater.prepare(version, progress=progress), done)

    def _hub_restart(self, path, version):
        if self.busy or self.host.storing:               # never restart in the middle of a keyboard write
            self._hub_update_state(f"TENET Hub {version} is ready. It will restart when the keyboard is finished.")
            self.root.after(2000, self._hub_restart, path, version)
            return
        try:
            updater.install(path, ["--tray"] if self.root.state() == "withdrawn" else [])
        except updater.UpdateError as e:
            self._hub_update_failed(e)
            return
        self.log(f"updating to {version}: restarting")
        watchdog = threading.Timer(15, os._exit, [0])    # the new copy is waiting: never hang on the way out
        watchdog.daemon = True
        watchdog.start()
        self.quit()

    def _hub_update_failed(self, err):
        self.log("update failed:", err)
        self._hub_update_state(None)
        if messagebox.askyesno(APP_NAME, f"TENET Hub couldn't update itself:\n\n{err}\n\n"
                                         "Your current version is unchanged. Open the download page instead?"):
            self._open_hub_release()

    def _open(self, path):
        try:
            if sys.platform == "win32":
                os.startfile(path)
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))


class CommunitySources(tk.Toplevel):
    """Other people's package catalogues (their own GitHub repos), added by link."""

    def __init__(self, app):
        super().__init__(app.root)
        self.app, self.reg = app, app.reg
        self.title("Community sources")
        self.transient(app.root)
        f = ttk.Frame(self, padding=10)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Community packages come from other people's GitHub repos. Add a repo by its link "
                          "(for example github.com/someone/tenet-packages) and its packages show up in the "
                          "Packages tab, marked with where they're from. TENET Hub doesn't check them: only "
                          "add people you trust. Community packages only update when you click Update, unless you "
                          "turn on automatic community updates in Settings.",
                  wraplength=560, justify="left").pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(10, 4))
        self.link = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.link, width=60)
        e.pack(side="left", fill="x", expand=True)
        e.bind("<Return>", lambda ev: self.add())
        self.b_add = ttk.Button(row, text="Add", command=self.add)
        self.b_add.pack(side="left", padx=(6, 0))
        tv = ttk.Treeview(f, columns=("packages", "status"), show="tree headings", height=6, selectmode="browse")
        tv.heading("#0", text="Repo")
        tv.heading("packages", text="Packages")
        tv.heading("status", text="Status")
        tv.column("#0", width=220)
        tv.column("packages", width=80, anchor="center")
        tv.column("status", width=240)
        tv.pack(fill="both", expand=True, pady=4)
        self.tv = tv
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Button(bar, text="Open repo page", command=self.open_page).pack(side="left")
        ttk.Button(bar, text="Remove", command=self.remove).pack(side="left", padx=6)
        ttk.Button(bar, text="How to make packages", command=lambda: webbrowser.open(
            "https://github.com/yuuusername/tenet-hub/blob/main/docs/community-packages.md")).pack(side="left")
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")
        self.msg = tk.StringVar()
        ttk.Label(f, textvariable=self.msg, foreground="#555", wraplength=560).pack(anchor="w", pady=(6, 0))
        self.refresh()
        e.focus_set()

    def refresh(self):
        self.tv.delete(*self.tv.get_children())
        for url in self.reg.sources():
            n = len(self.reg.from_source(url))
            err = self.reg.source_errors.get(url)
            status = f"couldn't read it: {err}" if err else ("OK" if url in self.reg.community else "not read yet")
            self.tv.insert("", "end", iid=url, text=packages.source_label(url), values=(n, status))

    def _sel(self):
        s = self.tv.selection()
        return s[0] if s else None

    def add(self):
        link = self.link.get().strip()
        if not link:
            return
        self.b_add.configure(state="disabled")
        self.msg.set("Reading the catalogue...")

        def done(r, err):
            if not self.winfo_exists():
                return
            self.b_add.configure(state="normal")
            if err:
                self.msg.set(f"Not added: {err}")
                return
            url, idx = r
            n = len(self.reg.from_source(url))
            skipped = len(idx["packages"]) - n
            self.msg.set(f"Added {packages.source_label(url)}: {n} package{'s' if n != 1 else ''}"
                         + (f" ({skipped} skipped: same id as an official package, or incomplete)" if skipped else "")
                         + ". Install them from the Packages tab.")
            self.link.set("")
            self.refresh()
            self.app.refresh_packages()
        self.app._thread(lambda: self.reg.add_source(link), done)

    def remove(self):
        url = self._sel()
        if not url:
            return
        if not messagebox.askyesno(APP_NAME, f"Remove {packages.source_label(url)}?\n\nPackages you installed "
                                             "from it stay installed (remove them in the Packages tab), but they "
                                             "won't get updates.", parent=self):
            return
        self.reg.remove_source(url)
        self.refresh()
        self.app.refresh_packages()

    def open_page(self):
        url = self._sel()
        if url:
            webbrowser.open(packages.source_page(url))


class PackageSettings(tk.Toplevel):
    """Settings dialog generated from the package's manifest."""

    def __init__(self, app, m):
        super().__init__(app.root)
        self.app, self.m = app, m
        self.title(f"{m['name']} settings")
        self.resizable(False, False)
        pc = app.cfg.pkg(m["id"])
        cur = dict(m.defaults())
        cur.update(pc.get("settings") or {})
        self.vars = {}
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both")
        r = 0
        fw = app.host.fw
        self.pick_mode = m["type"] == "screen" and fw is not None and not fw.at_least(screens.MIN_PATCH)
        if self.pick_mode:                       # before v21 packages share the keyboard's screens
            ttk.Label(body, text="Show on").grid(row=r, column=0, sticky="w", pady=3)
            names = list(api.PACKAGE_MODES.values())
            mode = pc.get("mode") or m.get("default_mode")
            self.mode_var = tk.StringVar(value=api.PACKAGE_MODES.get(mode, names[0]))
            ttk.Combobox(body, textvariable=self.mode_var, values=names, state="readonly",
                         width=24).grid(row=r, column=1, sticky="w")
            r += 1
        for s in m["settings"]:
            k, typ, label = s["key"], s.get("type", "str"), s.get("label", s["key"])
            if typ == "bool":
                v = tk.BooleanVar(value=bool(cur.get(k)))
                ttk.Checkbutton(body, text=label, variable=v).grid(row=r, column=0, columnspan=2, sticky="w", pady=3)
            else:
                ttk.Label(body, text=label).grid(row=r, column=0, sticky="w", pady=3)
                if typ == "int":
                    v = tk.IntVar(value=int(cur.get(k) or 0))
                    ttk.Spinbox(body, from_=s.get("min", 0), to=s.get("max", 1_000_000), width=8,
                                textvariable=v).grid(row=r, column=1, sticky="w")
                elif typ == "choice":
                    v = tk.StringVar(value=str(cur.get(k)))
                    ttk.Combobox(body, textvariable=v, values=s.get("choices", []), state="readonly",
                                 width=24).grid(row=r, column=1, sticky="w")
                else:
                    v = tk.StringVar(value=str(cur.get(k) or ""))
                    ttk.Entry(body, textvariable=v, width=28).grid(row=r, column=1, sticky="w")
            self.vars[k] = (v, s)
            r += 1
        bar = ttk.Frame(body)
        bar.grid(row=r, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="Save", command=self.save).pack(side="right", padx=6)

    def save(self):
        pc = self.app.cfg.pkg(self.m["id"])
        out = {}
        for k, (v, s) in self.vars.items():
            try:
                val = v.get()
            except tk.TclError:
                val = s.get("default")
            if s.get("type") == "int":
                val = max(s.get("min", val), min(s.get("max", val), int(val)))
            out[k] = val
        pc["settings"] = out
        if self.pick_mode:
            pc["mode"] = next(k for k, n in api.PACKAGE_MODES.items() if n == self.mode_var.get())
        self.app.cfg.save()
        self.app.host.reload()
        self.app.refresh_packages()
        self.destroy()
