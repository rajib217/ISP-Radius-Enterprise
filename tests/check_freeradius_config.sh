#!/usr/bin/env bash
# Run install.sh's FreeRADIUS edits on a real FreeRADIUS install and let FreeRADIUS check the result.
# Used by GitHub Actions (job "freeradius") on Ubuntu 24.04 with the distribution's FreeRADIUS 3.2.x.
# Run as root on a throw-away machine only: it edits /etc/freeradius.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FR_DIR="$(ls -d /etc/freeradius/*/ | head -n1)"; FR_DIR="${FR_DIR%/}"
STEP="$(mktemp)"
# the Python block between  python3 - "$FR_DIR" ... <<'PY'  and  PY  in install.sh
python3 - "$ROOT/install.sh" "$STEP" <<'EOF'
import sys
s = open(sys.argv[1], encoding="utf-8").read()
start = s.index('python3 - "$FR_DIR" "$FR_DB_PASS"')
open(sys.argv[2], "w").write(s[s.index("<<'PY'\n", start) + 7: s.index("\nPY\n", start)] + "\n")
EOF
cp "$FR_DIR/mods-config/sql/main/mysql/queries.conf" /tmp/queries.conf.stock

for run in 1 2; do                       # twice: a re-run must give the same, valid result
  python3 "$STEP" "$FR_DIR" "ci-db-pass" "ci-local-secret" "radius" "radius"
done

Q="$FR_DIR/mods-config/sql/main/mysql/queries.conf"
n=$(grep -c 'reason, nas_ip, mac' "$Q")
[[ "$n" == 1 ]] || { echo "expected exactly one edited post-auth query, found $n"; exit 1; }
awk '/^post-auth \{/{f=1} f' "$Q" | grep -q 'reason, nas_ip, mac' \
  || { echo "the edit is not in the top-level post-auth (authentication log) section"; exit 1; }

chgrp -R freerad "$FR_DIR" 2>/dev/null || true
if ! freeradius -Cx -lstdout -d "$FR_DIR" > /tmp/freeradius-check.log 2>&1; then
  tail -n 40 /tmp/freeradius-check.log
  # the last lines as an annotation too, so they can be read on the Actions page without signing in
  msg="$(grep -v '^\s*$' /tmp/freeradius-check.log | tail -n 15 | sed 's/%/%25/g' | awk '{printf "%s%%0A", $0}')"
  echo "::error file=install.sh::FreeRADIUS rejects the configuration written by install.sh:%0A${msg}"
  exit 1
fi
grep -q "Configuration appears to be OK" /tmp/freeradius-check.log && echo "FreeRADIUS: Configuration appears to be OK"
