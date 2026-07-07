#!/usr/bin/env python3
# repodash — Windows system-tray companion (ctypes + tkinter).
# Copyright (C) 2026 repodash contributors. GPL-3.0-or-later.
"""A system-tray companion for repodash on Windows.

Uses ctypes (Win32 Shell_NotifyIcon) for the tray icon and tkinter for
windows and dialogs. All pure helpers are imported from the shared core
module (``repodash_tray_core.py``).

Run ``repodash_tray_win.py --check`` for a headless dump of what the tray
sees (no tkinter required) — useful for verification.
"""

import os as _os
import sys as _sys

_tray_dir = _os.path.dirname(_os.path.abspath(__file__))
if _tray_dir not in _sys.path:
    _sys.path.insert(0, _tray_dir)

from repodash_tray_core import *  # noqa: F403
from repodash_tray_core import (  # noqa: F401
    _format_age, _git, _current_upstream, _repo_op_gate,
    _fetch_opencode_go_models,
    _FETCHED_OPENGODE_GO_MODELS, _OPENGODE_GO_HEADER,
    _NONINTERACTIVE_GIT_ENV,
)
del _os, _sys, _tray_dir


# ── Win32 API bindings (ctypes) ───────────────────────────────────────────────
# These are only needed on Windows.  On other platforms the --check mode
# works fine without them because run_check() is platform-agnostic.

import ctypes as _ctypes
try:
    from ctypes import wintypes
    _HAVE_WIN32 = True
except (ImportError, AttributeError):
    _HAVE_WIN32 = False


def _init_win32():
    """Return Win32 API bindings (only call on Windows)."""
    if not _HAVE_WIN32:
        raise RuntimeError("Win32 API not available on this platform")

    _kernel32 = _ctypes.windll.kernel32
    _shell32 = _ctypes.windll.shell32
    _user32 = _ctypes.windll.user32

    WM_TASKBARCREATED = _user32.RegisterWindowMessageW("TaskbarCreated")
    WM_COMMAND = 0x0111
    WM_DESTROY = 0x0002
    WM_CLOSE = 0x0010
    WM_USER = 0x0400
    WM_APP = 0x8000
    WM_TRAY_CALLBACK = WM_APP + 1

    NIM_ADD = 0
    NIM_MODIFY = 1
    NIM_DELETE = 2
    NIF_MESSAGE = 1
    NIF_ICON = 2
    NIF_TIP = 4
    NIF_INFO = 0x10
    NIIF_INFO = 1

    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x0010

    TPM_RIGHTBUTTON = 2
    TPM_LEFTBUTTON = 0

    MF_STRING = 0
    MF_SEPARATOR = 0x800
    MF_GRAYED = 1
    MF_CHECKED = 8

    IDI_APPLICATION = 32512

    class NOTIFYICONDATAW(_ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", wintypes.BYTE * 16),
            ("hBalloonIcon", wintypes.HICON),
        ]

    class POINT(_ctypes.Structure):
        _fields_ = [("x", _ctypes.c_long), ("y", _ctypes.c_long)]

    return {
        "kernel32": _kernel32,
        "shell32": _shell32,
        "user32": _user32,
        "WM_TASKBARCREATED": WM_TASKBARCREATED,
        "WM_COMMAND": WM_COMMAND,
        "WM_TRAY_CALLBACK": WM_TRAY_CALLBACK,
        "NIM_ADD": NIM_ADD,
        "NIM_MODIFY": NIM_MODIFY,
        "NIM_DELETE": NIM_DELETE,
        "NIF_MESSAGE": NIF_MESSAGE,
        "NIF_ICON": NIF_ICON,
        "NIF_TIP": NIF_TIP,
        "NOTIFYICONDATAW": NOTIFYICONDATAW,
        "IMAGE_ICON": IMAGE_ICON,
        "LR_LOADFROMFILE": LR_LOADFROMFILE,
        "TPM_RIGHTBUTTON": TPM_RIGHTBUTTON,
        "TPM_LEFTBUTTON": TPM_LEFTBUTTON,
        "MF_STRING": MF_STRING,
        "MF_SEPARATOR": MF_SEPARATOR,
        "IDI_APPLICATION": IDI_APPLICATION,
        "POINT": POINT,
    }


