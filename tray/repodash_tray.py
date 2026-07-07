#!/usr/bin/env python3
# repodash — GNOME tray icon + dashboard window (Linux / GTK3 only).
# Copyright (C) 2026 repodash contributors. GPL-3.0-or-later.
"""A system-tray companion for repodash. Linux / GTK3 GUI backend.

The pure functions live in ``repodash_tray_core.py``. This file contains only
the GTK3 GUI layer (``run_gui()``) and the entry point (``main()``). All
module-level helpers are imported from the shared core module.
"""

import os as _os
import sys as _sys

_tray_dir = _os.path.dirname(_os.path.abspath(__file__))
if _tray_dir not in _sys.path:
    _sys.path.insert(0, _tray_dir)

from repodash_tray_core import *  # noqa: F403  — shared pure helpers
from repodash_tray_core import (  # noqa: F401  — _-prefixed names used in GUI
    _format_age,
    _git,
    _current_upstream,
    _repo_op_gate,
    _fetch_opencode_go_models,
    _FETCHED_OPENGODE_GO_MODELS,
    _OPENGODE_GO_HEADER,
    _NONINTERACTIVE_GIT_ENV,
)
del _os, _sys, _tray_dir
# ── GUI layer (GTK3) ─────────────────────────────────────────────────────────
def run_gui() -> int:
    import gi
    gi.require_version("Gtk", "3.0")
    try:
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as AppIndicator
    except (ValueError, ImportError):
        try:
            gi.require_version("AppIndicator3", "0.1")
            from gi.repository import AppIndicator3 as AppIndicator
        except (ValueError, ImportError):
            sys.stderr.write(
                "error: no AppIndicator typelib found. Install "
                "gir1.2-ayatanaappindicator3-0.1 (see tray/README.md).\n")
            return 1
    from gi.repository import Gtk, GLib, Gdk
    import threading

    def _screen_fraction_size(parent, w_frac=0.7, h_frac=0.7):
        """(width, height) at *w_frac*/*h_frac* of the parent's monitor.

        Falls back to the default display's primary monitor when *parent*
        has no realized window (e.g. the tray has no dashboard open).
        Clamped to a sane minimum so a tiny/unknown monitor never produces
        an unusably small dialog.
        """
        display = Gdk.Display.get_default()
        monitor = None
        window = parent.get_window() if parent is not None else None
        if display is not None and window is not None:
            monitor = display.get_monitor_at_window(window)
        if monitor is None and display is not None:
            monitor = display.get_monitor(0)
        if monitor is None:
            return 480, 420
        geo = monitor.get_geometry()
        return max(400, int(geo.width * w_frac)), max(300, int(geo.height * h_frac))

    APP_ID = "org.repodash.Tray"
    ICON_SVG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "repodash.svg")
    FALLBACK_ICON = "utilities-terminal"

    def warn_if_no_indicator_extension():
        if not shutil.which("gnome-extensions"):
            return
        try:
            out = subprocess.run(["gnome-extensions", "list", "--enabled"],
                                 capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return
        if "appindicator" not in out.stdout.lower():
            sys.stderr.write(
                "warning: no AppIndicator GNOME extension enabled — the tray "
                "icon may not appear. Enable 'Ubuntu AppIndicators'.\n")

    def notify(parent, ok, message):
        """Surface an action failure; success is silent."""
        if ok:
            return
        dlg = Gtk.MessageDialog(transient_for=parent, modal=True,
                                message_type=Gtk.MessageType.WARNING,
                                buttons=Gtk.ButtonsType.OK,
                                text="Action failed")
        dlg.format_secondary_text(message or "unknown error")
        dlg.run()
        dlg.destroy()

    class TrayApp(Gtk.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID)
            self.indicator = None
            self.window = None
            self.repos = []          # cheap status list (menu tier)
            self.model = None        # full model (dashboard tier), lazy
            self._timer_id = 0
            self._op_running = False  # True while a commit/push dialog is open
            self.config = load_config()
            apply_config_to_env(self.config)

        def quit(self):
            """Force-quit even when a modal dialog (commit, push, settings, …)
            is open.  Destroy all windows so that any blocking Gtk.Dialog.run()
            call returns immediately, then hand off to Gtk.Application.quit()."""
            for w in list(self.get_windows()):
                w.destroy()
            Gtk.Application.quit(self)

        # -- lifecycle --
        def do_startup(self):
            Gtk.Application.do_startup(self)
            self.hold()  # stay alive without a window (tray-resident)
            self._first_activate = True
            warn_if_no_indicator_extension()
            # libappindicator resolves icons by *name* within a theme path more
            # reliably than by absolute file path; register tray/ as a theme dir
            # and reference "repodash" (→ repodash.svg). Fall back to a stock
            # theme icon if our SVG is missing.
            have_icon = os.path.isfile(ICON_SVG)
            icon_name = "repodash" if have_icon else FALLBACK_ICON
            self.indicator = AppIndicator.Indicator.new(
                "repodash-tray", icon_name,
                AppIndicator.IndicatorCategory.APPLICATION_STATUS)
            if have_icon:
                self.indicator.set_icon_theme_path(
                    os.path.dirname(os.path.abspath(__file__)))
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            self.indicator.set_title("repodash")
            self.indicator.set_menu(self._build_menu())
            self.refresh_menu()
            threading.Thread(target=_fetch_opencode_go_models, daemon=True).start()
            self._timer_id = GLib.timeout_add_seconds(
                resolve_interval(self.config), self._on_timer)

        def do_activate(self):
            # GtkApplication fires `activate` on the primary instance at normal
            # startup too — stay quietly tray-resident on first launch, and only
            # surface the dashboard on a genuine re-activation (second launch).
            if self._first_activate:
                self._first_activate = False
                return
            self.show_dashboard()

        def _on_timer(self):
            self.refresh_menu()
            return True  # keep ticking

        # -- menu tier --
        def refresh_menu(self):
            cfg = self.config
            self.indicator.set_label("↻", "")

            def work():
                base = resolve_base_dir(cfg)
                depth = resolve_depth(cfg)
                excluded = set(cfg.get("excluded_repos", []))
                repos = scan_dirty(base, depth, cfg)
                repos = [r for r in repos if r["path"] not in excluded]
                if not cfg.get("show_remoteless", True):
                    repos = [r for r in repos if r["has_remote"]]
                # Resolve GitHub URLs off the main thread so menu-building
                # (which runs on the GTK thread) never blocks on git. Both the
                # dirty and unpushed sections expose an "Open GitHub" action.
                for r in repos:
                    sw = r.get("stale_worktrees") or {}
                    if (r["dirty"] or (r["has_remote"] and r["unpushed"])
                            or sw.get("stuck") or sw.get("idle") or sw.get("merged")):
                        r["github"] = github_url(r["path"])
                GLib.idle_add(self._apply_repos, repos)
            threading.Thread(target=work, daemon=True).start()

        def _apply_repos(self, repos):
            self.repos = repos
            self.indicator.set_menu(self._build_menu())
            dirty = sum(1 for r in repos if r["dirty"])
            self.indicator.set_label(str(dirty) if dirty else "", "")
            return False

        def _build_menu(self):
            menu = Gtk.Menu()
            dirty = [r for r in self.repos if r["dirty"]]
            unpushed = [r for r in self.repos
                        if r["has_remote"] and r["unpushed"] > 0]
            header = Gtk.MenuItem(
                label=f"{len(dirty)} dirty · {len(unpushed)} unpushed · "
                      f"{len(self.repos)} repos")
            header.set_sensitive(False)
            menu.append(header)
            menu.append(Gtk.SeparatorMenuItem())

            for r in dirty:
                menu.append(self._repo_item(r))
            if not dirty:
                clean = Gtk.MenuItem(label="✓ all clean")
                clean.set_sensitive(False)
                menu.append(clean)
            else:
                self._action(menu, f"Commit all via {self._ai_label()} ({len(dirty)})…",
                             lambda *_: self._on_commit_all())

            # Unpushed repos get their own section (a repo can be both dirty and
            # unpushed — it then appears in both lists, each complete on its own).
            if unpushed:
                menu.append(Gtk.SeparatorMenuItem())
                sub_header = Gtk.MenuItem(label="Unpushed")
                sub_header.set_sensitive(False)
                menu.append(sub_header)
                for r in unpushed:
                    menu.append(self._repo_item(r, unpushed=True))
                self._action(menu, f"Push all ({len(unpushed)})…",
                             lambda *_: self._on_push_all())
                self._action(menu, f"Push all via {self._ai_label()} ({len(unpushed)})…",
                             lambda *_: self._on_push_claude_all())

            stuck_repos = [r for r in self.repos
                           if r.get("stale_worktrees", {}).get("stuck")]
            idle_repos = [r for r in self.repos
                          if r.get("stale_worktrees", {}).get("idle")]
            merged_repos = [r for r in self.repos
                            if r.get("stale_worktrees", {}).get("merged")]

            if stuck_repos:
                menu.append(Gtk.SeparatorMenuItem())
                hdr = Gtk.MenuItem(label="⚠ Stuck worktrees")
                hdr.set_sensitive(False)
                menu.append(hdr)
                for r in stuck_repos:
                    menu.append(self._stale_repo_item(r, "stuck"))

            if idle_repos:
                menu.append(Gtk.SeparatorMenuItem())
                hdr = Gtk.MenuItem(label="⏸ Idle worktrees")
                hdr.set_sensitive(False)
                menu.append(hdr)
                for r in idle_repos:
                    menu.append(self._stale_repo_item(r, "idle"))

            if merged_repos:
                menu.append(Gtk.SeparatorMenuItem())
                hdr = Gtk.MenuItem(label="✓ Merged worktrees")
                hdr.set_sensitive(False)
                menu.append(hdr)
                for r in merged_repos:
                    menu.append(self._stale_repo_item(r, "merged"))

            menu.append(Gtk.SeparatorMenuItem())
            self._action(menu, "Show dashboard…",
                         lambda *_: self.show_dashboard())
            self._action(menu, "Refresh now", lambda *_: self.refresh_menu())
            self._action(menu, "Settings…", lambda *_: self._on_settings())
            self._action(menu, "Help & About…", lambda *_: self._on_help())

            start_item = Gtk.CheckMenuItem(label="Start on login")
            start_item.set_active(autostart_enabled())  # set before connecting
            start_item.connect("toggled", self._on_toggle_autostart)
            menu.append(start_item)

            menu.append(Gtk.SeparatorMenuItem())
            self._action(menu, "Quit", lambda *_: self.quit())
            menu.show_all()
            return menu

        def _on_toggle_autostart(self, item):
            ok = set_autostart(item.get_active())
            # Reflect the real on-disk result (e.g. if the write failed).
            if ok != item.get_active():
                item.set_active(ok)

        def _show_error(self, title, detail):
            """Surface a dialog-construction failure (does not use a
            potentially-broken ConfigDialog).  Returns False if even the
            error dialog could not be shown (bare terminal fallback)."""
            try:
                parent = self.window if (self.window and self.window.get_visible()) else None
                dlg = Gtk.MessageDialog(transient_for=parent, modal=True,
                                        message_type=Gtk.MessageType.ERROR,
                                        buttons=Gtk.ButtonsType.OK,
                                        text=title)
                dlg.format_secondary_text(detail)
                dlg.run()
                dlg.destroy()
                return True
            except Exception:
                import traceback
                traceback.print_exc()
                return False

        def _on_settings(self):
            parent = self.window if (self.window and self.window.get_visible()) else None
            try:
                dlg = ConfigDialog(parent, self.config)
                response = dlg.run()
                if response == Gtk.ResponseType.OK:
                    self.config = dlg.get_config()
                    apply_config_to_env(self.config)
                    save_config(self.config)
                    if self._timer_id:
                        GLib.source_remove(self._timer_id)
                    self._timer_id = GLib.timeout_add_seconds(
                        resolve_interval(self.config), self._on_timer)
                    self.refresh_menu()
                    if self.window is not None:
                        self.window.set_config(self.config)
                        if self.window.get_visible():
                            self.window.reload()
                dlg.destroy()
            except Exception:
                import traceback
                msg = traceback.format_exc()
                traceback.print_exc()
                self._show_error(
                    "Settings could not be opened",
                    "A bug in the settings dialog construction prevented it "
                    "from being shown. The error has been printed to the "
                    "terminal.\n\n" + msg.split("\n")[-2])

        def _run_op_dialog(self, dlg):
            """Run a commit/push dialog, blocking concurrent ops."""
            if self._op_running:
                return
            self._op_running = True
            try:
                dlg.run()
            finally:
                self._op_running = False
                dlg.destroy()
                self.refresh_menu()

        def _on_push_all(self):
            if self._op_running:
                return
            # Recompute from the latest scan (the menu may have refreshed since
            # it was built) so we never push a stale set.
            repos = [r for r in self.repos
                     if r["has_remote"] and r["unpushed"] > 0]
            if not repos:
                return
            parent = self.window if (self.window and self.window.get_visible()) else None
            self._run_op_dialog(PushAllDialog(parent, repos))

        def _on_commit_all(self):
            if self._op_running:
                return
            # Recompute the dirty set from the latest scan so a refresh between
            # menu-build and click never commits a stale list.
            repos = [r for r in self.repos if r["dirty"]]
            if not repos:
                return
            cfg = self.config
            parent = self.window if (self.window and self.window.get_visible()) else None
            self._run_op_dialog(CommitAllDialog(parent, repos,
                                                cfg.get("commit_ram_mb", 2048),
                                                cfg.get("commit_max_workers", 0),
                                                cfg.get("commit_timeout", 3600),
                                                cfg.get("commit_budget_usd", 10.0),
                                                provider_selection(cfg), "commit"))

        def _on_commit_repo(self, r):
            if self._op_running:
                return
            # Single-repo counterpart to _on_commit_all: same headless AI
            # flow (logical chunks, repo-conventional messages, docs, merge),
            # just scoped to one repo via the shared progress dialog.
            cfg = self.config
            parent = self.window if (self.window and self.window.get_visible()) else None
            self._run_op_dialog(CommitAllDialog(parent, [r],
                                                cfg.get("commit_ram_mb", 2048),
                                                cfg.get("commit_max_workers", 0),
                                                cfg.get("commit_timeout", 3600),
                                                cfg.get("commit_budget_usd", 10.0),
                                                provider_selection(cfg), "commit"))

        def _on_push_claude_repo(self, r):
            if self._op_running:
                return
            cfg = self.config
            parent = self.window if (self.window and self.window.get_visible()) else None
            self._run_op_dialog(CommitAllDialog(
                parent, [r],
                cfg.get("commit_ram_mb", 2048),
                cfg.get("commit_max_workers", 0),
                cfg.get("commit_timeout", 3600),
                cfg.get("commit_budget_usd", 10.0),
                provider_selection(cfg), "push",
                verb="Push", verb_ing="Pushing", verb_past="Pushed",
                row_suffix=lambda rr: f"+{rr.get('unpushed', 0)}"))

        def _on_push_claude_all(self):
            if self._op_running:
                return
            repos = [r for r in self.repos
                     if r["has_remote"] and r["unpushed"] > 0]
            if not repos:
                return
            cfg = self.config
            parent = self.window if (self.window and self.window.get_visible()) else None
            self._run_op_dialog(CommitAllDialog(
                parent, repos,
                cfg.get("commit_ram_mb", 2048),
                cfg.get("commit_max_workers", 0),
                cfg.get("commit_timeout", 3600),
                cfg.get("commit_budget_usd", 10.0),
                provider_selection(cfg), "push",
                verb="Push", verb_ing="Pushing", verb_past="Pushed",
                row_suffix=lambda rr: f"+{rr.get('unpushed', 0)}"))

        def _on_commit_and_push_repo(self, r):
            if self._op_running:
                return
            cfg = self.config
            parent = self.window if (self.window and self.window.get_visible()) else None
            self._run_op_dialog(CommitAllDialog(
                parent, [r],
                cfg.get("commit_ram_mb", 2048),
                cfg.get("commit_max_workers", 0),
                cfg.get("commit_timeout", 3600),
                cfg.get("commit_budget_usd", 10.0),
                provider_selection(cfg), "commit_and_push",
                verb="Commit & Push", verb_ing="Committing & pushing",
                verb_past="Committed & pushed",
                row_suffix=lambda rr: f"{rr.get('count', '')}"))

        def _on_explain_repo(self, r):
            cfg = self.config
            parent = self.window if (self.window and self.window.get_visible()) else None
            dlg = ExplainDialog(parent, r,
                                cfg.get("commit_budget_usd", 10.0),
                                provider_selection(cfg))
            response = dlg.run()
            dlg.destroy()
            if response == ExplainDialog.RESPONSE_COMMIT:
                self._on_commit_repo(r)
            elif response == ExplainDialog.RESPONSE_PUSH:
                self._on_push_claude_repo(r)
            elif response == ExplainDialog.RESPONSE_COMMIT_PUSH:
                self._on_commit_and_push_repo(r)

        def _on_wt_push_claude(self, wt, repo):
            if self._op_running:
                return
            cfg = self.config
            parent = self.window if (self.window and self.window.get_visible()) else None
            r = {
                "path": wt["path"],
                "name": wt.get("branch", os.path.basename(wt["path"])),
                "branch": wt.get("branch", ""),
                "unpushed": 0,
            }
            self._run_op_dialog(CommitAllDialog(
                parent, [r],
                cfg.get("commit_ram_mb", 2048),
                cfg.get("commit_max_workers", 0),
                cfg.get("commit_timeout", 3600),
                cfg.get("commit_budget_usd", 10.0),
                provider_selection(cfg), "push",
                verb="Push", verb_ing="Pushing", verb_past="Pushed",
                row_suffix=lambda rr: rr.get("branch", "")))

        def _on_help(self):
            parent = self.window if (self.window and self.window.get_visible()) else None
            try:
                dlg = HelpDialog(parent)
                dlg.run()
                dlg.destroy()
            except Exception:
                import traceback
                traceback.print_exc()
                self._show_error(
                    "Help could not be opened",
                    "A bug in the help dialog construction prevented it "
                    "from being shown. The error has been printed to the "
                    "terminal.")


        def _ai_label(self):
            pid = self.config.get("ai_primary_provider", "claude")
            return PROVIDERS.get(pid, PROVIDERS["claude"]).label

        def _repo_item(self, r, unpushed=False):
            if unpushed:
                # In the unpushed section, lead with the unpushed-commit count
                # (a clean-but-unpushed repo has no dirty files to show).
                label = f"{r['name']}  ({r['branch']}, +{r['unpushed']})"
            else:
                track = ""
                if r["ahead"] or r["behind"]:
                    track = f" ▲{r['ahead']}▼{r['behind']}"
                label = f"{r['name']}  ({r['branch']}{track}, {r['count']})"
            item = Gtk.MenuItem(label=label)
            sub = Gtk.Menu()
            path = r["path"]
            ai_label = self._ai_label()
            pid = self.config.get("ai_primary_provider", "claude")
            self._action(sub, "Open terminal",
                         lambda *_: notify(self.window, *open_terminal(path)))
            self._action(sub, f"Open {ai_label}",
                         lambda *_: notify(self.window, *open_provider_terminal(path, pid)))
            if explain_actions(r):
                self._action(sub, "Explain changes…",
                             lambda *_, r=r: self._on_explain_repo(r))
            commit_label = "git commit" + (f" ({r['count']})" if r["count"] else "")
            self._action(sub, commit_label,
                         lambda *_: notify(self.window, *open_commit(path)))
            if r["count"]:
                self._action(sub, f"Commit via {ai_label}…",
                             lambda *_, r=r: self._on_commit_repo(r))
            push_label = "git push" + (f" (+{r['ahead']})" if r["ahead"] else "")
            self._action(sub, push_label,
                         lambda *_: notify(self.window, *open_push(path)))
            if r.get("has_remote") and r.get("unpushed", 0) > 0:
                self._action(sub, f"Push via {ai_label}…",
                             lambda *_, r=r: self._on_push_claude_repo(r))
            if r.get("github"):
                self._action(sub, "Open GitHub",
                             lambda *_: notify(self.window, *open_github(path)))
            self._action(sub, "Open folder",
                         lambda *_: notify(self.window, *open_folder(path)))
            self._action(sub, "Copy path", lambda *_: self._copy(path))
            item.set_submenu(sub)
            return item

        def _worktree_item(self, wt, severity, r):
            age_str = _format_age(wt["last_commit_age_hours"])
            if severity == "stuck":
                label = f"  {wt['branch']}  {age_str} ago (dirty)"
            elif severity == "merged":
                label = f"  {wt['branch']}  {age_str} ago (absorbed in main)"
            else:
                behind_s = f"  ▼{wt['behind']}" if wt["behind"] else ""
                label = f"  {wt['branch']}  {age_str} ago{behind_s}"
            item = Gtk.MenuItem(label=label)
            sub = Gtk.Menu()
            path = wt["path"]
            repo_path = r["path"]
            ai_label = self._ai_label()
            pid = self.config.get("ai_primary_provider", "claude")

            self._action(sub, "Open terminal",
                         lambda *_, p=path: notify(self.window, *open_terminal(p)))
            self._action(sub, f"Open {ai_label}",
                         lambda *_, p=path: notify(self.window, *open_provider_terminal(p, pid)))

            if severity == "stuck":
                count = len([ln for ln in
                             _git(path, "status", "--porcelain").splitlines()
                             if ln.strip()])
                commit_label = f"git commit ({count})" if count else "git commit"
                self._action(sub, commit_label,
                             lambda *_, p=path: notify(self.window, *open_commit(p)))
                ahead = wt.get("ahead", 0) or (
                    int(_git(path, "rev-list", "--count",
                             "HEAD", "--not", "--remotes").strip() or "0"))
                if ahead:
                    self._action(sub, f"git push (+{ahead})",
                                 lambda *_, p=path: notify(self.window, *open_push(p)))
                    self._action(sub, f"Push via {ai_label}…",
                                 lambda *_, w=wt, rr=r: self._on_wt_push_claude(w, rr))
                sub.append(Gtk.SeparatorMenuItem())
                self._action(sub, f"Finish & merge via {ai_label}…",
                             lambda *_, w=wt, rr=r: self._on_wt_finish(w, rr))
            elif severity == "merged":
                sub.append(Gtk.SeparatorMenuItem())
                self._action(sub, "Clean up (remove worktree + delete branch)",
                             lambda *_, w=wt, rp=repo_path: self._on_wt_cleanup(w, rp))
                self._action(sub, "Remove worktree only",
                             lambda *_, w=wt, rp=repo_path: self._on_wt_remove(w, rp))
            else:
                sub.append(Gtk.SeparatorMenuItem())
                self._action(sub, f"Close via {ai_label}…",
                             lambda *_, w=wt, rr=r: self._on_wt_close(w, rr))
                self._action(sub, "Remove worktree",
                             lambda *_, w=wt, rp=repo_path: self._on_wt_remove(w, rp))

            if r.get("github"):
                sub.append(Gtk.SeparatorMenuItem())
                self._action(sub, "Open GitHub",
                             lambda *_: notify(self.window, *open_github(repo_path)))
            sub.append(Gtk.SeparatorMenuItem())
            self._action(sub, "Open folder",
                         lambda *_, p=path: notify(self.window, *open_folder(p)))
            self._action(sub, "Copy path", lambda *_, p=path: self._copy(p))
            item.set_submenu(sub)
            return item

        def _on_wt_close(self, wt, r):
            cfg = self.config
            tmpl = cfg.get("worktree_idle_close_prompt") or IDLE_CLOSE_PROMPT
            prompt = tmpl.format(path=wt["path"], branch=wt["branch"],
                                 repo_path=r["path"])
            notify(self.window, *open_wt_provider(
                wt["path"], prompt, cfg.get("ai_primary_provider", "claude")))

        def _on_wt_finish(self, wt, r):
            cfg = self.config
            tmpl = cfg.get("worktree_stuck_finish_prompt") or STUCK_FINISH_PROMPT
            prompt = tmpl.format(path=wt["path"], branch=wt["branch"],
                                 repo_path=r["path"])
            notify(self.window, *open_wt_provider(
                wt["path"], prompt, cfg.get("ai_primary_provider", "claude")))

        def _on_wt_remove(self, wt, repo_path):
            ok, msg = remove_worktree(repo_path, wt["path"])
            notify(self.window, ok, msg or f"Removed {wt['branch']}")
            if ok:
                self.refresh_menu()

        def _on_wt_cleanup(self, wt, repo_path):
            ok, msg = remove_worktree(repo_path, wt["path"], branch=wt["branch"])
            notify(self.window, ok, msg or f"Cleaned up {wt['branch']}")
            if ok:
                self.refresh_menu()

        def _stale_repo_item(self, r, severity):
            wt_list = r["stale_worktrees"][severity]
            oldest = max(wt_list, key=lambda w: w["last_commit_age_hours"])
            n = len(wt_list)
            age_str = _format_age(oldest["last_commit_age_hours"])
            if severity == "stuck":
                label = f"⚠ {r['name']}  ({n} stuck, oldest {age_str})"
            elif severity == "merged":
                label = f"✓ {r['name']}  ({n} merged, oldest {age_str})"
            else:
                behind_s = f", {oldest['behind']} behind" if oldest["behind"] else ""
                label = f"⏸ {r['name']}  ({n} idle, oldest {age_str}{behind_s})"
            item = Gtk.MenuItem(label=label)
            sub = Gtk.Menu()
            for wt in wt_list:
                sub.append(self._worktree_item(wt, severity, r))
            item.set_submenu(sub)
            return item

        @staticmethod
        def _action(menu, label, handler):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", handler)
            menu.append(item)
            return item

        def _copy(self, text):
            notify(self.window, *copy_to_clipboard(text))

        # -- dashboard tier --
        def show_dashboard(self):
            try:
                if self.window is None:
                    self.window = DashboardWindow(self, self.config)
                self.window.show_all()
                self.window.present()
                self.window.reload()
            except Exception:
                import traceback
                traceback.print_exc()
                self._show_error(
                    "Dashboard could not be opened",
                    "A bug in the dashboard window construction prevented it "
                    "from being shown. The error has been printed to the "
                    "terminal.")

    class DashboardWindow(Gtk.Window):
        def __init__(self, app, config):
            super().__init__(title="repodash")
            self.app = app
            self.config = config
            self._loading = False  # guards against overlapping reloads
            self.set_default_size(720, 560)
            self.set_icon_name("utilities-terminal")
            self.connect("delete-event", self._on_close)

            outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            outer.set_border_width(8)
            self.add(outer)

            bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            self.search = Gtk.SearchEntry()
            self.search.set_placeholder_text("Filter by name or path…")
            self.search.connect("search-changed", lambda *_: self._refilter())
            bar.pack_start(self.search, True, True, 0)

            self.dirty_only = Gtk.CheckButton(label="Dirty only")
            self.dirty_only.connect("toggled", lambda *_: self._refilter())
            bar.pack_start(self.dirty_only, False, False, 0)
            self.has_todos = Gtk.CheckButton(label="Has TODOs")
            self.has_todos.connect("toggled", lambda *_: self._refilter())
            bar.pack_start(self.has_todos, False, False, 0)

            self.refresh_btn = Gtk.Button(label="Refresh")
            self.refresh_btn.connect("clicked", lambda *_: self.reload())
            bar.pack_start(self.refresh_btn, False, False, 0)

            settings_btn = Gtk.Button(label="Settings…")
            settings_btn.connect("clicked", lambda *_: self.app._on_settings())
            bar.pack_start(settings_btn, False, False, 0)

            outer.pack_start(bar, False, False, 0)

            self.status = Gtk.Label(label="", xalign=0)
            outer.pack_start(self.status, False, False, 0)

            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self.listbox = Gtk.ListBox()
            self.listbox.set_filter_func(self._filter_row)
            scroller.add(self.listbox)
            outer.pack_start(scroller, True, True, 0)

        def _on_close(self, *_):
            self.hide()
            return True  # keep the app alive; just hide

        def set_config(self, config):
            self.config = config

        def reload(self):
            # Ignore re-entry: a running scan holds a repodash.py --json
            # subprocess, and overlapping reloads would race to repopulate
            # the listbox. The button is also disabled for the duration.
            if self._loading:
                return
            self._loading = True
            self.refresh_btn.set_sensitive(False)
            self.refresh_btn.set_label("Refresh (…)")
            self.status.set_text("Scanning…")
            cfg = self.config

            def work():
                model = fetch_model()
                excluded = set(cfg.get("excluded_repos", []))
                model["repos"] = [
                    r for r in model.get("repos", [])
                    if r.get("path") not in excluded
                ]
                # Resolve GitHub URLs here (off the GTK thread) and stash them
                # on each repo so row-building never blocks on git.
                for repo in model["repos"]:
                    repo["github"] = github_url(repo.get("path", ""))
                GLib.idle_add(self._populate, model)
            threading.Thread(target=work, daemon=True).start()

        def _populate(self, model):
            for child in self.listbox.get_children():
                self.listbox.remove(child)
            self._loading = False
            self.refresh_btn.set_sensitive(True)
            if model.get("error"):
                self.refresh_btn.set_label("Refresh")
                self.status.set_text("Error: " + model["error"])
                return False
            repos = model.get("repos", [])
            for repo in repos:
                self.listbox.add(self._row(repo))
            self.listbox.show_all()
            # Show the result count in brackets on the button as a completion cue.
            self.refresh_btn.set_label(f"Refresh ({len(repos)})")
            self.status.set_text(f"{len(repos)} repos")
            self._refilter()
            return False

        def _row(self, repo):
            row = Gtk.ListBoxRow()
            row._repo = repo  # stash for the filter
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_border_width(6)

            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            git = repo.get("git", {})
            todos = repo.get("todos", {})
            track = ""
            if git.get("ahead") or git.get("behind"):
                track = f"  ▲{git.get('ahead', 0)}▼{git.get('behind', 0)}"
            dirty_n = len(git.get("dirty_files", []))
            title = Gtk.Label(xalign=0)
            mark = "●" if git.get("dirty") else "○"
            title.set_markup(
                f"<b>{GLib.markup_escape_text(repo.get('name', '?'))}</b>  "
                f"<small>{mark} {GLib.markup_escape_text(git.get('branch', ''))}"
                f"{track}</small>")
            info.pack_start(title, False, False, 0)

            bits = []
            if dirty_n:
                bits.append(f"{dirty_n} changed")
            if todos.get("total"):
                bits.append(f"{todos['total']} TODO")
            audit = repo.get("audit", {})
            if audit.get("files") or audit.get("archive"):
                bits.append("audit")
            if any(f.get("items") for f in repo.get("roadmap", {}).get("files", [])):
                bits.append("roadmap")
            sonar = repo.get("sonar", {})
            if sonar.get("configured"):
                bits.append("sonar" if sonar.get("ok") else "sonar!")
            sub = Gtk.Label(xalign=0)
            sub.set_markup(f"<small>{GLib.markup_escape_text(' · '.join(bits) or 'clean')}</small>")
            info.pack_start(sub, False, False, 0)
            box.pack_start(info, True, True, 0)

            path = repo.get("path", "")
            box.pack_start(self._btn("utilities-terminal", "Terminal",
                                     lambda *_: notify(self, *open_terminal(path))),
                           False, False, 0)
            pid = self.config.get("ai_primary_provider", "claude")
            ai_label = PROVIDERS.get(pid, PROVIDERS["claude"]).label
            box.pack_start(self._btn("system-run", ai_label,
                                     lambda *_: notify(self, *open_provider_terminal(path, pid))),
                           False, False, 0)
            push_tip = "git push" + (f" (▲{git.get('ahead')})" if git.get("ahead") else "")
            box.pack_start(self._btn("go-up", push_tip,
                                     lambda *_: notify(self, *open_push(path))),
                           False, False, 0)
            if repo.get("github"):
                box.pack_start(self._btn("web-browser", "GitHub",
                                         lambda *_: notify(self, *open_github(path))),
                               False, False, 0)
            box.pack_start(self._btn("folder", "Open folder",
                                     lambda *_: notify(self, *open_folder(path))),
                           False, False, 0)

            row.add(box)
            return row

        @staticmethod
        def _btn(icon_name, tooltip, handler):
            btn = Gtk.Button()
            btn.set_image(Gtk.Image.new_from_icon_name(
                icon_name, Gtk.IconSize.BUTTON))
            btn.set_tooltip_text(tooltip)
            btn.set_relief(Gtk.ReliefStyle.NONE)
            btn.connect("clicked", handler)
            return btn

        def _refilter(self):
            self.listbox.invalidate_filter()

        def _filter_row(self, row):
            repo = getattr(row, "_repo", None)
            if repo is None:
                return True
            text = self.search.get_text().strip().lower()
            if text and text not in repo.get("name", "").lower() \
                    and text not in repo.get("path", "").lower():
                return False
            if self.dirty_only.get_active() and not repo.get("git", {}).get("dirty"):
                return False
            if self.has_todos.get_active() and not repo.get("todos", {}).get("total"):
                return False
            return True

    class PushAllDialog(Gtk.Dialog):
        """Modal progress window for pushing every unpushed repo in sequence.

        Streaming git output appears live in the Details expander. A Stop button
        lets the user abort mid-run (after a confirmation prompt); Close stays
        disabled until every push has finished or been cancelled.
        """

        PENDING, RUNNING, OK, FAIL, STOPPED = "·", "↻", "✓", "✗", "⊘"

        def __init__(self, parent, repos):
            super().__init__(title="Push all", transient_for=parent, modal=True)
            self._repos = repos
            self._marks = {}  # path -> status Gtk.Label
            self._done = False
            self._cancel = threading.Event()
            self._proc = None
            self._proc_lock = threading.Lock()
            self.add_button("Close", Gtk.ResponseType.CLOSE)
            self.set_response_sensitive(Gtk.ResponseType.CLOSE, False)
            self.connect("delete-event", lambda *_: not self._done)
            self.set_default_size(*_screen_fraction_size(parent))

            area = self.get_content_area()
            area.set_border_width(10)
            area.set_spacing(8)

            self._summary = Gtk.Label(
                label=f"Pushing {len(repos)} repo(s)…", xalign=0)
            area.pack_start(self._summary, False, False, 0)

            self._bar = Gtk.ProgressBar()
            self._bar.set_show_text(True)
            self._bar.set_text(f"0 / {len(repos)}")
            area.pack_start(self._bar, False, False, 0)

            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            listbox = Gtk.ListBox()
            listbox.set_selection_mode(Gtk.SelectionMode.NONE)
            for r in repos:
                row = Gtk.ListBoxRow()
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                box.set_border_width(4)
                mark = Gtk.Label(label=self.PENDING)
                name = Gtk.Label(
                    label=f"{r['name']}  ({r['branch']}, +{r['unpushed']})",
                    xalign=0)
                box.pack_start(mark, False, False, 0)
                box.pack_start(name, True, True, 0)
                row.add(box)
                listbox.add(row)
                self._marks[r["path"]] = mark
            scroller.add(listbox)
            area.pack_start(scroller, True, True, 0)

            self._log = Gtk.TextView()
            self._log.set_editable(False)
            self._log.set_cursor_visible(False)
            self._log.set_monospace(True)
            self._log.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            log_scroll = Gtk.ScrolledWindow()
            log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC,
                                  Gtk.PolicyType.AUTOMATIC)
            log_scroll.set_min_content_height(150)
            log_scroll.add(self._log)
            expander = Gtk.Expander(label="Details")
            expander.add(log_scroll)
            expander.set_expanded(True)
            area.pack_start(expander, False, False, 0)

            # Stop button added to the action area directly so it does NOT
            # emit a dialog response (which would cause run() to return).
            action_area = self.get_action_area()
            self._stop_btn = Gtk.Button.new_with_label("Stop")
            self._stop_btn.connect("clicked", self._on_stop)
            action_area.pack_start(self._stop_btn, False, False, 0)
            action_area.reorder_child(self._stop_btn, 0)

            self.show_all()
            self._start()

        def _on_stop(self, *_):
            dlg = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text="Stop pushing?")
            dlg.format_secondary_text(
                "The current push will be killed. Any partially-pushed refs "
                "may be in an inconsistent state.")
            resp = dlg.run()
            dlg.destroy()
            if resp != Gtk.ResponseType.YES:
                return
            self._cancel.set()
            with self._proc_lock:
                if self._proc is not None:
                    self._killpg(self._proc)
            self._stop_btn.set_sensitive(False)

        @staticmethod
        def _killpg(proc):
            import signal as _sig
            try:
                os.killpg(os.getpgid(proc.pid), _sig.SIGKILL)
            except (ProcessLookupError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass

        def _start(self):
            env = {**os.environ, **_NONINTERACTIVE_GIT_ENV}

            def work():
                ok = 0
                total = len(self._repos)
                for i, r in enumerate(self._repos, 1):
                    if self._cancel.is_set():
                        GLib.idle_add(self._step, i, total, r, False, True)
                        continue

                    GLib.idle_add(self._mark, r["path"], self.RUNNING)
                    GLib.idle_add(self._append_log, f"=== {r['name']} ===\n")

                    if _current_upstream(r["path"], env):
                        argv = ["git", "-C", r["path"], "push", "--progress"]
                    else:
                        remote = next(iter(_git(r["path"], "remote").split()), "")
                        if not remote:
                            GLib.idle_add(self._append_log, "no remote configured\n")
                            GLib.idle_add(self._step, i, total, r, False, False)
                            continue
                        argv = ["git", "-C", r["path"], "push", "--progress",
                                "-u", remote, "HEAD"]

                    try:
                        proc = subprocess.Popen(
                            argv, env=env,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True)
                    except (OSError, subprocess.SubprocessError) as e:
                        GLib.idle_add(self._append_log, f"Error: {e}\n")
                        GLib.idle_add(self._step, i, total, r, False, False)
                        continue

                    with self._proc_lock:
                        self._proc = proc

                    try:
                        for raw in proc.stdout:
                            line = raw.replace("\r", "\n")
                            GLib.idle_add(self._append_log, line)
                            if self._cancel.is_set():
                                self._killpg(proc)
                                GLib.idle_add(self._append_log, "[Stopped]\n")
                                break
                    except Exception:
                        pass
                    finally:
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            self._killpg(proc)
                            proc.wait()
                        with self._proc_lock:
                            self._proc = None

                    cancelled = self._cancel.is_set()
                    success = proc.returncode == 0 and not cancelled
                    ok += 1 if success else 0
                    GLib.idle_add(self._step, i, total, r, success, cancelled)

                GLib.idle_add(self._finish, ok, total)

            threading.Thread(target=work, daemon=True).start()

        def _mark(self, path, glyph):
            self._marks[path].set_text(glyph)
            return False

        def _append_log(self, text):
            buf = self._log.get_buffer()
            buf.insert(buf.get_end_iter(), text)
            end = buf.get_end_iter()
            self._log.scroll_to_iter(end, 0.0, False, 0.0, 1.0)
            return False

        def _step(self, i, total, r, success, cancelled):
            if cancelled:
                self._marks[r["path"]].set_text(self.STOPPED)
            else:
                self._marks[r["path"]].set_text(self.OK if success else self.FAIL)
            self._bar.set_fraction(i / total)
            self._bar.set_text(f"{i} / {total}")
            return False

        def _finish(self, ok, total):
            failed = total - ok
            if self._cancel.is_set():
                msg = f"Pushed {ok}/{total} · stopped"
            elif failed:
                msg = f"Pushed {ok}/{total} · {failed} failed (see Details)"
            else:
                msg = f"Pushed {ok}/{total}"
            self._summary.set_text(msg)
            self._bar.set_fraction(1.0)
            self._done = True
            self._stop_btn.set_sensitive(False)
            self.set_response_sensitive(Gtk.ResponseType.CLOSE, True)
            return False

    class CommitAllDialog(Gtk.Dialog):
        """Modal progress window for committing/pushing repos via an AI CLI.

        Bounded-parallel (ThreadPoolExecutor, RAM-derived worker count). Each
        repo's row goes ·→↻→✓/✗/⊘. The provider's output streams live into the
        Details expander. A Stop button lets the user abort mid-run (with a
        confirmation prompt) using process-group kill so no orphaned children
        are left behind.

        ``provider_sel`` (see ``provider_selection()``) selects the primary AI
        provider and an optional secondary tried once, per repo, if the
        primary is missing or its run fails/times out — gated by
        ``_repo_op_gate`` so a fallback never double-commits/double-pushes or
        hands an interrupted git operation to a second agent. ``task`` picks
        the prompt/gate semantics: "commit" | "push" | "commit_and_push".
        """

        PENDING, RUNNING, OK, FAIL, STOPPED = "·", "↻", "✓", "✗", "⊘"

        def __init__(self, parent, repos, ram_mb, cap, timeout, budget_usd,
                     provider_sel=None, task="commit",
                     verb="Commit", verb_ing="Committing", verb_past="Committed",
                     worker=None, row_suffix=None):
            title = f"{verb} {repos[0]['name']}" if len(repos) == 1 else f"{verb} all"
            super().__init__(title=title, transient_for=parent, modal=True)
            self._repos = repos
            self._timeout = timeout
            self._budget = budget_usd
            self._provider_sel = provider_sel or provider_selection(CONFIG_DEFAULTS)
            self._task = task
            self._verb_past = verb_past
            self._row_suffix = row_suffix
            self._workers = commit_workers(ram_mb, cap)
            self._marks = {}  # path → status Gtk.Label
            self._done = False
            self._cancel = threading.Event()
            self._procs = {}   # path → Popen
            self._procs_lock = threading.Lock()
            self.add_button("Close", Gtk.ResponseType.CLOSE)
            self.set_response_sensitive(Gtk.ResponseType.CLOSE, False)
            self.connect("delete-event", lambda *_: not self._done)
            self.set_default_size(*_screen_fraction_size(parent))

            area = self.get_content_area()
            area.set_border_width(10)
            area.set_spacing(8)

            summary = (f"{verb_ing} {repos[0]['name']}…" if len(repos) == 1
                       else f"{verb_ing} {len(repos)} repo(s), "
                            f"{self._workers} at a time…")
            self._summary = Gtk.Label(label=summary, xalign=0)
            area.pack_start(self._summary, False, False, 0)

            self._bar = Gtk.ProgressBar()
            self._bar.set_show_text(True)
            self._bar.set_text(f"0 / {len(repos)}")
            area.pack_start(self._bar, False, False, 0)

            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            listbox = Gtk.ListBox()
            listbox.set_selection_mode(Gtk.SelectionMode.NONE)
            for r in repos:
                row = Gtk.ListBoxRow()
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                box.set_border_width(4)
                mark = Gtk.Label(label=self.PENDING)
                if self._row_suffix is not None:
                    row_label = f"{r['name']}  ({r['branch']}, {self._row_suffix(r)})"
                else:
                    track = ""
                    if r.get("ahead") or r.get("behind"):
                        track = f" ▲{r['ahead']}▼{r['behind']}"
                    row_label = f"{r['name']}  ({r['branch']}{track}, {r.get('count', '')})"
                name = Gtk.Label(label=row_label, xalign=0)
                box.pack_start(mark, False, False, 0)
                box.pack_start(name, True, True, 0)
                row.add(box)
                listbox.add(row)
                self._marks[r["path"]] = mark
            scroller.add(listbox)
            area.pack_start(scroller, True, True, 0)

            self._log = Gtk.TextView()
            self._log.set_editable(False)
            self._log.set_cursor_visible(False)
            self._log.set_monospace(True)
            self._log.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            log_scroll = Gtk.ScrolledWindow()
            log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC,
                                  Gtk.PolicyType.AUTOMATIC)
            log_scroll.set_min_content_height(150)
            log_scroll.add(self._log)
            expander = Gtk.Expander(label="Details")
            expander.add(log_scroll)
            expander.set_expanded(True)
            area.pack_start(expander, False, False, 0)

            action_area = self.get_action_area()
            self._stop_btn = Gtk.Button.new_with_label("Stop")
            self._stop_btn.connect("clicked", self._on_stop)
            action_area.pack_start(self._stop_btn, False, False, 0)
            action_area.reorder_child(self._stop_btn, 0)

            self.show_all()
            self._start()

        def _on_stop(self, *_):
            dlg = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text="Stop operation?")
            dlg.format_secondary_text(
                "Running repos will be killed. Any partial commits already "
                "landed will remain. This cannot be undone.")
            resp = dlg.run()
            dlg.destroy()
            if resp != Gtk.ResponseType.YES:
                return
            self._cancel.set()
            with self._procs_lock:
                for proc in list(self._procs.values()):
                    self._killpg(proc)
            self._stop_btn.set_sensitive(False)

        @staticmethod
        def _killpg(proc):
            import signal as _sig
            try:
                os.killpg(os.getpgid(proc.pid), _sig.SIGKILL)
            except (ProcessLookupError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass

        def _run_provider(self, r, provider, bin_path, multi):
            """Run one provider attempt for repo *r*. Returns True on success.

            Only called after the previous attempt (if any) for this repo has
            fully exited — attempts are sequential, never concurrent, so a
            fallback can never race the primary attempt it's replacing.
            """
            import time as _time

            model = self._provider_sel["models"].get(provider.id, "")
            effort = self._provider_sel["efforts"].get(provider.id, "")
            argv = provider.build_argv(bin_path, self._task, "stream-json",
                                       self._budget, model, effort)
            GLib.idle_add(self._append_log, f"=== {r['name']} ({provider.label}) ===\n")

            try:
                proc = subprocess.Popen(
                    argv, cwd=r["path"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, start_new_session=True)
            except (OSError, subprocess.SubprocessError) as e:
                GLib.idle_add(self._append_log, f"[{r['name']}] Error: {e}\n")
                return False

            with self._procs_lock:
                self._procs[r["path"]] = proc

            # Kill after timeout regardless of whether we're reading.
            timeout = self._timeout

            def _kill_on_timeout():
                _time.sleep(timeout)
                if proc.poll() is None:
                    self._killpg(proc)
                    GLib.idle_add(
                        self._append_log,
                        f"[{r['name']}] timed out after {timeout}s\n")

            threading.Thread(target=_kill_on_timeout, daemon=True).start()

            try:
                for raw in proc.stdout:
                    if self._cancel.is_set():
                        self._killpg(proc)
                        GLib.idle_add(self._append_log,
                                      f"[{r['name']}] Stopped\n")
                        break
                    text = provider.parse_event(raw)
                    if text:
                        prefix = f"[{r['name']}] " if multi else ""
                        GLib.idle_add(self._append_log, prefix + text)
            except Exception:
                pass
            finally:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._killpg(proc)
                    proc.wait()
                with self._procs_lock:
                    self._procs.pop(r["path"], None)

            return proc.returncode == 0 and not self._cancel.is_set()

        def _start(self):
            from concurrent.futures import ThreadPoolExecutor, as_completed

            total = len(self._repos)
            multi = total > 1  # prefix log lines with repo name when parallel

            def one(r):
                if self._cancel.is_set():
                    return r, False
                GLib.idle_add(self._mark, r["path"], self.RUNNING)

                sel = self._provider_sel
                order = [sel["primary"]]
                if sel["fallback_enabled"] and sel["secondary"]:
                    order.append(sel["secondary"])

                for i, pid in enumerate(order):
                    provider = PROVIDERS.get(pid)
                    if provider is None or not provider.headless:
                        GLib.idle_add(
                            self._append_log,
                            f"[{r['name']}] {pid} is not available for headless runs\n")
                        continue
                    bin_path = resolve_tool_bin(provider.bin_name)
                    if not bin_path:
                        GLib.idle_add(
                            self._append_log,
                            f"[{r['name']}] {provider.bin_name} not found on PATH\n")
                        continue

                    if self._run_provider(r, provider, bin_path, multi):
                        return r, True

                    if self._cancel.is_set() or i == len(order) - 1:
                        return r, False

                    gate = _repo_op_gate(r["path"], self._task)
                    if gate == "ok_in_effect":
                        GLib.idle_add(
                            self._append_log,
                            f"[{r['name']}] {provider.label} exited non-zero but the "
                            "repo already reflects the finished work\n")
                        return r, True
                    if gate == "needs_attention":
                        GLib.idle_add(
                            self._append_log,
                            f"[{r['name']}] interrupted git operation detected after "
                            f"{provider.label} failed — needs manual review\n")
                        return r, False
                    next_provider = PROVIDERS.get(order[i + 1])
                    next_label = next_provider.label if next_provider else order[i + 1]
                    GLib.idle_add(
                        self._append_log,
                        f"[{r['name']}] {provider.label} failed — retrying with "
                        f"{next_label}\n")

                return r, False

            def work():
                ok_count = 0
                done = 0
                try:
                    with ThreadPoolExecutor(max_workers=self._workers) as ex:
                        futures = [ex.submit(one, r) for r in self._repos]
                        for fut in as_completed(futures):
                            try:
                                r, success = fut.result()
                            except Exception:
                                continue
                            ok_count += 1 if success else 0
                            done += 1
                            GLib.idle_add(self._step, done, total, r, success)
                finally:
                    GLib.idle_add(self._finish, ok_count, total)

            threading.Thread(target=work, daemon=True).start()

        def _mark(self, path, glyph):
            self._marks[path].set_text(glyph)
            return False

        def _append_log(self, text):
            buf = self._log.get_buffer()
            buf.insert(buf.get_end_iter(), text)
            end = buf.get_end_iter()
            self._log.scroll_to_iter(end, 0.0, False, 0.0, 1.0)
            return False

        def _step(self, done, total, r, success):
            if not success and self._cancel.is_set():
                self._marks[r["path"]].set_text(self.STOPPED)
            else:
                self._marks[r["path"]].set_text(self.OK if success else self.FAIL)
            self._bar.set_fraction(done / total)
            self._bar.set_text(f"{done} / {total}")
            return False

        def _finish(self, ok, total):
            failed = total - ok
            msg = f"{self._verb_past} {ok}/{total}"
            if self._cancel.is_set():
                msg += " · stopped"
            elif failed:
                msg += f" · {failed} failed"
            msg += " · see menu for final state"
            self._summary.set_text(msg)
            self._bar.set_fraction(1.0)
            self._done = True
            self._stop_btn.set_sensitive(False)
            self.set_response_sensitive(Gtk.ResponseType.CLOSE, True)
            return False

    class ExplainDialog(Gtk.Dialog):
        """Read-only "explain this repo's changes" dialog, single repo.

        Streams a headless AI-provider run (same stream-json + Popen +
        _killpg pattern as CommitAllDialog/PushAllDialog) so it can be
        cancelled and never orphans a budget-spending process. Tool-call
        chatter goes in the collapsed Details expander; the final "result"
        event becomes the prominent explanation text. Response buttons for
        Commit/Push/Commit & Push are built from explain_actions(r) and stay
        disabled until the explain run finishes.

        Explain is read-only, so — unlike commit/push — a failed primary
        attempt falls back to the secondary provider unconditionally, with no
        ``_repo_op_gate`` check (there is nothing it could double-do).
        """

        RESPONSE_COMMIT = 100
        RESPONSE_PUSH = 101
        RESPONSE_COMMIT_PUSH = 102

        def __init__(self, parent, r, budget_usd, provider_sel=None):
            super().__init__(title=f"Explain changes — {r['name']}",
                             transient_for=parent, modal=True)
            self._repo = r
            self._budget = budget_usd
            self._provider_sel = provider_sel or provider_selection(CONFIG_DEFAULTS)
            self._done = False
            self._cancel = threading.Event()
            self._proc = None
            self._proc_lock = threading.Lock()

            self.add_button("Close", Gtk.ResponseType.CLOSE)
            self._action_codes = []
            actions = explain_actions(r)
            if "commit" in actions:
                self.add_button("Commit…", self.RESPONSE_COMMIT)
                self._action_codes.append(self.RESPONSE_COMMIT)
            if "push" in actions:
                self.add_button("Push…", self.RESPONSE_PUSH)
                self._action_codes.append(self.RESPONSE_PUSH)
            if "commit_push" in actions:
                self.add_button("Commit & Push…", self.RESPONSE_COMMIT_PUSH)
                self._action_codes.append(self.RESPONSE_COMMIT_PUSH)
            self.set_response_sensitive(Gtk.ResponseType.CLOSE, False)
            for code in self._action_codes:
                self.set_response_sensitive(code, False)
            self.connect("delete-event", lambda *_: not self._done)
            self.set_default_size(*_screen_fraction_size(parent))

            area = self.get_content_area()
            area.set_border_width(10)
            area.set_spacing(8)

            bits = []
            if r.get("count"):
                bits.append(f"{r['count']} uncommitted")
            if r.get("has_remote") and r.get("unpushed", 0) > 0:
                bits.append(f"{r['unpushed']} unpushed")
            subtitle = ", ".join(bits) or "no changes"
            header = Gtk.Label(
                label=f"{r['name']}  ({r.get('branch', '')}) — {subtitle}",
                xalign=0)
            area.pack_start(header, False, False, 0)

            self._main_view = Gtk.TextView()
            self._main_view.set_editable(False)
            self._main_view.set_cursor_visible(False)
            self._main_view.set_wrap_mode(Gtk.WrapMode.WORD)
            self._main_view.set_left_margin(4)
            self._main_view.set_right_margin(4)
            primary_label = PROVIDERS[self._provider_sel["primary"]].label
            self._main_view.get_buffer().set_text(
                f"Asking {primary_label} to explain changes…")
            main_scroll = Gtk.ScrolledWindow()
            main_scroll.set_policy(Gtk.PolicyType.AUTOMATIC,
                                   Gtk.PolicyType.AUTOMATIC)
            main_scroll.add(self._main_view)
            area.pack_start(main_scroll, True, True, 0)

            self._log = Gtk.TextView()
            self._log.set_editable(False)
            self._log.set_cursor_visible(False)
            self._log.set_monospace(True)
            self._log.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            log_scroll = Gtk.ScrolledWindow()
            log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC,
                                  Gtk.PolicyType.AUTOMATIC)
            log_scroll.set_min_content_height(150)
            log_scroll.add(self._log)
            expander = Gtk.Expander(label="Details")
            expander.add(log_scroll)
            expander.set_expanded(False)
            area.pack_start(expander, False, False, 0)

            action_area = self.get_action_area()
            self._stop_btn = Gtk.Button.new_with_label("Stop")
            self._stop_btn.connect("clicked", self._on_stop)
            action_area.pack_start(self._stop_btn, False, False, 0)
            action_area.reorder_child(self._stop_btn, 0)

            self.show_all()
            self._start()

        def _on_stop(self, *_):
            dlg = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text="Stop explaining?")
            dlg.format_secondary_text(
                "The running AI process will be killed.")
            resp = dlg.run()
            dlg.destroy()
            if resp != Gtk.ResponseType.YES:
                return
            self._cancel.set()
            with self._proc_lock:
                if self._proc is not None:
                    self._killpg(self._proc)
            self._stop_btn.set_sensitive(False)

        @staticmethod
        def _killpg(proc):
            import signal as _sig
            try:
                os.killpg(os.getpgid(proc.pid), _sig.SIGKILL)
            except (ProcessLookupError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass

        def _append_log(self, text):
            buf = self._log.get_buffer()
            buf.insert(buf.get_end_iter(), text)
            end = buf.get_end_iter()
            self._log.scroll_to_iter(end, 0.0, False, 0.0, 1.0)
            return False

        def _set_result(self, text):
            self._main_view.get_buffer().set_text(text or "(no explanation returned)")
            return False

        def _finish(self):
            self._done = True
            self._stop_btn.set_sensitive(False)
            self.set_response_sensitive(Gtk.ResponseType.CLOSE, True)
            for code in self._action_codes:
                self.set_response_sensitive(code, True)
            return False

        def _run_explain_provider(self, provider, bin_path):
            """Run one explain attempt. Returns (ok, result_text)."""
            import time as _time

            model = self._provider_sel["models"].get(provider.id, "")
            effort = self._provider_sel["efforts"].get(provider.id, "")
            argv = provider.build_argv(bin_path, "explain", "stream-json",
                                       self._budget, model, effort)
            GLib.idle_add(self._append_log, f"=== {provider.label} ===\n")
            try:
                proc = subprocess.Popen(
                    argv, cwd=self._repo["path"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, start_new_session=True)
            except (OSError, subprocess.SubprocessError) as e:
                GLib.idle_add(self._append_log, f"Error: {e}\n")
                return False, ""

            with self._proc_lock:
                self._proc = proc

            def _kill_on_timeout():
                _time.sleep(EXPLAIN_TIMEOUT)
                if proc.poll() is None:
                    self._killpg(proc)
                    GLib.idle_add(self._append_log,
                                  f"timed out after {EXPLAIN_TIMEOUT}s\n")

            threading.Thread(target=_kill_on_timeout, daemon=True).start()

            result_text = ""
            try:
                for raw in proc.stdout:
                    if self._cancel.is_set():
                        self._killpg(proc)
                        GLib.idle_add(self._append_log, "Stopped\n")
                        break
                    res = provider.extract_result(raw)
                    if res is not None:
                        result_text = res
                    text = provider.parse_event(raw)
                    if text:
                        GLib.idle_add(self._append_log, text)
            except Exception:
                pass
            finally:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._killpg(proc)
                    proc.wait()
                with self._proc_lock:
                    self._proc = None

            return proc.returncode == 0 and not self._cancel.is_set(), result_text

        def _start(self):
            def work():
                sel = self._provider_sel
                order = [sel["primary"]]
                if sel["fallback_enabled"] and sel["secondary"]:
                    order.append(sel["secondary"])

                result_text = ""
                ok = False
                for pid in order:
                    if self._cancel.is_set():
                        break
                    provider = PROVIDERS.get(pid)
                    if provider is None or not provider.headless:
                        GLib.idle_add(self._append_log,
                                      f"{pid} is not available for headless runs\n")
                        continue
                    bin_path = resolve_tool_bin(provider.bin_name)
                    if not bin_path:
                        GLib.idle_add(self._append_log,
                                      f"{provider.bin_name} not found on PATH\n")
                        continue
                    ok, text = self._run_explain_provider(provider, bin_path)
                    if text:
                        result_text = text
                    # Read-only, so — unlike commit/push — fall back on any
                    # failure with no _repo_op_gate check: there is nothing a
                    # second attempt could double-do. Also fall back when the
                    # provider ran successfully but produced no extractable
                    # explanation (only stop when we have the answer).
                    if (ok and result_text) or self._cancel.is_set():
                        break

                if self._cancel.is_set() and not result_text:
                    result_text = "Stopped before finishing."
                elif not result_text and not ok:
                    result_text = "(no explanation returned — is an AI CLI installed?)"
                GLib.idle_add(self._set_result, result_text)
                GLib.idle_add(self._finish)

            threading.Thread(target=work, daemon=True).start()

    class ConfigDialog(Gtk.Dialog):
        def __init__(self, parent, config):
            super().__init__(title="Settings", transient_for=parent, modal=True)
            self.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                             "Save", Gtk.ResponseType.OK)
            self._config = dict(config)
            self._config["excluded_repos"] = list(
                config.get("excluded_repos", []))
            # Deep-copy the per-provider sub-dicts: dict(config) above only
            # shallow-copies, so without this, editing a provider's model in
            # the dialog would mutate the caller's live config in place even
            # if the user hits Cancel.
            self._config["ai_providers"] = {
                pid: dict(vals) for pid, vals
                in config.get("ai_providers", CONFIG_DEFAULTS["ai_providers"]).items()
            }
            self._repo_checks = {}    # path -> Gtk.CheckButton
            self._provider_widgets = {}  # provider id -> {"model": combo, "effort": combo|None}

            notebook = Gtk.Notebook()
            notebook.append_page(self._build_general_tab(),
                                 Gtk.Label(label="General"))
            notebook.append_page(self._build_git_tab(),
                                 Gtk.Label(label="Git"))
            notebook.append_page(self._build_repos_tab(),
                                 Gtk.Label(label="Repositories"))
            notebook.append_page(self._build_ai_tab(),
                                 Gtk.Label(label="AI"))
            for pid in ("claude", "opencode", "codex", "gemini"):
                notebook.append_page(self._build_ai_provider_tab(pid),
                                     Gtk.Label(label=PROVIDERS[pid].label))
            self.get_content_area().pack_start(notebook, True, True, 0)
            self.set_default_size(560, 560)
            self.show_all()

        def _build_general_tab(self):
            grid = Gtk.Grid()
            grid.set_row_spacing(8)
            grid.set_column_spacing(12)
            grid.set_border_width(12)

            def row(r, label_text, widget, hint=None):
                lbl = Gtk.Label(label=label_text, xalign=1.0)
                grid.attach(lbl, 0, r, 1, 1)
                grid.attach(widget, 1, r, 1, 1)
                widget.set_hexpand(True)
                if hint:
                    sub = Gtk.Label(label=hint, xalign=0.0)
                    sub.get_style_context().add_class("dim-label")
                    grid.attach(sub, 1, r + 1, 1, 1)

            self._entry_base = Gtk.Entry()
            self._entry_base.set_placeholder_text(
                f"default: {base_dir()}")
            self._entry_base.set_text(self._config.get("base_dir", ""))
            row(0, "Base directory", self._entry_base)

            adj_depth = Gtk.Adjustment(value=self._config.get("depth", 0),
                                       lower=0, upper=10, step_increment=1)
            self._spin_depth = Gtk.SpinButton(adjustment=adj_depth, digits=0)
            row(2, "Scan depth", self._spin_depth,
                "0 = use REPODASH_DEPTH env var or default (3)")

            adj_iv = Gtk.Adjustment(
                value=self._config.get("refresh_interval", 0),
                lower=0, upper=3600, step_increment=5)
            self._spin_interval = Gtk.SpinButton(adjustment=adj_iv, digits=0)
            row(4, "Refresh interval (s)", self._spin_interval,
                "0 = use REPODASH_TRAY_INTERVAL env var or default (90)")

            self._entry_terminal = Gtk.Entry()
            self._entry_terminal.set_placeholder_text(
                "default: auto-detect (ptyxis, gnome-terminal, …)")
            self._entry_terminal.set_text(self._config.get("terminal", ""))
            row(6, "Terminal", self._entry_terminal)

            return grid

        def _build_repos_tab(self):
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            vbox.set_border_width(8)

            hint = Gtk.Label(
                label="Uncheck repos to exclude them from the dashboard and tray menu.",
                xalign=0.0, wrap=True)
            vbox.pack_start(hint, False, False, 0)

            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self._repo_listbox = Gtk.ListBox()
            self._repo_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
            scroller.add(self._repo_listbox)
            vbox.pack_start(scroller, True, True, 0)

            rescan_btn = Gtk.Button(label="Rescan")
            rescan_btn.connect("clicked", self._on_rescan)
            vbox.pack_start(rescan_btn, False, False, 0)

            self._populate_repo_list()
            return vbox

        def _populate_repo_list(self):
            for child in self._repo_listbox.get_children():
                self._repo_listbox.remove(child)
            self._repo_checks.clear()

            base = self._entry_base.get_text().strip() or base_dir()
            depth_val = int(self._spin_depth.get_value())
            depth = depth_val if depth_val > 0 else scan_depth()
            excluded = set(self._config.get("excluded_repos", []))

            def work():
                repos = find_repos(base, depth)
                GLib.idle_add(self._apply_repo_list, repos, excluded)

            threading.Thread(target=work, daemon=True).start()

        def _apply_repo_list(self, repos, excluded):
            for child in self._repo_listbox.get_children():
                self._repo_listbox.remove(child)
            self._repo_checks.clear()
            for path in repos:
                row = Gtk.ListBoxRow()
                chk = Gtk.CheckButton(label=path)
                chk.set_active(path not in excluded)
                row.add(chk)
                self._repo_listbox.add(row)
                self._repo_checks[path] = chk
            self._repo_listbox.show_all()
            return False

        def _on_rescan(self, *_):
            self._populate_repo_list()

        def _build_git_tab(self):
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            vbox.set_border_width(12)

            def section(title):
                lbl = Gtk.Label(xalign=0.0)
                # markup_escape_text is mandatory: set_markup silently renders an
                # empty label on invalid Pango XML (e.g. a bare & in a title string).
                lbl.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
                vbox.pack_start(lbl, False, False, 0)

            def spin_row(label_text, key, default, lo, hi, step, hint=None):
                hbox = Gtk.Box(spacing=8)
                lbl = Gtk.Label(label=label_text, xalign=1.0, width_chars=22)
                hbox.pack_start(lbl, False, False, 0)
                adj = Gtk.Adjustment(value=self._config.get(key, default),
                                     lower=lo, upper=hi, step_increment=step)
                spin = Gtk.SpinButton(adjustment=adj, digits=0)
                hbox.pack_start(spin, False, False, 0)
                if hint:
                    hl = Gtk.Label(label=hint, xalign=0.0)
                    hl.get_style_context().add_class("dim-label")
                    hbox.pack_start(hl, False, False, 0)
                vbox.pack_start(hbox, False, False, 0)
                return spin

            section("Repositories")
            self._chk_remoteless = Gtk.CheckButton(
                label="Show repos without a remote")
            self._chk_remoteless.set_active(
                self._config.get("show_remoteless", True))
            vbox.pack_start(self._chk_remoteless, False, False, 0)

            section("Stale worktree detection")
            self._chk_show_stale = Gtk.CheckButton(
                label="Enable stale worktree detection")
            self._chk_show_stale.set_active(
                self._config.get("show_stale_worktrees", True))
            vbox.pack_start(self._chk_show_stale, False, False, 0)

            section("⏸  Idle worktree")
            self._spin_idle_hours = spin_row(
                "Idle threshold (h):", "stale_worktree_idle_hours", 24, 1, 8760, 1,
                "clean+no-ahead worktrees older than this are idle")

            section("⚠  Stuck worktree")
            self._spin_stuck_hours = spin_row(
                "Stuck threshold (h):", "stale_worktree_stuck_hours", 12, 1, 8760, 1,
                "dirty worktrees older than this are stuck")

            outer = Gtk.ScrolledWindow()
            outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            outer.add(vbox)
            return outer

        def _build_ai_tab(self):
            """Generic AI settings: primary/secondary provider, fallback, the
            provider-agnostic run limits, and the worktree prompts (also
            provider-agnostic — whichever provider runs them gets the same
            English instructions)."""
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            vbox.set_border_width(12)

            def section(title):
                lbl = Gtk.Label(xalign=0.0)
                lbl.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
                vbox.pack_start(lbl, False, False, 0)

            def spin_row(label_text, key, default, lo, hi, step,
                        digits=0, hint=None):
                hbox = Gtk.Box(spacing=8)
                lbl = Gtk.Label(label=label_text, xalign=1.0, width_chars=22)
                hbox.pack_start(lbl, False, False, 0)
                adj = Gtk.Adjustment(value=self._config.get(key, default),
                                     lower=lo, upper=hi, step_increment=step)
                spin = Gtk.SpinButton(adjustment=adj, digits=digits)
                hbox.pack_start(spin, False, False, 0)
                if hint:
                    hl = Gtk.Label(label=hint, xalign=0.0)
                    hl.get_style_context().add_class("dim-label")
                    hbox.pack_start(hl, False, False, 0)
                vbox.pack_start(hbox, False, False, 0)
                return spin

            def prompt_row(label_text, key, default_text):
                lbl = Gtk.Label(label=label_text, xalign=0.0)
                vbox.pack_start(lbl, False, False, 0)
                sw = Gtk.ScrolledWindow()
                sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
                sw.set_size_request(-1, 100)
                tv = Gtk.TextView()
                tv.set_wrap_mode(Gtk.WrapMode.WORD)
                buf = tv.get_buffer()
                saved = self._config.get(key, "")
                buf.set_text(saved if saved else default_text)
                sw.add(tv)
                vbox.pack_start(sw, True, True, 0)
                return buf

            # ── Provider selection ────────────────────────────────────────
            section("AI provider")

            def provider_combo_row(label_text, key, allow_none, hint=None):
                hbox = Gtk.Box(spacing=8)
                lbl = Gtk.Label(label=label_text, xalign=1.0, width_chars=22)
                hbox.pack_start(lbl, False, False, 0)
                combo = Gtk.ComboBoxText()
                if allow_none:
                    combo.append("", "(none)")
                for pid in HEADLESS_PROVIDER_IDS:
                    provider = PROVIDERS[pid]
                    status = "✓ installed" if resolve_tool_bin(provider.bin_name) \
                        else "not found"
                    combo.append(pid, f"{provider.label} ({status})")
                current = self._config.get(key, "")
                if combo.set_active_id(current) is False:
                    combo.set_active_id("" if allow_none else "claude")
                hbox.pack_start(combo, False, False, 0)
                if hint:
                    hl = Gtk.Label(label=hint, xalign=0.0)
                    hl.get_style_context().add_class("dim-label")
                    hbox.pack_start(hl, False, False, 0)
                vbox.pack_start(hbox, False, False, 0)
                return combo

            self._combo_ai_primary = provider_combo_row(
                "Primary:", "ai_primary_provider", allow_none=False,
                hint="does the work for Commit/Push/Explain actions")
            self._combo_ai_secondary = provider_combo_row(
                "Secondary (fallback):", "ai_secondary_provider", allow_none=True,
                hint="tried once if the primary is missing or a run fails")
            self._chk_ai_fallback = Gtk.CheckButton(
                label="Fall back to the secondary provider on failure")
            self._chk_ai_fallback.set_active(
                self._config.get("ai_fallback_enabled", True))
            vbox.pack_start(self._chk_ai_fallback, False, False, 0)

            # ── Run limits (provider-agnostic) ────────────────────────────
            section("Run limits")
            self._spin_commit_ram = spin_row(
                "RAM/proc (MB):", "commit_ram_mb", 2048, 256, 65536, 256,
                hint="RAM budgeted per AI-provider process; workers = MemAvailable ÷ this")
            self._spin_commit_workers = spin_row(
                "Max workers:", "commit_max_workers", 0, 0, 64, 1,
                hint="0 = auto (RAM- and CPU-derived); >0 caps concurrency")
            self._spin_commit_timeout = spin_row(
                "Timeout (s):", "commit_timeout", 3600, 30, 7200, 30,
                hint="per-repo cap before a run is killed")
            self._spin_commit_budget = spin_row(
                "Budget ($/repo):", "commit_budget_usd", 10.0, 0, 1000, 1,
                digits=2, hint="max spend per repo — only Claude Code supports this")

            # ── Prompts ────────────────────────────────────────────────────
            section("⏸  Idle worktree — close prompt")
            hint_idle = Gtk.Label(
                label="Placeholders: {path}  {branch}  {repo_path}",
                xalign=0.0)
            hint_idle.get_style_context().add_class("dim-label")
            vbox.pack_start(hint_idle, False, False, 0)
            self._buf_idle_prompt = prompt_row(
                "", "worktree_idle_close_prompt", IDLE_CLOSE_PROMPT)

            section("⚠  Stuck worktree — finish & merge prompt")
            hint_stuck = Gtk.Label(
                label="Placeholders: {path}  {branch}  {repo_path}",
                xalign=0.0)
            hint_stuck.get_style_context().add_class("dim-label")
            vbox.pack_start(hint_stuck, False, False, 0)
            self._buf_stuck_prompt = prompt_row(
                "", "worktree_stuck_finish_prompt", STUCK_FINISH_PROMPT)

            outer = Gtk.ScrolledWindow()
            outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            outer.add(vbox)
            return outer

        def _build_ai_provider_tab(self, pid):
            """Model (and, if supported, effort) for one provider. Model is a
            freeform-editable combo: the seed list is suggestions, not a
            closed set (e.g. Claude Code can point at a DeepSeek/GLM endpoint
            via ANTHROPIC_BASE_URL — any model string the provider accepts
            is valid here)."""
            print("[repodash] _build_ai_provider_tab pid=%s _FETCHED=%r" %
                  (pid, _FETCHED_OPENGODE_GO_MODELS), file=sys.stderr)
            provider = PROVIDERS[pid]
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            vbox.set_border_width(12)
            widgets = {"model": None, "effort": None}

            def combo_entry_row(label_text, key, options, hint=None):
                hbox = Gtk.Box(spacing=8)
                lbl = Gtk.Label(label=label_text, xalign=1.0, width_chars=22)
                hbox.pack_start(lbl, False, False, 0)
                combo = Gtk.ComboBoxText.new_with_entry()
                for opt_id, _opt_label in options:
                    combo.append_text(opt_id)
                combo.set_row_separator_func(
                    lambda model, it: model[it][0] == _OPENGODE_GO_HEADER)
                current = self._config["ai_providers"].get(pid, {}).get(key, "")
                combo.get_child().set_text(current)

                store = combo.get_model()
                entry = combo.get_child()
                completion_store = Gtk.ListStore(str)
                for opt_id, _opt_label in options:
                    if opt_id != _OPENGODE_GO_HEADER:
                        completion_store.append([opt_id])
                completion = Gtk.EntryCompletion()
                completion.set_model(completion_store)
                completion.set_text_column(0)
                completion.set_minimum_key_length(1)
                completion.set_match_func(
                    lambda _c, key, it: key.lower() in completion_store[it][0].lower())
                entry.set_completion(completion)

                hbox.pack_start(combo, False, False, 0)
                if hint:
                    hl = Gtk.Label(label=hint, xalign=0.0)
                    hl.get_style_context().add_class("dim-label")
                    hbox.pack_start(hl, False, False, 0)
                vbox.pack_start(hbox, False, False, 0)
                return combo

            if not provider.headless:
                note = Gtk.Label(
                    xalign=0.0, wrap=True,
                    label=f"{provider.label} isn't wired for headless "
                          "Commit/Push/Explain yet — its JSON output format "
                          "isn't confirmed stable. You can still use "
                          f"“Open {provider.label}” for an "
                          "interactive session.")
                note.get_style_context().add_class("dim-label")
                vbox.pack_start(note, False, False, 0)

            opts = list(provider.model_options)
            if pid == "opencode":
                if not _FETCHED_OPENGODE_GO_MODELS:
                    print("[repodash]   → calling _fetch_opencode_go_models()",
                          file=sys.stderr)
                    _fetch_opencode_go_models()
                if _FETCHED_OPENGODE_GO_MODELS:
                    opts.append((_OPENGODE_GO_HEADER, ""))
                    opts.extend(_FETCHED_OPENGODE_GO_MODELS)
                    print("[repodash]   → appended header + %d go models" %
                          len(_FETCHED_OPENGODE_GO_MODELS), file=sys.stderr)
                else:
                    print("[repodash]   → no models to append", file=sys.stderr)
            widgets["model"] = combo_entry_row(
                "Model:", "model", opts,
                hint="freeform — pick a suggestion or type any model name/id")
            if provider.effort_options:
                widgets["effort"] = combo_entry_row(
                    "Effort:", "effort", provider.effort_options,
                    hint="reasoning effort for headless runs")
            if not provider.supports_budget:
                hint = Gtk.Label(
                    xalign=0.0,
                    label="No cost-budget flag for this provider — only the "
                          "AI tab's timeout applies.")
                hint.get_style_context().add_class("dim-label")
                vbox.pack_start(hint, False, False, 0)

            self._provider_widgets[pid] = widgets
            outer = Gtk.ScrolledWindow()
            outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            outer.add(vbox)
            return outer

        def get_config(self) -> dict:
            cfg = dict(self._config)
            cfg["base_dir"] = self._entry_base.get_text().strip()
            cfg["depth"] = int(self._spin_depth.get_value())
            cfg["refresh_interval"] = int(self._spin_interval.get_value())
            cfg["terminal"] = self._entry_terminal.get_text().strip()
            cfg["show_remoteless"] = self._chk_remoteless.get_active()
            cfg["commit_ram_mb"] = int(self._spin_commit_ram.get_value())
            cfg["commit_max_workers"] = int(self._spin_commit_workers.get_value())
            cfg["commit_timeout"] = int(self._spin_commit_timeout.get_value())
            cfg["commit_budget_usd"] = round(
                self._spin_commit_budget.get_value(), 2)
            cfg["ai_primary_provider"] = (
                self._combo_ai_primary.get_active_id() or "claude")
            cfg["ai_secondary_provider"] = self._combo_ai_secondary.get_active_id() or ""
            cfg["ai_fallback_enabled"] = self._chk_ai_fallback.get_active()
            ai_providers = {pid: dict(vals) for pid, vals
                           in self._config.get("ai_providers", {}).items()}
            for pid, widgets in self._provider_widgets.items():
                entry = dict(ai_providers.get(pid, {}))
                if widgets.get("model") is not None:
                    entry["model"] = widgets["model"].get_child().get_text().strip()
                if widgets.get("effort") is not None:
                    entry["effort"] = widgets["effort"].get_child().get_text().strip()
                ai_providers[pid] = entry
            cfg["ai_providers"] = ai_providers
            cfg["show_stale_worktrees"] = self._chk_show_stale.get_active()
            cfg["stale_worktree_idle_hours"] = int(self._spin_idle_hours.get_value())
            cfg["stale_worktree_stuck_hours"] = int(self._spin_stuck_hours.get_value())
            start, end = self._buf_idle_prompt.get_bounds()
            idle_text = self._buf_idle_prompt.get_text(start, end, False).strip()
            cfg["worktree_idle_close_prompt"] = (
                "" if idle_text == IDLE_CLOSE_PROMPT.strip() else idle_text)
            start, end = self._buf_stuck_prompt.get_bounds()
            stuck_text = self._buf_stuck_prompt.get_text(start, end, False).strip()
            cfg["worktree_stuck_finish_prompt"] = (
                "" if stuck_text == STUCK_FINISH_PROMPT.strip() else stuck_text)
            # Repos unchecked in the current scan list.
            shown_excluded = {
                path for path, chk in self._repo_checks.items()
                if not chk.get_active()
            }
            # Preserve exclusions for repos not shown in the current scan
            # (e.g. after a base_dir change the old paths aren't visible).
            old_excluded = set(self._config.get("excluded_repos", []))
            not_shown = old_excluded - set(self._repo_checks.keys())
            cfg["excluded_repos"] = sorted(shown_excluded | not_shown)
            return cfg

    class HelpDialog(Gtk.Dialog):
        _CONTENT = [
            ("h", "About repodash"),
            ("p", "version " + VERSION),
            ("p", "A tray companion for your git repositories. "
                  "Monitors dirty repos, unpushed commits, and stale "
                  "worktrees — and launches AI CLI actions (Claude Code, "
                  "OpenCode, Codex) from the menu."),
            ("p", "© 2026 repodash contributors"),
            ("p", "Licensed under GPL 3.0"),
            ("h2", "Workflow Guide"),
            ("p", "repodash watches your git repos and surfaces work that needs "
                  "attention. The tray icon shows a count of dirty repos; click "
                  "an entry to act on it."),
            ("h2", "Dirty repos (changed files)"),
            ("p", "Repos with uncommitted changes appear first, marked with ●."),
            ("item", "Open terminal",
             "Opens a terminal in the repo directory."),
            ("item", "Open <AI provider>",
             "Opens your configured primary AI CLI (Claude Code, OpenCode, "
             "Codex, or Gemini CLI) interactively in a terminal."),
            ("item", "git commit (N)",
             "Opens a terminal with your editor so you can write the commit "
             "message yourself. Good for quick, focused commits."),
            ("item", "Commit via <AI provider>…",
             "Your primary AI provider inspects the diff, groups changes into "
             "logical commits with appropriate messages, fixes any pre-commit "
             "hook failures, then optionally merges the branch into main. If "
             "it's missing or the run fails, a configured secondary provider "
             "is tried once — only after re-checking the repo shows work "
             "genuinely remains, so a fallback never double-commits. A "
             "progress dialog shows per-repo status."),
            ("h2", "Unpushed repos"),
            ("p", "Repos with local commits not yet on a remote appear in the "
                  "Unpushed section."),
            ("item", "git push",
             "Opens a terminal and runs git push. Use this when you need to "
             "enter a passphrase or watch the output interactively."),
            ("item", "Push via <AI provider>…",
             "Your primary AI provider runs git push, handles non-fast-forward "
             "divergence (pull --rebase + retry), and fixes pre-push hook "
             "failures — with the same safe fallback-to-secondary behavior as "
             "Commit. Use when a plain push fails and you want errors fixed "
             "automatically."),
            ("h2", "Stale worktrees"),
            ("p", "Extra git worktrees (from git worktree add) that have gone "
                  "quiet appear as ⚠ Stuck or ⏸ Idle sections."),
            ("item", "⚠ Stuck",
             "A worktree with uncommitted changes sitting idle longer than the "
             "configured threshold. Use “Finish & merge via <AI provider>” "
             "to commit, merge into main, and remove the worktree automatically."),
            ("item", "⏸ Idle",
             "A clean worktree with no ahead commits sitting idle. Use "
             "“Close via <AI provider>” to review and remove it, or "
             "“Remove worktree” for an immediate direct delete."),
            ("h2", "Dashboard"),
            ("p", "Lists every repo with full status. Open with "
                  "“Show dashboard…” or by re-launching the tray. "
                  "Each row has buttons for Terminal, your AI provider, Push, "
                  "GitHub, and Open folder."),
            ("h2", "Settings"),
            ("item", "General",
             "Scan root directory, depth, refresh interval, terminal."),
            ("item", "Git",
             "Show/hide remoteless repos; stale-worktree thresholds."),
            ("item", "Repositories",
             "Per-repo include/exclude list. Rescan after changing the root."),
            ("item", "AI",
             "Pick a primary AI CLI provider (Claude Code, OpenCode, Codex — "
             "each shown with its install status) and an optional secondary "
             "tried once as a fallback if the primary is missing or a run "
             "fails/times out. Also holds the provider-agnostic run limits "
             "(RAM/worker/timeout/budget — budget only applies to Claude "
             "Code) and the customisable worktree close/finish prompts. "
             "Placeholders {path}, {branch}, {repo_path} are substituted at "
             "runtime."),
            ("item", "Claude Code / OpenCode / Codex / Gemini",
             "Model and (where the provider supports it) reasoning-effort "
             "level for that provider's headless runs. Model is freeform — "
             "type any model name/id the provider accepts, e.g. a DeepSeek or "
             "GLM endpoint via Claude Code's ANTHROPIC_BASE_URL. Gemini CLI "
             "is detected and launchable interactively but not yet wired for "
             "headless Commit/Push/Explain."),
        ]

        def __init__(self, parent):
            super().__init__(title="repodash — Help",
                             transient_for=parent, modal=True)
            self.add_button("Close", Gtk.ResponseType.CLOSE)
            self.set_default_size(580, 500)

            area = self.get_content_area()
            area.set_border_width(0)

            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

            tv = Gtk.TextView()
            tv.set_editable(False)
            tv.set_cursor_visible(False)
            tv.set_wrap_mode(Gtk.WrapMode.WORD)
            tv.set_left_margin(16)
            tv.set_right_margin(16)
            tv.set_top_margin(12)
            tv.set_bottom_margin(12)

            buf = tv.get_buffer()
            t_h = buf.create_tag("h", weight=700, scale=1.2,
                                 pixels_above_lines=4, pixels_below_lines=4)
            t_h2 = buf.create_tag("h2", weight=700, pixels_above_lines=10)
            t_bold = buf.create_tag("bold", weight=700)
            t_dim = buf.create_tag("dim", foreground="#888888")

            def ins(text, *tags):
                end = buf.get_end_iter()
                active = [t for t in tags if t is not None]
                if active:
                    buf.insert_with_tags(end, text, *active)
                else:
                    buf.insert(end, text)

            for entry in self._CONTENT:
                kind = entry[0]
                if kind == "h":
                    ins(entry[1] + "\n", t_h)
                elif kind == "h2":
                    ins("\n" + entry[1] + "\n", t_h2)
                elif kind == "p":
                    ins(entry[1] + "\n")
                elif kind == "item":
                    ins("  " + entry[1], t_bold)
                    ins("\n    " + entry[2] + "\n", t_dim)

            scroller.add(tv)
            area.pack_start(scroller, True, True, 0)

            link = Gtk.LinkButton.new_with_label(
                "https://github.com/sicambria/repodash",
                "repodash on GitHub")
            link.set_halign(Gtk.Align.START)
            link.set_margin_start(16)
            link.set_margin_bottom(8)
            area.pack_start(link, False, False, 0)

            self.show_all()

    app = TrayApp()
    return app.run([sys.argv[0]])


# ── entry point ──────────────────────────────────────────────────────────────
def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    if "--check" in argv:
        return run_check()
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
