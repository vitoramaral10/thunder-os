#!/bin/bash
# Monta o rootfs do Arch Linux ARM para o thunder em $BUILD/rootfs, sem root no host:
# user namespace + binfmt_misc proprio do namespace + qemu-aarch64 estatico.
#
# Entradas: $BUILD/kernel (kernel/build.sh), firmware proprietario em $FIRMWARE
#           (docs/vendor-firmware.md), chave SSH publica em $AUTHORIZED_KEYS.
set -euo pipefail
. "$(dirname "$0")/common.sh"

# Chave de assinatura do Arch Linux ARM Build System <builder@archlinuxarm.org>.
ALARM_KEY=68B3537F39A313B3E574D06777193F152BDBE6A6
TARBALL=ArchLinuxARM-aarch64-latest.tar.gz
FIRMWARE=${FIRMWARE:-$BUILD/firmware}
AUTHORIZED_KEYS=${AUTHORIZED_KEYS:-$HOME/.ssh/id_ed25519.pub}
THUNDER_USER=${THUNDER_USER:-$(id -un)}
# Senha provisoria de root e do usuario, para o telnet de emergencia: troque no aparelho.
THUNDER_PASSWORD=${THUNDER_PASSWORD:-147147}
BUILDROOT=$BUILD/buildroot
pac="pacman --noconfirm --needed --disable-sandbox"

[ -d "$BUILD/kernel/lib/modules/$KVER" ] || { echo "sem modulos: rode kernel/build.sh" >&2; exit 1; }
[ -s "$FIRMWARE/nt36672c_tm_01_ts_fw.bin" ] || { echo "sem firmware em $FIRMWARE (docs/vendor-firmware.md)" >&2; exit 1; }
[ -s "$AUTHORIZED_KEYS" ] || { echo "sem chave SSH em $AUTHORIZED_KEYS" >&2; exit 1; }

echo "== qemu-aarch64 estatico"
if [ ! -x "$QEMU" ]; then
	f=$(fetch_host_pkg qemu-user-static)
	bsdtar -xOf "$f" usr/bin/qemu-aarch64-static > "$QEMU"
	chmod 755 "$QEMU"
fi

echo "== tarball do Arch Linux ARM, com assinatura"
for f in "$TARBALL" "$TARBALL.sig"; do
	[ -s "$BUILD/dl/$f" ] || curl -fL -o "$BUILD/dl/$f" "http://os.archlinuxarm.org/os/$f"
done
export GNUPGHOME=$BUILD/tools/gnupg
mkdir -p -m 700 "$GNUPGHOME"
gpg --batch --list-keys "$ALARM_KEY" >/dev/null 2>&1 ||
	gpg --batch --keyserver hkps://keyserver.ubuntu.com --recv-keys "$ALARM_KEY"
gpg --batch --status-fd 1 --verify "$BUILD/dl/$TARBALL.sig" "$BUILD/dl/$TARBALL" 2>/dev/null |
	grep -q "^\[GNUPG:\] VALIDSIG $ALARM_KEY " || { echo "assinatura invalida" >&2; exit 1; }

echo "== extracao"
userns rm -rf "${ROOTFS:?}" "${BUILDROOT:?}"
mkdir -p "$ROOTFS"
userns bsdtar -xpf "$BUILD/dl/$TARBALL" -C "$ROOTFS" --numeric-owner

echo "== atualizacao; fora o kernel generico, o mkinitcpio e o linux-firmware"
tools_achroot() { "$repo/tools/achroot.sh" "$@"; }
tools_achroot /bin/bash -c "pacman-key --init && pacman-key --populate archlinuxarm && $pac -Syu"
tools_achroot /bin/bash -c "pacman -Rns --noconfirm --disable-sandbox linux-aarch64 mkinitcpio mkinitcpio-busybox \$(pacman -Qq | grep '^linux-firmware')"

echo "== chroot de build (toolchain fica fora do rootfs)"
userns cp -a "$ROOTFS" "$BUILDROOT"
ROOTFS=$BUILDROOT tools_achroot /bin/bash -c "$pac -S base-devel meson psmisc && (id builder >/dev/null 2>&1 || useradd -m builder)"
# Valores entram nos shells do namespace pelo ambiente, nunca colados no codigo: um
# caminho ou uma senha com apostrofo quebraria a sintaxe.
export repo BUILD BUILDROOT KVER FIRMWARE AUTHORIZED_KEYS THUNDER_USER THUNDER_PASSWORD
UIDGID=$(userns awk -F: '$1=="builder"{print $3":"$4}' "$BUILDROOT/etc/passwd")
export UIDGID
userns bash -c 'set -e
	w=$BUILDROOT/home/builder/work
	rm -rf "${w:?}"; mkdir -p "$w"
	cp "$repo/rootfs/packages/openrc/PKGBUILD" "$repo/device/tools/thunder-reboot.c" "$w/"
	chown -R "$UIDGID" "$w"'
ROOTFS=$BUILDROOT tools_achroot /bin/bash -c 'cd /home/builder/work &&
	su builder -c "MAKEFLAGS=-j$(nproc) makepkg -f --noconfirm" &&
	gcc -static -O2 -Wall -Wextra -o thunder-reboot thunder-reboot.c && strip thunder-reboot'

echo "== stage: overlay, pacotes, modulos, firmware, chave"
S=$ROOTFS/root/thunder-stage
export S
userns bash -c 'set -e
	w=$BUILDROOT/home/builder/work
	rm -rf "${S:?}"; mkdir -p "$S/packages" "$S/modules" "$S/firmware"
	cp -a "$repo/rootfs/overlay" "$S/overlay"
	install -Dm755 "$w/thunder-reboot" "$S/overlay/usr/local/bin/thunder-reboot"
	cp "$w"/openrc-*.pkg.tar.* "$S/packages/"
	cp -a "$BUILD/kernel/lib/modules/$KVER" "$S/modules/"
	cp -a "$FIRMWARE/." "$S/firmware/"
	cp "$AUTHORIZED_KEYS" "$S/authorized_keys"
	printf "THUNDER_USER=%q\nTHUNDER_PASSWORD=%q\n" "$THUNDER_USER" "$THUNDER_PASSWORD" > "$S/config"
	cp "$repo/rootfs/configure.sh" "$repo/rootfs/finalize.sh" "$S/"
	chown -R 0:0 "$S"'

echo "== configuracao"
tools_achroot /bin/bash /root/thunder-stage/configure.sh
tools_achroot /bin/bash /root/thunder-stage/finalize.sh
