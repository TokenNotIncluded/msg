"""Opaque position cursors are not authentication credentials."""
import hashlib
import hmac
from msg.core.codec import b64,unb64,canonical,loads
from msg.core.errors import require


class CursorCodec:
    def __init__(self,key):
        self.key=key

    def encode(self,kind,query,position):
        data=canonical({'v':1,'kind':kind,'query':query,'position':position})
        tag=hmac.digest(self.key,b'cursor-v1\0'+data,'sha256')
        return b64(data)+'.'+b64(tag)

    def decode(self,value,kind,query):
        require(isinstance(value,str) and len(value)<=8192 and value.count('.')==1,'invalid_cursor')
        encoded,signature=value.split('.')
        raw=unb64(encoded)
        require(hmac.compare_digest(hmac.digest(self.key,b'cursor-v1\0'+raw,'sha256'),unb64(signature)),'invalid_cursor')
        data=loads(raw)
        require(data.get('v')==1 and data.get('kind')==kind and data.get('query')==query,'cursor_query_mismatch')
        return data['position']
