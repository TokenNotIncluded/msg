"""Server configuration and client configuration have separate roots."""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from msg.core.errors import require
from msg.core.models import MailConfig, ServerConfig, TransportLimits
from msg.security.age_keys import public_from_recipient,encryption_key_id


@dataclass(frozen=True,slots=True)
class RecoveryCustodian:
    id: str
    name: str
    recipient: str
    fingerprint: str
    description: str | None = None
    policy_ref: str | None = None


@dataclass(frozen=True,slots=True)
class MoneyConfig:
    enabled: bool = True
    currency_id: str = 'primary'
    display_name: str = 'MSG'
    code: str = 'MSG'
    scale: int = 6
    transfer_fee: int = 0
    allow_overdraft: bool = False


def root_private_dir(config_dir: Path) -> Path:
    """Root material is outside the network service configuration tree."""
    directory = Path(config_dir)
    if directory == Path('/etc/msgd'):
        return Path('/var/lib/msgd-root')
    return directory.parent / (directory.name + '-root')


def server_config_file(config_dir: Path) -> Path:
    """Prefer the current name, but keep an existing legacy install readable."""
    directory = Path(config_dir)
    current, legacy = directory/'msgd.toml', directory/'server.toml'
    require(not (current.exists() and legacy.exists()), 'ambiguous_server_configuration')
    return legacy if legacy.exists() else current


@dataclass(frozen=True,slots=True)
class Settings:
    server: ServerConfig
    service_url: str
    listen: str='127.0.0.1'
    port: int=8042
    public_web_origin: str | None=None
    temporary_ttl: int=3600
    credential_delivery_recovery_window: int=900
    transfer_ttl: int=86400
    base_certificate_ttl: int=2592000
    max_part_bytes: int=65536
    worker_isolation: str='bubblewrap'
    tool_timeout_ms: int=10000
    tool_max_response_bytes: int=4194304
    tool_methods: tuple[str,...]=('GET','HEAD')
    tool_ports: tuple[int,...]=(80,443)
    recovery_custodians: tuple[RecoveryCustodian,...]=()
    money: MoneyConfig=MoneyConfig()

    @property
    def config_dir(self):
        return self.server.config_dir

    @property
    def trust_file(self):
        return self.config_dir/'trust'/'root.json'

    @property
    def service_keys(self):
        return self.server.service_keys_dir

    @property
    def root_private_dir(self):
        return root_private_dir(self.config_dir)

    @property
    def repositories_dir(self):
        return self.server.repositories_dir


