#!/bin/bash
# Atualiza o kernel de um aparelho que ja roda o Thunder OS, pelo cabo, sem reinstalar
# e sem apagar dados: compila se as entradas mudaram, empacota (linux-<aparelho>),
# envia e instala com o pacman. O hook do pacote monta o boot.img no aparelho e grava a
# particao boot do slot ativo, guardando o anterior em /var/lib/thunder/boot-previous.img.
#
#   tools/update-kernel.sh            atualiza; o kernel novo vale no proximo boot
#   tools/update-kernel.sh --reboot   atualiza e reinicia o aparelho
# Em aparelho com BOOT_DEPLOY=fastboot (o bootloader protege a particao boot), o boot.img
# montado no aparelho e gravado pelo fastboot: o aparelho vai ao fastboot sozinho
# (thunder-reboot bootloader), e o script grava, reinicia e confere o boot gravado.
# O corpo vai num bloco: editar este arquivo com ele rodando nao muda a execucao.
{
set -euo pipefail
. "$(dirname "$0")/common.sh"
require_device
take_build_lock

reboot=0
for a in "$@"; do
	case $a in
	--reboot) reboot=1 ;;
	*) echo "argumento desconhecido: $a" >&2; exit 2 ;;
	esac
done

addr=${THUNDER_ADDR:-172.16.42.1}
# ServerAlive: uma conexao com um aparelho que caiu (reboot) desiste em ~6 s, em vez de
# esperar o TCP por minutos.
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=2 -o ServerAliveCountMax=3)
ssh_() { ssh "${ssh_opts[@]}" "root@$addr" "$@" </dev/null; }
# Comando que derruba a conexao (reboot): com limite de tempo, e sem esperar resposta.
ssh_bye() { timeout 15 ssh "${ssh_opts[@]}" "root@$addr" "$@" </dev/null || true; }

here=$(ssh_ "sed -n \"s/^DEVICE='\\(.*\\)'\$/\\1/p\" /etc/thunder/device") ||
	{ echo "sem SSH em $addr: o aparelho esta no cabo, rodando o Thunder OS?" >&2; exit 1; }
[ "$here" = "$DEVICE" ] || { echo "no cabo esta $here, nao $DEVICE" >&2; exit 1; }

stamp=$KERNEL_OUT/.thunder-inputs
want=$(kernel_inputs)
if [ -d "$KERNEL_OUT/lib/modules/$KVER" ] && [ "$(cat "$stamp" 2>/dev/null)" = "$want" ]; then
	echo "kernel ja compilado com estas entradas"
else
	"$repo/tools/build-kernel.sh"
	echo "$want" > "$stamp"
fi
"$repo/tools/package-kernel.sh"
pkg=$(readlink -f "$OUT/linux-$DEVICE.pkg.tar.zst")

# As ferramentas de boot vao junto, do repositorio: o boot.img gravado sai sempre da
# receita atual, e um aparelho de imagem anterior ao pacote do kernel ganha o
# boot-deploy e o hook.
echo "enviando ferramentas de boot e $(basename "$pkg")"
ssh_ "mkdir -p /usr/lib/thunder/initramfs /usr/share/libalpm/hooks"
scp -q -o BatchMode=yes "$repo/initramfs/init" "root@$addr:/usr/lib/thunder/initramfs/init"
scp -q -o BatchMode=yes "$repo/initramfs/thunder-mkboot" "root@$addr:/usr/lib/thunder/thunder-mkboot"
scp -q -o BatchMode=yes "$repo/rootfs/overlay/usr/lib/thunder/boot-deploy" "root@$addr:/usr/lib/thunder/boot-deploy"
scp -q -o BatchMode=yes "$repo/rootfs/overlay/usr/share/libalpm/hooks/90-thunder-boot.hook" \
	"root@$addr:/usr/share/libalpm/hooks/90-thunder-boot.hook"
ssh_ "chmod 755 /usr/lib/thunder/initramfs/init /usr/lib/thunder/thunder-mkboot /usr/lib/thunder/boot-deploy"
scp -q -o BatchMode=yes "$pkg" "root@$addr:/var/cache/pacman/pkg/"
# --overwrite: imagens anteriores ao pacote trazem os modulos como arquivos soltos (sem
# dono no pacman); o pacote assume os da mesma versao. --needed fora: reinstalar a mesma
# versao refaz o boot.img.
ssh_ "rm -f /var/lib/thunder/boot-deploy.status; pacman -U --noconfirm --disable-sandbox \
	--overwrite '/usr/lib/modules/$KVER/*,usr/lib/modules/$KVER/*' /var/cache/pacman/pkg/$(basename "$pkg")"

# O pacman devolve sucesso mesmo quando o hook falha: o resultado vem do status.
read -r st sum < <(ssh_ "cat /var/lib/thunder/boot-deploy.status 2>/dev/null" || true)
case ${st:-} in
gravado)
	echo "boot gravado pelo proprio aparelho"
	if [ "$reboot" = 1 ]; then
		echo "reiniciando o aparelho"
		ssh_bye "systemctl reboot 2>/dev/null || openrc-shutdown -r now"
	fi
	;;
pendente)
	# BOOT_DEPLOY=fastboot: o aparelho montou o boot.img; o PC grava pelo fastboot.
	img=$OUT/boot-update.img
	scp -q -o BatchMode=yes "root@$addr:/var/lib/thunder/boot-new.img" "$img"
	[ "$(sha256sum < "$img" | cut -d' ' -f1)" = "$sum" ] ||
		{ echo "o boot.img copiado do aparelho nao confere; nada gravado" >&2; exit 1; }
	echo "levando o aparelho ao fastboot para gravar o boot"
	ssh_bye "systemctl reboot --reboot-argument=bootloader 2>/dev/null || thunder-reboot bootloader"
	BOOT_IMG=$img FASTBOOT_WAIT=${FASTBOOT_WAIT:-90} "$repo/tools/flash.sh" --sim --so-boot
	echo "esperando o aparelho voltar"
	ok=0
	for _ in $(seq 1 60); do
		sleep 3
		if ssh_ true 2>/dev/null; then ok=1; break; fi
	done
	[ "$ok" = 1 ] || { echo "o aparelho nao voltou por SSH em 3 min: confira a tela/fastboot" >&2; exit 1; }
	n=$(stat -c %s "$img")
	got=$(ssh_ "part=\$(grep -lx PARTNAME=boot\$(grep -o 'androidboot.slot_suffix=[^ ]*' /proc/cmdline | cut -d= -f2) /sys/class/block/*/uevent | head -n 1); head -c $n /dev/\$(sed -n 's/^DEVNAME=//p' \$part) | sha256sum | cut -d' ' -f1")
	[ "$got" = "$sum" ] || { echo "a particao boot nao tem o boot.img novo" >&2; exit 1; }
	ssh_ "echo 'gravado $sum' > /var/lib/thunder/boot-deploy.status"
	echo "kernel atualizado: $(ssh_ 'uname -rv')"
	;;
*)
	echo "o boot-deploy falhou no aparelho (${st:-sem status} ${sum:-}); nada foi reiniciado" >&2
	exit 1
	;;
esac
exit
}
