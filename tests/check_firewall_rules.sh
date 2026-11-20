#!/usr/bin/env bash
# Let UFW itself check every ufw command install.sh and deploy/isp-panel-fwsync run, with --dry-run (nothing is
# applied). Used by GitHub Actions (job "firewall") on Ubuntu 24.04; needs root.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v ufw >/dev/null || { apt-get install -y -qq ufw >/dev/null; }

# the firewall section of install.sh, with every ufw call made a dry run and output kept
section="$(sed -n '/^# -* firewall$/,/^ufw --force enable/p' "$ROOT/install.sh" | grep -v '^ufw --force enable')"
[[ -n "$section" ]] || { echo "firewall section not found in install.sh"; exit 1; }
section="$(sed "s# >/dev/null##g" <<<"$section")"      # keep ufw output; 2>/dev/null stays
fails=0
ufw() {
  local out
  if out="$(command ufw --dry-run "$@" 2>&1)" && ! grep -q '^ERROR' <<<"$out"; then
    echo "ok    ufw $*"
  else
    echo "FAIL  ufw $*  ->  $(grep -m1 'ERROR' <<<"$out" || echo "$out" | tail -n1)"
    [[ "$1" == "delete" ]] || fails=$((fails + 1))      # deleting a rule that is not there is fine
  fi
  return 0
}
c_ok() { :; }
eval "$section"

# the rule format deploy/isp-panel-fwsync uses for each router
ufw allow from 203.0.113.7 to any port 1812,1813 proto udp comment isp-radius
ufw allow from 2001:db8::7 to any port 1812,1813 proto udp comment isp-radius
ufw allow from 10.30.0.0/24 to any port 1812,1813 proto udp comment isp-radius

if (( fails )); then
  echo "::error file=install.sh::UFW refuses $fails firewall command(s) from install.sh or isp-panel-fwsync (see the job output)"
  exit 1
fi
echo "UFW accepts every firewall command"
