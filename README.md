# thunder-linux

**thunder-linux** runs Arch Linux ARM on the MediaTek **MT6833** (Dimensity 700) handset that Xiaomi
sells as **Redmi 10 5G** and **POCO M4 5G** (codename `thunder`, also `light`).

The phone runs Xiaomi's 4.19 vendor kernel with our patches, a small initramfs
written for this project, and the stock Arch Linux ARM userland with
**OpenRC** as PID 1. There is no newer kernel for this SoC: mainline has no
support for any phone Dimensity, and MediaTek never published a newer BSP.

> **Status: boots.** Since 2026-10-10 the phone boots Arch Linux ARM with
> OpenRC and is reachable over SSH on the USB cable. The rest of the hardware
> support below was proven on this kernel with an earlier userland and is not
> wired into the Arch image yet.

## What works on this kernel

| Component | Kernel | Arch image | Notes |
|---|---|---|---|
| Boot, storage, USB network | works | works | RNDIS gadget, phone at `172.16.42.1`, SSH over the cable |
| Display | works | no GUI yet | no text console on screen: see [docs/kernel.md](docs/kernel.md) |
| Touchscreen | works | wake service included | needs the Novatek firmware from your phone |
| GPU (Mali-G57) | works | not wired | panfrost backported to 4.19, see [docs/gpu-bringup.md](docs/gpu-bringup.md) |
| Wi-Fi | works | not wired | gen4m stack rebuilt for MT6833, see [docs/wifi-bringup.md](docs/wifi-bringup.md) |
| Bluetooth | works | not wired | BR/EDR and LE, no audio yet, see [docs/bluetooth-bringup.md](docs/bluetooth-bringup.md) |
| Battery and charging | works | — | fuel gauge algorithm runs in the kernel |
| Sensors | works | not wired | light, accelerometer, proximity as IIO, see [docs/sensors.md](docs/sensors.md) |
| Speaker | works | not wired | |
| Modem, cameras | not started | | |

"Not wired" means the driver works, but the service that brings it up is not
in the Arch image yet.

## Why OpenRC and not systemd

Arch's systemd (262) does not run on a 4.19 kernel. It calls `close_range`
(Linux 5.9) and `waitid(P_PIDFD)` (5.4) with no fallback, since systemd 260
raised its baseline to 5.10, and this kernel has `CONFIG_FHANDLE` off.
`systemd-udevd` hits the same walls. OpenRC has no such dependency and is
already proven on this kernel. The `systemd` package stays installed but
inert, because `pacman` depends on it.

## Layout

```
kernel/       build.sh, config, patches/ (applied in patches/series order),
              connectivity/ (MediaTek Wi-Fi/BT driver source graft)
initramfs/    init: mounts userdata, falls back to a rescue shell over USB
rootfs/       overlay/ (services, USB gadget, boot scripts), configure.sh,
              finalize.sh, packages/ (OpenRC PKGBUILD, glib patches)
device/       Wi-Fi and Bluetooth bring-up daemons, their OpenRC services,
              thunder-reboot
tools/        rootless build pipeline and flashing
docs/         build, flash, kernel catalogue, hardware bring-up write-ups
```

Everything generated (kernel tree, rootfs, images, toolchain) goes to `build/`,
which is not versioned.

## Getting started

1. [docs/vendor-firmware.md](docs/vendor-firmware.md): copy the proprietary
   firmware from your own phone's factory ROM. It is never in this repository.
2. [docs/building.md](docs/building.md): build the kernel, the rootfs and the
   images. No root needed on the host.
3. [docs/flashing.md](docs/flashing.md): **read before flashing**. It erases
   `userdata` and disables verified boot.

## Security notes

- **Verified boot is disabled** when flashing, since the bootloader rejects an
  unsigned `boot.img` otherwise. The phone will boot anything after that.
- **The rescue shell has no password.** If the initramfs cannot mount the root
  filesystem, or `thunder.debug=1` is on the command line, it opens a root
  telnet shell on the USB link (`172.16.42.1`). It never listens on Wi-Fi, and
  it stops when the system boots.
- **The image ships a provisional password** (`THUNDER_PASSWORD`, default
  `147147`) for root and the user, used by the telnet login on the USB link.
  Change it with `passwd` on first login.

## Licensing

See [LICENSING.md](LICENSING.md). Kernel patches and the connectivity source
are GPL-2.0; everything written for this project is GPL-3.0.
