#!/bin/sh
# Explicit offline OS-administrator preparation, never called by hosting.load().
# Stop writers first. This changes only OS identities and content permissions.
set -eu
[ "$(id -u)" = 0 ] || { echo 'OS administrator required' >&2; exit 1; }
if systemctl is-active --quiet msgd.service || systemctl is-active --quiet msgd-worker.service; then
    echo 'Stop all writers, including any non-systemd instances, before preparation.' >&2
    exit 1
fi
getent passwd msgd >/dev/null || { echo 'Prepare the write service first.' >&2; exit 1; }
for directory in /var/lib/msgd /var/lib/msgd/git /var/lib/msgd/blobs /var/lib/msgd/git/content /var/lib/msgd/blobs/sha256; do
    [ -d "$directory" ] && [ ! -L "$directory" ] || { echo "Missing or symlinked directory: $directory" >&2; exit 1; }
done
for directory in /var/lib/msgd/git/content /var/lib/msgd/blobs/sha256; do
    [ -z "$(find -P "$directory" -type l -print -quit)" ] || { echo 'Content symlinks require operator review.' >&2; exit 1; }
done
getent group msgd-content-read >/dev/null || groupadd --system msgd-content-read
if ! getent passwd msgd-hosting >/dev/null; then
    useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin msgd-hosting
fi
usermod -a -G msgd-content-read msgd
usermod -a -G msgd-content-read msgd-hosting
for directory in /var/lib/msgd /var/lib/msgd/git /var/lib/msgd/blobs; do
    chgrp msgd-content-read "$directory"
    chmod 0710 "$directory"
done
for directory in /var/lib/msgd/git/content /var/lib/msgd/blobs/sha256; do
    chgrp -R msgd-content-read "$directory"
    find "$directory" -type d -exec chmod 2750 {} +
    find "$directory" -type f -exec chmod u+rw,g+r,g-w,o-rwx {} +
done
git -c safe.directory=/var/lib/msgd/git/content/private.git --git-dir=/var/lib/msgd/git/content/private.git config core.sharedRepository 0640
install -d -o root -g msgd-hosting -m 0750 /etc/msgd-hosting
install -d -o root -g msgd-hosting -m 0750 /etc/msgd-hosting/trust
[ -e /etc/msgd/trust/root.json ] || { echo 'Public trust is missing.' >&2; exit 1; }
[ -e /etc/msgd-hosting/trust/root.json ] || ln -s /etc/msgd/trust/root.json /etc/msgd-hosting/trust/root.json
printf '%s\n' 'Now enable hosting.content_group_read=true in the WRITER config and review the separate reader config/DB role.'
printf '%s\n' 'No configuration secrets were copied, no service started, and no database permissions changed.'
