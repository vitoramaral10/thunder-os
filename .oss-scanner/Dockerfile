# Build of Thunder OS for Anthropic's OSS Scanner (https://github.com/anthropics/oss-scanner).
# The scanner builds this with the repository as the build context, online; the scan
# afterwards runs offline, so everything is fetched here.
#
# What gets built, the way the maintainers build it:
#   - the xiaomi-thunder kernel (vendor 4.19 + our patches/series), cross-compiled for
#     aarch64 with the toolchain tools/fetch-cross.sh takes from Arch's packages;
#   - thunder-reboot (static, aarch64), as tools/build-rootfs.sh builds it;
#   - the Flutter installer (analyze, tests, Linux debug build);
#   - the thunder-mediatek-wmt unit tests.
# Not built: the Arch Linux ARM root filesystem (tools/build-rootfs.sh). It needs user
# namespaces and binfmt_misc with qemu-user, which a plain docker build does not have;
# its scripts (rootfs/, tools/) are read as source. See threat_model.md.
#
# Arch, not Debian: tools/fetch-cross.sh resolves the toolchain packages with pacman.
FROM archlinux:base-devel

RUN pacman -Syu --noconfirm --needed \
      git curl libarchive bison flex perl openssl cpio kmod python unzip xz zstd which \
      clang cmake ninja pkgconf gtk3 \
    && pacman -Scc --noconfirm

# Flutter, pinned to the version the installer is developed with.
ARG FLUTTER_VERSION=3.47.7
RUN git clone --depth 1 --branch "$FLUTTER_VERSION" https://github.com/flutter/flutter.git /opt/flutter \
    && git config --global --add safe.directory /opt/flutter
ENV PATH=/opt/flutter/bin:$PATH \
    FLUTTER_SUPPRESS_ANALYTICS=true
RUN flutter config --no-analytics --enable-linux-desktop && flutter precache --linux

# scanner contract: the checkout lives inside the image, at /src
COPY . /src
WORKDIR /src
ENV DEVICE=xiaomi-thunder \
    BUILD=/src/build

# Kernel: toolchain, then source (checked against KERNEL_SHA512), device hook (MediaTek
# connectivity graft), patches/series, config, build. The tree stays in
# build/xiaomi-thunder/kernel-src with its objects; the installed kernel and modules in
# build/xiaomi-thunder/kernel.
# JOBS empty means every core (the scanner); set it to build on a smaller machine.
ARG JOBS=
RUN mkdir -p "$BUILD/dl" \
    && tools/fetch-cross.sh \
    && JOBS="${JOBS:-$(nproc)}" tools/build-kernel.sh

# thunder-reboot, as the image build compiles it (static), for the phone and for the host.
RUN mkdir -p build/thunder-reboot \
    && build/cross/usr/bin/aarch64-linux-gnu-gcc -static -O2 -g -Wall -Wextra \
         -o build/thunder-reboot/thunder-reboot rootfs/src/thunder-reboot.c \
    && gcc -O0 -g -fno-omit-frame-pointer -Wall -Wextra \
         -o build/thunder-reboot/thunder-reboot-host rootfs/src/thunder-reboot.c

# Installer: dependencies, then a build. Tests and analysis must not abort the image.
RUN cd installer && flutter pub get && flutter build linux --debug
RUN cd installer && (flutter analyze || echo "WARNING: flutter analyze reported issues") \
    && (flutter test || echo "WARNING: flutter test reported failures")

# Python: the WMT daemon's unit tests (no install needed; it is a single module).
RUN (python3 -B -m unittest discover -s rootfs/packages/thunder-mediatek-wmt -v \
      || echo "WARNING: thunder-mediatek-wmt tests reported failures")
