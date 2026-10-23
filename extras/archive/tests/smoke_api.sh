#!/usr/bin/env bash
# Run inside a disposable Debian container with the repository at /source.

set -euo pipefail

fail() { echo "FAIL: $*" >&2; exit 1; }

cat >/tmp/archive-control-test <<'EOF'
#!/bin/sh
printf '{"ok":true,"action":"%s"}\n' "$1"
EOF
chmod 0755 /tmp/archive-control-test

# The production web process reaches the root-owned helper through
# `sudo -n`. Keep this disposable test independent of whether its base image
# happens to include sudo while preserving that exact invocation boundary.
mkdir -p /tmp/archive-api-bin
cat >/tmp/archive-api-bin/sudo <<'EOF'
#!/bin/sh
[ "${1:-}" = -n ] && shift
exec "$@"
EOF
chmod 0755 /tmp/archive-api-bin/sudo
export PATH="/tmp/archive-api-bin:$PATH"

# Model an initialized station with LAN password protection disabled. HTTP
# runtimes deliberately ignore the CLI metadata bypass and fail closed when
# the root-managed state is absent, so the smoke must provide real metadata.
state_dir=/run/archive-api-state
mkdir -p "$state_dir"
chmod 0755 "$state_dir"
printf 'v1\t0\t0\t-\n' >"$state_dir/admin-auth.state"
chown root:caddy "$state_dir/admin-auth.state"
chmod 0640 "$state_dir/admin-auth.state"

AV_ADMIN_STATE_FILE="$state_dir/admin-auth.state" \
AV_ARCHIVE_CONTROL=/tmp/archive-control-test php -S 127.0.0.1:8899 -t /source \
  >/tmp/archive-api-server.log 2>&1 &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT

for _ in 1 2 3 4 5; do
  curl -fsS http://127.0.0.1:8899/avian/api/archive.php >/tmp/archive-api-body 2>/dev/null && break
  sleep 1
done
grep -q '"action":"status"' /tmp/archive-api-body || fail "GET status"

code=$(curl -sS -o /tmp/archive-api-body -w '%{http_code}' -X POST \
  -H 'Content-Type: text/plain' --data '{}' http://127.0.0.1:8899/avian/api/archive.php)
[ "$code" = 415 ] || fail "content type guard"

code=$(curl -sS -o /tmp/archive-api-body -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' --data '{"action":"enable"}' \
  http://127.0.0.1:8899/avian/api/archive.php)
[ "$code" = 403 ] || fail "action header guard"

code=$(curl -sS -o /tmp/archive-api-body -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' -H 'X-Avian-Action: 1' \
  --data '{"action":"other"}' http://127.0.0.1:8899/avian/api/archive.php)
[ "$code" = 400 ] || fail "action allowlist"

code=$(curl -sS -o /tmp/archive-api-body -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' -H 'X-Avian-Action: 1' \
  --data '{"action":"purge-on"}' http://127.0.0.1:8899/avian/api/archive.php)
[ "$code" = 400 ] || fail "cleanup confirmation"

code=$(curl -sS -o /tmp/archive-api-body -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' -H 'X-Avian-Action: 1' \
  --data '{"action":"purge-on","confirm":"verified-local-files"}' \
  http://127.0.0.1:8899/avian/api/archive.php)
[ "$code" = 200 ] || fail "confirmed cleanup action"
grep -q '"action":"purge-on"' /tmp/archive-api-body || fail "fixed action routing"

echo 'archive api smoke: ok'

code=$(curl -sS -o /tmp/archive-api-body -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' -H 'X-Avian-Action: 1' \
  --data '{"action":"stop"}' http://127.0.0.1:8899/avian/api/archive.php)
[ "$code" = 200 ] || fail "stop action"
grep -q '"action":"stop"' /tmp/archive-api-body || fail "stop action routing"

for action in stop 'stop birdnet_recording.service'; do
  code=$(curl -sS -o /tmp/archive-api-body -w '%{http_code}' -X POST \
    -H 'Content-Type: application/json' \
    -H 'Origin: https://untrusted.example' --data "{\"action\":\"$action\"}" \
    http://127.0.0.1:8899/avian/api/archive.php)
  [ "$code" = 403 ] || fail "cross-origin stop accepted"
done

code=$(curl -sS -D /tmp/archive-api-headers -o /tmp/archive-api-body -w '%{http_code}' -X OPTIONS \
  -H 'Origin: https://untrusted.example' -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: X-Avian-Action, Content-Type' \
  http://127.0.0.1:8899/avian/api/archive.php)
[ "$code" = 405 ] || fail "cross-origin preflight accepted"
if grep -qi '^Access-Control-Allow-Origin:' /tmp/archive-api-headers; then fail 'cross-origin access allowed'; fi

code=$(curl -sS -o /tmp/archive-api-body -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' -H 'X-Avian-Action: 1' \
  --data '{"action":"stop birdnet_recording.service"}' http://127.0.0.1:8899/avian/api/archive.php)
[ "$code" = 400 ] || fail "stop accepts an arbitrary service"

code=$(curl -sS -o /tmp/archive-api-body -w '%{http_code}' -X POST \
  -H 'Host: public.example' -H 'Content-Type: application/json' -H 'X-Avian-Action: 1' \
  --data '{"action":"stop"}' http://127.0.0.1:8899/avian/api/archive.php)
[ "$code" = 401 ] || fail "public unauthenticated stop accepted"

echo 'archive stop API smoke: ok'
