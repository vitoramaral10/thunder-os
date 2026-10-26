#!/bin/bash
# Copia o firmware proprietario do aparelho da ROM de fabrica para $FIRMWARE, sem root:
# tira a particao FIRMWARE_ROM_PARTITION (vendor) do super.img (rom-extract-partition.py)
# e le os arquivos FIRMWARE_FILES de dentro dela, procurando em FIRMWARE_ROM_DIRS.
#
# Uso: tools/extract-firmware.sh <rom>
#   <rom>: o pacote de fastboot (.tgz), a pasta extraida dele ou um super.img.
# O corpo vai num bloco: o bash le o bloco inteiro antes de executar, entao editar
# este arquivo com ele rodando nao muda a execucao em curso.
{
set -euo pipefail
. "$(dirname "$0")/common.sh"
require_device
take_build_lock

rom=${1:?uso: $0 <rom de fabrica (.tgz, pasta ou super.img)>}
[ -e "$rom" ] || { echo "nao existe: $rom" >&2; exit 1; }
[ ${#FIRMWARE_FILES[@]} -gt 0 ] || { echo "$DEVICE nao lista FIRMWARE_FILES: nada a extrair"; exit 0; }

tmp=$WORK/rom-tmp
rm -rf "$tmp"
mkdir -p "$tmp" "$FIRMWARE"
trap 'rm -rf "$tmp"' EXIT

echo "== particao $FIRMWARE_ROM_PARTITION da ROM"
"$repo/tools/rom-extract-partition.py" "$rom" "$FIRMWARE_ROM_PARTITION" "$tmp/part.img"

fs=$(blkid -o value -s TYPE "$tmp/part.img" 2>/dev/null || true)
[ -n "$fs" ] || fs=$(file -b "$tmp/part.img")
echo "== arquivos ($fs)"
case $fs in
ext*|*ext2*|*ext4*)
	# O -R e interpretado pelo parser do proprio debugfs: aspas duplas protegem espacos.
	get() { debugfs -R "dump -p \"$1\" \"$2\"" "$tmp/part.img" >/dev/null 2>&1 && [ -s "$2" ]; } ;;
erofs*|*EROFS*)
	command -v fsck.erofs >/dev/null || { echo "particao EROFS: instale erofs-utils (fsck.erofs)" >&2; exit 1; }
	mkdir -p "$tmp/tree"
	fsck.erofs --extract="$tmp/tree" "$tmp/part.img" >/dev/null
	# A ROM vem de fora: um link simbolico nela (absoluto, ou com ..) faria o cp ler um
	# arquivo do PC e leva-lo para a imagem. So vale arquivo que resolve dentro da arvore.
	tree=$(realpath -e -- "$tmp/tree")
	get() {
		local f
		f=$(realpath -e -- "$tmp/tree$1" 2>/dev/null) || return 1
		case $f in "$tree"/*) ;; *) return 1 ;; esac
		[ -f "$f" ] && [ -s "$f" ] && cp -- "$f" "$2"
	} ;;
*)
	echo "sistema de arquivos desconhecido na particao: $fs" >&2; exit 1 ;;
esac

missing=()
for f in "${FIRMWARE_FILES[@]}"; do
	ok=0
	for d in "${FIRMWARE_ROM_DIRS[@]}"; do
		rm -f "$FIRMWARE/$f"
		if get "$d/$f" "$FIRMWARE/$f"; then ok=1; echo "  $d/$f"; break; fi
	done
	[ "$ok" = 1 ] || missing+=("$f")
done

for f in "${FIRMWARE_REQUIRED[@]}" "${INITRAMFS_FIRMWARE[@]}"; do
	[ -s "$FIRMWARE/$f" ] || { echo "faltou $f, obrigatorio para $DEVICE: a ROM e deste aparelho?" >&2; exit 1; }
done
[ ${#missing[@]} -eq 0 ] || echo "aviso: nao achei na ROM: ${missing[*]}" >&2

# Arvores do modem (MODEM_ROM_TREES): so arquivos regulares; link simbolico vindo da ROM
# e apagado depois da copia (nunca seguido), pelo mesmo motivo do get() acima.
if [ ${#MODEM_ROM_TREES[@]} -gt 0 ]; then
	rm -rf "$MODEM_RO"; mkdir -p "$MODEM_RO"
	for t in "${MODEM_ROM_TREES[@]}"; do
		src=${t%%:*} name=${t#*:}
		case $fs in
		ext*|*ext2*|*ext4*)
			mkdir -p "$tmp/trees"
			debugfs -R "rdump \"$src\" \"$tmp/trees\"" "$tmp/part.img" >/dev/null 2>&1 || true
			got=$tmp/trees/$(basename "$src") ;;
		*)
			got=$tmp/tree$src ;;
		esac
		[ -d "$got" ] && [ ! -L "$got" ] || { echo "aviso: $src ausente na ROM" >&2; continue; }
		cp -r --no-dereference -- "$got" "$MODEM_RO/$name"
		find "$MODEM_RO/$name" -type l -delete
		echo "  $src -> modem-ro/$name ($(find "$MODEM_RO/$name" -type f | wc -l) arquivos)"
	done
fi
echo "firmware em $FIRMWARE: $(( ${#FIRMWARE_FILES[@]} - ${#missing[@]} )) de ${#FIRMWARE_FILES[@]} arquivos"
exit
}
