#!/bin/bash
# Monta o initramfs e o boot.img do aparelho no PC, com a mesma receita que o aparelho
# usa ao atualizar o kernel (initramfs/thunder-mkboot). Nao precisa de root.
#
# Entradas: a pasta do kernel do pacote ($WORK/kernel-pkg, tools/package-kernel.sh) e o
#           rootfs ($WORK/rootfs, tools/build-rootfs.sh), de onde vem o conteudo do
#           initramfs. O init e o thunder-mkboot do repositorio sao copiados para o
#           rootfs antes, para uma edicao no initramfs valer sem remontar o rootfs.
# Saida:    $WORK/out/boot.img (ou $OUT_NAME) e $WORK/out/initramfs.cpio.gz
# CMDLINE_EXTRA vai no fim da cmdline (ex.: thunder.debug=1).
# O corpo vai num bloco: editar este arquivo com ele rodando nao muda a execucao.
{
set -euo pipefail
. "$(dirname "$0")/common.sh"
require_device
take_build_lock

kdir=$WORK/kernel-pkg/usr/lib/thunder/kernel
[ -s "$kdir/boot.conf" ] || { echo "sem $kdir: rode tools/package-kernel.sh" >&2; exit 1; }
[ -x "$ROOTFS/usr/bin/busybox" ] || { echo "sem rootfs em $ROOTFS: rode tools/build-rootfs.sh" >&2; exit 1; }

export repo ROOTFS
userns sh -c 'install -Dm755 "$repo/initramfs/init" "$ROOTFS/usr/lib/thunder/initramfs/init"
	install -Dm755 "$repo/initramfs/thunder-mkboot" "$ROOTFS/usr/lib/thunder/thunder-mkboot"'

"$repo/initramfs/thunder-mkboot" "$ROOTFS" "$kdir" "$OUT/${OUT_NAME:-boot.img}" "$OUT/initramfs.cpio.gz"
exit
}
