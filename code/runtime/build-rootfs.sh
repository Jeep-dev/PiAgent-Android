#!/bin/sh
# Builds the Linux userland the app runs pi in: Alpine with Node.js and a few packages, packed as
# one tar.xz (unpacked by the app on first launch; pi itself is installed then, with pi-update).
# usage: build-rootfs.sh <aarch64|x86_64> <out.tar.xz>   (as root: apk sets file owners)
set -eu
ARCH=$1
OUT=$(realpath "$2")
ALPINE=3.24
ALPINE_RELEASE=3.24.2
PACKAGES="bash python3 git ripgrep fd libgcc libstdc++ curl less nodejs npm"
MIRROR=https://dl-cdn.alpinelinux.org/alpine
# What the app's apk installs from later (fast from China, works anywhere).
APP_MIRROR=https://mirrors.aliyun.com/alpine

case $ARCH in
    aarch64|x86_64) ;;
    *) echo "unknown arch $ARCH" >&2; exit 1 ;;
esac

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
root=$work/root
mkdir -p "$root"

curl -fsSL "$MIRROR/v$ALPINE/releases/$ARCH/alpine-minirootfs-$ALPINE_RELEASE-$ARCH.tar.gz" | tar -xz -C "$root"

# apk for this machine, installing packages for $ARCH without running them.
host=$(uname -m)
curl -fsSL "$MIRROR/v$ALPINE/main/$host/APKINDEX.tar.gz" | tar -xzO APKINDEX > "$work/index"
apkver=$(awk '/^P:apk-tools-static$/ {f=1} f && /^V:/ {print substr($0, 3); exit}' "$work/index")
curl -fsSL "$MIRROR/v$ALPINE/main/$host/apk-tools-static-$apkver.apk" | tar -xz -C "$work" sbin/apk.static 2>/dev/null
"$work/sbin/apk.static" --arch "$ARCH" --root "$root" --no-scripts --no-cache \
    -X "$MIRROR/v$ALPINE/main" -X "$MIRROR/v$ALPINE/community" -U add $PACKAGES

printf '%s/v%s/main\n%s/v%s/community\n' "$APP_MIRROR" "$ALPINE" "$APP_MIRROR" "$ALPINE" > "$root/etc/apk/repositories"
echo /bin/bash >> "$root/etc/shells"
sed -i 's|^root:x:0:0:root:/root:/bin/sh$|root:x:0:0:root:/root:/bin/bash|' "$root/etc/passwd"

# pi is not in it: the app installs it from npm with pi-update on first launch, and pi-update
# updates it (pi's own `pi update self` works too).
cat > "$root/usr/local/bin/pi-update" <<'UPDATE'
#!/bin/sh
# pi-update [version]: installs that version of pi (the latest when none is given) from npm as
# /usr/local/bin/pi. npm's own registry first, then npmmirror (fast from China). App updates leave it.
set -eu
V=${1:-latest}
for registry in https://registry.npmjs.org https://registry.npmmirror.com; do
    if npm install -g --ignore-scripts --no-fund --no-audit --registry "$registry" "@earendil-works/pi-coding-agent@$V"; then
        pi --version
        exit 0
    fi
    echo "pi-update: $registry failed" >&2
done
exit 1
UPDATE
chmod 755 "$root/usr/local/bin/pi-update"

rm -rf "$root/var/cache/apk/"*
tar --numeric-owner --hard-dereference -C "$root" -cf - . | xz -T0 -6 > "$OUT"
ls -l "$OUT"
