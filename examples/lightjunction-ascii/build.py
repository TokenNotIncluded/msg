"""Build the standalone reviewed runtime and print its exact script CSP pin."""

import base64
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parent
source = (
    ROOT.joinpath('world_geometry.js').read_text() + '\n' + ROOT.joinpath('scene.js').read_text()
)
html = ROOT.joinpath('template.html').read_text().replace('__SCRIPT__', source)
ROOT.joinpath('index.html').write_text(html)
inline = '\n' + source + '\n'
report = {
    'html_sha256': hashlib.sha256(html.encode()).hexdigest(),
    'bytes': len(html.encode()),
    'script_sha256': base64.b64encode(hashlib.sha256(inline.encode()).digest()).decode(),
    'csp': "sandbox allow-scripts allow-pointer-lock; default-src 'none'; script-src 'sha256-"
    + base64.b64encode(hashlib.sha256(inline.encode()).digest()).decode()
    + "'; script-src-attr 'none'; style-src 'unsafe-inline'; font-src data:; img-src data:; connect-src 'none'; frame-src 'none'; object-src 'none'; worker-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}
print(json.dumps(report, indent=2))
