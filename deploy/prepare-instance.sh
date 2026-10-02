#!/bin/sh
# Prepare a NEW named instance after init. Never migrate or unlock Root material.
set -eu
[ "$(id -u)" = 0 ] || { echo 'OS administrator required' >&2; exit 1; }
[ "$#" = 1 ] || { echo 'Usage: prepare-instance.sh INSTANCE' >&2; exit 1; }
instance=$1
# Validate before interpolating the name into any path or account.
case "$instance" in ''|[!a-z]*|*[!a-z0-9_-]*) echo 'Invalid instance name' >&2; exit 1;; esac
[ "${#instance}" -le 26 ] || exit 1
config=/etc/msgd/$instance
data=/var/lib/msgd/$instance
private=/var/lib/private/msgd/$instance/root
account=msgd-$instance
# A valid instance name can overlap an older installation's private Root
# directory. Reject known artifacts before creating users or changing modes.
for directory in "$config" "$config/root"; do
    for name in key.json initialization.pending rotation.pending.json history .rotation.lock; do
        artifact=$directory/$name
        [ ! -e "$artifact" ] && [ ! -L "$artifact" ] || {
            echo 'Root private artifacts in instance configuration; refusing ownership changes.' >&2
            exit 1
        }
    done
done
# Refuse mixed legacy/named data; changing ownership there can break a live service.
for legacy in /etc/msgd/msgd.toml /etc/msgd/server.toml /var/lib/msgd/git /var/lib/msgd/service; do
    [ ! -e "$legacy" ] || { echo 'Migrate the legacy installation offline first.' >&2; exit 1; }
done
for directory in /etc/msgd /var/lib/msgd /var/lib/private /var/lib/private/msgd /var/lib/private/msgd/$instance "$config" "$data" "$private"; do
    [ -d "$directory" ] && [ ! -L "$directory" ] || { echo "Missing or symlinked directory: $directory" >&2; exit 1; }
done
for tree in "$config" "$data" "$private"; do
    [ -z "$(find "$tree" -type l -print -quit)" ] || { echo "Symlink in installation: $tree" >&2; exit 1; }
done
# No alias/config/database guessing: validate the installation selected by its name.
/usr/lib/msgd/python3.15/bin/python3.15 -I - "$instance" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, '/usr/lib/msgd/site-packages')
from msg.config import load_settings
from msg.paths import ServerPaths
p=ServerPaths.for_instance(sys.argv[1])
s=load_settings(p.config)
assert s.server.content_dir==p.data/'git/content'
assert s.server.repositories_dir==p.data/'git/repos'
assert s.server.blob_dir==p.data/'blobs/sha256'
assert s.server.staging_dir==p.data/'transfers/staging'
assert s.server.service_keys_dir==p.data/'service'
assert s.root_private_dir==p.root
PY
if ! getent passwd "$account" >/dev/null; then
    useradd --system --user-group --home-dir "$data" --no-create-home --shell /usr/bin/nologin "$account"
fi
install -d -m 0755 -o root -g root /var/cache/msgd /run/msgd
# Shared parent directories cannot be renamed by a service account.
chown root:root /etc/msgd /var/lib/msgd /var/cache/msgd /run/msgd
chmod 0755 /etc/msgd /var/lib/msgd /var/cache/msgd /run/msgd
chown root:root /var/lib/private/msgd /var/lib/private/msgd/$instance
chmod 0700 /var/lib/private/msgd /var/lib/private/msgd/$instance
chown -R root:root "$private"
chmod 0700 "$private"
find "$private" -type f -exec chmod 0600 {} +
chown -R "$account:$account" "$data"
chmod 0750 "$data"
chown -R "root:$account" "$data/service"
chmod 0750 "$data/service"
find "$data/service" -type f -exec chmod 0640 {} +
chown -R "root:$account" "$config"
chmod 0750 "$config"
find "$config" -type f -exec chmod 0640 {} +
install -d -m 0750 -o "$account" -g "$account" /var/cache/msgd/$instance /run/msgd/$instance
printf '%s\n' 'Prepared instance. Configure unique listen ports and database before enabling its units.'
