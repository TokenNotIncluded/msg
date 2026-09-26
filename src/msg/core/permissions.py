"""Mode helpers; complete authorization lives in security.authorization."""
from msg.security.policy import (
    CERTGATE,SETGID,STICKY,READ,WRITE,EXECUTE,allows,class_bits,
    format_mode,inherit_group,selected_class,
)

def has_certgate(mode):
    return bool(mode&CERTGATE)

def has_setgid(mode):
    return bool(mode&SETGID)

def has_sticky(mode):
    return bool(mode&STICKY)
