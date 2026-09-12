#!/usr/bin/env bash
# Run interactively on the management Mac. PBS password is read by SSH from
# the terminal, never passed in an argument or written to a file.
set -euo pipefail
target='root@172.16.10.51'
expected_fingerprint='SHA256:kca8W7i6ilEkTbNoCWwmggmCFNDXqGkyIVc4GKSDNG4'

# authorized_keys restrictions. This key only needs to run commands; no
# forwarding is required. PBS is the asset that must survive a compromised
# cluster, so a leaked key must not turn it into a pivot host. `restrict` is
# deliberately not used, to keep interactive root login working.
#
# Pass MANAGEMENT_CIDR to also pin the source address:
#   MANAGEMENT_CIDR=172.16.10.100 ./scripts/setup-pbs-ssh-key.sh
key_options='no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-user-rc'
if [ -n "${MANAGEMENT_CIDR:-}" ]; then
  # Keep quotes and shell metacharacters out of authorized_keys.
  if ! printf '%s' "$MANAGEMENT_CIDR" | grep -Eq '^[0-9./,*?:a-fA-F]+$'; then
    echo 'MANAGEMENT_CIDR must be an address, CIDR or pattern list.' >&2
    exit 1
  fi
  key_options="from=\"${MANAGEMENT_CIDR}\",${key_options}"
fi
temporary_dir="$(mktemp -d)"
trap 'rm -rf -- "$temporary_dir"' EXIT

# Select the exact existing management key from the agent, not an arbitrary key.
python3 - "$expected_fingerprint" "$temporary_dir/public-key" <<'PY'
import pathlib, subprocess, sys
keys = subprocess.check_output(['ssh-add', '-L'], text=True).splitlines()
for key in keys:
    result = subprocess.run(['ssh-keygen', '-lf', '-'], input=key + '\n',
                            text=True, capture_output=True, check=True)
    if result.stdout.split()[1] == sys.argv[1]:
        pathlib.Path(sys.argv[2]).write_text(key + '\n')
        break
else:
    raise SystemExit('The existing management key is not loaded in the SSH agent.')
PY

printf 'Registering management key on %s:\n' "$target"
ssh-keygen -lf "$temporary_dir/public-key"
printf 'Enter the PBS root password when SSH asks for it.\n'
ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=yes "$target" "KEY_OPTIONS='$key_options'"' 
  set -eu
  umask 077
  mkdir -p /root/.ssh
  chmod 700 /root/.ssh
  key_file=$(mktemp /root/.ssh/pbs-key.XXXXXX)
  trap '\''rm -f "$key_file" "$key_file.next"'\'' EXIT
  cat > "$key_file"
  ssh-keygen -lf "$key_file" >/dev/null
  key_blob=$(awk '\''{print $2}'\'' "$key_file")
  if [ -f /root/.ssh/authorized_keys ]; then
    cp -p /root/.ssh/authorized_keys "/root/.ssh/authorized_keys.before-monitoring.$(date +%Y%m%d%H%M%S)"
  else
    touch /root/.ssh/authorized_keys
  fi
  chmod 600 /root/.ssh/authorized_keys
  desired="$KEY_OPTIONS $(cat "$key_file")"
  if grep -qF "$key_blob" /root/.ssh/authorized_keys; then
    # Already present: replace the line, so an entry without the
    # restrictions is upgraded rather than left as it is.
    awk -v blob="$key_blob" -v line="$desired" \
      '\''index($0, blob) { print line; next } { print }'\'' \
      /root/.ssh/authorized_keys > "$key_file.next"
    cat "$key_file.next" > /root/.ssh/authorized_keys
    rm -f "$key_file.next"
  else
    # Leading newline: the last line may not be newline-terminated.
    printf "\n%s\n" "$desired" >> /root/.ssh/authorized_keys
  fi
' < "$temporary_dir/public-key"

ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes "$target" \
  'printf "PBS public-key login verified.\n"'
