#!/bin/sh
# Run speak.py with the first Python 3.9+ that actually works. On Windows,
# "python3" is usually the Microsoft Store stub: it exists, but only prints an
# install hint, so trying names in order is not enough; each one is probed.
# $0 may be a Windows path (C:\...\speak.sh); dirname only understands "/".
here=$(dirname -- "$(printf '%s\n' "$0" | sed 's|\\|/|g')")
for py in python3 python py; do
  if "$py" -c 'import sys; sys.exit(sys.version_info < (3, 9))' </dev/null >/dev/null 2>&1; then
    exec "$py" "$here/speak.py" "$@"
  fi
done
echo "read-aloud: no Python 3.9+ found (tried python3, python, py)" >&2
exit 127
