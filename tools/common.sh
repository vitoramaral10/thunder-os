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

mkdir -p "$BUILD"/{dl,tools}

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
	BOOTIMG_ARGS=()
	CMDLINE=
	ROOT_PARTITION=userdata
	INITRAMFS_FIRMWARE=()
	FASTBOOT_PRODUCTS=()
	FASTBOOT_AB=1
	FASTBOOT_SLOT=
	FASTBOOT_DISABLE_VBMETA=1
	FIRMWARE_REQUIRED=()
	DEVICE_PACKAGES=()
	DEVICE_SERVICES=()
	. "$DEVICE_DIR/device.sh"

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
	KERNEL_SRC=$WORK/kernel-src
	KERNEL_OUT=${KERNEL:-$WORK/kernel}
	OUT=$WORK/out
	KVER=$KERNEL_VERSION
	mkdir -p "$WORK" "$OUT"
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
