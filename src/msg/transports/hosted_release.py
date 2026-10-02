"""Release-owned executable hosting, independent of mutable resource metadata.

The coordinator packages the reviewed HTML at the path below and updates its
whole-file fingerprint in the same reviewed release. An absent or mismatched
package asset never grants script execution. This is deliberately one fixed
website, not an account-level or user-editable hosting capability.
"""

from functools import lru_cache
from importlib.resources import files

from msg.core.codec import digest

ASCII_SITE_ID = 'r_27e7106a06c8432187567cb506cf7f6f'
ASCII_FILE_PATH = 'index.html'
ASCII_ASSET = 'lightjunction-ascii.html'
# Whole-file fingerprint of the reviewed and packaged release.
ASCII_RELEASE_DIGEST = 'sha256:80c8d39141b7e60e921352809615c5485ddcbc1bbc09cdd07ba792fda0fc31b1'


@lru_cache(maxsize=1)
def ascii_release():
    """Return reviewed bytes or fail closed when packaging does not match."""
    if ASCII_RELEASE_DIGEST is None:
        return None
    try:
        body = files('msg.data').joinpath(ASCII_ASSET).read_bytes()
    except FileNotFoundError:
        return None
    return body if digest(body) == ASCII_RELEASE_DIGEST else None


def ascii_bundle(site_id, file_path, blob_digest):
    if (
        site_id != ASCII_SITE_ID
        or file_path != ASCII_FILE_PATH
        or blob_digest != ASCII_RELEASE_DIGEST
    ):
        return None
    return ascii_release()
