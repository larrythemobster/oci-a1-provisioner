#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if (( EUID != 0 )); then
    echo 'Run this installer as root.' >&2
    exit 1
fi

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
target=/opt/oci-a1-provisioner
config=/etc/oci-a1-provisioner
state=/var/lib/oci-a1-provisioner
logs=/var/log/oci-a1-provisioner
unit=/etc/systemd/system/oci-a1-provisioner.service
status_command=/usr/local/bin/oci-a1-status
service=oci-a1-provisioner.service
account=oci-a1-provisioner
python=${PYTHON:-python3}

for command in systemctl systemd-analyze install getent id useradd groupadd mktemp realpath ln chmod; do
    command -v "$command" >/dev/null || { echo "Missing prerequisite: $command" >&2; exit 1; }
done
if [[ $python != python3 && $python != /* ]]; then
    echo 'PYTHON must be an absolute interpreter path.' >&2
    exit 1
fi
command -v "$python" >/dev/null || { echo "Python interpreter not found: $python" >&2; exit 1; }
if [[ $(realpath -m "$source_dir") == $(realpath -m "$target") ]]; then
    echo 'Run the installer from a source directory outside the deployed target.' >&2
    exit 1
fi
for file in provisioner.py oci-a1-status requirements.txt systemd/oci-a1-provisioner.service; do
    [[ -f $source_dir/$file ]] || { echo "Missing source file: $file" >&2; exit 1; }
done

install -d -o root -g root -m 0700 "$config"
for name in oci-config oci-api-key.pem ssh-public-key.pub; do
    file=$config/$name
    if [[ ! -f $file || -L $file ]]; then
        echo "Provision the regular file $file before installation." >&2
        exit 1
    fi
    chown root:root "$file"
    chmod 0600 "$file"
done
if [[ ! -e $config/discord-webhook && ! -L $config/discord-webhook ]]; then
    install -o root -g root -m 0600 /dev/null "$config/discord-webhook"
fi
if [[ ! -f $config/discord-webhook || -L $config/discord-webhook ]]; then
    echo "Webhook credential must be a regular file: $config/discord-webhook" >&2
    exit 1
fi
chown root:root "$config/discord-webhook"
chmod 0600 "$config/discord-webhook"

getent group "$account" >/dev/null || groupadd --system "$account"
id "$account" >/dev/null 2>&1 || useradd --system --gid "$account" --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin "$account"
install -d -o root -g root -m 0755 "$target"
install -d -o "$account" -g "$account" -m 0700 "$state" "$logs"

staged=$(mktemp -d "$target/.venv-XXXXXXXX")
backup=''
stopped=0
was_active=0
was_enabled=0
cleanup() {
    rc=$?
    trap - EXIT
    if (( rc != 0 && stopped )); then
        echo "Installation failed; restoring previous service files from $backup" >&2
        systemctl stop "$service" >/dev/null 2>&1 || true
        for entry in provisioner.py oci-a1-status; do
            if [[ -e $backup/$entry ]]; then
                cp -a -- "$backup/$entry" "$target/$entry" || true
            else
                rm -f -- "$target/$entry"
            fi
        done
        if [[ -e $backup/service ]]; then
            cp -a -- "$backup/service" "$unit" || true
        else
            rm -f -- "$unit"
        fi
        if [[ -e $backup/status-command || -L $backup/status-command ]]; then
            cp -a --remove-destination -- "$backup/status-command" "$status_command" || true
        else
            rm -f -- "$status_command"
        fi
        if [[ -d $backup/venv ]]; then
            rm -rf -- "$target/.venv"
            mv -- "$backup/venv" "$target/.venv" || true
        elif [[ -f $backup/no-venv ]]; then
            rm -rf -- "$target/.venv"
        fi
        systemctl daemon-reload || true
        if (( ! was_enabled )); then systemctl disable "$service" >/dev/null 2>&1 || true; fi
        if (( was_active )); then systemctl start "$service" || true; fi
        echo 'Runtime state was not rolled back.' >&2
    fi
    if (( rc != 0 )); then rm -rf -- "$staged"; fi
    exit "$rc"
}
trap cleanup EXIT
"$python" -m venv "$staged"
"$staged/bin/python" -m pip install -r "$source_dir/requirements.txt"
chmod -R a+rX "$staged"

install -d -o root -g root -m 0700 /var/backups/oci-a1-provisioner
backup=$(mktemp -d /var/backups/oci-a1-provisioner/"$(date -u +%Y%m%dT%H%M%SZ)".XXXXXXXX)
for entry in provisioner.py oci-a1-status; do
    if [[ -e $target/$entry ]]; then cp -a -- "$target/$entry" "$backup/$entry"; fi
done
if [[ -e $unit ]]; then cp -a -- "$unit" "$backup/service"; fi
if [[ -e $status_command || -L $status_command ]]; then
    cp -a -- "$status_command" "$backup/status-command"
fi
systemctl is-active --quiet "$service" && was_active=1
systemctl is-enabled --quiet "$service" && was_enabled=1
# Mark first so a failed stop also attempts to restore the original service.
stopped=1
systemctl stop "$service"
if [[ -e $target/state.json ]]; then cp -a -- "$target/state.json" "$backup/legacy-state.json"; fi
if [[ -e $state/state.json ]]; then cp -a -- "$state/state.json" "$backup/state.json"; fi
if [[ ! -e $state/state.json && -f $target/state.json ]]; then
    install -o "$account" -g "$account" -m 0600 "$target/state.json" "$state/state.json"
fi
if [[ -e $state/state.json ]]; then chown "$account:$account" "$state/state.json"; chmod 0600 "$state/state.json"; fi
if [[ -e $target/.venv ]]; then
    mv -- "$target/.venv" "$backup/venv"
else
    touch "$backup/no-venv"
fi
ln -s -- "$staged" "$target/.venv"
install -o root -g root -m 0755 "$source_dir/provisioner.py" "$target/provisioner.py"
install -o root -g root -m 0755 "$source_dir/oci-a1-status" "$target/oci-a1-status"
install -o root -g root -m 0755 "$source_dir/oci-a1-status" "$status_command"
install -o root -g root -m 0644 "$source_dir/systemd/oci-a1-provisioner.service" "$unit"
systemd-analyze verify "$unit"
systemctl daemon-reload
systemctl enable "$service"
systemctl restart "$service"
systemctl is-active --quiet "$service"
stopped=0
echo "Installation complete. Previous files and state are in $backup."
