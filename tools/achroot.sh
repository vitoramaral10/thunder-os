#!/bin/bash
# Roda um comando dentro do rootfs aarch64 do Arch como root do namespace, sem sudo.
# User namespace com o mapa de subuid completo, binfmt_misc proprio do namespace
# (kernel 6.7+) e o qemu estatico do pacote qemu-user-static do Arch, registrado com a
# flag F para funcionar de dentro do chroot sem copiar o binario para la.
#
# Uso: tools/achroot.sh [comando...]        (padrao: bash -l)
#      ROOTFS=/outro/dir tools/achroot.sh ...   (sem ROOTFS: o rootfs de $DEVICE)
set -euo pipefail
. "$(dirname "$0")/common.sh"
# ROOTFS dado pelo chamador dispensa o aparelho (o build-rootfs.sh usa no buildroot).
[ -n "${ROOTFS:-}" ] || require_device
export ROOTFS QEMU
[ -x "$QEMU" ] || { echo "sem $QEMU: rode tools/build-rootfs.sh" >&2; exit 1; }
[ $# -gt 0 ] || set -- /bin/bash -l

exec unshare --map-auto --map-root-user --mount --pid --fork --kill-child -- \
	/bin/bash -euo pipefail -c '
mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc
magic="\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\xb7\x00"
mask="\xff\xff\xff\xff\xff\xff\xff\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff"
echo ":qemu-aarch64:M::$magic:$mask:$QEMU:FP" > /proc/sys/fs/binfmt_misc/register

# A raiz precisa ser um ponto de montagem: o CheckSpace do pacman procura o mount do cachedir.
mount --bind "$ROOTFS" "$ROOTFS"
# /dev proprio e minimo: com o /dev do host por bind, hooks como o systemd-tmpfiles tentam
# mudar dono de no do host.
mount -t tmpfs -o mode=0755,nosuid dev "$ROOTFS/dev"
for n in null zero full random urandom tty; do
	touch "$ROOTFS/dev/$n"
	mount --bind "/dev/$n" "$ROOTFS/dev/$n"
done
mkdir -p "$ROOTFS/dev/pts" "$ROOTFS/dev/shm"
mount -t devpts -o newinstance,ptmxmode=0666 devpts "$ROOTFS/dev/pts"
ln -s pts/ptmx "$ROOTFS/dev/ptmx"
ln -s /proc/self/fd "$ROOTFS/dev/fd"
mount -t proc proc "$ROOTFS/proc"
mount --rbind /sys "$ROOTFS/sys"
mount -t tmpfs tmpfs "$ROOTFS/run"
mount -t tmpfs tmpfs "$ROOTFS/tmp"
if [ -L "$ROOTFS/etc/resolv.conf" ] || [ ! -e "$ROOTFS/etc/resolv.conf" ]; then
	rm -f "$ROOTFS/etc/resolv.conf"
	cp /etc/resolv.conf "$ROOTFS/etc/resolv.conf"
fi

exec chroot "$ROOTFS" /usr/bin/env -i HOME=/root TERM="${TERM:-xterm}" \
	PATH=/usr/local/sbin:/usr/local/bin:/usr/bin LANG=C.UTF-8 "$@"
' achroot "$@"
