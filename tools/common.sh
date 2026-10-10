# Caminhos comuns aos scripts. Tudo que e gerado fica em $BUILD (padrao: build/ na raiz
# do repositorio, fora do Git).
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
BUILD=${BUILD:-$repo/build}
QEMU=${QEMU:-$BUILD/tools/qemu-aarch64-static}
ROOTFS=${ROOTFS:-$BUILD/rootfs}
KVER=4.19.191-perf

mkdir -p "$BUILD"/{dl,out,tools}

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
