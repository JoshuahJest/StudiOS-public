#!/bin/zsh
set -u

SSH_USER="${SSH_USER:-archive}"
SSH_HOST="${SSH_HOST:-archive-host.local}"
ROOT='~/Music/StudiOS Source Archives'
DEST="$ROOT/icarus-iii-itunes-20261004"
REPO='~/StudiOS-1.1'
PYTHON='~/.hermes/hermes-agent/venv/bin/python'
IDENTITY='~/.ssh/icarus_iii_music_migration_ed25519'
KNOWN_HOSTS='~/.ssh/known_hosts_icarus_iii'
MAX_ATTEMPTS=40

print -- "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] Icarus III archive runner starting"

if [[ -f "$DEST/manifest.json" ]]; then
  print -- "archive already published: $DEST"
  /usr/bin/shasum -a 256 "$DEST/manifest.json"
  exit 0
fi

if [[ ! -d "$ROOT" ]]; then
  print -u2 -- "archive root is missing: $ROOT"
  exit 72
fi

cd "$REPO" || exit 72

attempt=1
while [[ "$attempt" -le "$MAX_ATTEMPTS" ]]; do
  if [[ -f "$DEST/manifest.json" ]]; then
    print -- "archive already published: $DEST"
    /usr/bin/shasum -a 256 "$DEST/manifest.json"
    exit 0
  fi

  # Music.app must stay closed on the source for the duration of the copy.
  /usr/bin/ssh \
    -i "$IDENTITY" \
    -o IdentitiesOnly=yes \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$KNOWN_HOSTS" \
    "${SSH_USER}@${SSH_HOST}" \
    '/usr/bin/pgrep -x Music >/dev/null'
  ssh_rc=$?
  if [[ "$ssh_rc" -eq 0 ]]; then
    print -u2 -- 'Music.app is running on Icarus III; refusing to archive'
    exit 78
  fi
  if [[ "$ssh_rc" -ne 1 ]]; then
    print -u2 -- "could not verify that Music.app is closed (SSH exit $ssh_rc)"
    exit 76
  fi

  print -- "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] attempt $attempt/$MAX_ATTEMPTS"
  /usr/bin/caffeinate -dimsu env PYTHONPATH=src "$PYTHON" scripts/music_archive.py \
    --archive-root "$ROOT" \
    --source-id 'icarus-iii-itunes-20261004' \
    --ssh-host "$SSH_HOST" \
    --ssh-user 'archive' \
    --ssh-source-path '/Users/archive/Music/iTunes' \
    --ssh-identity "$IDENTITY" \
    --ssh-known-hosts "$KNOWN_HOSTS" \
    --expected-filesystem-type apfs \
    --connect-timeout-seconds 15 \
    --io-timeout-seconds 300 \
    --operation-timeout-seconds 172800 \
    --max-entries 1000000 \
    --max-file-bytes 1099511627776 \
    --max-total-bytes 2199023255552 \
    --execute
  rc=$?

  if [[ "$rc" -eq 0 ]]; then
    print -- "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] archive runner exit=0"
    exit 0
  fi

  # Exit 75 means the transport was interrupted. The staged partial copy is
  # preserved, so the next attempt resumes it instead of starting over.
  if [[ "$rc" -eq 75 ]]; then
    print -- "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] transport interrupted; partial copy preserved, retrying"
    attempt=$((attempt + 1))
    /bin/sleep 30
    continue
  fi

  print -- "[$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')] archive runner exit=$rc"
  exit "$rc"
done

print -u2 -- "archive did not complete after $MAX_ATTEMPTS attempts"
exit 79
