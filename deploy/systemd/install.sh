#!/bin/sh
# Install (or refresh) the four comfy-vast units on this host.
#
#     sudo deploy/systemd/install.sh
#
# The unit files in this directory are templates because the repository is
# public and the paths are not: __ROOT__, __USER__ and __HOME__ are filled in
# from wherever this checkout actually lives and whoever actually owns it.
#
# System units, not `systemctl --user`, and the difference matters: a user
# manager only exists once its user has a session, or once someone remembers
# `loginctl enable-linger`. The reaper must come up on a cold boot with nobody
# logged in - that is the entire point of moving it off the workstation.
#
# Units are installed and enabled, not started. Starting them is a decision
# about a machine that may already be renting a GPU; see docs/vps-deploy.md.
set -eu

[ "$(id -u)" = "0" ] || { echo "install.sh: run me with sudo" >&2; exit 1; }

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
# The checkout's owner, not root and not $SUDO_USER: the services have to read
# .env and write output/, so the right answer is whoever owns the files.
USER_NAME=$(stat -c %U "$ROOT")
HOME_DIR=$(getent passwd "$USER_NAME" | cut -d: -f6)
UNIT_DIR=/etc/systemd/system

[ -n "$HOME_DIR" ] || { echo "install.sh: no home for $USER_NAME" >&2; exit 1; }
echo "repo   $ROOT"
echo "user   $USER_NAME ($HOME_DIR)"

for unit in cv-api cv-fleet cv-web cv-tunnel; do
  sed -e "s#__ROOT__#$ROOT#g" -e "s#__USER__#$USER_NAME#g" -e "s#__HOME__#$HOME_DIR#g" \
      "$HERE/$unit.service" > "$UNIT_DIR/$unit.service"
  chmod 644 "$UNIT_DIR/$unit.service"
  echo "wrote  $UNIT_DIR/$unit.service"
done

systemd-analyze verify "$UNIT_DIR/cv-api.service" "$UNIT_DIR/cv-fleet.service" \
                       "$UNIT_DIR/cv-web.service" "$UNIT_DIR/cv-tunnel.service"
systemctl daemon-reload

# cv-tunnel is left out unless its configuration exists: enabling a unit that
# cannot start turns every future boot into a failed unit nobody reads.
systemctl enable cv-api.service cv-fleet.service cv-web.service
if [ -f "$HOME_DIR/.cloudflared/config.yml" ]; then
  systemctl enable cv-tunnel.service
  echo "enabled cv-api cv-fleet cv-web cv-tunnel"
else
  echo "enabled cv-api cv-fleet cv-web"
  echo "skipped cv-tunnel: no $HOME_DIR/.cloudflared/config.yml yet"
fi
