"""Select signed Debian repositories; optional mirrors never disable TLS/GPG."""
import os
from pathlib import Path
from urllib.parse import urlsplit


def mirror(name, default):
    value=os.environ.get(name,default).rstrip('/')
    parsed=urlsplit(value)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SystemExit(f'{name} must be a public HTTPS repository URL without credentials or query parameters')
    return value


normal=mirror('DEBIAN_MIRROR','https://deb.debian.org/debian')
security=mirror('DEBIAN_SECURITY_MIRROR','https://deb.debian.org/debian-security')
path=Path('/etc/apt/sources.list.d/debian.sources')
mapping={f'{scheme}://deb.debian.org/{name}':value
    for scheme in ('http','https') for name,value in [('debian',normal),('debian-security',security)]}
lines=[]
for line in path.read_text(encoding='utf-8').splitlines():
    if line.startswith('URIs:'):
        line='URIs: '+' '.join(mapping.get(url.rstrip('/'),url) for url in line.split(':',1)[1].split())
    lines.append(line)
# Keep Suites, Signed-By and Components, and choose security independently.
path.write_text('\n'.join(lines)+'\n',encoding='utf-8')
Path('/etc/apt/apt.conf.d/80video-factory-network').write_text(
    'Acquire::Retries "3";\nAcquire::http::Timeout "45";\nAcquire::https::Timeout "45";\n',encoding='ascii')
