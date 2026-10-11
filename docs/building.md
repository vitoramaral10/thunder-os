# Building

Everything is built on an Arch-based host without root. Generated files go to `build/`
(ignored by Git). Run all commands from the repository root.

## Choosing the device

Every step except the cross toolchain builds for one device, a folder under
[`devices/`](../devices). Set it once in `local.conf` (ignored by Git):

```sh
cp local.conf.example local.conf    # then edit DEVICE=
```

or per command: `DEVICE=xiaomi-thunder tools/build-kernel.sh`. The environment
wins over `local.conf`. `local.conf` takes plain `KEY=value` lines (no shell
code) and can hold any variable from the tables below.

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
- The device's proprietary firmware, copied from your own phone as its folder
  explains. It is never committed.

## Why it needs no root

- `unshare --map-auto --map-root-user` gives a user namespace where you are root with a
  full subuid/subgid map, so file ownership inside the rootfs comes out right
  (`tools/common.sh`, function `userns`).
- Arch's `qemu-user-static` is extracted from its package and registered in the
  namespace's own `binfmt_misc`, so aarch64 binaries run inside the chroot without
  touching the host.
- `tools/achroot.sh` is a rootless `arch-chroot` built on the same namespace. With no
  `ROOTFS` it enters the device's rootfs.
- `mke2fs -d` runs inside the namespace and writes the image straight from the rootfs
  directory, with the right owners.

## Layout of `build/`

Shared by all devices:

| Path | Content |
| --- | --- |
| `build/dl/` | Downloads: kernel sources, Arch packages, Arch Linux ARM tarball and signature |
| `build/tools/` | Static qemu (`qemu-aarch64-static`), GPG home used to verify the tarball (`gnupg/`) |
| `build/cross/` | Cross toolchain and `bc` (step 1) |
| `build/base/`, `build/base.key` | Cached Arch Linux ARM base: the verified tarball extracted, keyring set up, fully upgraded, generic kernel removed. Rebuilt only when the tarball changes |
| `build/packages/` | Our packages (`rootfs/packages/*`) and `thunder-reboot`, built once per recipe hash |
| `build/pacman-cache/` | Packages downloaded by pacman, bind-mounted into every chroot. Files unused for 60 days are removed |
| `build/buildroot/` | Chroot where our packages are built, kept between runs and only used when a recipe changed |

Per device, in `build/<device>/`:

| Path | Content |
| --- | --- |
| `kernel-src/` | Patched kernel source and its `out/` tree |
| `kernel/` | Kernel result: `boot/vmlinuz`, `boot/dtbs/`, `lib/modules/<version>`, `config` |
| `firmware/` | Vendor firmware you place (step 3) |
| `rootfs/` | Arch Linux ARM root filesystem |
| `out/` | `initramfs.cpio.gz`, `boot.img`, `userdata.img` |

