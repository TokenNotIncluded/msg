"""Opaque position cursors are not authentication credentials."""
import hmac
from msg.core.codec import b64,unb64,canonical,loads,digest,parse_time,wire
from msg.core.errors import require


class CursorCodec:
    def __init__(self,key):
        self.key=key

    def encode(self,kind,query,position):
        data=canonical({'v':1,'kind':kind,'query':query,'position':position})
        tag=hmac.digest(self.key,b'cursor-v1\0'+data,'sha256')
        return b64(data)+'.'+b64(tag)

    def inspect(self,value):
        require(isinstance(value,str) and len(value)<=8192 and value.count('.')==1,'invalid_cursor')
        encoded,signature=value.split('.')
        raw=unb64(encoded)
        require(hmac.compare_digest(hmac.digest(self.key,b'cursor-v1\0'+raw,'sha256'),unb64(signature)),'invalid_cursor')
        data=loads(raw)
        require(data.get('v')==1,'invalid_cursor')
        return data

    def decode(self,value,kind,query):
        data=self.inspect(value)
        require(data.get('v')==1 and data.get('kind')==kind and data.get('query')==query,'cursor_query_mismatch')
        return data['position']

    def encode_page(self,operation,arguments,position,snapshot,principal,expires_at):
        query={'operation':operation,'arguments':arguments}
        return self.encode('read-page',query,{'last':position,'snapshot':wire(snapshot),
            'principal':principal,'expires_at':wire(expires_at),'query_digest':digest(query)})

    def inspect_page(self,value,now):
        data=self.inspect(value)
        require(data.get('kind')=='read-page','cursor_kind_mismatch')
        position=data.get('position')
        require(isinstance(position,dict) and position.get('query_digest')==digest(data.get('query')),
                'invalid_cursor')
        require(now<parse_time(position['expires_at']),'cursor_expired')
        return data['query'],position

    def decode_page(self,value,operation,arguments,principal,now):
        query,position=self.inspect_page(value,now)
        require(query=={'operation':operation,'arguments':arguments},'cursor_query_mismatch')
        require(position['principal']==principal,'cursor_principal_mismatch')
        return position['last'],parse_time(position['snapshot'])
