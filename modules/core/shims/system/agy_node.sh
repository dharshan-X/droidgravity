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
export LD_LIBRARY_PATH="$PREFIX/lib"
export PATH="$PREFIX/bin${SELF_DIR:+:$SELF_DIR}:/system/bin:/usr/bin:/bin"
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
AGY_NODE_LOG="${TMPDIR:-$PREFIX/tmp}/agy-node.log"
{
  echo "[$(date 2>/dev/null)] agy-node js: node=$NODE_BIN exists=$([ -x "$NODE_BIN" ] && echo yes || echo no) args=$*"
} >> "$AGY_NODE_LOG" 2>/dev/null || true
if [ -x "$NODE_BIN" ]; then
  exec "$NODE_BIN" "$@"
fi

# Wait briefly if EnvInstaller is still extracting the bootstrap environment
TRIES=0
while [ -d "$PREFIX" ] && [ ! -f "$PREFIX/.installed" ] && [ ! -f "$PREFIX/bin/pkg" ] && [ ! -x "$NODE_BIN" ] && [ "$TRIES" -lt 80 ]; do
  sleep 0.25 2>/dev/null || sleep 1
  TRIES=$((TRIES + 1))
done

if [ ! -x "$NODE_BIN" ] && [ -f "$PREFIX/bin/pkg" ]; then
  export TMPDIR="${TMPDIR:-$PREFIX/tmp}"
  mkdir -p "$TMPDIR" 2>/dev/null || true
  LOCK_DIR="$TMPDIR/agy-node-install.lock"

  if mkdir "$LOCK_DIR" 2>/dev/null; then
    if [ ! -x "$NODE_BIN" ]; then
      echo "agy-node: $NODE_BIN missing; installing nodejs via pkg..." >&2
      PKG_INDEX_DIR="$PREFIX/var/lib/pkg/index"
      HAS_INDEX=0
      for idx in "$PKG_INDEX_DIR"/*.Packages; do
        if [ -f "$idx" ]; then
          HAS_INDEX=1
          break
        fi
      done

      if [ -x "$PREFIX/bin/bash" ]; then
        [ "$HAS_INDEX" -eq 0 ] && "$PREFIX/bin/bash" "$PREFIX/bin/pkg" update </dev/null >&2
        "$PREFIX/bin/bash" "$PREFIX/bin/pkg" install -y nodejs </dev/null >&2
      elif [ -x "$PREFIX/bin/pkg" ]; then
        [ "$HAS_INDEX" -eq 0 ] && "$PREFIX/bin/pkg" update </dev/null >&2
        "$PREFIX/bin/pkg" install -y nodejs </dev/null >&2
      fi
    fi
    rmdir "$LOCK_DIR" 2>/dev/null || true
  else
    WAIT_TRIES=0
    while [ -d "$LOCK_DIR" ] && [ ! -x "$NODE_BIN" ] && [ "$WAIT_TRIES" -lt 240 ]; do
      sleep 0.25 2>/dev/null || sleep 1
      WAIT_TRIES=$((WAIT_TRIES + 1))
    done
  fi
fi

if [ -x "$NODE_BIN" ]; then
  exec "$NODE_BIN" "$@"
fi

echo "agy-node: node binary not installed at $NODE_BIN" >&2
exit 127
