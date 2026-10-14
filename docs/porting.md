# Porting a device

A port is one folder, `devices/<vendor>-<codename>/`, with a `device.sh` that
tells the shared tools how to build and flash for that phone. The core
(initramfs, rootfs, tools) should not need changes; if your phone needs one,
open an issue or make the change generic in the same pull request.

Thunder OS v1 targets **Android phones that boot a `boot.img` through
fastboot**, with an unlocked bootloader and an aarch64 kernel. Other boot
methods (U-Boot, UEFI, SD card) are not supported yet.

## Before you start

- An unlocked bootloader, and the factory ROM package for your exact model and
  region, so you can always go back.
- The kernel source for your phone: the vendor's GPL release (Xiaomi publishes
  on `MiCode/Xiaomi_Kernel_OpenSource`, others on their open-source portals) or
  a mainline/close-to-mainline tree if your SoC has one. A tarball URL with a
  fixed commit, so the build is reproducible.
- The stock `boot.img`. `unpack_bootimg --boot_img boot.img --format=mkbootimg`
  prints the header version, page size, offsets and stock command line you
  need for `BOOTIMG_ARGS` and `CMDLINE`.
- The kernel `.config`: from the vendor defconfig, or `/proc/config.gz` on the
  running stock ROM.

## Steps

1. `cp -r devices/xiaomi-thunder devices/<vendor>-<codename>`, delete what is
   thunder-only (`connectivity/`, `wifi/`, `bluetooth/`, the
   patches, the overlay, the docs) and rewrite `device.sh`.
