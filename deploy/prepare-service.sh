#!/bin/sh
# Run after interactive msgd init. This script never generates or unlocks a root key.
set -eu
[ "$(id -u)" = 0 ] || { echo 'OS administrator required' >&2; exit 1; }
for directory in /etc/msgd /etc/msgd/trust /var/lib/msgd /var/lib/msgd-root /var/lib/msgd/service; do
    [ -d "$directory" ] && [ ! -L "$directory" ] || { echo "Missing or symlinked directory: $directory" >&2; exit 1; }
done
config_file=/etc/msgd/msgd.toml
if [ ! -f "$config_file" ]; then config_file=/etc/msgd/server.toml; fi
[ -f "$config_file" ] || exit 1
/opt/msgd/venv/bin/python - <<'PY'
from pathlib import Path
from msg.config import load_settings
s=load_settings()
assert s.server.postgres_dsn, 'PostgreSQL DSN must be configured'
assert s.server.content_dir==Path('/var/lib/msgd/git/content')
assert s.server.repositories_dir==Path('/var/lib/msgd/git/repos')
assert s.server.blob_dir==Path('/var/lib/msgd/blobs/sha256')
assert s.server.staging_dir==Path('/var/lib/msgd/transfers/staging')
assert s.server.service_keys_dir==Path('/var/lib/msgd/service')
assert s.root_private_dir==Path('/var/lib/msgd-root')
PY
if ! getent passwd msgd >/dev/null; then
    useradd --system --user-group --home-dir /var/lib/msgd --no-create-home --shell /bin/sh msgd
fi
chown root:root /etc/msgd
chmod 0755 /etc/msgd
chown -R msgd:msgd /var/lib/msgd
chmod 0750 /var/lib/msgd
chown -R root:root /var/lib/msgd-root
chmod 0700 /var/lib/msgd-root
find /var/lib/msgd-root -type f -exec chmod 0600 {} +
chown -R root:msgd /var/lib/msgd/service
chmod 0750 /var/lib/msgd/service
find /var/lib/msgd/service -type f -exec chmod 0640 {} +
chown -R root:root /etc/msgd/trust
chmod 0755 /etc/msgd/trust
find /etc/msgd/trust -type f -exec chmod 0644 {} +
chown root:msgd "$config_file"
chmod 0640 "$config_file"
for directory in /var/lib/msgd/git/content /var/lib/msgd/git/repos /var/lib/msgd/blobs/sha256 /var/lib/msgd/transfers/staging; do
    install -d -m 0700 -o msgd -g msgd "$directory"
done
install -d -m 0750 -o msgd -g msgd /var/cache/msgd
install -d -m 0750 -o msgd -g msgd /run/msgd
printf '%s\n' 'Service permissions prepared. Review units before enabling them.'
