# Building

Everything is built on an Arch-based host without root. Generated files go to `build/`
(ignored by Git). Run all commands from the repository root.

## Host requirements

- Arch Linux or a derivative. `tools/common.sh` uses `pacman -Sp` only to resolve package
  URLs; nothing is installed on the host.
- Linux kernel 6.7 or newer on the host: the rootfs build needs a per-namespace
  `binfmt_misc` to run aarch64 binaries under qemu.
- `unshare` from util-linux, and a range for your user in `/etc/subuid` and `/etc/subgid`
  (`--map-auto` maps the whole range).
- `bsdtar` (libarchive), `gpg`, `curl`, `sha512sum`, `patch`, `gzip`.
- `e2fsprogs` 1.47 or newer (`mke2fs -d`, `e2fsck`, `dumpe2fs`).
- `mkbootimg` and `avbtool` (Android boot image and vbmeta tools).
- `fastboot`, for flashing only.
- Kernel build basics: `make`, `gcc`, `g++`, `bison`, `flex`, `perl`, `openssl`, `cpio`,
  `kmod`. The aarch64 cross toolchain and `bc` are fetched by step 1.
- The vendor firmware blobs, see [vendor-firmware.md](vendor-firmware.md). They are
  proprietary and never committed.

## Why it needs no root

- `unshare --map-auto --map-root-user` gives a user namespace where you are root with a
  full subuid/subgid map, so file ownership inside the rootfs comes out right
  (`tools/common.sh`, function `userns`).
- Arch's `qemu-user-static` is extracted from its package and registered in the
  namespace's own `binfmt_misc`, so aarch64 binaries run inside the chroot without
  touching the host.
- `tools/achroot.sh` is a rootless `arch-chroot` built on the same namespace.
- `mke2fs -d` runs inside the namespace and writes the image straight from the rootfs
  directory, with the right owners.

## Layout of `build/`

| Path | Content |
| --- | --- |
| `build/dl/` | Downloads: kernel source, Arch packages, Arch Linux ARM tarball and signature |
| `build/tools/` | Static qemu (`qemu-aarch64-static`), GPG home used to verify the tarball (`gnupg/`) |
| `build/cross/` | Cross toolchain and `bc` (step 1) |
| `build/kernel-src/` | Patched kernel source and its `out/` tree |
| `build/kernel/` | Kernel result: `boot/vmlinuz`, `boot/dtbs/mediatek/mt6833.dtb`, `lib/modules/4.19.191-perf`, `config` |
| `build/firmware/` | Vendor firmware you place (step 3) |
| `build/rootfs/` | Arch Linux ARM root filesystem |
| `build/buildroot/` | Throwaway chroot where OpenRC and `thunder-reboot` are built |
| `build/out/` | `initramfs.cpio.gz`, `boot.img`, `userdata.img` |

`BUILD` overrides the location of `build/`. `ROOTFS` and `QEMU` override the rootfs
directory and the qemu binary (defaults `build/rootfs` and
`build/tools/qemu-aarch64-static`).

## Steps

### 1. Cross toolchain

```sh
tools/fetch-cross.sh
```

Extracts `aarch64-linux-gnu-{binutils,gcc,glibc,linux-api-headers}` and `bc` from the
official Arch packages into `build/cross`. Skip it if you already have
`aarch64-linux-gnu-gcc` in `PATH` (or set `CROSS_COMPILE`).

### 2. Kernel

```sh
kernel/build.sh
```

- Downloads Xiaomi_Kernel_OpenSource at commit
  `a40ceab8943703f33e935cb0b86d0f69aa2044dd` and checks its sha512.
- Grafts the MediaTek connectivity drop (`kernel/connectivity/`) over the source.
- Applies `kernel/patches/series` in order.
- Strips `-Werror` from the vendor Makefiles (modern gcc rejects the code otherwise).
- Runs `oldconfig` on `kernel/config-thunder.aarch64` and cross-builds with
  `aarch64-linux-gnu-`. `JOBS` sets parallelism (default: all cores).
- Installs to `build/kernel`. The bootloader only accepts a compressed kernel, so
  `boot/vmlinuz` is `Image.gz`.

### 3. Firmware

Put the proprietary blobs in `build/firmware/` (or point `FIRMWARE` elsewhere). The
rootfs step stops if `nt36672c_tm_01_ts_fw.bin` is missing. File list and how to extract
them: [vendor-firmware.md](vendor-firmware.md).

### 4. Root filesystem

```sh
tools/build-rootfs.sh
```

- Downloads `ArchLinuxARM-aarch64-latest.tar.gz` and verifies its GPG signature against
  key `68B3537F39A313B3E574D06777193F152BDBE6A6`. The build fails if it does not match.
- Extracts it in the user namespace, runs `pacman -Syu`, and removes `linux-aarch64`,
  `mkinitcpio` and `linux-firmware` (we ship our own kernel, initramfs and firmware).
- Builds OpenRC 0.64.1 from `rootfs/packages/openrc/PKGBUILD`, and `thunder-reboot`
  from `device/tools/thunder-reboot.c`, in a separate build chroot so the toolchain
  stays out of the final rootfs.
- Stages `rootfs/overlay`, the kernel modules, the firmware and your SSH key, then runs
  `rootfs/configure.sh` and `rootfs/finalize.sh` inside the chroot.

Environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `THUNDER_USER` | current user (`id -un`) | Login account created on the device |
| `THUNDER_PASSWORD` | `147147` | Password for root and that user, used by the telnet login fallback (`telnet-usb` service). Provisional: change it on the device. The initramfs rescue shell asks for no password |
| `AUTHORIZED_KEYS` | `~/.ssh/id_ed25519.pub` | Public key installed for SSH. Must exist |
| `FIRMWARE` | `build/firmware` | Firmware directory |
| `BUILD` | `build/` in the repository | Output root |

Example:

```sh
THUNDER_USER=me AUTHORIZED_KEYS=~/.ssh/id_ed25519.pub tools/build-rootfs.sh
```

The script re-extracts the tarball and replaces `build/rootfs` on each run.

### 5. Boot image

```sh
tools/build-boot.sh
```

Builds the initramfs from `initramfs/init` plus a static busybox and the touchscreen
firmware taken from `build/rootfs`, then packs `build/out/boot.img` with `mkbootimg`:
header v2, page size 2048, kernel offset `0x40080000`, ramdisk offset `0x51100000`,
tags and dtb offset `0x47c80000`. The initramfs is gzip because `CONFIG_RD_GZIP` is the
only initramfs decompressor in this kernel.

Default command line: `bootopt=64S3,32N2,64N2 buildvariant=user thunder.root=userdata`.
Override it with `CMDLINE`. Parameters understood by the init: `thunder.root=`,
`thunder.init=`, `thunder.debug=1`; the last occurrence wins, because the bootloader
prepends its own arguments (including `root=/dev/ram`).

Outputs: `build/out/boot.img`, `build/out/initramfs.cpio.gz`.

### 6. Userdata image

```sh
tools/make-userdata.sh
```

Creates `build/out/userdata.img`, an ext4 image of `build/rootfs` (`SIZE`, default `4G`),
labeled `thunder-root`. `orphan_file` and `metadata_csum_seed` are disabled, since the
4.19 kernel refuses to mount an ext4 that has them. The image is checked with
`e2fsck -fn`. On first boot `/etc/local.d/10-resize-root.start` grows the filesystem to
the end of the userdata partition.

## Next

Flash with `tools/flash.sh`, see [flashing.md](flashing.md). It is a dry run unless you
pass `--sim`.
