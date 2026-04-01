#!/bin/bash
# Post-deploy health check for alias-api
# Usage: ./healthcheck.sh [base_url]
# Example: ./healthcheck.sh https://api.byboernie.de

BASE_URL="${1:-https://api.byboernie.de}"
PASS=0
FAIL=0

check() {
  local path="$1"
  local expected="${2:-200}"
  local status
  status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$BASE_URL$path")
  if [ "$status" = "$expected" ]; then
    echo "  ✓ $path ($status)"
    ((PASS++))
  else
    echo "  ✗ $path — erwartet $expected, bekommen $status"
    ((FAIL++))
  fi
}

echo "Health-Check: $BASE_URL"
echo ""

check "/login"
check "/privacy"
check "/impressum"
check "/api/addresses"
check "/doesnotexist" "404"

echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "Alle $PASS Checks bestanden."
  exit 0
else
  echo "$FAIL von $((PASS + FAIL)) Checks fehlgeschlagen!"
  exit 1
fi