def load_settings(config_dir=Path('/etc/msgd')):
    config_dir=Path(config_dir)
    path=server_config_file(config_dir)
    require(path.is_file(),'configuration_missing')
    data=tomllib.loads(path.read_text())
    require(set(data)<={'server','storage','limits','plugins','tools','recovery','identity','money'},'unknown_configuration_section')
    identity=data.get('identity',{})
    require(isinstance(identity,dict) and set(identity)<={'credential_delivery_recovery_window'},
            'unknown_identity_configuration')
    window=identity.get('credential_delivery_recovery_window','15m')
    require(isinstance(window,str) and len(window)<=3 and
            re.fullmatch(r'[1-9][0-9]*m',window) is not None,
            'invalid_credential_delivery_recovery_window')
    window_minutes=int(window[:-1])
    require(1<=window_minutes<=60,'invalid_credential_delivery_recovery_window')
    money=data.get('money',{})
    require(isinstance(money,dict) and set(money)<=set(MoneyConfig.__dataclass_fields__),
            'unknown_money_configuration')
    fixed={'enabled':True,'currency_id':'primary','scale':6,
           'transfer_fee':0,'allow_overdraft':False}
    for name,expected in fixed.items():
        value=money.get(name,expected)
        require(type(value) is type(expected) and value==expected,
                'invalid_money_configuration')
    display_name=money.get('display_name','MSG')
    code=money.get('code','MSG')
    require(type(display_name) is str and 1<=len(display_name)<=80 and
            display_name==display_name.strip() and all(ord(char)>=32 for char in display_name),
            'invalid_money_display_name')
    require(type(code) is str and re.fullmatch(r'[A-Z][A-Z0-9]{1,15}',code) is not None,
            'invalid_money_code')
    server=data.get('server',{})
    require(set(server)<={'service_url','listen','port','public_web_origin','temporary_ttl','transfer_ttl'},
            'unknown_server_configuration')
    service=server.get('service_url','https://msg.lmm.best')
    require(isinstance(service,str),'invalid_service_url')
    service=service.rstrip('/')
    url=urlsplit(service)
    require(url.scheme in {'https','http'} and url.hostname and not url.username and not url.password and not url.query and not url.fragment and not url.path,
            'invalid_service_url')
    require(type(server.get('port',8042)) is int and 1<=server.get('port',8042)<=65535,'invalid_listen_port')
    require(isinstance(server.get('listen','127.0.0.1'),str) and server.get('listen','127.0.0.1'),'invalid_listen_address')
    for key,default in (('temporary_ttl',3600),('transfer_ttl',86400)):
        require(type(server.get(key,default)) is int and server.get(key,default)>0,'invalid_ttl')
    store=data.get('storage',{})
    require(set(store)<={'postgres_dsn','valkey_url','content','repositories','blobs','staging','service_keys'},
            'unknown_storage_configuration')
    postgres_dsn=store.get('postgres_dsn')
    require(isinstance(postgres_dsn,str) and bool(postgres_dsn.strip()) and not any(ord(c)<32 for c in postgres_dsn),
            'invalid_postgres_dsn')
    # libpq service files keep credentials outside the service configuration.
    if postgres_dsn.startswith(('postgresql://','postgres://')):
        try:
            pg_url=urlsplit(postgres_dsn)
            require(bool(pg_url.hostname) and not pg_url.fragment,'invalid_postgres_dsn')
        except ValueError:
            require(False,'invalid_postgres_dsn')
    else:
        require(postgres_dsn.startswith('service=') and len(postgres_dsn.split())==1 and
                len(postgres_dsn)>len('service='),'invalid_postgres_dsn')
    valkey_url=store.get('valkey_url')
    if valkey_url is not None:
        require(isinstance(valkey_url,str),'invalid_valkey_url')
        try:
            cache_url=urlsplit(valkey_url)
            require(cache_url.scheme in {'redis','rediss','unix'} and
                    (bool(cache_url.hostname) if cache_url.scheme!='unix' else bool(cache_url.path)) and
                    not cache_url.fragment,'invalid_valkey_url')
        except ValueError:
            require(False,'invalid_valkey_url')
    content_dir=Path(store.get('content','/var/lib/msgd/git/content'))
    # Existing installations with an explicit old content path keep their old
    # sibling repository and embedded blob locations until an operator migrates.
    modern_layout=content_dir.name=='content' and content_dir.parent.name=='git'
    repository_default=(content_dir.parent/'repos' if modern_layout else
                        content_dir.parent/'repositories')
    blob_default=(content_dir.parent.parent/'blobs'/'sha256' if modern_layout else
                  content_dir/'binary')
    service_keys_default=(config_dir/'service' if (config_dir/'service').exists() and
                          not modern_layout else
                          (content_dir.parent.parent if modern_layout else content_dir.parent)/'service')
    require(all(isinstance(store.get(key,str(default)),str) and
                Path(store.get(key,str(default))).is_absolute()
                for key,default in (('content',content_dir),('repositories',repository_default),
                                    ('blobs',blob_default),('staging',Path('/var/lib/msgd/transfers/staging')),
                                    ('service_keys',service_keys_default))),
            'storage_paths_must_be_absolute')
    limits=data.get('limits',{})
    require(set(limits)<={'request_bytes','response_bytes','path_bytes','part_bytes'},'unknown_limit')
    for value in limits.values():
        require(type(value) is int and value>=256,'invalid_limit')
    mail_file=config_dir/'mail.toml'
    mail=None
    if mail_file.exists():
        raw=tomllib.loads(mail_file.read_text())
        require(set(raw)<={'enabled','host','port','tls','sender','credential_file'},'unknown_mail_configuration')
        if raw.get('enabled',False):
            require(raw.get('tls') in {'starttls','tls'},'mail_tls_required')
            mail=MailConfig(enabled=True,host=raw['host'],port=raw.get('port',587),tls=raw['tls'],sender=raw['sender'],
                credential_file=Path(raw['credential_file']) if raw.get('credential_file') else None)
    recovery=data.get('recovery',{})
    require(isinstance(recovery,dict) and set(recovery)<={'custodians'},'unknown_recovery_configuration')
    raw_custodians=recovery.get('custodians',[])
    require(isinstance(raw_custodians,list) and len(raw_custodians)<=16,'invalid_recovery_custodians')
    custodians=[]
    for item in raw_custodians:
        require(isinstance(item,dict) and set(item)<={'id','name','recipient','description','policy_ref'},
                'unknown_recovery_custodian_field')
        require(all(name in item for name in ('id','name','recipient')),
                'invalid_recovery_custodian')
        require(type(item['id']) is str and re.fullmatch(r'[a-z][a-z0-9-]{1,63}',item['id']) is not None and
                type(item['name']) is str and 1<=len(item['name'])<=100,
                'invalid_recovery_custodian')
        require(all(value is None or (type(value) is str and len(value)<=500)
                    for value in (item.get('description'),item.get('policy_ref'))),
                'invalid_recovery_custodian')
        public=public_from_recipient(item['recipient'])
        custodians.append(RecoveryCustodian(id=item['id'],name=item['name'],
            recipient=item['recipient'],fingerprint=encryption_key_id(public),
            description=item.get('description'),policy_ref=item.get('policy_ref')))
    require(len({item.id for item in custodians})==len(custodians),'duplicate_recovery_custodian')
    require(set(data.get('plugins',{}))<={'enabled'},'unknown_plugin_configuration')
    plugins=tuple(data.get('plugins',{}).get('enabled',('identity','content','discussion','communication','discovery','achievements','recovery','sharing','money','offers','store','bounty','orders','delivery','transfer','extensions','system','batch')))
    require(all(isinstance(name,str) for name in plugins) and len(set(plugins))==len(plugins),'invalid_plugin_list')
    require('identity' in plugins,'identity_plugin_required')
    tools=data.get('tools',{})
    require(set(tools)<={'isolation','timeout_ms','max_response_bytes','methods','ports'},'unknown_tool_configuration')
    require(tools.get('isolation','bubblewrap')=='bubblewrap','unsafe_tool_worker')
    for name,default in (('timeout_ms',10000),('max_response_bytes',4194304)):
        require(type(tools.get(name,default)) is int and tools.get(name,default)>0,'invalid_tool_limit')
    require(isinstance(tools.get('methods',[]),(list,tuple)) and all(m in {'GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'} for m in tools.get('methods',[])),'invalid_tool_methods')
    require(isinstance(tools.get('ports',[]),(list,tuple)) and all(type(p) is int and 1<=p<=65535 for p in tools.get('ports',[])),'invalid_tool_ports')
    public_web=server.get('public_web_origin')
    if public_web is not None:
        require(isinstance(public_web,str),'invalid_hosting_origin')
        hosted=urlsplit(public_web)
        require(hosted.scheme in {'https','http'} and hosted.hostname and not hosted.username and not hosted.password and not hosted.path and not hosted.query and not hosted.fragment,'invalid_hosting_origin')
    require(public_web is None or public_web.rstrip('/')==service.rstrip('/'),
            'hosting_origin_must_match_service')
    return Settings(server=ServerConfig(config_dir=config_dir,
        postgres_dsn=postgres_dsn,valkey_url=valkey_url,
        content_dir=content_dir,
        repositories_dir=Path(store.get('repositories',repository_default)),
        blob_dir=Path(store.get('blobs',blob_default)),
        staging_dir=Path(store.get('staging','/var/lib/msgd/transfers/staging')),plugins=plugins,
        service_keys_dir=Path(store.get('service_keys',service_keys_default)),
        limits=TransportLimits(max_request_bytes=limits.get('request_bytes',1048576),
            max_response_bytes=limits.get('response_bytes',1048576),max_path_bytes=limits.get('path_bytes',8192),
            encodings=frozenset({'j','gz'})),mail=mail),service_url=service,listen=server.get('listen','127.0.0.1'),
        port=server.get('port',8042),public_web_origin=public_web,
        temporary_ttl=server.get('temporary_ttl',3600),transfer_ttl=server.get('transfer_ttl',86400),
        credential_delivery_recovery_window=window_minutes*60,
        max_part_bytes=limits.get('part_bytes',65536),tool_timeout_ms=tools.get('timeout_ms',10000),
        tool_max_response_bytes=tools.get('max_response_bytes',4194304),
        tool_methods=tuple(tools.get('methods',('GET','HEAD'))),tool_ports=tuple(tools.get('ports',(80,443))),
        recovery_custodians=tuple(custodians),
        money=MoneyConfig(display_name=display_name,code=code))


