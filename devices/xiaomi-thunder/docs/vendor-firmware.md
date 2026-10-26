# Vendor firmware

None of the proprietary blobs are in this repository (`.gitignore` excludes
them). You extract them from the factory ROM of your own device. Everything
below is read-only with respect to the phone.

## What you need, and why

| File | Needed for |
|---|---|
| `nt36672c_tm_01_ts_fw.bin` | touchscreen firmware |
| `nt36672c_tm_01_ts_mp.bin` | touchscreen self-test |
| `WIFI_RAM_CODE_soc2_2_1_1.bin`, `WIFI_RAM_CODE_soc2_2_1a_1.bin` | Wi-Fi RAM code |
| `soc2_2_ram_mcu_1_1_hdr.bin`, `soc2_2_ram_mcu_1a_1_hdr.bin` | CONNSYS MCU patch |
| `soc2_2_ram_wifi_1_1_hdr.bin`, `soc2_2_ram_wifi_1a_1_hdr.bin` | Wi-Fi patch |
| `soc2_2_ram_bt_1_1_hdr.bin`, `soc2_2_ram_bt_1a_1_hdr.bin` | Bluetooth patch |
| `WMT_SOC*.cfg`, `wifi.cfg`, `txpowerctrl.cfg`, `BT_FW.cfg` | connectivity configuration |

The Novatek NT36672C is a host-download touchscreen controller: the IC holds
no firmware of its own and the host uploads it on every boot. Without the
`.bin` the driver logs `update_firmware_request: firmware load failed, ret=-2`
and the touch interrupt line is never raised. The touch firmware is also
copied into the initramfs, because the driver loads it before the root
filesystem is mounted.

The connectivity patches are equally non-optional: the CONNSYS subsystem has
its own CPU and boots from DRAM. See [wifi-bringup.md](wifi-bringup.md).

## `_1_1` versus `_1a_1`

The factory ROM ships two variants of each connectivity patch. They are
selected by the A-die chip ID, not by anything in the header: `u2HwVer`,
`u2SwVer`, the load address and the patch type are byte-identical between
them, only the build date differs. Both sets are installed, since the
variant depends on the unit.

## Where they come from

The factory ROM is `light_global_images_OS1.0.7.0.ULSMIXM`. The files live in
its `vendor_a` image. The device uses dynamic partitions, so the ROM carries a
`super` image that has to be split first (or dump `super` from fastboot with an
unlocked bootloader):

```sh
lpunpack super.img out/          # splits into vendor_a.img, system_a.img, ...
```

For an ext4 `vendor_a.img`, no mounting and no root are needed:

```sh
debugfs -R "ls -l /firmware" vendor_a.img     # to browse
debugfs -R "dump /firmware/nt36672c_tm_01_ts_fw.bin nt36672c_tm_01_ts_fw.bin" vendor_a.img
```

Paths vary by ROM build; `debugfs -R "ls -l /firmware"` is the way to find
them. On some builds the connectivity files are under `/firmware/` and on
others under `/etc/firmware/`.

Put everything in one flat directory (default `build/xiaomi-thunder/firmware`, or any
directory exported as `$FIRMWARE`).

## Calibration NVRAM (MAC address and RF calibration)

Not a file you copy out of the ROM. It lives in the device's own `nvdata`
partition, is unique to each unit, and is read at run time:

```sh
debugfs -R "cat /APCFG/APRDEB/WIFI" /dev/disk/by-partlabel/nvdata | xxd | head -4   # read-only, saves nothing
```

Never write to it. It holds the factory RF calibration for this specific
radio; there is no way to regenerate it if you corrupt it. `debugfs` without
`-w` opens the filesystem read-only, and unlike a read-only mount it does not
replay the journal.

The Wi-Fi file is 2050 bytes, with the MAC address at offset 4.
`thunder-wmt` (package `thunder-mediatek-wmt`) reads it this way at every
boot and hands it to the driver before the first power-on; nothing is copied
to disk. See
[wifi-bringup.md](wifi-bringup.md#the-factory-nvram-mac-and-rf-calibration).

The Bluetooth address lives in the same place, at `/APCFG/APRDEB/BT_Addr`: six
bytes, same OUI as the Wi-Fi MAC. `thunder-wmt bt-on` (unit `thunder-wmt-bt`) reads it
at every boot the same way, without mounting, and sets it through BlueZ's mgmt (Set
Public Address); the `btmtk_stp` driver programs it on every power-on. See
[bluetooth-bringup.md](bluetooth-bringup.md).

Because it is per-unit data, no MAC address or calibration blob belongs in
this repository.

## Installing them

**From the phone itself (the default):** the installation only writes `boot`,
`vbmeta` and `userdata`, so the factory `super` partition stays intact. On the
first boot `thunder-firmware-seed` maps the active slot's `vendor` out of
`super` (a read-only dm-linear from the `lpdump` extents), mounts it
`ro,noload` in a private namespace and copies the same files to the same
places as below, plus the GPS chroot (`GNSS_ROM_FILES`: `mnld`, its config and
the closure of its libraries, from `vendor`, `system` and two APEX images). An
image built without firmware carries none, so it can be distributed. Checked
on the phone on 2026-10-26: the 17 firmware files, the 189 modem files and the
48 files of the GPS chroot came out byte-identical to the ones taken from the
factory ROM.
The touch firmware is not in that first initramfs; the driver requests it again
after the copy (14 s after probe, and on every resume), and the next boot.img
built on the phone carries it.

**From a factory ROM on the PC:**

[`tools/build-rootfs.sh`](../../../tools/build-rootfs.sh) takes the files from
`$FIRMWARE` (default `build/xiaomi-thunder/firmware`) and installs them in
`/usr/lib/firmware` of the root filesystem. The touch firmware is also staged
into the initramfs by [`tools/build-boot.sh`](../../../tools/build-boot.sh), from the built rootfs. See
[building.md](../../../docs/building.md).

`request_firmware()` searches that path, and `thunder-wmt` reads the ROM patch
headers from it (`WMT_ROM_PATCHES` in `device.sh`). The files the Wi-Fi
sequence cannot do without (`WMT_SOC.cfg`, the three `*_1_1_hdr.bin` ROM
patches and `WIFI_RAM_CODE_soc2_2_1_1.bin`) are in the device's
`FIRMWARE_REQUIRED`: a missing one stops `tools/extract-firmware.sh` and the
image build.

## Licensing

These files are proprietary to Novatek, MediaTek and Xiaomi. Extracting them
from a device you own for use on that device is one thing; redistributing them
is another. Do not commit them to any repository.
