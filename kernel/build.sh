#!/bin/bash
# Compila o kernel 4.19 da Xiaomi para o thunder: fonte oficial no commit fixado,
# enxerto do driver de conectividade MediaTek (gen4m, MT6833), patches em series, config.
#
# Saida em $BUILD/kernel: boot/vmlinuz (Image.gz), boot/dtbs/mediatek/mt6833.dtb,
# lib/modules/4.19.191-perf/ e config.
#
# Requisitos: cross-compilador aarch64 (tools/fetch-cross.sh poe um em $BUILD/cross) e
# o basico de build do kernel no host (make, gcc, bison, flex, perl, openssl, cpio, kmod).
set -euo pipefail
. "$(dirname "$0")/../tools/common.sh"

COMMIT=a40ceab8943703f33e935cb0b86d0f69aa2044dd
SRC_SHA512=d0aa0cd26632a4ce272eea248c3db0003ba61c525814bc1d7aa83f14860f55a7b7e6cb15153154be6319936bcb4171aaed3de913e09d577623a1c5c55893a1df
CONN=mtk-connectivity-gen4m-mt6833-2
JOBS=${JOBS:-$(nproc)}
K=$repo/kernel

if [ -x "$BUILD/cross/usr/bin/aarch64-linux-gnu-gcc" ]; then
	export PATH=$BUILD/cross/usr/bin:$PATH
fi
CROSS_COMPILE=${CROSS_COMPILE:-aarch64-linux-gnu-}
command -v "${CROSS_COMPILE}gcc" >/dev/null || { echo "sem ${CROSS_COMPILE}gcc: rode tools/fetch-cross.sh" >&2; exit 1; }

src=$BUILD/kernel-src

kmake() {
	make -C "$src" O=out ARCH=arm64 CROSS_COMPILE="$CROSS_COMPILE" \
		HOSTCC=gcc HOSTCXX=g++ \
		KBUILD_BUILD_USER=thunder KBUILD_BUILD_HOST=thunder-linux \
		KBUILD_BUILD_VERSION=1 "$@"
}

# Fonte limpa, enxerto, patches e config. REBUILD=1 pula isto e recompila a arvore que ja
# esta em $BUILD/kernel-src (iteracao rapida num patch aplicado a mao).
prepare() {
	# Fonte da Xiaomi, verificada.
	tarball=$BUILD/dl/Xiaomi_Kernel_OpenSource-$COMMIT.tar.gz
	[ -s "$tarball" ] || curl -fL -o "$tarball" \
		"https://github.com/MiCode/Xiaomi_Kernel_OpenSource/archive/$COMMIT.tar.gz"
	echo "$SRC_SHA512  $tarball" | sha512sum -c -

	rm -rf "$src"
	mkdir -p "$src"
	bsdtar -xf "$tarball" -C "$src" --strip-components 1

	# Enxerto da conectividade (wmt_drv, wmt_chrdev_wifi, wlan_drv_gen4m, connfem, bt_drv).
	# common/ tem de vir do mesmo drop: o struct wmt_platform_bridge muda de layout entre
	# geracoes, e a mistura erra em silencio em runtime. O Makefile do fonte da Xiaomi fixa
	# o chip errado (CONNAC/6765).
	conn=$src/drivers/misc/mediatek/connectivity
	drop=$(mktemp -d)
	trap 'rm -rf "$drop"' EXIT
	bsdtar -xf "$K/connectivity/$CONN.tar.gz" -C "$drop"
	for d in wmt_drv wmt_chrdev_wifi wlan_drv_gen4m connfem bt_drv; do
		rm -rf "${conn:?}/$d"
		cp -a "$drop/$CONN/$d" "$conn/$d"
	done
	cp -f "$drop/$CONN"/wmt_drv/{connectivity_build_in_adapter,wmt_build_in_adapter}.{c,h} "$conn/common/"
	cp -f "$K/connectivity/Makefile-mt6833" "$conn/Makefile"
	patch -p1 -d "$conn" < "$K/connectivity/gen4m-mt6833-kernel419.diff"

	# Patches, na ordem do series.
	grep -v '^#' "$K/patches/series" | while read -r p; do
		[ -n "$p" ] || continue
		echo "patch: $p"
		patch -p1 -d "$src" < "$K/patches/$p"
	done

	# Codigo de vendor nao compila com -Werror em gcc moderno.
	find "$src" -type f \( -name Makefile -o -name Makefile.common -o -name Kbuild \) \
		-exec sed -i 's/-Werror-/-W/g; s/-Werror=/-W/g; s/-Werror//g' {} +

	mkdir -p "$src/out"
	cp "$K/config-thunder.aarch64" "$src/out/.config"
	kmake olddefconfig >/dev/null
}

[ "${REBUILD:-0}" = 1 ] || prepare
# PREPARE_ONLY=1: para depois do oldconfig (confere enxerto, patches e config sem compilar).
[ "${PREPARE_ONLY:-0}" = 1 ] && { echo "preparado em $src"; exit 0; }
kmake -j"$JOBS"

# Instala em $BUILD/kernel. O LK deste aparelho so aceita kernel comprimido (Image.gz).
out=$BUILD/kernel
rm -rf "$out"
mkdir -p "$out/boot/dtbs"
cp "$src/out/arch/arm64/boot/Image.gz" "$out/boot/vmlinuz"
kmake dtbs_install INSTALL_DTBS_PATH="$out/boot/dtbs"
kmake modules_install INSTALL_MOD_PATH="$out" INSTALL_MOD_STRIP=1
rm -f "$out/lib/modules/$KVER"/{build,source}
cp "$src/out/.config" "$out/config"

strings "$src/out/vmlinux" | grep -m1 '^Linux version' || true
ls -l "$out/boot/vmlinuz" "$out/boot/dtbs/mediatek/mt6833.dtb"
find "$out/lib/modules" -name '*.ko' | wc -l
