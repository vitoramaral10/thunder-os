#!/bin/bash
# Compila o kernel do aparelho: fonte em KERNEL_URL (verificada por KERNEL_SHA512), hook
# device_kernel_prepare do device.sh, patches do series do aparelho, config.
#
# Saida em $WORK/kernel: boot/vmlinuz (KERNEL_IMAGE), boot/dtbs/, lib/modules/$KVER/ e
# config.
#
# Requisitos: cross-compilador aarch64 (tools/fetch-cross.sh poe um em $BUILD/cross) e
# o basico de build do kernel no host (make, gcc, bison, flex, perl, openssl, cpio, kmod).
set -euo pipefail
. "$(dirname "$0")/common.sh"
require_device

JOBS=${JOBS:-$(nproc)}

if [ -x "$BUILD/cross/usr/bin/aarch64-linux-gnu-gcc" ]; then
	export PATH=$BUILD/cross/usr/bin:$PATH
fi
CROSS_COMPILE=${CROSS_COMPILE:-aarch64-linux-gnu-}
command -v "${CROSS_COMPILE}gcc" >/dev/null || { echo "sem ${CROSS_COMPILE}gcc: rode tools/fetch-cross.sh" >&2; exit 1; }

src=$KERNEL_SRC

kmake() {
	make -C "$src" O=out ARCH=arm64 CROSS_COMPILE="$CROSS_COMPILE" \
		HOSTCC=gcc HOSTCXX=g++ \
		KBUILD_BUILD_USER=thunder KBUILD_BUILD_HOST=thunder-os \
		KBUILD_BUILD_VERSION=1 "$@"
}

# Fonte limpa, hook do aparelho, patches e config. REBUILD=1 pula isto e recompila a
# arvore que ja esta em $KERNEL_SRC (iteracao rapida num patch aplicado a mao).
prepare() {
	local tarball=$BUILD/dl/kernel-$DEVICE-${KERNEL_SHA512:0:16}.tar.gz
	[ -s "$tarball" ] || curl -fL -o "$tarball" "$KERNEL_URL"
	echo "$KERNEL_SHA512  $tarball" | sha512sum -c -

	rm -rf "$src"
	mkdir -p "$src"
	bsdtar -xf "$tarball" -C "$src" --strip-components 1

	if declare -F device_kernel_prepare >/dev/null; then
		echo "hook: device_kernel_prepare"
		device_kernel_prepare "$src"
	fi

	# Patches, na ordem do series.
	if [ -f "$DEVICE_DIR/patches/series" ]; then
		grep -v '^#' "$DEVICE_DIR/patches/series" | while read -r p; do
			[ -n "$p" ] || continue
			echo "patch: $p"
			patch -p1 -d "$src" < "$DEVICE_DIR/patches/$p"
		done
	fi

	if [ "$KERNEL_STRIP_WERROR" = 1 ]; then
		find "$src" -type f \( -name Makefile -o -name Makefile.common -o -name Kbuild \) \
			-exec sed -i 's/-Werror-/-W/g; s/-Werror=/-W/g; s/-Werror//g' {} +
	fi

	mkdir -p "$src/out"
	cp "$DEVICE_DIR/$KERNEL_CONFIG" "$src/out/.config"
	kmake olddefconfig >/dev/null
}

[ "${REBUILD:-0}" = 1 ] || prepare
# PREPARE_ONLY=1: para depois do oldconfig (confere hook, patches e config sem compilar).
[ "${PREPARE_ONLY:-0}" = 1 ] && { echo "preparado em $src"; exit 0; }
kmake -j"$JOBS"

# Sempre $WORK/kernel: KERNEL= aponta outro kernel so para leitura (build-boot.sh).
out=$WORK/kernel
rm -rf "$out"
mkdir -p "$out/boot/dtbs"
cp "$src/out/arch/arm64/boot/$KERNEL_IMAGE" "$out/boot/vmlinuz"
kmake dtbs_install INSTALL_DTBS_PATH="$out/boot/dtbs"
kmake modules_install INSTALL_MOD_PATH="$out" INSTALL_MOD_STRIP=1
rm -f "$out/lib/modules/$KVER"/{build,source}
cp "$src/out/.config" "$out/config"

strings "$src/out/vmlinux" | grep -m1 '^Linux version' || true
[ -d "$out/lib/modules/$KVER" ] || { echo "a build gerou modulos de outra versao: KERNEL_VERSION=$KVER no device.sh?" >&2; ls "$out/lib/modules" >&2; exit 1; }
ls -l "$out/boot/vmlinuz" ${KERNEL_DTB:+"$out/boot/dtbs/$KERNEL_DTB"}
find "$out/lib/modules" -name '*.ko' | wc -l
