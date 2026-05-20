#!/bin/bash
# TimeTraveller dev installer.
#
# Places the bin/ and libexec/ scripts on a system path (via symlinks pointing
# back at this checkout) and installs the Polkit action policy.
#
# Run with --uninstall to reverse.
#
# Requires sudo.

set -e

REPO="$(cd "$(dirname "$0")" && pwd)"
BIN_TARGET=/usr/local/bin/timetraveller-backup
LIBEXEC_TARGET=/usr/libexec/timetraveller-install-system-cron
POLKIT_TARGET=/usr/share/polkit-1/actions/com.timetraveller.install-system-crontab.policy

usage() {
    cat <<EOF
TimeTraveller dev installer.

Usage:
  $0              install
  $0 --uninstall  remove

What this installs:
  symlink  $BIN_TARGET     ->  $REPO/bin/timetraveller-backup
  symlink  $LIBEXEC_TARGET ->  $REPO/libexec/timetraveller-install-system-cron
  file     $POLKIT_TARGET  (copied from $REPO/polkit/)

Requires sudo. Run uninstall to reverse.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi

if [[ $EUID -ne 0 ]]; then
    echo "Re-running under sudo..."
    exec sudo "$0" "$@"
fi

if [[ "${1:-}" == "--uninstall" ]]; then
    echo "Removing TimeTraveller install..."
    rm -fv "$BIN_TARGET" "$LIBEXEC_TARGET" "$POLKIT_TARGET"
    echo "Done."
    exit 0
fi

echo "Installing TimeTraveller from $REPO"
echo ""
echo "About to:"
echo "  - symlink $BIN_TARGET     -> $REPO/bin/timetraveller-backup"
echo "  - symlink $LIBEXEC_TARGET -> $REPO/libexec/timetraveller-install-system-cron"
echo "  - copy    $REPO/polkit/com.timetraveller.install-system-crontab.policy"
echo "       to   $POLKIT_TARGET"
echo ""

# Symlinks (use -f to replace any existing symlink, but bail on real files).
for pair in "$REPO/bin/timetraveller-backup $BIN_TARGET" "$REPO/libexec/timetraveller-install-system-cron $LIBEXEC_TARGET"; do
    src="${pair% *}"
    dest="${pair#* }"
    if [[ -e "$dest" && ! -L "$dest" ]]; then
        echo "ERROR: $dest exists and is not a symlink; refusing to overwrite." >&2
        echo "  (rm it yourself if you really want.)" >&2
        exit 2
    fi
    ln -sfvn "$src" "$dest"
done

# Polkit policy: must be owned by root, not world-writable.
install -o root -g root -m 644 \
    "$REPO/polkit/com.timetraveller.install-system-crontab.policy" \
    "$POLKIT_TARGET"
echo "Installed Polkit policy."

echo ""
echo "Python dependency note:"
echo "  TimeTraveller requires the 'zstandard' Python package for archive framing."
echo "  On Ubuntu/Debian, prefer the distro package (system-managed Python on 24.04+ blocks pip):"
echo "      sudo apt install python3-zstandard"
echo "  If apt isn't an option:  pip install --user 'zstandard>=0.20'"
echo ""
echo "Done. Try:"
echo "  timetraveller-backup --plan home --show-mounts"
echo "  timetraveller-backup --plan home --show-schedule"
