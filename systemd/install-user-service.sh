#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -eq 0 ]]; then
  echo "Run this installer as the target desktop user, not as root." >&2
  exit 2
fi

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
service_name="livinggaul-x-source-availability.service"
timer_name="livinggaul-x-source-availability.timer"

install -d -m 700 "$unit_dir"
install -m 644 "$repo_dir/systemd/$service_name" "$unit_dir/$service_name"
install -m 644 "$repo_dir/systemd/$timer_name" "$unit_dir/$timer_name"

systemctl --user daemon-reload
systemctl --user enable --now "$timer_name"

linger="$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || true)"
if [[ "$linger" != "yes" ]]; then
  if ! sudo -n loginctl enable-linger "$USER"; then
    echo "Linger is required for the hourly user timer to survive logout/reboot." >&2
    echo "Run: sudo loginctl enable-linger $USER" >&2
    exit 3
  fi
fi

# Run one bounded smoke execution now rather than waiting for the next hour.
systemctl --user start "$service_name"

systemctl --user is-enabled "$timer_name"
systemctl --user is-active "$timer_name"
loginctl show-user "$USER" -p Linger --value
systemctl --user list-timers "$timer_name" --no-pager
