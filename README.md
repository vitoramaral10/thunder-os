# Thunder OS

**Thunder OS** puts Arch Linux ARM on Android phones. It keeps each phone's own
kernel (vendor or mainline, with our patches), adds a small initramfs written
for this project, and runs the stock Arch Linux ARM userland with Arch's **systemd** as
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
  `172.16.42.2`), with SSH. A telnet login opens on the cable only in emergency
  and rescue mode, or when SSH has been down for two minutes.
- Arch Linux ARM, verified against its signing key, with systemd, your SSH key,
  your user and the device's firmware, modules and services.
- `thunder-reboot bootloader` to get back to fastboot without buttons.
- A persistent journal (`journalctl -b -1` for the previous boot) and the
  previous boot's panic from pstore in `/var/lib/systemd/pstore`.

## systemd on old vendor kernels

Phone kernels are often old: vendor trees stay on the kernel the SoC shipped
with, and Arch's systemd (262) expects features from 5.x. Instead of replacing
the init, the device kernel learns what it needs:

- Small backports: `statx` mount id, `waitid(P_PIDFD)`, `close_range`,
  `utimensat` and `faccessat2`, plus the configs `FHANDLE`, `CGROUP_PIDS` and
  `AUTOFS`.
- The initramfs mounts cgroup2 on `/sys/fs/cgroup` with `nsdelegate`, because
  the mount systemd does itself adds `memory_recursiveprot`, which 4.19 refuses.
- Three drop-ins turn off credentials (`ImportCredential=` and friends),
  `MemoryPressureWatch=` (with `PrivateNetwork=` it needs `open_tree()`) and
  `ProtectProc=`/`ProcSubset=`, which need the new mount API and a per-mount
  procfs. They are installed only when the kernel is older than 5.2 or 5.8, so a
  newer kernel runs Arch's units untouched.

## Layout

```
initramfs/    init: mounts the root partition, falls back to a rescue shell over USB
rootfs/       overlay/ (systemd units, USB gadget, drop-ins), configure.sh,
              finalize.sh, packages/ (our own pacman packages, glib patches), src/
tools/        rootless build pipeline and flashing, driven by DEVICE
devices/      one folder per phone: device.sh, kernel config and patches,
              overlay, docs, bring-up units
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
cp local.conf.example local.conf    # set DEVICE=<folder name> and THUNDER_PASSWORD=<password>
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
- **SSH accepts passwords on every network**, Wi-Fi included, as on any Arch
  install with sshd on. There is no default password: the build stops without
  `THUNDER_PASSWORD`, and the installer asks for one (and a separate root
  password). Three wrong passwords lock the account for a while, root
  included (1 minute; 10 minutes for the user).
- **The data partition is not encrypted**, as on a default Arch install. With
  the bootloader unlocked, whoever holds the phone can boot their own image
  from fastboot and read it.
- **The `console` profile has no lock screen.** A touch wakes the panel straight
  into your terminal. `sudo` still asks for the password. The `phosh` profile
  locks.
- Kernel controls that Android leaves world-writable and closes with SELinux
  (touch modes, UID statistics, sdcardfs) are root-only through
  `systemd-tmpfiles`.

## Licensing

See [LICENSING.md](LICENSING.md). Kernel patches and kernel source are GPL-2.0;
everything written for this project is GPL-3.0.
