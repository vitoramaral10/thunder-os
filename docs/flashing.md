# Flashing

> **Flashing erases the root partition** (`ROOT_PARTITION` in the device's
> `device.sh`, usually `userdata`). Everything on the internal storage is lost.
> Back up what you care about before you start.

Device-specific notes (how to reach fastboot, tested slot, recovery quirks) are
in the device's `README.md` under [`devices/`](../devices).

## Prerequisites

- **Unlocked bootloader.** `tools/flash.sh` refuses to run if
  `fastboot getvar unlocked` is not `yes`. Nothing in this project locks the
  bootloader, and you should never lock it: with a custom kernel and
  verification off, a locked bootloader will not boot.
- **The right phone.** The script checks that `fastboot getvar product` is one
  of the device's `FASTBOOT_PRODUCTS`, and, if the device sets `FASTBOOT_SLOT`,
  that this slot is active. It aborts otherwise.
- **Keep the factory ROM archive** (the vendor's fastboot package for your
  model and region). It is the way back if anything goes wrong; see
  [Full recovery](#full-recovery-to-the-factory-rom).
- `fastboot` and `avbtool` on the host, and the images built:
  `build/<device>/out/boot.img` (`tools/build-boot.sh`) and
  `build/<device>/out/userdata.img` (`tools/make-userdata.sh`).
- Battery above 50% and a cable you trust.

## tools/flash.sh

```sh
tools/flash.sh                    # dry run: prints the plan, writes nothing
tools/flash.sh --sim              # writes vbmeta, boot and the root partition
tools/flash.sh --sim --so-boot    # writes only boot (kernel/initramfs change)
```

Boot the phone into fastboot first (see [Getting to
fastboot](#getting-to-fastboot)). Always run the dry run first and read the
plan.

What `--sim` writes (on A/B devices, `boot` and `vbmeta` get the active slot's
suffix, e.g. `boot_a`):

| Partition | Content | Note |
|-----------|---------|------|
| `vbmeta` | empty vbmeta with flags 2 | disables verity and verification (the flag is in the image; fastboot 37 rejects this image with `--disable-verity`). Skipped if the device sets `FASTBOOT_DISABLE_VBMETA=0` |
| `boot` | `build/<device>/out/boot.img` | kernel, dtb and initramfs |
| `ROOT_PARTITION` | `build/<device>/out/userdata.img` | **erases all user data** |

To update only the kernel of a phone that already runs Thunder OS, prefer
`tools/update-kernel.sh` (see [building.md](building.md#updating-the-kernel-without-reinstalling)):
it keeps the kernel package in pacman in step with the boot partition.

With `--so-boot` only `boot` is written; vbmeta and the root partition are left alone,
so the installed system and its data survive. Use it when you only changed the
kernel or the initramfs. The script reboots the device when it finishes.

## First boot

- **The screen may stay black.** Thunder OS does not need a console on the
  panel, and on some kernels it is off on purpose. Use the cable.
- **Connect the USB cable to the host.** The phone exposes an RNDIS network
  gadget (`1d6b:0104`). The host gets `172.16.42.2` by DHCP; the phone is
  `172.16.42.1`. Measured: 50 MB in 2.7 s over ssh.
- `ssh root@172.16.42.1` with the key that was staged into the image.
- Fallback: `telnet 172.16.42.1`, which asks for a login (root or
  `THUNDER_USER`, password `THUNDER_PASSWORD`, see [building.md](building.md)).
  It is a second way in if sshd does not start. In emergency and rescue mode
  `thunder-sshd-rescue` keeps ssh on `172.16.42.1` too.
- The first boot grows the root filesystem to fill its partition, so it takes
  longer than later boots.
- **The clock.** The RTC may be back in the past after the battery ran flat.
  `systemd-timesyncd` sets the clock once there is network and
  `thunder-rtc-sync` writes it back to the RTC. To set it by hand from the
  host: `ssh root@172.16.42.1 "date -s @$(date +%s) && hwclock --systohc"`.

## Rescue shell

If the initramfs cannot find or mount the root partition within 30 s, or the
init (`/usr/bin/init` by default) is missing from it, or `thunder.debug=1` is on
the kernel command line, it drops to a rescue shell instead of rebooting:

```sh
telnet 172.16.42.1        # no password
```

In emergency or rescue mode (the system is up but did not reach
`multi-user.target`) the login telnet and `ssh root@172.16.42.1` both work
(`thunder-sshd-rescue`).

The shell never reboots the device by itself. To retry mounting the root and
continue the normal boot from inside it:

```sh
touch /run/continue
```

Other cmdline parameters the initramfs reads: `thunder.root=` (partition name,
default: the device's `ROOT_PARTITION`) and `thunder.init=`. Add them through
`CMDLINE_EXTRA` when running `tools/build-boot.sh`. The last value wins, because the bootloader prepends its own
arguments, including `root=/dev/ram`.

## Getting to fastboot

- From the running system: `systemctl reboot bootloader` (clean shutdown) or
  `thunder-reboot bootloader` (immediate; installed as
  `/usr/local/bin/thunder-reboot`, it uses `LINUX_REBOOT_CMD_RESTART2`).
- From a powered-off phone: the device's key combination, in its `README.md`
  (often **volume down** while powering on).

Check with `fastboot devices`.

## Reading logs

- `journalctl -b` is this boot, `journalctl -b -1` the previous one and
  `journalctl -k` the kernel log. The journal is persistent.
- `/var/lib/systemd/pstore`: archived at each boot by `systemd-pstore.service`;
  the console-ramoops record is the panic log of the previous boot.

If the system does not come up at all, boot into the rescue shell with
`thunder.debug=1`. The rescue shell mounts pstore at `/sys/fs/pstore`; the root
filesystem is not mounted there, so mount the root partition yourself to
read `/var/log`.

## Trying another init once

`echo <init path> > /var/lib/thunder/init-once` makes the initramfs use that
init for the next boot only (it consumes the file before `switch_root`), so a
bad init never sticks. Without it the init is `thunder.init=` or
`/usr/bin/init`.

## Full recovery to the factory ROM

Extract the vendor's factory fastboot package and, with the phone in
fastboot, run its flash script. **Never use a variant that relocks the
bootloader** (Xiaomi's `flash_all_lock.sh`, for example): with verification
off and a locked bootloader the phone may not boot at all.

The factory flash restores vbmeta with verification enabled, so the custom
kernel will not boot afterwards. To go back to this system, run
`tools/flash.sh --sim` again (a full run, not `--so-boot`).