`BUILD` moves `build/`; `WORK` moves the device folder. `ROOTFS`, `FIRMWARE`,
`KERNEL` and `QEMU` override the rootfs, the firmware directory, the installed
kernel and the qemu binary.

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
tools/build-kernel.sh
```

- Downloads `KERNEL_URL` from the device's `device.sh` and checks it against
  `KERNEL_SHA512`.
- Runs the device's `device_kernel_prepare` hook, if it has one (the thunder
  grafts the MediaTek connectivity drop here).
- Applies the device's `patches/series` in order.
- Strips `-Werror` from the Makefiles when `KERNEL_STRIP_WERROR=1` (vendor code
  rarely builds with modern gcc otherwise).
- Runs `olddefconfig` on the device's `KERNEL_CONFIG` and cross-builds with
  `aarch64-linux-gnu-`. `JOBS` sets parallelism (default: all cores).
- Installs to `build/<device>/kernel`; `boot/vmlinuz` is the device's
  `KERNEL_IMAGE`.

`PREPARE_ONLY=1` stops after `olddefconfig`, to check that the source, hook,
patches and config apply. `REBUILD=1` skips the preparation and rebuilds the
tree already in `kernel-src/`, for quick iteration on a hand-applied change.

### 2b. Kernel package

```sh
tools/package-kernel.sh
```

Packs the kernel as a pacman package, `build/<device>/out/linux-<device>-<version>-1-aarch64.pkg.tar.zst`:
the modules, the kernel, the DTB and `boot.conf` (the boot.img layout from `device.sh`). The image
installs it with pacman, so the phone can later be updated with `pacman` like any other package.
No proprietary firmware goes in: the initramfs is assembled on the phone, from the firmware already
there. `install.sh` runs this after the kernel step.

### 3. Firmware

```sh
tools/extract-firmware.sh ~/Downloads/<factory-rom>.tgz
```

Copies the device's `FIRMWARE_FILES` from your factory ROM into
`build/<device>/firmware/`, reading only the vendor partition out of `super.img`
(about 30 s for a 6 GB package; nothing is unpacked to disk besides that
partition). You can also put the blobs there by hand, or point `FIRMWARE`
elsewhere. The rootfs step stops if a file in the device's `FIRMWARE_REQUIRED`
or `INITRAMFS_FIRMWARE` is missing. Which files and how to extract them is in
the device's docs.

### 4. Root filesystem

```sh
tools/build-rootfs.sh
```

- Downloads `ArchLinuxARM-aarch64-latest.tar.gz` and verifies its GPG signature against
  key `68B3537F39A313B3E574D06777193F152BDBE6A6`. The build fails if it does not match.
- Extracts it in the user namespace, runs `pacman -Syu`, and removes `linux-aarch64`,
  `mkinitcpio` and `linux-firmware` (the device brings its own kernel and firmware,
  and Thunder OS its own initramfs).
- Builds OpenRC 0.64.1 from `rootfs/packages/openrc/PKGBUILD`, and `thunder-reboot`
  from `rootfs/src/thunder-reboot.c`, in a separate build chroot so the toolchain
  stays out of the final rootfs.
- Stages `rootfs/overlay`, the device's `overlay/`, the kernel modules, the
  firmware and your SSH key, then runs `rootfs/configure.sh` and
  `rootfs/finalize.sh` inside the chroot. `configure.sh` installs the device's
  `DEVICE_PACKAGES`, enables its `DEVICE_SERVICES` and writes its identity to
  `/etc/thunder/device`.

Environment variables (or `local.conf`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `THUNDER_USER` | current user (`id -un`) | Login account created on the device |
| `THUNDER_PASSWORD` | `147147` | Password of that user, also used by the telnet login fallback (`telnet-usb` service). Provisional: change it on the device. The initramfs rescue shell asks for no password |
| `THUNDER_ROOT_PASSWORD` | `THUNDER_PASSWORD` | Password of root |
| `THUNDER_PROFILE` | `none` | Interface profile from `rootfs/profiles/`. Profiles marked `planned` are refused (set `THUNDER_ALLOW_PLANNED=1` to build one anyway while porting) |
| `THUNDER_EXTRA_PACKAGES` | empty | Extra Arch packages, separated by spaces |
| `THUNDER_WIFI_SSID`, `THUNDER_WIFI_PASSWORD` | empty | Wi-Fi network saved in the image, for devices with `WIFI_INTERFACE`. Only the derived PSK is written; empty password means an open network |
| `THUNDER_LOCALE` | `en_US.UTF-8` | `LANG` of the image; `en_US.UTF-8` is always generated too |
| `THUNDER_TIMEZONE` | `UTC` | Zone under `/usr/share/zoneinfo`, e.g. `America/Sao_Paulo` |
| `THUNDER_HOSTNAME` | the device codename | Hostname |
| `AUTHORIZED_KEYS` | `~/.ssh/id_ed25519.pub` if it exists | Public key installed for SSH. Set it empty for no key (password login only) |
| `FIRMWARE` | `build/<device>/firmware` | Firmware directory |

Example:

```sh
THUNDER_USER=me THUNDER_LOCALE=pt_BR.UTF-8 tools/build-rootfs.sh
```

The script re-extracts the tarball and replaces the device's rootfs on each run.

### 5. Boot image

```sh
tools/build-boot.sh
```

Runs `initramfs/thunder-mkboot`, the same recipe the phone runs when its kernel
is updated: the initramfs comes from `initramfs/init` plus a static busybox,
`thunder-reboot`, `/etc/thunder/device` and the device's `INITRAMFS_FIRMWARE`,
all taken from the built rootfs; the kernel, DTB and boot.img layout come from
the kernel package tree (`build/<device>/kernel-pkg`). The cpio is written with
fixed inodes, owners and dates, so the PC and the phone produce the same boot.img
byte for byte. The initramfs is gzip, the one initramfs decompressor every vendor
kernel has.

The command line is the device's `CMDLINE`, then `thunder.root=<ROOT_PARTITION>`,
then `CMDLINE_EXTRA` if you set it. Parameters understood by the init:
`thunder.root=`, `thunder.init=`, `thunder.debug=1`; the last occurrence wins,
because bootloaders often prepend their own arguments. For example:

```sh
CMDLINE_EXTRA=thunder.debug=1 OUT_NAME=boot-debug.img tools/build-boot.sh
```

Outputs: `build/<device>/out/boot.img` (or `OUT_NAME`), `build/<device>/out/initramfs.cpio.gz`.

### 6. Root partition image

```sh
tools/make-userdata.sh
```

Creates `build/<device>/out/userdata.img`, an ext4 image of the rootfs (`SIZE`,
default `4G`), labeled `thunder-root`. `orphan_file` and `metadata_csum_seed` are
disabled, since kernels before 5.15 refuse to mount an ext4 that has them. The
image is checked with `e2fsck -fn`. On first boot
`/etc/local.d/10-resize-root.start` grows the filesystem to the end of the
partition.

## What is reused between runs

| Step | Reused when | Force a fresh one |
| --- | --- | --- |
| Cross toolchain | `build/cross` exists | delete `build/cross` |
| Kernel | the kernel inputs did not change: the device folder minus `overlay/`, `docs/` and `device.sh`, plus the `KERNEL_*` variables and `device_kernel_prepare` (`kernel/.thunder-inputs`) | delete `build/<device>/kernel` |
| Firmware | same ROM (path, size, date) and same `FIRMWARE_FILES` (`build/<device>/firmware.stamp`) | delete the stamp |
| Arch base | same tarball; each run still applies the day's incremental upgrade | `THUNDER_REFRESH_BASE=1` (downloads the latest tarball) |
| Our packages | same recipe (hash of `rootfs/packages/<name>/`, of `thunder-reboot.c`) | delete `build/packages/<name>` |
| Device rootfs | never: it carries your user, passwords and profile. It is a reflink copy of the base (instant on btrfs/xfs) plus the configuration | — |

The first run on a machine takes as long as before; the next ones skip
straight to the configuration.

## All steps at once

```sh
THUNDER_ROM=~/Downloads/<factory-rom>.tgz tools/install.sh
```

Runs steps 1 to 6 (and the flash, with `THUNDER_FLASH=1`), skipping the
toolchain if present and the kernel if its inputs did not change since the last
build. It prints `::step <id> <title>` before each step and `::done` or
`::error <message>` at the end; this is what the graphical installer reads.

## Updating the kernel without reinstalling

```sh
tools/update-kernel.sh            # build if needed, package, send, install
tools/update-kernel.sh --reboot   # and reboot into it
```

For a phone already running Thunder OS on the cable. It installs the kernel
package with pacman on the phone; the package's hook (`90-thunder-boot.hook`)
runs `/usr/lib/thunder/boot-deploy`, which assembles the boot.img on the phone.
What happens next depends on the device's `BOOT_DEPLOY`:

- `partition`: the phone writes its own boot partition, keeping the previous
  boot.img in `/var/lib/thunder/boot-previous.img` and checking the write by
  sha256 (restoring the copy if it does not match).
- `fastboot` (the thunder): the bootloader write-protects the boot partition, so
  the phone leaves the boot.img pending, and the script copies it back, checks
  its hash, sends the phone to fastboot (`thunder-reboot bootloader`), flashes
  it, waits for SSH and checks the partition. About 35 s, no buttons, no data
  touched.

The boot tools (`init`, `thunder-mkboot`, `boot-deploy`, the hook) are copied
from the repository on every run, so the boot.img always comes from the current
recipe, and phones installed before the kernel package migrate on the first run.

## Next

Flash with `tools/flash.sh`, see [flashing.md](flashing.md). It is a dry run unless you
pass `--sim`.
