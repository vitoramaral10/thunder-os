#!/bin/bash
# Empacota o kernel do aparelho como pacote do pacman: linux-<aparelho>.
#
# Conteudo:
#   usr/lib/modules/<versao>/          modulos
#   usr/lib/thunder/kernel/vmlinuz     kernel (KERNEL_IMAGE)
#   usr/lib/thunder/kernel/dtb         DTB (KERNEL_DTB), se o aparelho usa
#   usr/lib/thunder/kernel/boot.conf   layout do boot.img: BOOTIMG_ARGS, CMDLINE,
#                                      INITRAMFS_FIRMWARE, particao e slot
#   usr/lib/sysctl.d/51-thunder-coredump-no-pidfd.conf   core_pattern sem %F, so em kernel
#                                      < 6.16 (veja abaixo)
# Sem firmware proprietario: o initramfs e montado no aparelho (boot-deploy), com o
# firmware que ja esta nele. Instalar ou atualizar o pacote dispara o hook
# 90-thunder-boot.hook, que grava o boot.img novo.
#
# Saida: $WORK/out/linux-<aparelho>-<versao>-1-aarch64.pkg.tar.zst, e a arvore do pacote
# em $WORK/kernel-pkg (o build-boot.sh do PC usa a mesma pasta do kernel).
# O corpo vai num bloco: editar este arquivo com ele rodando nao muda a execucao.
{
set -euo pipefail
. "$(dirname "$0")/common.sh"
require_device
take_build_lock

[ -d "$KERNEL_OUT/lib/modules/$KVER" ] || { echo "sem kernel: rode tools/build-kernel.sh" >&2; exit 1; }

name=linux-$DEVICE
# Versao: a do kernel, mais a hora da build, para cada build nova ser uma atualizacao.
stamp=$(date -u -r "$KERNEL_OUT/boot/vmlinuz" +%Y%m%d%H%M%S)
pkgver=${KVER//-/.}.r$stamp
pkgrel=1
pkg=$OUT/$name-$pkgver-$pkgrel-aarch64.pkg.tar.zst

root=$WORK/kernel-pkg
rm -rf "$root"
kdir=$root/usr/lib/thunder/kernel
mkdir -p "$kdir" "$root/usr/lib/modules"
cp -a "$KERNEL_OUT/lib/modules/$KVER" "$root/usr/lib/modules/"
install -m644 "$KERNEL_OUT/boot/vmlinuz" "$kdir/vmlinuz"
if [ -n "$KERNEL_DTB" ]; then
	install -m644 "$KERNEL_OUT/boot/dtbs/$KERNEL_DTB" "$kdir/dtb"
fi

# Kernel sem %F (pidfd no core_pattern, Linux 6.16): o padrao do systemd-coredump
# ("... %d %F %I", sysctl.d/50-coredump.conf) perde o %F, o argv_split junta os espacos e o
# %I cai na posicao do pidfd. Sem %F e sem %I o systemd-coredump 262 le tudo que precisa
# (_META_ARGV_REQUIRED = ate %c; %h %d opcionais). O arquivo ordena depois de
# 50-coredump.conf e so troca o core_pattern; vai no pacote do kernel para chegar ao
# aparelho junto da atualizacao do kernel (update-kernel.sh so instala este pacote).
sysctl_coredump=usr/lib/sysctl.d/51-thunder-coredump-no-pidfd.conf
if [ "$(printf '%s\n' 6.16 "${KVER%%-*}" | sort -V | head -n1)" != 6.16 ]; then
	mkdir -p "$root/usr/lib/sysctl.d"
	cat > "$root/$sysctl_coredump" <<'EOF'
# Gerado por tools/package-kernel.sh: este kernel nao tem %F (pidfd, 6.16).
# Mesmo padrao do 50-coredump.conf do systemd, sem %F e sem %I.
kernel.core_pattern=|/usr/lib/systemd/systemd-coredump %P %u %g %s %t %c %h %d
EOF
	chmod 644 "$root/$sysctl_coredump"
else
	sysctl_coredump=
fi

q() { printf "'%s'" "${1//\'/\'\\\'\'}"; }
qa() { local x; for x in "$@"; do printf '%s ' "$(q "$x")"; done; }
{
	echo "# $name $pkgver: lido pelo thunder-mkboot e pelo boot-deploy."
	echo "DEVICE=$(q "$DEVICE")"
	echo "KERNEL_VERSION=$(q "$KVER")"
	echo "BOOTIMG_ARGS=($(qa "${BOOTIMG_ARGS[@]}"))"
	echo "CMDLINE=$(q "${CMDLINE:+$CMDLINE }thunder.root=$ROOT_PARTITION")"
	echo "INITRAMFS_FIRMWARE=($(qa "${INITRAMFS_FIRMWARE[@]}"))"
	echo "FASTBOOT_AB=$(q "$FASTBOOT_AB")"
	echo "BOOT_DEPLOY=$(q "$BOOT_DEPLOY")"
} > "$kdir/boot.conf"

size=$(du -sb "$root" | cut -f1)
cat > "$root/.PKGINFO" <<EOF
pkgname = $name
pkgbase = $name
xdata = pkgtype=pkg
pkgver = $pkgver-$pkgrel
pkgdesc = Kernel $KVER do $DEVICE_VENDOR $DEVICE_NAME para o Thunder OS
url = https://github.com/vitoramaral10/thunder-os
builddate = $(date -u +%s)
packager = Thunder OS
size = $size
arch = aarch64
license = GPL-2.0-only
depend = android-tools
depend = python
depend = libarchive
depend = gzip
depend = kmod
depend = busybox
EOF

# Como o makepkg: .MTREE com donos root, depois o pacote com .PKGINFO na frente.
(
	cd "$root"
	files=$(find . -mindepth 1 -maxdepth 1 ! -name .PKGINFO ! -name .MTREE -printf '%P\n' | LC_ALL=C sort)
	LANG=C bsdtar -czf .MTREE --format=mtree --uid 0 --gid 0 \
		--options='!all,use-set,type,uid,gid,mode,time,size,md5,sha256,link' \
		.PKGINFO $files
	LANG=C bsdtar --zstd --uid 0 --gid 0 -cf "$pkg" .MTREE .PKGINFO $files
)
rm -f "$root/.PKGINFO" "$root/.MTREE"
# Aceite: kernel sem %F tem de levar o core_pattern no pacote.
if [ -n "$sysctl_coredump" ]; then
	bsdtar -tf "$pkg" | grep -qx "$sysctl_coredump" ||
		{ echo "pacote sem $sysctl_coredump" >&2; exit 1; }
fi
ln -sfn "$(basename "$pkg")" "$OUT/$name.pkg.tar.zst"
echo "pacote: $pkg ($(du -h "$pkg" | cut -f1))"
exit
}
