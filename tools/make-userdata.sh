#!/bin/bash
# Gera $WORK/out/userdata.img: ext4 com o rootfs, sem root no host (mke2fs -d dentro do
# user namespace, para os donos sairem certos). O systemd-growfs-root (x-systemd.growfs no
# /etc/fstab) cresce o fs ate o fim da particao no primeiro boot.
#
# Recursos: os padroes do e2fsprogs 1.47 menos orphan_file (entrou no 5.15; o ext4 de
# kernel de vendor 4.x recusaria o mount) e metadata_csum_seed.
set -euo pipefail
. "$(dirname "$0")/common.sh"
require_device
take_build_lock

SIZE=${SIZE:-4G}
img=$OUT/userdata.img

# O SIZE e so o tamanho inicial (o growfs estende no primeiro boot). Se o rootfs nao cabe,
# a imagem cresce para caber, em GiB inteiros: o rootfs mais 15% e 256 MiB para o journal,
# as tabelas de inodes e a reserva do ext4 (blocos alocados, nao o tamanho aparente:
# arquivo pequeno ocupa um bloco inteiro). Ex.: o perfil phosh pede 5 GiB.
used=$(userns du -sx --block-size=1 "$ROOTFS" | cut -f1)
need=$(( (used * 115 / 100 + 256 * 1024 * 1024 + (1 << 30) - 1) >> 30 ))
if [ "$(numfmt --from=iec "$SIZE")" -lt $((need << 30)) ]; then
	echo "rootfs com $(numfmt --to=iec "$used"): imagem de ${need}G em vez de $SIZE"
	SIZE=${need}G
fi

rm -f "$img"
truncate -s "$SIZE" "$img"
userns mke2fs -q -t ext4 -b 4096 -L thunder-root \
	-O has_journal,extent,huge_file,flex_bg,metadata_csum,64bit,dir_nlink,extra_isize,^orphan_file,^metadata_csum_seed \
	-E root_owner=0:0 \
	-d "$ROOTFS" "$img"
e2fsck -fn "$img"
dumpe2fs -h "$img" 2>/dev/null | grep -E 'volume name|features|Block size|Block count'
