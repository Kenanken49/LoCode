#!/usr/bin/env bash
# Provision a headless Chromium for LOCODE UI checks, without root.
#
# Why this exists: the hosted browser runner dies on Node 26 (it strips
# TypeScript with mode "transform", which Node 26 rejects), and this container
# has no Chromium, no libnss3/libnspr4 and no fontconfig database. So:
#   1. @sparticuz/chromium ships a Chromium that bundles all but three libraries
#   2. those three (libnspr4, libnss3 + co.) come from Ubuntu's main archive,
#      unpacked into a private sysroot instead of installed system-wide
#   3. DejaVu + a minimal fonts.conf, because a missing font database is a FATAL
#      Skia abort ("SkFontMgr_FontConfigInterface ... Not implemented")
#
# Idempotent: re-running reuses everything already in /tmp/locode-playwright.
set -euo pipefail

PT=/tmp/locode-playwright
SYSROOT=$PT/sysroot
ARCHIVE=http://archive.ubuntu.com/ubuntu
mkdir -p "$PT/debs" "$SYSROOT" /tmp/locode-playwright/fontcache

echo "== npm packages =="
cd "$PT"
[ -f package.json ] || npm init -y > /dev/null
npm i --no-audit --no-fund playwright-core @sparticuz/chromium > /dev/null
echo "playwright-core $(node -p "require('playwright-core/package.json').version")"

echo "== chromium binary =="
node -e 'import("@sparticuz/chromium").then(async (m) => console.log(await (m.default || m).executablePath()))' > "$PT/chrome-path.txt"
CHROME=$(tail -1 "$PT/chrome-path.txt")
echo "chromium: $CHROME"

echo "== shared libraries + fonts =="
if [ ! -d "$SYSROOT/usr/share/fonts/truetype/dejavu" ] || [ ! -f "$SYSROOT/usr/lib/x86_64-linux-gnu/libnspr4.so" ]; then
  curl -sf --max-time 120 -o "$PT/Packages.gz" "$ARCHIVE/dists/noble/main/binary-amd64/Packages.gz"
  # the .debs' pool paths and versions come from the index, not from guessing
  python3 - "$PT" <<'PY'
import gzip, re, sys, pathlib
pt = pathlib.Path(sys.argv[1])
text = gzip.open(pt / 'Packages.gz', 'rt', errors='replace').read()
want = {'libnspr4', 'libnss3', 'fonts-dejavu-core'}
found = {}
for block in text.split('\n\n'):
    m = re.search(r'^Package: (.*)$', block, re.M)
    if not m or m.group(1).strip() not in want:
        continue
    arch = re.search(r'^Architecture: (.*)$', block, re.M)
    if not arch or arch.group(1).strip() not in {'amd64', 'all'}:
        continue
    found[m.group(1).strip()] = re.search(r'^Filename: (.*)$', block, re.M).group(1).strip()
missing = want - set(found)
if missing:
    sys.exit('not in the index: ' + ', '.join(sorted(missing)))
(pt / 'urls.txt').write_text('\n'.join('http://archive.ubuntu.com/ubuntu/' + f for f in found.values()) + '\n')
for name, f in found.items():
    print(f'{name}: {f}')
PY
  while read -r url; do
    [ -n "$url" ] || continue
    curl -sf --max-time 240 -O --output-dir "$PT/debs" "$url"
  done < "$PT/urls.txt"
  for deb in "$PT"/debs/*.deb; do dpkg-deb -x "$deb" "$SYSROOT"; done
else
  echo "already unpacked in $SYSROOT"
fi

cat > "$PT/fonts.conf" <<'EOF'
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<fontconfig>
  <dir>/tmp/locode-playwright/sysroot/usr/share/fonts</dir>
  <dir>/usr/share/fonts</dir>
  <cachedir>/tmp/locode-playwright/fontcache</cachedir>
</fontconfig>
EOF

cat > "$PT/env.sh" <<EOF
export CHROME=$CHROME
export LD_LIBRARY_PATH=$SYSROOT/usr/lib/x86_64-linux-gnu:$SYSROOT/lib/x86_64-linux-gnu\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}
export FONTCONFIG_FILE=$PT/fonts.conf
export FONTCONFIG_PATH=$PT
export PW_ROOT=$PT          # playwright-core is resolved from here
EOF

echo "== check =="
# shellcheck disable=SC1091
. "$PT/env.sh"
missing=$(ldd "$CHROME" | grep -c 'not found' || true)
if [ "$missing" != "0" ]; then
  echo "still missing libraries:" >&2
  ldd "$CHROME" | grep 'not found' >&2
  exit 1
fi
# --version proves the binary and every shared library load without starting a
# browser process (a full --dump-dom start leaves the zygote running here and
# never exits on its own)
"$CHROME" --version
echo "chromium runs; now: source $PT/env.sh"
