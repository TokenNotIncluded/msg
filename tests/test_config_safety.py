from pathlib import Path
import pytest
from msg.config import write_example, load_settings
from msg.core.errors import Failure

@pytest.mark.parametrize('line',[
 'port = 0','port = true','temporary_ttl = -1','transfer_ttl = 0',
 'service_url = "https://example.org/path"',
 'public_web_origin = "javascript:alert(1)"',
 'public_web_origin = "https://msg.lmm.best"',
])
def test_reject_ambiguous_or_invalid_server_configuration(tmp_path,line):
    directory=tmp_path/'config'
    write_example(directory,tmp_path/'data')
    text=(directory/'server.toml').read_text()
    key=line.split('=')[0].strip()
    text='\n'.join(row for row in text.splitlines() if not row.startswith(key+' ='))
    text=text.replace('[server]','[server]\n'+line)
    (directory/'server.toml').write_text(text)
    with pytest.raises(Failure):load_settings(directory)

def test_example_quotes_filesystem_paths_and_unknown_plugin_keys_are_rejected(tmp_path):
    settings=write_example(tmp_path/'config',tmp_path/'with"quote')
    assert settings.server.content_dir==tmp_path/'with"quote'/'content'
    with (tmp_path/'config'/'server.toml').open('a') as f:f.write('\n[plugins]\nload_arbitrary_code=true\n')
    with pytest.raises(Failure):load_settings(tmp_path/'config')
