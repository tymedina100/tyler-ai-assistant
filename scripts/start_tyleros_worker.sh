#!/bin/sh
# Reads credentials from inherited environment; never sources or prints secrets.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "${TYLEROS_PYTHON:-python3}" "$SCRIPT_DIR/../tyleros_worker.py" "$@"
