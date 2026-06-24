#!/usr/bin/env bash
# Set up the repodash GNOME tray companion on Ubuntu / Debian-based GNOME.
#
# Installs the GTK3 + AppIndicator dependencies and (optionally) registers the
# tray to start automatically on login or adds a start-menu launcher entry.
# Safe to re-run.
#
#   bash tray/setup.sh                        # install dependencies
#   bash tray/setup.sh --autostart            # install deps + enable login autostart
#   bash tray/setup.sh --menu                 # install deps + start-menu icon
#   bash tray/setup.sh --autostart --menu     # all three
#
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
TRAY_PY="$HERE/repodash_tray.py"

# Parse flags (order-independent).
DO_AUTOSTART=false
DO_MENU=false
for arg in "$@"; do
  case "$arg" in
    --autostart) DO_AUTOSTART=true ;;
    --menu)      DO_MENU=true ;;
  esac
done

PACKAGES=(
  python3-gi
  gir1.2-gtk-3.0
  gir1.2-ayatanaappindicator3-0.1
  libayatana-appindicator3-1
  gnome-shell-extension-appindicator
)

echo "==> Installing dependencies: ${PACKAGES[*]}"
if command -v apt >/dev/null 2>&1; then
  sudo apt update
  sudo apt install -y "${PACKAGES[@]}"
else
  echo "!! apt not found — install these packages with your package manager:"
  printf '   %s\n' "${PACKAGES[@]}"
  exit 1
fi

# Confirm the typelib actually loads (the package set is right but Wayland may
# need a log-out/in for the GNOME extension to take effect).
if python3 -c "import gi; gi.require_version('AyatanaAppIndicator3','0.1')" 2>/dev/null; then
  echo "==> AppIndicator typelib OK"
else
  echo "!! AppIndicator typelib still not loadable — check the install above."
fi

if gnome-extensions list --enabled 2>/dev/null | grep -qi appindicator; then
  echo "==> AppIndicator GNOME extension is enabled"
else
  echo "!! AppIndicator GNOME extension not enabled yet."
  echo "   Enable 'Ubuntu AppIndicators' and log out/in once (Wayland needs it)."
fi

if $DO_AUTOSTART; then
  AUTOSTART_DIR="$HOME/.config/autostart"
  DEST="$AUTOSTART_DIR/repodash-tray.desktop"
  mkdir -p "$AUTOSTART_DIR"
  # Write a .desktop with the resolved absolute path to this checkout.
  sed "s#^Exec=.*#Exec=/usr/bin/python3 $TRAY_PY#" \
    "$HERE/repodash-tray.desktop" > "$DEST"
  echo "==> Autostart enabled: $DEST"
fi

if $DO_MENU; then
  ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
  APP_DIR="$HOME/.local/share/applications"
  mkdir -p "$ICON_DIR" "$APP_DIR"
  cp "$HERE/repodash-app.svg" "$ICON_DIR/repodash.svg"
  sed "s#^Exec=.*#Exec=/usr/bin/python3 $TRAY_PY#" \
    "$HERE/repodash-launcher.desktop" > "$APP_DIR/repodash.desktop"
  update-desktop-database "$APP_DIR" 2>/dev/null || true
  echo "==> Start-menu icon installed: $APP_DIR/repodash.desktop"
  echo "   Icon: $ICON_DIR/repodash.svg"
fi

echo
echo "Done. Launch now with:"
echo "    python3 $TRAY_PY"
