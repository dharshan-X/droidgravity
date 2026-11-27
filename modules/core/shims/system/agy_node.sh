#!/system/bin/sh
# AGY Node shim (agy-node):
# Runs Python-backed MCP scripts (such as chrome-devtools-mcp.py) via Droidgravity's
# bundled Bionic Python 3.14 runtime, and delegates JS targets to $PREFIX/bin/node if installed.

SELF_DIR=$(cd "$(dirname "$0")" 2>/dev/null && pwd)

if [ -z "$PREFIX" ]; then
  if [ -n "$HOME" ] && [ -d "$HOME/usr" ]; then
    PREFIX="$HOME/usr"
  elif [ -n "$SELF_DIR" ]; then
    PREFIX="$(dirname "$(dirname "$SELF_DIR")")/usr"
  fi
fi

IS_PY=0
for arg in "$@"; do
  case "$arg" in
    -*) ;;
    *.py)
      IS_PY=1
      break
      ;;
    *)
      break
      ;;
  esac
done

export PREFIX
export LD_LIBRARY_PATH="$PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PATH="$PREFIX/bin${SELF_DIR:+:$SELF_DIR}:/system/bin"
[ -f "$PREFIX/etc/tls/cert.pem" ] && export SSL_CERT_FILE="$PREFIX/etc/tls/cert.pem"
unset LD_PRELOAD

if [ "$IS_PY" -eq 1 ]; then
  PY_BIN="$PREFIX/bin/python3"
  TRIES=0
  while [ ! -x "$PY_BIN" ] && [ "$TRIES" -lt 80 ]; do
    sleep 0.25 2>/dev/null || sleep 1
    TRIES=$((TRIES + 1))
  done
  if [ ! -x "$PY_BIN" ]; then
    echo "agy-node: python3 not found at $PY_BIN" >&2
    exit 127
  fi
  export PYTHONHOME="$PREFIX"
  export PYTHONUNBUFFERED="1"
  exec "$PY_BIN" "$@"
fi

NODE_BIN="$PREFIX/bin/node"
if [ -x "$NODE_BIN" ]; then
  exec "$NODE_BIN" "$@"
fi

echo "agy-node: node binary not installed at $NODE_BIN" >&2
exit 127
