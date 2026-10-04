#!/usr/bin/env bash
# One-time host preparation (needs root). Idempotent; does not reboot.
#   sudo ./host-setup.sh              then, as your normal user: ./bootstrap.sh
#   sudo ./host-setup.sh --uninstall  removes the Airspy udev rule
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with: sudo $0 $*" >&2; exit 1; }
TARGET_USER=${SUDO_USER:-${TARGET_USER:-}}
[[ -n $TARGET_USER ]] || { echo "set TARGET_USER or run via sudo" >&2; exit 1; }
cd "$(dirname "$(readlink -f "$0")")"
RULE=/etc/udev/rules.d/60-airspy.rules

if [[ ${1:-} == --uninstall ]]; then
    rm -f "$RULE" && udevadm control --reload
    echo "removed $RULE."
    echo "Left alone on purpose: group memberships, linger (other user services may"
    echo "rely on it), and the GRUB nomodeset removal."
    exit 0
fi

command -v docker >/dev/null && docker compose version >/dev/null 2>&1 || {
    echo "Docker Engine with the compose plugin is required: https://docs.docker.com/engine/install/" >&2
    exit 1
}

echo "== Airspy udev rule (device group plugdev)"
groupadd -f plugdev
install -m 644 udev/60-airspy.rules "$RULE"
udevadm control --reload
udevadm trigger --subsystem-match=usb --attr-match=idVendor=1d50 || true

echo "== groups for $TARGET_USER: docker, plugdev, render (where present)"
for g in docker plugdev render; do
    getent group "$g" >/dev/null && usermod -aG "$g" "$TARGET_USER"
done

echo "== linger: start the user's systemd services at boot without a login"
loginctl enable-linger "$TARGET_USER"

echo "== GPU (/dev/dri for future hardware transcoding): drop nomodeset from GRUB"
if [[ -f /etc/default/grub ]] && grep -qE '^GRUB_CMDLINE_LINUX_DEFAULT=.*\bnomodeset\b' /etc/default/grub; then
    cp -n /etc/default/grub /etc/default/grub.bak-nomodeset
    sed -i -E '/^GRUB_CMDLINE_LINUX_DEFAULT=/ s/[[:space:]]*\bnomodeset\b//' /etc/default/grub
    update-grub
    # On Intel graphics, load i915 now too (modeset=1 overrides this boot's nomodeset)
    for d in /sys/bus/pci/devices/*; do
        if [[ $(<"$d/vendor") == 0x8086 && $(<"$d/class") == 0x03* ]]; then
            modprobe i915 modeset=1 || echo "i915 not loaded now; it will be after a reboot"
            break
        fi
    done
else
    echo "nothing to do"
fi

echo "done. Log out and back in (new SSH session) so group changes apply, then run ./bootstrap.sh"
