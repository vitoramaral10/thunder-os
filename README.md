# Thunder OS

**Thunder OS** puts Arch Linux ARM on Android phones. It keeps each phone's own
kernel (vendor or mainline, with our patches), adds a small initramfs written
for this project, and runs the stock Arch Linux ARM userland with **OpenRC** as
PID 1. There is nothing from postmarketOS or Android in the result.

The core (initramfs, rootfs, build and flash tools) is the same for every
phone. Everything that belongs to one phone lives in `devices/<vendor>-<codename>/`,
and adding a phone means adding one of those folders. See
[docs/porting.md](docs/porting.md).

> **Status: early.** One device boots to a shell over SSH. There is no graphical
> session in the image yet.

## Devices

| Device | Folder | SoC | Kernel | Status |
|---|---|---|---|---|
| Xiaomi Redmi 10 5G / POCO M4 5G (`thunder`) | [xiaomi-thunder](devices/xiaomi-thunder) | MediaTek MT6833 | vendor 4.19 | boots, SSH over USB |

Status levels and what each port supports: [devices/README.md](devices/README.md).
Want your phone here? Read [docs/porting.md](docs/porting.md) and open a pull request.

## What every device gets

- An initramfs that brings up a USB network gadget first, mounts the root
  partition and switches to Arch. If anything fails it stays up with a rescue
  shell on the cable instead of rebooting.
- The phone at `172.16.42.1` over the USB cable (RNDIS, DHCP gives the host
  `172.16.42.2`), with SSH and a telnet login fallback.
- Arch Linux ARM, verified against its signing key, with OpenRC, your SSH key,
  your user and the device's firmware, modules and services.
- `thunder-reboot bootloader` to get back to fastboot without buttons.
- Logs of every boot, including the previous boot's panic from pstore, in
  `/var/log/thunder/`.

## Why OpenRC and not systemd

Phone kernels are often old: vendor trees stay on the kernel the SoC shipped
with. Arch's systemd (262) does not run on them. It calls `close_range`
(Linux 5.9) and `waitid(P_PIDFD)` (5.4) with no fallback, since systemd 260
raised its baseline to 5.10, and needs `CONFIG_FHANDLE`. OpenRC has no such
dependency, so one init works on every device. The `systemd` package stays
installed but inert, because `pacman` depends on it.

## Layout

```
initramfs/    init: mounts the root partition, falls back to a rescue shell over USB
rootfs/       overlay/ (services, USB gadget, boot scripts), configure.sh,
              finalize.sh, packages/ (OpenRC PKGBUILD, glib patches), src/
tools/        rootless build pipeline and flashing, driven by DEVICE
devices/      one folder per phone: device.sh, kernel config and patches,
              overlay, docs, bring-up helpers
docs/         building, flashing, porting
```

Everything generated (kernel tree, rootfs, images, toolchain) goes to `build/`,
which is not versioned: shared downloads at the top, one folder per device.

## Installer

[`installer/`](installer) is a desktop app that does all of this in the steps of
a Linux installer: it detects the phone on the cable, takes the factory ROM for
the firmware, and lets you pick the interface, the users, the root password, the
locale and the timezone. Interface options and their state:
[docs/profiles.md](docs/profiles.md).

## Getting started

1. Pick your device and read its folder. Keep its factory ROM at hand: the
   proprietary firmware is copied from it, and is never in this repository.
2. [docs/building.md](docs/building.md): build the kernel, the rootfs and the
   images. No root needed on the host.
3. [docs/flashing.md](docs/flashing.md): **read before flashing**. It erases
   the root partition (usually `userdata`) and disables verified boot.

```sh
cp local.conf.example local.conf    # set DEVICE=<folder name>
tools/fetch-cross.sh
tools/build-kernel.sh
tools/extract-firmware.sh ~/Downloads/<factory-rom>.tgz
tools/build-rootfs.sh
tools/build-boot.sh
tools/make-userdata.sh
tools/flash.sh                      # dry run; --sim writes
```

## Security notes

- **Verified boot is disabled** when flashing, since bootloaders reject an
  unsigned `boot.img` otherwise. The phone will boot anything after that.
- **The rescue shell has no password.** If the initramfs cannot mount the root
  filesystem, or `thunder.debug=1` is on the command line, it opens a root
  telnet shell on the USB link (`172.16.42.1`). It never listens on Wi-Fi, and
  it stops when the system boots.
- **The image ships a provisional password** (`THUNDER_PASSWORD`, default
  `147147`) for root and the user, used by the telnet login on the USB link.
  Change it with `passwd` on first login.

## Licensing

See [LICENSING.md](LICENSING.md). Kernel patches and kernel source are GPL-2.0;
everything written for this project is GPL-3.0.
