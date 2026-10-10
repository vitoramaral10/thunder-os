# Xiaomi Redmi 10 5G / POCO M4 5G (`xiaomi-thunder`)

| | |
|---|---|
| Codename | `thunder` (also sold as `light`) |
| SoC | MediaTek MT6833 (Dimensity 700), Mali-G57 MC2 |
| Kernel | Xiaomi vendor 4.19.191 with our patches ([docs/kernel.md](docs/kernel.md)) |
| Boot | boot.img header v2, A/B, fastboot, AVB disabled |
| Root partition | `userdata` (ext4 written directly, no nested GPT) |
| Status | **boots**: OpenRC up in ~20 s, SSH over USB |
| Maintainer | vitoramaral10 |

There is no newer kernel for this SoC: mainline supports no phone Dimensity,
and MediaTek never published a newer BSP.

## What works

| Component | Kernel | Thunder OS image | Notes |
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
- **The screen stays black.** There is no text console: the framebuffer
  console caused a bootloop on this kernel. Use the USB cable.
- **Clock:** the RTC may be back in 2009 after the battery ran flat. From the
  host: `ssh root@172.16.42.1 "date -s @$(date +%s) && hwclock --systohc"`.
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
overlay/         files added to the rootfs (touchscreen-wake service)
init.d/, wifi/, bluetooth/   Wi-Fi and Bluetooth bring-up, not in the image yet
docs/            kernel catalogue, firmware, hardware bring-up write-ups
```
