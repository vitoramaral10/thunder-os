# Caminhos e aparelho comuns aos scripts. Tudo que e gerado fica em $BUILD (padrao:
# build/ na raiz do repositorio, fora do Git): downloads, qemu e toolchain sao
# compartilhados; o resto fica em $BUILD/<aparelho>/.
#
# O aparelho vem de DEVICE (ex.: DEVICE=xiaomi-thunder) ou de local.conf na raiz do
# repositorio (ignorado pelo Git), que tambem pode fixar THUNDER_USER, THUNDER_LOCALE etc.
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# local.conf: linhas CHAVE=valor, sem codigo. Nao sobrescreve o que ja veio pelo ambiente.
if [ -f "$repo/local.conf" ]; then
	while IFS= read -r _l || [ -n "$_l" ]; do
		[[ $_l =~ ^[[:space:]]*([A-Z_][A-Z0-9_]*)=(.*)$ ]] || continue
		_k=${BASH_REMATCH[1]} _v=${BASH_REMATCH[2]}
		_v=${_v%\"} _v=${_v#\"}
		[[ $_v == "~/"* ]] && _v=$HOME/${_v#"~/"}
		[ -n "${!_k+x}" ] || export "$_k=$_v"
	done < "$repo/local.conf"
	unset _l _k _v
fi
BUILD=${BUILD:-$repo/build}
QEMU=${QEMU:-$BUILD/tools/qemu-aarch64-static}
BUILDROOT=${BUILDROOT:-$BUILD/buildroot}
# Caches compartilhados entre aparelhos e instalacoes (tools/build-rootfs.sh):
#   base/         Arch Linux ARM extraido, com keyring, atualizado, sem kernel generico
#   packages/     nossos pacotes compilados, por nome e hash da receita
#   pacman-cache/ pacotes baixados pelo pacman, montado no /var/cache/pacman/pkg do chroot
BASE=${BASE:-$BUILD/base}
LOCAL_PKGS=${LOCAL_PKGS:-$BUILD/packages}
PACMAN_CACHE=${PACMAN_CACHE:-$BUILD/pacman-cache}

mkdir -p "$BUILD"/{dl,tools} "$PACMAN_CACHE"

# Carrega devices/$DEVICE/device.sh e define os caminhos do aparelho. Os scripts que
# dependem de aparelho chamam isto logo depois de carregar o common.sh.
require_device() {
	if [ -z "${DEVICE:-}" ]; then
		echo "escolha o aparelho: DEVICE=<nome> $0 (ou DEVICE=... em local.conf)" >&2
		echo "aparelhos: $(cd "$repo/devices" && ls -d */ | tr -d / | tr '\n' ' ')" >&2
		exit 2
	fi
	DEVICE_DIR=$repo/devices/$DEVICE
	[ -f "$DEVICE_DIR/device.sh" ] || { echo "aparelho desconhecido: $DEVICE (sem $DEVICE_DIR/device.sh)" >&2; exit 2; }
	# Padroes das opcionais; o device.sh sobrescreve.
	KERNEL_IMAGE=Image.gz
	KERNEL_DTB=
	KERNEL_STRIP_WERROR=0
	# Arquivos e pastas (relativos a pasta do aparelho) que entram no kernel, alem do
	# KERNEL_CONFIG: o carimbo de reaproveitamento so olha estes.
	KERNEL_INPUTS=(patches)
	BOOTIMG_ARGS=()
	CMDLINE=
	ROOT_PARTITION=userdata
	INITRAMFS_FIRMWARE=()
	MODEM_ROM_TREES=()
	FASTBOOT_PRODUCTS=()
	ANDROID_DEVICES=()
	FASTBOOT_AB=1
	FASTBOOT_SLOT=
	FASTBOOT_DISABLE_VBMETA=1
	# Como um kernel novo chega a particao boot: partition (o aparelho grava sozinho) ou
	# fastboot (o aparelho prepara o boot.img e o PC grava pelo fastboot).
	BOOT_DEPLOY=partition
	FIRMWARE_REQUIRED=()
	FIRMWARE_FILES=()
	FIRMWARE_ROM_PARTITION=vendor
	FIRMWARE_ROM_DIRS=(/firmware /etc/firmware /vendor/firmware)
	DEVICE_PACKAGES=()
	# Pacotes nossos do aparelho (rootfs/packages/<nome>/PKGBUILD), montados como os do perfil.
	DEVICE_LOCAL_PACKAGES=()
	# Interface Wi-Fi de estacao; vazio = aparelho sem Wi-Fi (sem NetworkManager nem thunder-wifi).
	WIFI_INTERFACE=
	# Units do systemd do aparelho a habilitar, com sufixo (ex.: thunder-wmt-wifi.service).
	DEVICE_SERVICES=()
	# Swap (rootfs/configure.sh). zram pelo zram-generator: tamanho na sintaxe do
	# zram-size= dele e algoritmo (vazio = o padrao do kernel); ZRAM_SIZE vazio = sem zram.
	# SWAPFILE_SIZE (ex.: 4G) cria o /swapfile no 1o boot, com prioridade abaixo do zram:
	# so entra quando o zram enche. Vazio = sem swap em disco (poupa a flash).
	ZRAM_SIZE="min(ram / 2, 4096)"
	ZRAM_ALGORITHM=
	SWAPFILE_SIZE=
	# Perfis de interface que o aparelho aceita (rootfs/profiles/); vazio = todos.
	DEVICE_PROFILES=()
	# Conectividade MediaTek WMT (pacote thunder-mediatek-wmt); vazio = aparelho sem WMT.
	# Viram /etc/thunder/wmt.conf no rootfs/configure.sh.
	WMT_CORE_MODULES=()
	WMT_WIFI_MODULES=()
	WMT_BT_MODULES=()
	WMT_BT_ADDR=
	WMT_ROM_PATCHES=()
	WMT_HIF=
	WMT_WIFI_NVRAM=
	. "$DEVICE_DIR/device.sh"
	if [ ${#DEVICE_PROFILES[@]} -eq 0 ]; then
		local p
		for p in "$repo"/rootfs/profiles/*.sh; do DEVICE_PROFILES+=("$(basename "$p" .sh)"); done
	fi

	local v missing=
	for v in DEVICE_NAME DEVICE_VENDOR DEVICE_CODENAME KERNEL_URL KERNEL_SHA512 \
		KERNEL_VERSION KERNEL_CONFIG; do
		[ -n "${!v:-}" ] || missing="$missing $v"
	done
	[ ${#FASTBOOT_PRODUCTS[@]} -gt 0 ] || missing="$missing FASTBOOT_PRODUCTS"
	[ -z "$missing" ] || { echo "$DEVICE_DIR/device.sh nao define:$missing" >&2; exit 2; }

	WORK=${WORK:-$BUILD/$DEVICE}
	ROOTFS=${ROOTFS:-$WORK/rootfs}
	FIRMWARE=${FIRMWARE:-$WORK/firmware}
	# Arvores so leitura do modem tiradas da ROM (MODEM_ROM_TREES): fora de $FIRMWARE, que
	# vai inteiro para /usr/lib/firmware.
	MODEM_RO=${MODEM_RO:-$WORK/modem-ro}
	KERNEL_SRC=$WORK/kernel-src
	KERNEL_OUT=${KERNEL:-$WORK/kernel}
	OUT=$WORK/out
	KVER=$KERNEL_VERSION
	mkdir -p "$WORK" "$OUT"
}

# Uma montagem por vez: base, pacotes, buildroot e caches sao compartilhados em $BUILD,
# e duas instalacoes juntas apagam o que a outra esta escrevendo. Os scripts chamados
# de dentro de quem ja tem a trava (THUNDER_LOCK_HELD) passam direto. O kernel solta a
# trava sozinho quando o processo termina, ate num kill -9.
take_build_lock() {
	[ "${THUNDER_LOCK_HELD:-}" = 1 ] && return 0
	local f=$BUILD/.lock
	exec 9<>"$f"
	if ! flock -n 9; then
		echo "outra montagem do Thunder OS esta rodando (pid $(cat "$f" 2>/dev/null || echo ?)); espere ela terminar" >&2
		exit 75
	fi
	truncate -s 0 "$f"
	echo $$ >&9
	export THUNDER_LOCK_HELD=1
}

# Hash das entradas do kernel do aparelho (depois de require_device): KERNEL_CONFIG e os
# caminhos de KERNEL_INPUTS (patches/, enxertos), as variaveis KERNEL_* e o hook de
# preparo. Mexer em servico, pacote ou doc do aparelho nao muda o hash. O install.sh compara com $KERNEL_OUT/.thunder-inputs para pular a build.
kernel_inputs() {
	{
		# So o que o device.sh declara como entrada do kernel (KERNEL_INPUTS): um arquivo de
		# servico ou script do aparelho nao pode forcar uma recompilacao de 17 min.
		(cd "$DEVICE_DIR" && find "$KERNEL_CONFIG" "${KERNEL_INPUTS[@]}" -type f -print0 2>/dev/null |
			LC_ALL=C sort -z | xargs -0 -r sha256sum)
		declare -p KERNEL_URL KERNEL_SHA512 KERNEL_VERSION KERNEL_CONFIG KERNEL_IMAGE \
			KERNEL_DTB KERNEL_STRIP_WERROR 2>/dev/null
		declare -f device_kernel_prepare 2>/dev/null || true
	} | sha256sum | cut -d' ' -f1
}

# Baixa um pacote do Arch do host pelo nome (o host precisa ser Arch ou derivado:
# usa `pacman -Sp` so para resolver a URL; nada e instalado).
fetch_host_pkg() {
	local name=$1 url
	# -dd: so o pacote pedido (sem -dd o -p lista as dependencias antes dele).
	url=$(pacman -Sddp --print-format '%l' "$name" 2>/dev/null | grep -E '^(https?|ftp)://' | head -n 1)
	[ -n "$url" ] || { echo "nao achei a URL do pacote $name" >&2; return 1; }
	# Alguns mirrors recusam (HTTP 418) quem nao se apresenta como pacman.
	curl -fsSL -A 'pacman/7.0.0 (Linux x86_64) libalpm/15.0.0' \
		-o "$BUILD/dl/${url##*/}" "$url" || return 1
	echo "$BUILD/dl/${url##*/}"
}

# Root do user namespace, com o mapa de subuid completo, sem sudo.
userns() {
	unshare --map-auto --map-root-user -- "$@"
}
