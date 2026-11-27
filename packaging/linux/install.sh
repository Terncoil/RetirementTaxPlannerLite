#!/bin/sh
# Install a PyInstaller Linux build for the current user, with menu entry,
# icons and the .rtpl and .eqta file types (so double-clicking a file opens it).
# Run from the repository root after building:
#   sh packaging/linux/install.sh
set -e
APP=RetirementTaxPlannerLite
DEST="${XDG_DATA_HOME:-$HOME/.local/share}"
mkdir -p "$DEST/$APP" "$DEST/applications" "$DEST/mime/packages" "$HOME/.local/bin"
cp -r "dist/$APP/." "$DEST/$APP/"
ln -sf "$DEST/$APP/$APP" "$HOME/.local/bin/$APP"
for png in src/rtpl/data/icons/rtpl_*.png; do
  n=$(basename "$png" .png | cut -d_ -f2)
  mkdir -p "$DEST/icons/hicolor/${n}x${n}/apps"
  cp "$png" "$DEST/icons/hicolor/${n}x${n}/apps/rtpl.png"
done
sed "s|^Exec=[^ ]*|Exec=\"$DEST/$APP/$APP\"|" packaging/linux/rtpl.desktop > "$DEST/applications/rtpl.desktop"
cp packaging/linux/rtpl-mime.xml "$DEST/mime/packages/rtpl.xml"
command -v update-mime-database >/dev/null && update-mime-database "$DEST/mime" || true
command -v update-desktop-database >/dev/null && update-desktop-database "$DEST/applications" || true
command -v xdg-mime >/dev/null && xdg-mime default rtpl.desktop application/x-rtpl application/x-eqta || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q "$DEST/icons/hicolor" || true
echo "Installed $APP. It is in your applications menu, and .rtpl and .eqta files open in it."
