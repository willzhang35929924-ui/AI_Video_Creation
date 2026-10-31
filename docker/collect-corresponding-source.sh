#!/bin/sh
# Build-time Debian source archives for the exact distributed FFmpeg/x264 build.
# Other dependencies retain their own notices; this is not a blanket license grant.
set -eu
destination=/usr/share/video-factory/corresponding-source
mkdir -p "$destination"

# The pinned Debian bookworm Python base uses deb822 source definitions. Clone
# each configured binary repository to a source-only entry using the same mirror,
# suites, components and signing key (including security updates).
python - <<'PY'
from pathlib import Path
source_dir = Path('/etc/apt/sources.list.d')
blocks = []
for path in sorted(source_dir.glob('*.sources')):
    if path.name == 'video-factory-sources.sources':
        continue
    for block in path.read_text().split('\n\n'):
        lines = block.splitlines()
        for index, line in enumerate(lines):
            if line.startswith('Types:') and 'deb' in line.split(':', 1)[1].split():
                lines[index] = 'Types: deb-src'
                blocks.append('\n'.join(lines))
                break
if not blocks:
    raise SystemExit('No supported Debian source repositories; cannot bundle matching FFmpeg/x264 sources.')
(source_dir / 'video-factory-sources.sources').write_text('\n\n'.join(blocks) + '\n')
PY
apt-get update
ffmpeg_version="$(dpkg-query -W -f='${source:Version}' ffmpeg)"
x264_version="$(dpkg-query -W -f='${db:Status-Status}\t${source:Version}\n' 'libx264-*' | awk '$1=="installed" {print $2; exit}')"
[ -n "$ffmpeg_version" ] && [ -n "$x264_version" ] || {
    echo 'Cannot resolve installed FFmpeg/x264 source versions.' >&2
    exit 1
}
cd "$destination"
apt-get source --download-only "ffmpeg=$ffmpeg_version" "x264=$x264_version"
sha256sum ./*.dsc ./*.tar.* > SHA256SUMS
dpkg-query -W -f='${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n' > installed-debian-packages.tsv
ffmpeg -version > ffmpeg-version.txt 2>&1
ffmpeg -buildconf > ffmpeg-buildconf.txt 2>&1
printf 'ffmpeg\t%s\nx264\t%s\n' "$ffmpeg_version" "$x264_version" > bundled-source-versions.tsv
cat > README.txt <<'NOTICE'
This directory contains Debian source archives matching the installed FFmpeg
and x264 source package versions. SHA256SUMS verifies the .dsc and source tar
archives. The Debian patch series and package build rules are in the Debian
source tar archives; original upstream license texts are in the source archives.
Installed-package copyright/license notices are under /usr/share/doc, including
/usr/share/doc/ffmpeg/copyright and the installed libx264 package's copyright.
Source repository origins: /etc/apt/sources.list.d/*.sources.
The application and other packages retain their respective license terms.
This inventory does not declare every third-party dependency to use one license.
NOTICE
