#!/bin/bash
# Toolchain de cross-compilacao aarch64 e bc, extraidos dos pacotes oficiais do Arch para
# $BUILD/cross, sem instalar nada no sistema. Usado pelo kernel/build.sh.
set -euo pipefail
. "$(dirname "$0")/common.sh"

dest=$BUILD/cross
rm -rf "$dest"
mkdir -p "$dest"
for p in aarch64-linux-gnu-binutils aarch64-linux-gnu-gcc aarch64-linux-gnu-glibc \
	aarch64-linux-gnu-linux-api-headers bc; do
	f=$(fetch_host_pkg "$p")
	bsdtar -xf "$f" -C "$dest" --exclude '.PKGINFO' --exclude '.BUILDINFO' \
		--exclude '.MTREE' --exclude '.INSTALL'
done
"$dest/usr/bin/aarch64-linux-gnu-gcc" --version | head -n 1
"$dest/usr/bin/bc" --version | head -n 1
