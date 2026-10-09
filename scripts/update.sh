#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${DOCKERGO_REPO_DIR:-}"
if [[ -z "$REPO_DIR" ]] || ! git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "DockerGo update: configured source is not a Git checkout" >&2
  exit 1
fi

REPO_OWNER="$(stat -c '%U' "$REPO_DIR")"
APP_ACTIVE_STATE="$(systemctl show -p ActiveState --value dockergo.service 2>/dev/null || true)"

if timeout 25s runuser -u "$REPO_OWNER" -- git -C "$REPO_DIR" fetch --quiet origin main; then
  if ! runuser -u "$REPO_OWNER" -- git -C "$REPO_DIR" merge --ff-only --quiet FETCH_HEAD; then
    echo "DockerGo update: main is not a fast-forward; keeping the local checkout" >&2
  fi
else
  echo "DockerGo update: GitHub unavailable; checking the local checkout" >&2
fi

if ! cmp -s "$REPO_DIR/scripts/update.sh" /usr/local/sbin/dockergo-update; then
  install -m 0755 "$REPO_DIR/scripts/update.sh" /usr/local/sbin/dockergo-update
  exec /usr/local/sbin/dockergo-update
fi

COMMIT="$(runuser -u "$REPO_OWNER" -- git -C "$REPO_DIR" rev-parse HEAD)"
STATE_DIR=/var/lib/dockergo
MARKER="$STATE_DIR/installed-commit"
INSTALLED_COMMIT=""
if [[ -f "$MARKER" ]]; then
  read -r INSTALLED_COMMIT < "$MARKER"
fi

INSTALLED_OK=false
if INSTALLED_PACKAGE_DIR="$(cd / && /usr/bin/python3 -c 'import dockergo; print(dockergo.__path__[0])' 2>/dev/null)"; then
  if /usr/bin/python3 "$REPO_DIR/scripts/verify_package.py" installed \
      "$REPO_DIR" "$INSTALLED_PACKAGE_DIR" && \
      (cd / && /usr/bin/python3 -c 'import dockergo.__main__'); then
    INSTALLED_OK=true
  fi
fi

if [[ "$INSTALLED_OK" == true && "$COMMIT" == "$INSTALLED_COMMIT" ]]; then
  echo "DockerGo update: package verified at commit $COMMIT"
  exit 0
fi

echo "DockerGo update: installed package failed verification; rebuilding commit $COMMIT"
bash "$REPO_DIR/scripts/deploy_package.sh" "$REPO_DIR" no-deps
install -d "$STATE_DIR"
printf '%s\n' "$COMMIT" > "$MARKER.tmp"
mv "$MARKER.tmp" "$MARKER"
install -m 0755 "$REPO_DIR/scripts/update.sh" /usr/local/sbin/dockergo-update

if [[ "$APP_ACTIVE_STATE" == "active" ]]; then
  systemctl --no-block try-restart dockergo.service
fi
echo "DockerGo update: clean package installed from commit $COMMIT"