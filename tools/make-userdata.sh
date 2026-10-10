#!/bin/bash
# Gera $WORK/out/userdata.img: ext4 com o rootfs, sem root no host (mke2fs -d dentro do
# user namespace, para os donos sairem certos). O 10-resize-root.start cresce o fs ate o
# fim da particao no primeiro boot.
#
# Recursos: os padroes do e2fsprogs 1.47 menos orphan_file (entrou no 5.15; o ext4 de
# kernel de vendor 4.x recusaria o mount) e metadata_csum_seed.
set -euo pipefail
. "$(dirname "$0")/common.sh"
require_device

SIZE=${SIZE:-4G}
img=$OUT/userdata.img

rm -f "$img"
truncate -s "$SIZE" "$img"
userns mke2fs -q -t ext4 -b 4096 -L thunder-root \
	-O has_journal,extent,huge_file,flex_bg,metadata_csum,64bit,dir_nlink,extra_isize,^orphan_file,^metadata_csum_seed \
	-E root_owner=0:0 \
	-d "$ROOTFS" "$img"
e2fsck -fn "$img"
dumpe2fs -h "$img" 2>/dev/null | grep -E 'volume name|features|Block size|Block count'
