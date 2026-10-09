#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: deploy_package.sh REPO_DIR full|no-deps" >&2
  exit 2
fi

REPO_DIR="$(cd "$1" && pwd)"
INSTALL_MODE="$2"
case "$INSTALL_MODE" in
  full|no-deps) ;;
  *) echo "Install mode must be 'full' or 'no-deps'" >&2; exit 2 ;;
esac

echo "DockerGo deploy: removing stale setuptools build output"
rm -rf "$REPO_DIR/build" "$REPO_DIR/dockergo.egg-info"
WHEEL_DIR="$(mktemp -d /tmp/dockergo-wheel.XXXXXX)"
trap 'rm -rf "$WHEEL_DIR"' EXIT

echo "DockerGo deploy: building a clean wheel"
/usr/bin/python3 -m pip wheel --no-build-isolation --no-deps --no-cache-dir \
  --wheel-dir "$WHEEL_DIR" "$REPO_DIR"
mapfile -t WHEELS < <(find "$WHEEL_DIR" -maxdepth 1 -type f -name 'dockergo-*.whl' -print)
if [[ ${#WHEELS[@]} -ne 1 ]]; then
  echo "Expected one DockerGo wheel in $WHEEL_DIR; found ${#WHEELS[@]}" >&2
  exit 1
fi

/usr/bin/python3 "$REPO_DIR/scripts/verify_package.py" wheel "$REPO_DIR" "${WHEELS[0]}"

PIP_ARGS=(--break-system-packages --no-cache-dir --force-reinstall)
if [[ "$INSTALL_MODE" == "no-deps" ]]; then
  PIP_ARGS+=(--no-deps)
fi
echo "DockerGo deploy: installing the verified wheel"
/usr/bin/python3 -m pip install "${PIP_ARGS[@]}" "${WHEELS[0]}"

INSTALLED_PACKAGE_DIR="$(cd / && /usr/bin/python3 -c 'import dockergo; from pathlib import Path; print(Path(dockergo.__file__).parent)')"
/usr/bin/python3 "$REPO_DIR/scripts/verify_package.py" installed \
  "$REPO_DIR" "$INSTALLED_PACKAGE_DIR"
if ! (cd / && /usr/bin/python3 -c 'import dockergo.__main__'); then
  echo "DockerGo deploy: entry point import failed" >&2
  exit 1
fi
echo "DockerGo deploy: entry point import passed; package at $INSTALLED_PACKAGE_DIR"