def write_example(config_dir,data_dir,service_url='https://msg.lmm.best',*,postgres_dsn='service=msgd',valkey_url=None):
    """Local install helper: writes no private key or default PIN."""
    config_dir,data_dir=Path(config_dir),Path(data_dir)
    config_dir.mkdir(parents=True,exist_ok=True)
    path=server_config_file(config_dir)
    if not path.exists():
        path.write_text(f'''[server]
service_url = {json.dumps(service_url)}
listen = "127.0.0.1"
port = 8042

[identity]
credential_delivery_recovery_window = "15m"

[money]
enabled = true
currency_id = "primary"
display_name = "MSG"
code = "MSG"
scale = 6
transfer_fee = 0
allow_overdraft = false

[storage]
postgres_dsn = {json.dumps(postgres_dsn)}
{f'valkey_url = {json.dumps(valkey_url)}' if valkey_url is not None else '# valkey_url = "redis://127.0.0.1:6379/0"'}
content = {json.dumps(str(data_dir/"git"/"content"))}
repositories = {json.dumps(str(data_dir/"git"/"repos"))}
blobs = {json.dumps(str(data_dir/"blobs"/"sha256"))}
staging = {json.dumps(str(data_dir/"transfers"/"staging"))}
service_keys = {json.dumps(str(data_dir/"service"))}

[limits]
request_bytes = 1048576
response_bytes = 1048576
path_bytes = 8192
part_bytes = 65536

[tools]
isolation = "bubblewrap"
methods = ["GET", "HEAD"]
ports = [80, 443]
''')
    return load_settings(config_dir)
