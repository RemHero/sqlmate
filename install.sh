#!/usr/bin/env bash
# SqlMate 可移植安装脚本：支持自定义包索引和 OpenSSL 路径。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy

if [[ -n "${SQLMATE_OPENSSL_LIB_DIR:-}" ]]; then
  export LD_LIBRARY_PATH="${SQLMATE_OPENSSL_LIB_DIR}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
fi

python_is_usable() {
  local candidate="$1"
  "$candidate" -c 'import ssl, sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    >/dev/null 2>&1
}

find_python() {
  local candidate
  if [[ -n "${SQLMATE_PYTHON:-}" ]] && python_is_usable "$SQLMATE_PYTHON"; then
    printf '%s\n' "$SQLMATE_PYTHON"
    return 0
  fi
  for candidate in python3.10 python3.11 python3.12 python3.13 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_is_usable "$candidate"; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

build_python() {
  local source_dir="${SQLMATE_PYTHON_SOURCE_DIR:-}"
  local prefix="$SCRIPT_DIR/.python310"
  if [[ -z "$source_dir" ]] || [[ ! -x "$source_dir/configure" ]]; then
    return 1
  fi

  local configure_args=("--prefix=$prefix" "--enable-optimizations=no")
  if [[ -n "${SQLMATE_OPENSSL_ROOT:-}" ]]; then
    configure_args+=("--with-openssl=$SQLMATE_OPENSSL_ROOT")
  fi

  echo "No usable Python 3.10+ found; building from $source_dir"
  (
    cd "$source_dir"
    ./configure "${configure_args[@]}"
    make -j"${SQLMATE_BUILD_JOBS:-2}"
    make install
  )

  # make 阶段找不到动态库时，构建过程可能把扩展标为 _failed.so。
  local dynload="$prefix/lib/python3.10/lib-dynload"
  local failed
  for failed in "$source_dir"/build/lib.*/_ssl*_failed.so "$source_dir"/build/lib.*/_hashlib*_failed.so; do
    [[ -e "$failed" ]] || continue
    cp "$failed" "$dynload/$(basename "${failed/_failed.so/.so}")"
  done

  python_is_usable "$prefix/bin/python3.10" || return 1
  printf '%s\n' "$prefix/bin/python3.10"
}

PYTHON_BIN="$(find_python || build_python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  cat >&2 <<'EOF'
SqlMate requires Python 3.10+ with SSL support.
Set SQLMATE_PYTHON to a usable interpreter, or set SQLMATE_PYTHON_SOURCE_DIR
(and, when needed, SQLMATE_OPENSSL_ROOT / SQLMATE_OPENSSL_LIB_DIR) for an
offline source build.
EOF
  exit 1
fi

echo "Using Python: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"
"$PYTHON_BIN" -m venv .venv

PIP_ARGS=(install -r requirements.txt)
if [[ -n "${SQLMATE_PIP_INDEX_URL:-}" ]]; then
  PIP_ARGS+=(--index-url "$SQLMATE_PIP_INDEX_URL")
fi
if [[ -n "${SQLMATE_PIP_TRUSTED_HOST:-}" ]]; then
  PIP_ARGS+=(--trusted-host "$SQLMATE_PIP_TRUSTED_HOST")
fi

.venv/bin/python -m pip "${PIP_ARGS[@]}"
.venv/bin/python -c 'import ssl, agents, openai, pydantic, yaml, httpx, rich; print("SqlMate dependencies verified")'

echo "Installation complete. Run: ./sqlmate --help"
