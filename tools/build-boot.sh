#!/bin/bash
# Monta o initramfs do Thunder OS e o boot.img do aparelho. Nao precisa de root.
#
# Entradas: $WORK/kernel (tools/build-kernel.sh), $WORK/rootfs (busybox estatico,
#           thunder-reboot e INITRAMFS_FIRMWARE, de tools/build-rootfs.sh),
#           initramfs/init, rootfs/overlay.
# Saida:    $WORK/out/boot.img (ou $OUT_NAME) e $WORK/out/initramfs.cpio.gz
# KERNEL=dir usa outro kernel instalado (padrao: $WORK/kernel); os modulos do rootfs
# precisam ser da mesma build (MODVERSIONS).
set -euo pipefail
. "$(dirname "$0")/common.sh"
require_device
take_build_lock

# thunder.root vai por ultimo: vale o ultimo valor da cmdline.
CMDLINE="${CMDLINE:+$CMDLINE }thunder.root=$ROOT_PARTITION${CMDLINE_EXTRA:+ $CMDLINE_EXTRA}"
kernel=$KERNEL_OUT
name=${OUT_NAME:-boot.img}

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT

install -Dm755 "$repo/initramfs/init" "$stage/init"
install -Dm755 "$ROOTFS/usr/bin/busybox" "$stage/bin/busybox"
install -Dm755 "$repo/rootfs/overlay/usr/lib/thunder/usb-gadget" "$stage/usr/lib/thunder/usb-gadget"
install -Dm644 "$repo/rootfs/overlay/etc/udhcpd-usb.conf" "$stage/etc/udhcpd-usb.conf"
install -Dm644 "$ROOTFS/etc/thunder/device" "$stage/etc/thunder/device"
# Estatico: no resgate, "thunder-reboot bootloader" volta ao fastboot sem botao.
install -Dm755 "$ROOTFS/usr/local/bin/thunder-reboot" "$stage/bin/thunder-reboot"
for f in "${INITRAMFS_FIRMWARE[@]}"; do
	install -Dm644 "$ROOTFS/usr/lib/firmware/$f" "$stage/lib/firmware/$f"
done
mkdir -p "$stage"/{dev,proc,sys,run,tmp,newroot}

# cpio newc com dono 0:0. O kernel aceita arquivos cpio concatenados: o segundo traz
# o /dev/console, que nao da para criar sem root. gzip: o descompressor de initramfs
# que todo kernel de vendor tem (CONFIG_RD_GZIP).
(
	cd "$stage"
	find . -mindepth 1 | LC_ALL=C sort | bsdtar --format newc --uid 0 --gid 0 -n -cf - -T -
	printf '#mtree\n./dev/console type=char mode=0600 uid=0 gid=0 device=native,5,1\n' |
		bsdtar --format newc -cf - @-
) | gzip -9 -n > "$OUT/initramfs.cpio.gz"

mkbootimg "${BOOTIMG_ARGS[@]}" \
	--kernel "$kernel/boot/vmlinuz" \
	--ramdisk "$OUT/initramfs.cpio.gz" \
	${KERNEL_DTB:+--dtb "$kernel/boot/dtbs/$KERNEL_DTB"} \
	--cmdline "$CMDLINE" \
	--output "$OUT/$name"

echo "cmdline: $CMDLINE"
ls -l "$OUT/$name" "$OUT/initramfs.cpio.gz"
