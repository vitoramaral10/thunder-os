# Xiaomi Redmi 10 5G / POCO M4 5G (`xiaomi-thunder`)

| | |
|---|---|
| Codename | `thunder` (also sold as `light`) |
| SoC | MediaTek MT6833 (Dimensity 700), Mali-G57 MC2 |
| Kernel | Xiaomi vendor 4.19.191 with our patches ([docs/kernel.md](docs/kernel.md)) |
| Boot | boot.img header v2, A/B, fastboot, AVB disabled |
| Root partition | `userdata` (ext4 written directly, no nested GPT) |
| Status | **boots** with systemd; SSH over USB; on-screen profiles `console` and `phosh` are experimental |
| Maintainer | vitoramaral10 |

There is no newer kernel for this SoC: mainline supports no phone Dimensity,
and MediaTek never published a newer BSP.

## What works

| Component | Kernel | Thunder OS image | Notes |
|---|---|---|---|
| Boot, storage, USB network | works | works | RNDIS gadget, phone at `172.16.42.1`, SSH over the cable |
| Display | works | works | the `console` (phoc, foot, squeekboard) and `phosh` profiles; no text console: see [docs/kernel.md](docs/kernel.md) |
| Touchscreen | works | wake service included | needs the Novatek firmware from your phone |
| GPU (Mali-G57) | works | works | panfrost backported to 4.19 and Mesa in the on-screen profiles, see [docs/gpu-bringup.md](docs/gpu-bringup.md) |
| Wi-Fi | works | works | gen4m stack rebuilt for MT6833, 2.4 and 5 GHz; connects from the installer or `thunder-wifi`, see [docs/wifi-bringup.md](docs/wifi-bringup.md) |
| Bluetooth | works | works | BlueZ through the `btmtk_stp` HCI driver, factory address; BR/EDR and LE, no audio yet, see [docs/bluetooth-bringup.md](docs/bluetooth-bringup.md) |
| Battery and charging | works | works | fuel gauge algorithm runs in the kernel; UPower |
| Sensors | works | works | light, accelerometer, proximity as IIO through iio-sensor-proxy, see [docs/sensors.md](docs/sensors.md) |
| Audio | works | works | speaker and internal mic through PipeWire (UCM); headphones with jack detection (accdet reports through ALSA jacks), no earpiece yet, see [docs/audio.md](docs/audio.md) |
| Suspend | works with the cable (test levels) | off | policy and guard in the image, `AllowSuspend=no` until tested on battery, see [docs/suspend.md](docs/suspend.md) |
| Vibration | works | works | `regulator-haptic` on the PMIC's VIBR LDO: a force-feedback input that feedbackd (Phosh) uses |
| Flashlight | works | works | rear LED (MT6360 FLED1) as `white:torch`, up to 200 mA; Phosh's torch button |
| Modem, cameras, GPS | not started | | the vendor stacks are Android userspace |

"Not wired" means the driver works, but the service that brings it up is not
in the image yet.

## Before you build

Copy the proprietary firmware from your phone's factory ROM:
[docs/vendor-firmware.md](docs/vendor-firmware.md). The rootfs build stops if
the touchscreen firmware is missing, and the initramfs carries it because the
built-in touch driver loads it before the root is mounted.

## Device notes

- **Fastboot:** hold **volume down** while powering on, or run
  `thunder-reboot bootloader` on the phone.
- `tools/flash.sh` checks that fastboot reports `product: thunder` and that the
  active slot is `a` (the only one tested).
- **With the `none` profile the screen stays black.** There is no text
  console: the framebuffer console caused a bootloop on this kernel. Use the
  USB cable, or the `console` or `phosh` profile.
- **Clock:** the RTC may be back in 2009 after the battery ran flat. `systemd-timesyncd`
  fixes the system clock as soon as Wi-Fi is up and `thunder-rtc-sync` writes it
  to the RTC.
- **Kernel updates go through fastboot.** The bootloader turns on the UFS
  power-on write protection for the boot partition, so Linux cannot write it
  (`DATA PROTECT`, sense key 0x7) until the next power cycle.
  `tools/update-kernel.sh` handles it: the phone builds the boot.img, the PC
  flashes it over fastboot, about 35 s without buttons (`BOOT_DEPLOY=fastboot`).
- **Factory recovery:** run the factory fastboot package's `flash_all.sh`.
  **Never `flash_all_lock.sh`**: it relocks the bootloader. Afterwards run a
  full `tools/flash.sh --sim` again, since the factory flash re-enables
  verification.

## Folder

```
device.sh        identity, kernel source, boot.img layout, fastboot, services,
                 and the connectivity graft hook
kernel-config    kernel .config
patches/         kernel patches, applied in patches/series order
connectivity/    MediaTek Wi-Fi/BT driver source grafted over the kernel
overlay/         files added to the rootfs: touchscreen-wake rule, NetworkManager and
                 sleep policy (Wi-Fi/BT bring-up is the thunder-mediatek-wmt package)
docs/            kernel catalogue, firmware, hardware bring-up write-ups
```
