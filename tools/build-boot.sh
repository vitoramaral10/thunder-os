#!/bin/bash
# Monta o initramfs proprio e o boot.img do thunder. Nao precisa de root.
#
# Entradas: $BUILD/kernel (kernel/build.sh), $BUILD/rootfs (busybox estatico e firmware
#           do toque, de tools/build-rootfs.sh), initramfs/init, rootfs/overlay.
# Saida:    $BUILD/out/boot.img (ou $OUT_NAME) e $BUILD/out/initramfs.cpio.gz
# KERNEL=dir usa outro kernel instalado (padrao: $BUILD/kernel); os modulos do rootfs
# precisam ser da mesma build (MODVERSIONS).
set -euo pipefail
. "$(dirname "$0")/common.sh"

CMDLINE=${CMDLINE:-"bootopt=64S3,32N2,64N2 buildvariant=user thunder.root=userdata"}
kernel=${KERNEL:-$BUILD/kernel}
out=$BUILD/out
name=${OUT_NAME:-boot.img}

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT

install -Dm755 "$repo/initramfs/init" "$stage/init"
install -Dm755 "$ROOTFS/usr/bin/busybox" "$stage/bin/busybox"
install -Dm755 "$repo/rootfs/overlay/usr/lib/thunder/usb-gadget" "$stage/usr/lib/thunder/usb-gadget"
install -Dm644 "$repo/rootfs/overlay/etc/udhcpd-usb.conf" "$stage/etc/udhcpd-usb.conf"
# Estatico: no resgate, "thunder-reboot bootloader" volta ao fastboot sem botao.
install -Dm755 "$ROOTFS/usr/local/bin/thunder-reboot" "$stage/bin/thunder-reboot"
# O driver do toque e built-in e pede o firmware no probe, antes do switch_root.
for f in nt36672c_tm_01_ts_fw.bin nt36672c_tm_01_ts_mp.bin; do
	install -Dm644 "$ROOTFS/usr/lib/firmware/$f" "$stage/lib/firmware/$f"
done
mkdir -p "$stage"/{dev,proc,sys,run,tmp,newroot}

# cpio newc com dono 0:0. O kernel aceita arquivos cpio concatenados: o segundo traz
# o /dev/console, que nao da para criar sem root. So gzip: e o unico descompressor
# de initramfs ligado neste kernel (CONFIG_RD_GZIP).
(
	cd "$stage"
	find . -mindepth 1 | LC_ALL=C sort | bsdtar --format newc --uid 0 --gid 0 -n -cf - -T -
	printf '#mtree\n./dev/console type=char mode=0600 uid=0 gid=0 device=native,5,1\n' |
		bsdtar --format newc -cf - @-
) | gzip -9 -n > "$out/initramfs.cpio.gz"

# Offsets do boot.img de fabrica (header v2, pagina de 2048).
mkbootimg --header_version 2 \
	--kernel "$kernel/boot/vmlinuz" \
	--ramdisk "$out/initramfs.cpio.gz" \
	--dtb "$kernel/boot/dtbs/mediatek/mt6833.dtb" \
	--pagesize 2048 --base 0 \
	--kernel_offset 0x40080000 --ramdisk_offset 0x51100000 \
	--second_offset 0 --tags_offset 0x47c80000 --dtb_offset 0x47c80000 \
	--cmdline "$CMDLINE" \
	--output "$out/$name"

ls -l "$out/$name" "$out/initramfs.cpio.gz"
