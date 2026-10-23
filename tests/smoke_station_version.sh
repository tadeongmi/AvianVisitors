#!/usr/bin/env bash
set -euo pipefail

[ -f /.dockerenv ] && [ "${AVIAN_VERSION_TEST:-0}" = 1 ] \
  || { echo 'Run only in an opted-in disposable container.' >&2; exit 1; }
fail() { echo "FAIL: $*" >&2; exit 1; }
useradd -m versionbird
repo=/home/versionbird/BirdNET-Pi
mkdir -p "$repo" /etc/birdnet
printf 'BIRDNET_USER=versionbird\n' >/etc/birdnet/birdnet.conf
chown versionbird:versionbird "$repo"
control=/source/scripts/admin_control.sh
station_git() { runuser -u versionbird -- git -C "$repo" "$@"; }

if "$control" release >/tmp/release.json; then fail 'missing checkout reported a version'; fi
station_git init -q
station_git config user.name Test
station_git config user.email test@example.invalid
printf 'one\n' >"$repo/example"
chown versionbird:versionbird "$repo/example"
station_git add example
station_git commit -qm initial
commit=$(station_git rev-parse --short=12 HEAD)
"$control" release | grep -Fq "\"release\":\"$commit\"" || fail 'untagged commit'
station_git tag v1.3.1
"$control" release | grep -Fq '"release":"v1.3.1"' || fail 'release tag'
station_git config core.fsmonitor '/usr/bin/touch /tmp/version-hook-ran'
printf 'two\n' >>"$repo/example"
index_before=$(sha256sum "$repo/.git/index")
"$control" release | grep -Fq '"release":"v1.3.1"' || fail 'readout scanned local edits'
[ ! -e /tmp/version-hook-ran ] || fail 'readout executed fsmonitor'
[ "$(sha256sum "$repo/.git/index")" = "$index_before" ] || fail 'readout changed index'
station_git config --unset core.fsmonitor
station_git add example
station_git commit -qm second
commit=$(station_git rev-parse --short=12 HEAD)
"$control" release | grep -Fq "\"release\":\"$commit\"" || fail 'post-release commit'
printf 'example filter=probe\n' >"$repo/.git/info/attributes"
station_git config filter.probe.clean 'touch /tmp/version-filter-ran; cat'
touch -d '2000-01-01' "$repo/example"
"$control" release >/tmp/release.json
[ ! -e /tmp/version-filter-ran ] || fail 'readout executed a clean filter'
if "$control" release /tmp >/tmp/release.json; then fail 'caller chose checkout'; fi
station_git tag 'v2.0.0<script>'
if "$control" release >/tmp/release.json; then fail 'unsafe tag accepted'; fi
station_git tag -d 'v2.0.0<script>' >/dev/null

install -m 0755 "$control" /usr/local/sbin/avian-admin-control
printf 'caddy ALL=(root) NOPASSWD: /usr/local/sbin/avian-admin-control release\n' >/etc/sudoers.d/099-version-test
chmod 0440 /etc/sudoers.d/099-version-test
visudo -cf /etc/sudoers.d/099-version-test >/dev/null
mkdir -p /run/version-api-state
printf 'v1\t0\t0\t-\n' >/run/version-api-state/admin-auth.state
chown root:caddy /run/version-api-state/admin-auth.state
chmod 0640 /run/version-api-state/admin-auth.state
runuser -u caddy -- env AV_ADMIN_STATE_FILE=/run/version-api-state/admin-auth.state \
  php -S 127.0.0.1:8898 -t /source >/tmp/version-api-server.log 2>&1 &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT
for _ in 1 2 3 4 5; do
  curl -fsS 'http://127.0.0.1:8898/avian/api/birdnet-status.php?action=version' >/tmp/release.json 2>/dev/null && break
  sleep 1
done
grep -Fq "\"release\":\"$commit\"" /tmp/release.json || fail 'HTTP version dispatch'
code=$(curl -sS -o /tmp/release.json -w '%{http_code}' -H 'Host: public.example' \
  'http://127.0.0.1:8898/avian/api/birdnet-status.php?action=version')
[ "$code" = 401 ] || fail 'public version access bypassed admin authentication'
station_git tag 'v2.0.0<script>'
code=$(curl -sS -o /tmp/release.json -w '%{http_code}' \
  'http://127.0.0.1:8898/avian/api/birdnet-status.php?action=version')
[ "$code" = 503 ] || fail 'invalid version did not fail closed'
grep -q 'station version is unavailable' /tmp/release.json || fail 'missing generic version error'
if grep -qE '<script>|/home/versionbird' /tmp/release.json; then fail 'version failure leaked internal data'; fi

station_git tag -d 'v2.0.0<script>' >/dev/null
station_git tag v2.0.0
full_commit=$(station_git rev-parse HEAD)
station_git config remote.origin.url 'ext::/usr/bin/touch /tmp/version-lazy-fetch-ran'
station_git config remote.origin.promisor true
station_git config remote.origin.partialclonefilter blob:none
station_git config protocol.ext.allow always
object_path="$repo/.git/objects/${full_commit:0:2}/${full_commit:2}"
mv "$object_path" /tmp/version-head-object
"$control" release | grep -Fq "\"release\":\"$commit\"" || fail 'missing object fallback'
[ ! -e /tmp/version-lazy-fetch-ran ] || fail 'readout attempted a lazy fetch'
mv /tmp/version-head-object "$object_path"

mkfifo -m 0644 /tmp/version-blocked-git-config
station_git config include.path /tmp/version-blocked-git-config
result=0
timeout 12 "$control" release >/tmp/release.json || result=$?
[ "$result" = 1 ] || fail "blocked Git did not fail within its timeout ($result)"
grep -q 'station version is unavailable' /tmp/release.json || fail 'blocked Git error leaked'

echo 'station version smoke: ok'