2. Put the kernel config in the folder and apply the
   [kernel requirements](#kernel-requirements) below.
3. `DEVICE=<folder> PREPARE_ONLY=1 tools/build-kernel.sh` until the source,
   hook, patches and config apply; then the full `tools/build-kernel.sh`.
4. Copy the firmware your built-in drivers need to `build/<folder>/firmware/`,
   then `tools/build-rootfs.sh`, `tools/build-boot.sh` and
   `tools/make-userdata.sh`.
5. Test the kernel without writing anything: `fastboot boot
   build/<folder>/out/boot.img` boots it once from RAM, on bootloaders that
   allow it. Add `CMDLINE_EXTRA=thunder.debug=1` to stop at the rescue shell.
6. Flash with `tools/flash.sh` (read [flashing.md](flashing.md) first).
7. Write a `README.md` for the folder (copy the thunder one's shape: identity,
   what works, device notes) and add the device to the tables in
   [README.md](../README.md) and [devices/README.md](../devices/README.md).

If nothing appears on the cable, the initramfs probably never ran or the USB
gadget did not come up. `thunder.debug=1` also starts a UDP beacon that sends
the previous boot's pstore and the current dmesg to `172.16.42.2:9999`
(`nc -lu 9999` on the host, after sending one datagram to the phone's port 9999
so your firewall lets the answers in).

## `device.sh` reference

The file is sourced by bash. Relative paths are relative to the device folder,
and `$DEVICE_DIR` holds its absolute path. Defaults are in
`tools/common.sh` (`require_device`).

### Identity

| Variable | Required | Meaning |
|---|---|---|
| `DEVICE_NAME` | yes | Marketing name, e.g. `"Redmi 10 5G / POCO M4 5G"`. Shown by the USB gadget |
| `DEVICE_VENDOR` | yes | Manufacturer, e.g. `Xiaomi`. USB gadget manufacturer string |
| `DEVICE_CODENAME` | yes | Codename; default hostname and USB serial fallback |
| `DEVICE_SOC` | no | Documentation only |
| `DEVICE_MAINTAINER` | no | GitHub handle of who answers for the port |
| `DEVICE_STATUS` | no | `bring-up`, `boots` or `daily` ([devices/README.md](../devices/README.md)) |
| `ANDROID_DEVICES` | no | `ro.product.device` values of the stock Android, so the installer recognizes the phone over adb |

### Kernel

| Variable | Default | Meaning |
|---|---|---|
| `KERNEL_URL` | required | Tarball of the source at a fixed commit. Extracted with the top directory stripped |
| `KERNEL_SHA512` | required | sha512 of that tarball. The build stops if it does not match |
| `KERNEL_VERSION` | required | `uname -r` of the result (`make kernelrelease`); modules install under this name, and `build-kernel.sh` fails if they do not |
| `KERNEL_CONFIG` | required | The `.config`, run through `olddefconfig` |
| `KERNEL_IMAGE` | `Image.gz` | File under `arch/arm64/boot/` that becomes the boot.img kernel (`Image`, `Image.gz`, `Image.gz-dtb`...) |
| `KERNEL_DTB` | empty | DTB under the installed `dtbs/` passed to `mkbootimg --dtb`; empty for no `--dtb` (appended DTB or header v0/v1) |
| `KERNEL_INPUTS` | `(patches)` | Paths in the device folder that feed the kernel build besides `KERNEL_CONFIG` (patches, source grafts). Only these, the `KERNEL_*` variables and `device_kernel_prepare` decide whether `install.sh`/`update-kernel.sh` reuse the last kernel build |
| `KERNEL_STRIP_WERROR` | `0` | `1` removes `-Werror` from every Makefile/Kbuild; vendor trees often need it on modern gcc |
| `patches/series` | — | Optional. Patch file names, one per line, `#` for comments, applied with `patch -p1` in order |
| `device_kernel_prepare` | — | Optional bash function, called with the source directory after extraction and before the patches. For source grafts and other steps a patch cannot express |

### Boot image and initramfs

| Variable | Default | Meaning |
|---|---|---|
| `BOOTIMG_ARGS` | `()` | Bash array of `mkbootimg` arguments: header version, page size, base, offsets. Copy them from the stock `boot.img` |
| `CMDLINE` | empty | Base kernel command line; `thunder.root=$ROOT_PARTITION` is appended. Users add more with `CMDLINE_EXTRA` |
| `ROOT_PARTITION` | `userdata` | GPT partition name (`PARTNAME`) the initramfs mounts as root and `flash.sh` writes |
| `INITRAMFS_FIRMWARE` | `()` | Firmware files copied into the initramfs, for built-in drivers that load firmware before the root is mounted |

The initramfs is gzip-compressed, since every vendor kernel has
`CONFIG_RD_GZIP`. The kernel command line is read with "the last value wins",
because many bootloaders prepend their own arguments (including `root=`).

### Fastboot

| Variable | Default | Meaning |
|---|---|---|
| `FASTBOOT_PRODUCTS` | required | Accepted values of `fastboot getvar product`. `flash.sh` refuses any other phone |
| `FASTBOOT_AB` | `1` | `1` writes `boot_<slot>` and `vbmeta_<slot>`; `0` writes `boot` and `vbmeta` |
| `FASTBOOT_SLOT` | empty | If set, `flash.sh` refuses unless that slot is active. Use it for the slot you tested |
| `FASTBOOT_DISABLE_VBMETA` | `1` | Writes an empty vbmeta with verification disabled before `boot`. `0` for phones without AVB |
| `BOOT_DEPLOY` | `partition` | How a kernel update reaches the boot partition. `partition`: the phone writes it. `fastboot`: the bootloader write-protects it (writes fail with `DATA PROTECT`, sense key 0x7), so `tools/update-kernel.sh` flashes it over fastboot. Test with a write of identical data before choosing `partition` |

### Root filesystem

| Variable | Default | Meaning |
|---|---|---|
| `FIRMWARE_REQUIRED` | `()` | Files that must exist in `$FIRMWARE` before the rootfs build starts |
| `DEVICE_PACKAGES` | `()` | Extra Arch packages installed in the image |
| `DEVICE_SERVICES` | `()` | systemd units (with suffix, e.g. `thunder-wmt-wifi.service`) from the overlay (`overlay/usr/lib/systemd/system`) or from packages, enabled with `systemctl enable` |
| `DEVICE_LOCAL_PACKAGES` | `()` | Our own packages (`rootfs/packages/<name>/PKGBUILD`) the device needs, built like `PROFILE_LOCAL_PACKAGES` and installed before them; updatable on the phone with `pacman -U` |
| `WMT_CORE_MODULES`, `WMT_WIFI_MODULES` | `()` | MediaTek WMT (CONNAC 1.0 SoC): modules loaded before `DO_MODULE_INIT`, and the Wi-Fi modules loaded after CONNSYS is up. Non-empty `WMT_CORE_MODULES` writes `/etc/thunder/wmt.conf` for `thunder-wmt` |
| `WMT_ROM_PATCHES` | `()` | ROM patch file names in the firmware directory; type and EMI offset are read from each header. List them in `FIRMWARE_REQUIRED` too, together with `WMT_SOC.cfg` and the Wi-Fi RAM code |
| `WMT_HIF` | `0x23` | `SET_STP_MODE` argument: HIF type in bits 0-3 (`0x03` = BTIF full), FM mode in bits 4-7 |
| `WMT_WIFI_NVRAM` | empty | `<partlabel>:<path in its ext4>` of the Wi-Fi NVRAM, pushed before the first power-on; empty = no NVRAM |
| `WIFI_INTERFACE` | empty | Station interface (e.g. `wlan0`). Set it and the image gets NetworkManager (wpa_supplicant backend) managing only this interface, and `thunder-wifi`. If the driver needs a bring-up, ship a unit that creates the interface (MediaTek WMT: the `thunder-mediatek-wmt` package and the `WMT_*` variables); nothing has to order after it (NetworkManager follows hotplug). Extra driver interfaces are left unmanaged automatically |
| `ZRAM_SIZE` | `min(ram / 2, 4096)` | Compressed swap in RAM via `zram-generator`, in its `zram-size =` syntax (MiB), priority 100; empty = no zram |
| `ZRAM_ALGORITHM` | empty | zram compressor (e.g. `lz4`); it must be built into the kernel (`cat /sys/block/zram0/comp_algorithm`). Empty = the kernel default |
| `SWAPFILE_SIZE` | empty | Disk swap behind zram (e.g. `4G`): `thunder-swapfile.service` creates `/swapfile` on the first boot, after the root filesystem grows, and it is used at priority 10, only once zram is full. Empty = no disk swap, which spares the flash |
| `DEVICE_PROFILES` | all | Interface profiles (`rootfs/profiles/`) the device offers; empty means all |
| `overlay/` | — | Optional. Copied over `/` after the core and profile overlays, so a device file can replace either |

### Firmware from the factory ROM

`tools/extract-firmware.sh <rom>` copies the proprietary files from the user's
own factory ROM: the fastboot package as downloaded (`.tgz`), its extracted
folder or a bare `super.img`. It reads one logical partition straight out of
`super.img` (sparse or raw) without unpacking the rest, then pulls the files
out of it with `debugfs` (ext4) or `fsck.erofs` (EROFS), all without root.

| Variable | Default | Meaning |
|---|---|---|
| `FIRMWARE_FILES` | `()` | File names to copy. Empty means the device needs no ROM |
| `FIRMWARE_ROM_PARTITION` | `vendor` | Logical partition in `super`; `_a`, `_b` and no suffix are tried in that order |
| `FIRMWARE_ROM_DIRS` | `(/firmware /etc/firmware /vendor/firmware)` | Directories searched inside the partition, in order |

The whole firmware directory goes to `/usr/lib/firmware`, and the kernel
modules to `/usr/lib/modules/$KERNEL_VERSION`.

## Kernel requirements

The core needs these from the kernel config:

| Option | Why |
|---|---|
| `CONFIG_BLK_DEV_INITRD`, `CONFIG_RD_GZIP` | the initramfs |
| `CONFIG_DEVTMPFS` | the initramfs mounts devtmpfs on `/dev` |
| `CONFIG_EXT4_FS` (built in) | the root filesystem, mounted before any module can load |
| `CONFIG_USB_CONFIGFS`, `CONFIG_USB_CONFIGFS_RNDIS` (built in) | the USB network gadget in the initramfs |
| `CONFIG_UNIX`, `CONFIG_INET`, `CONFIG_UNIX98_PTYS` | SSH and telnet |
| `CONFIG_PSTORE` + `CONFIG_PSTORE_CONSOLE` (recommended) | the previous boot's console after a panic, archived in `/var/lib/systemd/pstore` |
| `CONFIG_CGROUPS` + cgroup2 | systemd tracks services in cgroups; the initramfs mounts cgroup2 on `/sys/fs/cgroup` with `nsdelegate` |
| `CONFIG_FHANDLE`, `CONFIG_CGROUP_PIDS`, `CONFIG_AUTOFS_FS` (recommended) | cgroup ids (`NFTSet=`, oomd, coredump), `TasksMax=`, and `x-systemd.automount`/`.automount` units. systemd 262 starts without them, with fewer features |
| `CONFIG_VT` | the console profile runs its session on tty1 |
| Syscalls: `faccessat2` (5.8), `statx` with mount id (5.8), `waitid(P_PIDFD)` (5.4), `close_range` (5.9), `utimensat` with `AT_EMPTY_PATH` (5.8) | Arch's systemd calls them with no fallback. Older kernels need the backports in `devices/xiaomi-thunder/patches/` |

Kernels before 5.15 cannot mount an ext4 with `orphan_file`;
`tools/make-userdata.sh` already leaves it out. On kernels older than 5.10,
glib-based software may need the patches in `rootfs/packages/glib/`.

## Unit conventions

- The cable units (`thunder-usb-network`, `thunder-telnet-usb`,
  `thunder-telnet-fallback`, `thunder-sshd-rescue`) stay `DefaultDependencies=no`, so the cable works in
  emergency and rescue mode.
- Device units never order `Before=` the cable units.
- Device policy (`sleep.conf.d`, generator masks) goes in the device overlay,
  with the reason in a comment.
- The core `zz-thunder-*` drop-ins (credentials, `MemoryPressureWatch=`,
  `ProtectProc=`) are installed by `configure.sh` only when `KERNEL_VERSION` is old
  enough to need them: into `/usr/lib/systemd/system/service.d/`, and the first two also
  into `/usr/lib/systemd/user/service.d/`, because the user manager fails the same way.
- To opt one unit out of a drop-in:
  `ln -s /dev/null /etc/systemd/system/<unit>.d/<same name>.conf`
  (`/etc/systemd/user/<unit>.d/` for a user unit).
- A graphical session started by a system unit with `PAMName=` inherits
  `CAP_WAKE_ALARM` in the ambient set from `pam_systemd`, and `bwrap` (glycin's
  image loaders, Flatpak) refuses to run with it. Start the session under
  `capsh --noamb`, as Phosh's `phosh.service` does.

Two lessons from the first port that are likely to repeat on other vendor
kernels:

- **A text console on the panel can bootloop the phone** (`CONFIG_FRAMEBUFFER_CONSOLE`
  on MediaTek). Start without it and use the cable.
- **Check `u_ether` on old kernels**: a 4.19 vendor `u_ether` sent garbage in
  paged TX fragments, which broke TCP from phone to host. See
  `devices/xiaomi-thunder/patches/fix-u-ether-linearize-tx.patch`.

## What makes a good port

- Everything the phone needs is in its folder. A change to the core is fine,
  but it has to keep working for the other devices.
- Every patch says why it exists, in its header and in the device docs.
- Proprietary firmware is never committed (`.gitignore` blocks the usual
  names). Document how to copy it from the user's own phone.
- The status in the tables is backed by a boot on a real unit.
