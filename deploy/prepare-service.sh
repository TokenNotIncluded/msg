#!/bin/sh
# Run after interactive msgd init. This script never generates or unlocks a root key.
set -eu
[ "$(id -u)" = 0 ] || { echo 'OS administrator required' >&2; exit 1; }
for directory in /etc/msgd /etc/msgd/root /etc/msgd/service /var/lib/msgd; do
    [ -d "$directory" ] && [ ! -L "$directory" ] || { echo "Missing or symlinked directory: $directory" >&2; exit 1; }
done
[ -f /etc/msgd/server.toml ] || exit 1
/opt/msgd/venv/bin/python - <<'PY'
from pathlib import Path
from msg.config import load_settings
s=load_settings()
assert s.server.postgres_dsn, 'PostgreSQL DSN must be configured'
assert s.server.content_dir==Path('/var/lib/msgd/content')
assert s.server.staging_dir==Path('/var/lib/msgd/staging')
PY
if ! getent passwd msgd >/dev/null; then
    useradd --system --user-group --home-dir /var/lib/msgd --no-create-home --shell /bin/sh msgd
fi
chown root:root /etc/msgd
chmod 0755 /etc/msgd
chown -R root:root /etc/msgd/root
chmod 0700 /etc/msgd/root
find /etc/msgd/root -type f -exec chmod 0600 {} +
chown -R root:msgd /etc/msgd/service
chmod 0750 /etc/msgd/service
find /etc/msgd/service -type f -exec chmod 0640 {} +
chown -R root:root /etc/msgd/trust
chmod 0755 /etc/msgd/trust
find /etc/msgd/trust -type f -exec chmod 0644 {} +
chown root:msgd /etc/msgd/server.toml
chmod 0640 /etc/msgd/server.toml
chown -R msgd:msgd /var/lib/msgd
chmod 0750 /var/lib/msgd
install -d -m 0700 -o msgd -g msgd /var/lib/msgd/staging
printf '%s\n' 'Service permissions prepared. Review units before enabling them.'
