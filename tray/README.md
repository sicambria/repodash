# repodash tray (Linux / GNOME)

A system-tray companion for [repodash](../README.md), for **Ubuntu 26.04 / GNOME**
(also works on any GNOME with the AppIndicator extension). It puts a tray icon in
your top bar whose menu lists only repos with a **dirty** working tree, each with
one-click actions, plus a larger dashboard window showing every repo's status.

This is a **consumer** of the core: it never modifies `repodash.py` / `repodash`,
and does not affect their cross-platform, dependency-free guarantees. The tray
itself is Linux-only and needs GTK3 + PyGObject (because AyatanaAppIndicator3 has
no GTK4 binding).

## What it does

**Tray menu** (refreshes every ~90s, cheaply — just `git status` per repo):
- header line: `N dirty · M repos` (+ a count badge on the icon);
- one submenu per dirty repo → **Open terminal**, **Open Claude Code**
  (`claude --dangerously-skip-permissions`), **Open GitHub** (if an `origin`
  GitHub remote exists), **Open folder**, **Copy path**;
- **Show dashboard…**, **Refresh now**, **Quit**.

**Dashboard window** (full scan on demand — runs `repodash.py --json`):
- every repo with branch / ahead-behind / changed-file / TODO / audit / roadmap /
  sonar summary;
- a search box and **Dirty only** / **Has TODOs** filters;
- the same per-repo action buttons.

## Install

Quickest path — the setup script installs the dependencies and (optionally)
registers a start-menu launcher or login autostart with the correct absolute path:

```bash
bash tray/setup.sh                     # install dependencies only
bash tray/setup.sh --menu              # deps + start-menu icon (GNOME Activities)
bash tray/setup.sh --autostart         # deps + start on login
bash tray/setup.sh --autostart --menu  # all three
```

Or install the packages manually:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 \
    gir1.2-ayatanaappindicator3-0.1 libayatana-appindicator3-1 \
    gnome-shell-extension-appindicator
```

The **Ubuntu AppIndicators** extension is preinstalled on Ubuntu desktop and is
what makes the icon appear under GNOME (GNOME has no native tray). On a fresh
install you may need to **log out and back in** once for it to take effect.
Verify it is enabled:

```bash
gnome-extensions list --enabled | grep -i appindicator
```

Run it:

```bash
python3 tray/repodash_tray.py
```

## Start-menu icon (GNOME Activities)

```bash
bash tray/setup.sh --menu
```

This copies `repodash-app.svg` to `~/.local/share/icons/hicolor/scalable/apps/repodash.svg`
and installs a launcher entry to `~/.local/share/applications/repodash.desktop` so the app
appears when searching in GNOME Activities. The icon uses explicit colors and a dark
background so it renders correctly at app-grid sizes (unlike the symbolic tray glyph).

## Autostart on login

```bash
bash tray/setup.sh --autostart
# or manually:
cp tray/repodash-tray.desktop ~/.config/autostart/
# then edit the Exec= line to the absolute path of your checkout, e.g.
#   Exec=/usr/bin/python3 /home/you/git/repodash/tray/repodash_tray.py
```

The `X-GNOME-Autostart-Delay=3` gives the AppIndicator extension time to load
before the icon registers (without it the icon sometimes won't appear on login).

## Configuration (environment)

| Variable | Purpose |
|---|---|
| `REPODASH_DIR` | scan root (default `~/git`) — same as the core |
| `REPODASH_DEPTH` | repo-discovery depth (default 3) — same as the core |
| `REPODASH_TERMINAL` | force a terminal instead of auto-detect |
| `REPODASH_TRAY_INTERVAL` | seconds between menu refreshes (default 90, min 5) |
| `SONAR_URL` / `SONAR_TOKEN` | passed through to the core for the dashboard's sonar column |

The terminal is auto-detected in this order: **ptyxis → gnome-terminal → kgx
(GNOME Console) → ghostty → xterm**. Ubuntu 26.04 ships Ptyxis by default.

## Headless check

To see what the tray would show without launching any GUI (handy over SSH):

```bash
python3 tray/repodash_tray.py --check
```

It prints the scan root, the resolved terminal command (and the exact argv it
would run for a terminal and for Claude Code), and every dirty repo with its
branch, change count, and GitHub URL.

## Troubleshooting

- **No icon appears.** Confirm the AppIndicator extension is enabled (see above)
  and log out/in once on a fresh install. On Wayland the legacy `Gtk.StatusIcon`
  tray does not work — this app uses StatusNotifierItem via AppIndicator, which is
  the supported path.
- **"no AppIndicator typelib found".** Install `gir1.2-ayatanaappindicator3-0.1`.
- **A terminal action does nothing.** Set `REPODASH_TERMINAL` to a terminal you
  have installed, or install one of the auto-detected ones.

## License

GPL-3.0-or-later, same as the rest of repodash.
