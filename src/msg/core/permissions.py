"""Mode helpers; complete authorization lives in security.authorization."""

from msg.security.policy import (
    CERTGATE as CERTGATE,
    EXECUTE as EXECUTE,
    READ as READ,
    SETGID as SETGID,
    STICKY as STICKY,
    WRITE as WRITE,
    allows as allows,
    class_bits as class_bits,
    format_mode as format_mode,
    inherit_group as inherit_group,
    selected_class as selected_class,
)


def has_certgate(mode):
    return bool(mode & CERTGATE)


def has_setgid(mode):
    return bool(mode & SETGID)


def has_sticky(mode):
    return bool(mode & STICKY)