# ── GUI layer (tkinter + ctypes) ─────────────────────────────────────────────
def run_gui_win() -> int:
    import ctypes as _ct
    from ctypes import wintypes as _wt
    import tkinter as tk
    from tkinter import ttk, messagebox, scrolledtext
    import threading
    import queue

    w32 = _init_win32()
    _kernel32 = w32["kernel32"]
    _shell32 = w32["shell32"]
    _user32 = w32["user32"]
    WM_TASKBARCREATED = w32["WM_TASKBARCREATED"]
    WM_COMMAND = w32["WM_COMMAND"]
    WM_TRAY_CALLBACK = w32["WM_TRAY_CALLBACK"]
    NIM_ADD = w32["NIM_ADD"]
    NIM_MODIFY = w32["NIM_MODIFY"]
    NIM_DELETE = w32["NIM_DELETE"]
    NIF_MESSAGE = w32["NIF_MESSAGE"]
    NIF_ICON = w32["NIF_ICON"]
    NIF_TIP = w32["NIF_TIP"]
    NOTIFYICONDATAW = w32["NOTIFYICONDATAW"]
    IMAGE_ICON = w32["IMAGE_ICON"]
    LR_LOADFROMFILE = w32["LR_LOADFROMFILE"]
    TPM_RIGHTBUTTON = w32["TPM_RIGHTBUTTON"]
    TPM_LEFTBUTTON = w32["TPM_LEFTBUTTON"]
    MF_STRING = w32["MF_STRING"]
    MF_SEPARATOR = w32["MF_SEPARATOR"]
    IDI_APPLICATION = w32["IDI_APPLICATION"]
    POINT = w32["POINT"]

    REFRESH_INTERVAL_MS = 90_000

    def _load_icon():
        ico = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "repodash.ico")
        if os.path.isfile(ico):
            hicon = _user32.LoadImageW(None, ico, IMAGE_ICON, 0, 0,
                                       LR_LOADFROMFILE)
            if hicon:
                return hicon
        return _user32.LoadIconW(None, IDI_APPLICATION)

    # ── Win32 message pump thread ─────────────────────────────────────────
    def _win32_pump(root, msg_queue):
        """Run a Windows message loop on a background thread.

        Posts WM_ messages to msg_queue for tkinter main thread processing.
        """
        try:
            msg = _wt.MSG()
            while _user32.GetMessageW(_ct.byref(msg), None, 0, 0) > 0:
                if msg.message in (
                        WM_TASKBARCREATED, WM_COMMAND,
                        WM_TRAY_CALLBACK):
                    msg_queue.put((msg.message, msg.wParam, msg.lParam))
                _user32.TranslateMessage(_ct.byref(msg))
                _user32.DispatchMessageW(_ct.byref(msg))
        except Exception as e:
            import traceback
            traceback.print_exc(file=sys.stderr)

    # ── WinTrayIcon ───────────────────────────────────────────────────────
    class WinTrayIcon:
        def __init__(self, root, config):
            self.root = root
            self.config = config
            self.repos = []
            self.hwnd = int(root.frame(), 16)
            self.hicon = _load_icon()
            self.nid = NOTIFYICONDATAW()
            self._timer_id = None
            self._op_running = False
            self._menu_handles = []
            self._menu_callbacks = {}
            self._msg_queue = queue.Queue()

            self._add_icon()
            self._start_refresh()
            threading.Thread(target=_fetch_opencode_go_models,
                             daemon=True).start()

            root.after(100, self._process_messages)

            self._pump_thread = threading.Thread(
                target=_win32_pump, args=(root, self._msg_queue),
                daemon=True)
            self._pump_thread.start()

        def _add_icon(self):
            self.nid.cbSize = _ct.sizeof(NOTIFYICONDATAW)
            self.nid.hWnd = self.hwnd
            self.nid.uID = 1
            self.nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            self.nid.uCallbackMessage = WM_TRAY_CALLBACK
            self.nid.hIcon = self.hicon
            tip = "repodash"
            self.nid.szTip = tip
            _shell32.Shell_NotifyIconW(NIM_ADD, _ct.byref(self.nid))

        def _remove_icon(self):
            _shell32.Shell_NotifyIconW(NIM_DELETE, _ct.byref(self.nid))

        def _update_tip(self, text):
            self.nid.szTip = text[:127]
            _shell32.Shell_NotifyIconW(NIM_MODIFY, _ct.byref(self.nid))

        def _start_refresh(self):
            interval = resolve_interval(self.config)
            delay = max(5, int(interval)) * 1000 if interval else REFRESH_INTERVAL_MS
            self.refresh_menu()
            self._timer_id = self.root.after(delay, self._start_refresh)

        def refresh_menu(self):
            def work():
                base = resolve_base_dir(self.config)
                depth = resolve_depth(self.config)
                repos = scan_dirty(base, depth, self.config)
                excluded = set(self.config.get("excluded_repos", []))
                repos = [r for r in repos if r["path"] not in excluded]
                if not self.config.get("show_remoteless", True):
                    repos = [r for r in repos if r["has_remote"]]
                for r in repos:
                    r["github"] = github_url(r["path"])
                self.root.after(0, lambda: self._apply_repos(repos))
            threading.Thread(target=work, daemon=True).start()

        def _apply_repos(self, repos):
            self.repos = repos
            dirty = [r for r in repos if r["dirty"]]
            unpushed = [r for r in repos
                        if r["has_remote"] and r.get("unpushed", 0) > 0]
            tip = f"repodash — {len(dirty)} dirty, {len(unpushed)} unpushed"
            self._update_tip(tip)

        def _process_messages(self):
            while True:
                try:
                    msg_type, wParam, lParam = self._msg_queue.get_nowait()
                except queue.Empty:
                    break
                self._handle_win32_msg(msg_type, wParam, lParam)
            self.root.after(100, self._process_messages)

        def _handle_win32_msg(self, msg, wParam, lParam):
            if msg == WM_TASKBARCREATED:
                self._add_icon()
            elif msg == WM_TRAY_CALLBACK:
                if lParam == 0x0205:  # WM_RBUTTONUP
                    self._show_menu()
                elif lParam == 0x0202:  # WM_LBUTTONUP
                    self.show_dashboard()
            elif msg == WM_COMMAND:
                self._handle_menu_command(wParam)

        def _show_menu(self):
            menu = _user32.CreatePopupMenu()
            self._menu_handles = [menu]
            self._build_menu(menu)
            _user32.SetForegroundWindow(self.hwnd)
            pos = POINT()
            _user32.GetCursorPos(_ct.byref(pos))
            cmd = _user32.TrackPopupMenu(
                menu, TPM_RIGHTBUTTON | TPM_LEFTBUTTON,
                pos.x, pos.y, 0, self.hwnd, None)
            _user32.PostMessageW(self.hwnd, 0, 0, 0)
            for h in reversed(self._menu_handles):
                _user32.DestroyMenu(h)
            self._menu_handles = []
            if cmd:
                self._handle_menu_command(cmd)

        def _build_menu(self, menu):
            dirty = [r for r in self.repos if r["dirty"]]
            unpushed = [r for r in self.repos
                        if r["has_remote"] and r.get("unpushed", 0) > 0]
            stale_worktrees = []
            for r in self.repos:
                for severity, wts in r.get("stale_worktrees", {}).items():
                    for wt in wts:
                        wt["_repo"] = r
                        wt["_severity"] = severity
                        stale_worktrees.append(wt)

            cmd_id = 1000
            sub_id = 2000
            wt_id = 3000

            if dirty:
                for r in dirty:
                    sub = _user32.CreatePopupMenu()
                    self._menu_handles.append(sub)
                    self._repo_submenu(sub, r, sub_id)
                    sub_id += 100
                    text = f"{r['name']}  [{r['branch']}]"
                    _user32.AppendMenuW(menu, MF_STRING | 0x0010,  # MF_POPUP
                                        sub, text)
                _user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)

            if stale_worktrees:
                for wt in stale_worktrees:
                    sub = _user32.CreatePopupMenu()
                    self._menu_handles.append(sub)
                    self._worktree_submenu(sub, wt, wt_id)
                    wt_id += 100
                    sev = wt["_severity"]
                    age = _format_age(wt["last_commit_age_hours"])
                    text = f"[{sev}] {wt['_repo']['name']}/{wt['branch']}  {age}"
                    _user32.AppendMenuW(menu, MF_STRING | 0x0010,
                                        sub, text)
                _user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)

            self._action(menu, "Show dashboard…", cmd_id + 1)
            self._action(menu, "Refresh now", cmd_id + 2)
            self._action(menu, "Settings…", cmd_id + 3)
            self._action(menu, "Help & About…", cmd_id + 4)
            _user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            self._action(menu, "Quit", cmd_id + 5)

        def _repo_submenu(self, menu, r, base_id):
            self._action(menu, "Open terminal", base_id + 1,
                         lambda: open_terminal(r["path"]))
            self._action(menu, "Open folder", base_id + 2,
                         lambda: open_folder(r["path"]))
            if r.get("github"):
                self._action(menu, "Open GitHub", base_id + 3,
                             lambda: open_github(r["path"]))
            self._action(menu, "Copy path", base_id + 4,
                         lambda: copy_to_clipboard(r["path"]))

        def _worktree_submenu(self, menu, wt, base_id):
            r = wt["_repo"]
            self._action(menu, "Open terminal", base_id + 1,
                         lambda: open_terminal(wt["path"]))
            self._action(menu, "Remove worktree", base_id + 2,
                         lambda: self._on_wt_remove(wt))

        def _action(self, menu, text, cmd_id, callback=None):
            _user32.AppendMenuW(menu, MF_STRING, cmd_id, text)
            if callback:
                self._menu_callbacks[cmd_id] = callback

        def _handle_menu_command(self, cmd_id):
            callbacks = self._menu_callbacks
            if cmd_id in callbacks:
                callbacks[cmd_id]()
                return

            if cmd_id == 1001:  # Show dashboard
                self.show_dashboard()
            elif cmd_id == 1002:  # Refresh
                self.refresh_menu()
            elif cmd_id == 1003:  # Settings
                self._on_settings()
            elif cmd_id == 1004:  # Help
                self._on_help()
            elif cmd_id == 1005:  # Quit
                self._remove_icon()
                self.root.destroy()

        def _on_wt_remove(self, wt):
            ok, msg = remove_worktree(wt["_repo"]["path"], wt["path"],
                                      wt.get("branch", ""))
            if not ok:
                messagebox.showwarning(
                    "Remove worktree",
                    msg or "failed to remove worktree (no output)",
                    parent=self.root)
            self.refresh_menu()

        def show_dashboard(self):
            if hasattr(self, "_dashboard") and self._dashboard:
                self._dashboard.deiconify()
                self._dashboard.lift()
                return
            self._dashboard = DashboardWindow(self.root, self.config, self)
            self._dashboard.protocol("WM_DELETE_WINDOW",
                                     self._on_dashboard_close)

        def _on_dashboard_close(self):
            if hasattr(self, "_dashboard"):
                self._dashboard.withdraw()

        def _on_settings(self):
            dlg = SettingsDialog(self.root, self.config)
            self.root.wait_window(dlg)
            if dlg.result:
                self.config = dlg.result
                apply_config_to_env(self.config)
                save_config(self.config)
                self.refresh_menu()

        def _on_help(self):
            HelpDialog(self.root)

        def _ai_label(self):
            sel = provider_selection(self.config)
            provider = PROVIDERS.get(sel["primary"], PROVIDERS["claude"])
            return provider.label

    # ── Dashboard Window ──────────────────────────────────────────────────
    class DashboardWindow(tk.Toplevel):
        def __init__(self, master, config, tray):
            super().__init__(master)
            self.title("repodash — dashboard")
            self.config = config
            self.tray = tray
            self.geometry("800x600")
            self._repos = []

            top = ttk.Frame(self)
            top.pack(fill=tk.X, padx=5, pady=5)

            ttk.Label(top, text="Search:").pack(side=tk.LEFT)
            self._search_var = tk.StringVar()
            self._search_var.trace_add("write", lambda *a: self._filter())
            search = ttk.Entry(top, textvariable=self._search_var, width=30)
            search.pack(side=tk.LEFT, padx=5)

            self._dirty_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(top, text="Dirty only",
                            variable=self._dirty_var,
                            command=self._filter).pack(side=tk.LEFT, padx=5)

            ttk.Button(top, text="Refresh",
                       command=self.reload).pack(side=tk.RIGHT, padx=5)

            columns = ("name", "branch", "status")
            self._tree = ttk.Treeview(self, columns=columns,
                                      show="headings", selectmode="browse")
            self._tree.heading("name", text="Repository")
            self._tree.heading("branch", text="Branch")
            self._tree.heading("status", text="Status")
            self._tree.column("name", width=250)
            self._tree.column("branch", width=120)
            self._tree.column("status", width=150)

            scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL,
                                      command=self._tree.yview)
            self._tree.configure(yscrollcommand=scrollbar.set)

            self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                            padx=(5, 0), pady=5)
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 5), pady=5)

            btn_frame = ttk.Frame(self)
            btn_frame.pack(fill=tk.X, padx=5, pady=(0, 5))
            ttk.Button(btn_frame, text="Open terminal",
                       command=self._on_open_terminal).pack(side=tk.LEFT, padx=2)
            ttk.Button(btn_frame, text="Open folder",
                       command=self._on_open_folder).pack(side=tk.LEFT, padx=2)
            ttk.Button(btn_frame, text="Open GitHub",
                       command=self._on_open_github).pack(side=tk.LEFT, padx=2)

            self.reload()

        def reload(self):
            if hasattr(self, "_loading") and self._loading:
                return
            self._loading = True
            def work():
                model = fetch_model()
                repos = model.get("repos", [])
                if not model.get("error"):
                    excluded = set(self.config.get("excluded_repos", []))
                    repos = [r for r in repos if r.get("path") not in excluded]
                    for r in repos:
                        r["github"] = github_url(r.get("path", ""))
                self.tray.root.after(0, lambda: self._populate(model, repos))
            threading.Thread(target=work, daemon=True).start()

        def _populate(self, model, repos):
            self._loading = False
            self._tree.delete(*self._tree.get_children())
            if model.get("error"):
                self._tree.insert("", tk.END, values=(f"Error: {model['error']}", "", ""))
                return
            self._repos = repos
            self._filter()

        def _filter(self):
            self._tree.delete(*self._tree.get_children())
            query = self._search_var.get().strip().lower()
            dirty_only = self._dirty_var.get()
            for r in self._repos:
                if dirty_only and not r.get("git", {}).get("dirty"):
                    continue
                name = r.get("name", "")
                if query and query not in name.lower():
                    continue
                git = r.get("git", {})
                branch = git.get("branch", "")
                ahead = git.get("ahead", 0)
                behind = git.get("behind", 0)
                status = ""
                if git.get("dirty"):
                    status = f"dirty ({len(git.get('dirty_files', []))} files)"
                elif ahead or behind:
                    status = f"+{ahead} -{behind}"
                else:
                    status = "clean"
                error = r.get("error", "")
                if error:
                    status = f"ERROR: {error[:60]}"
                values = (name, branch, status)
                self._tree.insert("", tk.END, values=values,
                                  iid=r.get("path", name))

        def _selected(self):
            sel = self._tree.selection()
            if not sel:
                return None
            iid = sel[0]
            for r in self._repos:
                if r.get("path") == iid or r.get("name") == iid:
                    return r
            return None

        def _on_open_terminal(self):
            r = self._selected()
            if r:
                open_terminal(r["path"])

        def _on_open_folder(self):
            r = self._selected()
            if r:
                open_folder(r["path"])

        def _on_open_github(self):
            r = self._selected()
            if r and r.get("github"):
                open_url(r["github"])

    # ── Settings Dialog ───────────────────────────────────────────────────
    class SettingsDialog(tk.Toplevel):
        def __init__(self, master, config):
            super().__init__(master)
            self.title("repodash — settings")
            self.config = dict(config)
            self.result = None
            self.geometry("550x500")
            self.resizable(False, False)

            nb = ttk.Notebook(self)
            nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

            gframe = ttk.Frame(nb)
            nb.add(gframe, text="General")
            self._build_general(gframe)

            gframe2 = ttk.Frame(nb)
            nb.add(gframe2, text="Git")
            self._build_git(gframe2)

            btn_frame = ttk.Frame(self)
            btn_frame.pack(fill=tk.X, padx=10, pady=(0, 10))
            ttk.Button(btn_frame, text="Cancel",
                       command=self.destroy).pack(side=tk.RIGHT, padx=5)
            ttk.Button(btn_frame, text="Save",
                       command=self._save).pack(side=tk.RIGHT)

        def _build_general(self, frame):
            row = 0
            ttk.Label(frame, text="Base directory:").grid(
                row=row, column=0, sticky=tk.W, padx=5, pady=5)
            self._base_var = tk.StringVar(value=self.config.get("base_dir", ""))
            ttk.Entry(frame, textvariable=self._base_var, width=50).grid(
                row=row, column=1, padx=5, pady=5)
            row += 1

            ttk.Label(frame, text="Scan depth:").grid(
                row=row, column=0, sticky=tk.W, padx=5, pady=5)
            self._depth_var = tk.StringVar(
                value=str(self.config.get("depth", "")))
            ttk.Spinbox(frame, textvariable=self._depth_var,
                        from_=0, to=20, width=5).grid(
                row=row, column=1, sticky=tk.W, padx=5, pady=5)
            row += 1

            ttk.Label(frame, text="Refresh interval (s):").grid(
                row=row, column=0, sticky=tk.W, padx=5, pady=5)
            self._interval_var = tk.StringVar(
                value=str(self.config.get("refresh_interval", "")))
            ttk.Spinbox(frame, textvariable=self._interval_var,
                        from_=0, to=3600, width=5).grid(
                row=row, column=1, sticky=tk.W, padx=5, pady=5)
            row += 1

            ttk.Label(frame, text="Terminal:").grid(
                row=row, column=0, sticky=tk.W, padx=5, pady=5)
            self._term_var = tk.StringVar(
                value=self.config.get("terminal", ""))
            ttk.Combobox(frame, textvariable=self._term_var,
                         values=["wt", "cmd", "powershell", "pwsh"],
                         width=15).grid(
                row=row, column=1, sticky=tk.W, padx=5, pady=5)
            row += 1

            self._remoteless_var = tk.BooleanVar(
                value=self.config.get("show_remoteless", True))
            ttk.Checkbutton(frame,
                            text="Show repos without a remote in menu",
                            variable=self._remoteless_var).grid(
                row=row, column=0, columnspan=2, sticky=tk.W, padx=5, pady=5)
            row += 1

            self._autostart_var = tk.BooleanVar(value=autostart_enabled())
            ttk.Checkbutton(frame, text="Start with Windows",
                            variable=self._autostart_var).grid(
                row=row, column=0, columnspan=2, sticky=tk.W, padx=5, pady=5)

        def _build_git(self, frame):
            row = 0
            ttk.Label(frame, text="Stale worktree — idle (hours):").grid(
                row=row, column=0, sticky=tk.W, padx=5, pady=5)
            self._idle_var = tk.StringVar(
                value=str(self.config.get("stale_worktree_idle_hours", 24)))
            ttk.Spinbox(frame, textvariable=self._idle_var,
                        from_=0, to=720, width=5).grid(
                row=row, column=1, sticky=tk.W, padx=5, pady=5)
            row += 1

            ttk.Label(frame, text="Stale worktree — stuck (hours):").grid(
                row=row, column=0, sticky=tk.W, padx=5, pady=5)
            self._stuck_var = tk.StringVar(
                value=str(self.config.get("stale_worktree_stuck_hours", 12)))
            ttk.Spinbox(frame, textvariable=self._stuck_var,
                        from_=0, to=720, width=5).grid(
                row=row, column=1, sticky=tk.W, padx=5, pady=5)

        def _save(self):
            cfg = dict(self.config)
            base = self._base_var.get().strip()
            cfg["base_dir"] = base
            try:
                cfg["depth"] = int(self._depth_var.get())
            except ValueError:
                cfg["depth"] = 0
            try:
                cfg["refresh_interval"] = int(self._interval_var.get())
            except ValueError:
                cfg["refresh_interval"] = 0
            cfg["terminal"] = self._term_var.get().strip()
            cfg["show_remoteless"] = self._remoteless_var.get()
            try:
                cfg["stale_worktree_idle_hours"] = int(self._idle_var.get())
            except ValueError:
                pass
            try:
                cfg["stale_worktree_stuck_hours"] = int(self._stuck_var.get())
            except ValueError:
                pass
            cfg["excluded_repos"] = self.config.get("excluded_repos", [])
            cfg["ai_providers"] = self.config.get("ai_providers", {})
            self.result = cfg
            set_autostart(self._autostart_var.get())
            self.destroy()

    # ── Help Dialog ───────────────────────────────────────────────────────
    class HelpDialog(tk.Toplevel):
        def __init__(self, master):
            super().__init__(master)
            self.title("repodash — help & about")
            self.geometry("500x400")

            text = scrolledtext.ScrolledText(self, wrap=tk.WORD, padx=10,
                                             pady=10)
            text.pack(fill=tk.BOTH, expand=True)

            content = (
                "repodash — Windows tray companion\n"
                f"Version: {VERSION}\n"
                "Copyright (C) 2026 repodash contributors\n"
                "License: GPL-3.0-or-later\n"
                "\n"
                "Usage:\n"
                "  python repodash_tray_win.py                 Launch tray\n"
                "  python repodash_tray_win.py --check         Headless status dump\n"
                "  python repodash_tray_win.py --help          This help\n"
                "\n"
                "The tray icon shows an overview of your git repositories.\n"
                "Right-click the icon for the context menu.\n"
                "\n"
                "Tray menu sections:\n"
                "  • Dirty repos — repos with uncommitted changes\n"
                "  • Stale worktrees — old or stuck git worktrees\n"
                "  • Show dashboard — full repository list with search\n"
                "  • Settings — configure scan root, depth, interval\n"
                "\n"
                "Settings are stored in %APPDATA%\\repodash\\config.json\n"
            )
            text.insert(tk.END, content)
            text.config(state=tk.DISABLED)

            ttk.Button(self, text="Close",
                       command=self.destroy).pack(pady=10)

    # ── entry point ───────────────────────────────────────────────────────
    root = tk.Tk()
    root.withdraw()

    cfg = load_config()
    apply_config_to_env(cfg)

    tray = WinTrayIcon(root, cfg)

    def _on_quit():
        tray._remove_icon()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_quit)
    root.mainloop()
    return 0


# ── entry point ──────────────────────────────────────────────────────────────
def main(argv=None):
    import sys
    argv = sys.argv[1:] if argv is None else argv
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    if "--check" in argv:
        return run_check()
    return run_gui_win()


if __name__ == "__main__":
    import sys
    sys.exit(main())
