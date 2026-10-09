#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${DOCKERGO_REPO_DIR:-}"
if [[ -z "$REPO_DIR" ]]; then
  echo "DockerGo update: DOCKERGO_REPO_DIR is not configured" >&2
  exit 1
fi

if ! git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "DockerGo update: $REPO_DIR is not a Git checkout" >&2
  exit 1
fi

REPO_OWNER="$(stat -c '%U' "$REPO_DIR")"
if ! runuser -u "$REPO_OWNER" -- git -C "$REPO_DIR" pull --ff-only --quiet; then
  echo "DockerGo update: GitHub sync failed; keeping the installed version" >&2
  exit 1
fi

COMMIT="$(runuser -u "$REPO_OWNER" -- git -C "$REPO_DIR" rev-parse HEAD)"
STATE_DIR=/var/lib/dockergo
MARKER="$STATE_DIR/installed-commit"
INSTALLED_COMMIT=""
if [[ -f "$MARKER" ]]; then
  read -r INSTALLED_COMMIT < "$MARKER"
fi

if [[ "$COMMIT" == "$INSTALLED_COMMIT" ]]; then
  echo "DockerGo update: already current at $COMMIT"
  exit 0
fi

echo "DockerGo update: installing commit $COMMIT"
/usr/bin/python3 -m pip install --break-system-packages --no-deps --force-reinstall "$REPO_DIR"
install -d "$STATE_DIR"
printf '%s\n' "$COMMIT" > "$MARKER.tmp"
mv "$MARKER.tmp" "$MARKER"
systemctl --no-block try-restart dockergo.service || true
echo "DockerGo update: installed commit $COMMIT"