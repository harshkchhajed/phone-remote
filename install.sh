#!/bin/sh
# Installs Phone Remote to ~/.local/share/phone-remote and starts it on every login.
set -e
SRC=$(cd "$(dirname "$0")" && pwd)
DEST="$HOME/.local/share/phone-remote"
UNIT="$HOME/.config/systemd/user/phone-remote.service"

mkdir -p "$DEST/static" "$(dirname "$UNIT")"
cp "$SRC/server.py" "$SRC/xinput.py" "$SRC/clip.py" "$DEST/"
cp "$SRC"/static/* "$DEST/static/"

cat > "$UNIT" <<EOF
[Unit]
Description=Phone Remote (control this laptop from a phone)
After=graphical-session.target
PartOf=graphical-session.target

[Service]
ExecStart=/usr/bin/python3 $DEST/server.py --port 8765
Restart=on-failure
RestartSec=3
# Apps launched from the phone must survive a service restart.
KillMode=process

[Install]
WantedBy=graphical-session.target
EOF

# Alt+C anywhere on the laptop: copy what's selected and send it to the phone.
KB=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/phone-remote/
LIST=$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings)
case "$LIST" in
  *"$KB"*) ;;
  "@as []"|"[]") gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "['$KB']" ;;
  *) gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "${LIST%]}, '$KB']" ;;
esac
S="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KB"
gsettings set "$S" name "Send selection to phone (Phone Remote)"
gsettings set "$S" command "systemctl --user kill --kill-who=main -s SIGUSR1 phone-remote.service"
gsettings set "$S" binding "<Alt>c"

systemctl --user daemon-reload
systemctl --user enable phone-remote.service
systemctl --user restart phone-remote.service
echo "Installed. Open https://$(hostname)-remote.local:8765/ on the phone."
