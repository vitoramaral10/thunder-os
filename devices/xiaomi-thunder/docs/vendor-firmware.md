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
mount -o ro /dev/disk/by-partlabel/nvdata /mnt      # READ-ONLY, always
cp /mnt/APCFG/APRDEB/WIFI /lib/firmware/wifi_nvram.bin
umount /mnt
```

Mount it read-only and never write to it. It holds the factory RF calibration
for this specific radio; there is no way to regenerate it if you corrupt it.

The Wi-Fi file is 2050 bytes, with the MAC address at offset 4. It is consumed
by [`devices/xiaomi-thunder/wifi/wifi-nvram-push.py`](../wifi/wifi-nvram-push.py), which
hands it to the driver at bring-up. The push is gated behind
`/etc/thunder-wifi-nvram` and off by default, for reasons explained in
[wifi-bringup.md](wifi-bringup.md#the-factory-mac-solved-mechanism-unusable-in-practice).

The Bluetooth address lives in the same place, at `/APCFG/APRDEB/BT_Addr`: six
bytes, same OUI as the Wi-Fi MAC. [`devices/xiaomi-thunder/init.d/bluetooth-thunder`](../init.d/bluetooth-thunder)
extracts it into `/lib/firmware/bt_addr.bin` on first start, again with the
partition mounted read-only, and the bridge writes it to the controller.

Because it is per-unit data, no MAC address or calibration blob belongs in
this repository.

## Installing them

[`tools/build-rootfs.sh`](../../../tools/build-rootfs.sh) takes the files from
`$FIRMWARE` (default `build/xiaomi-thunder/firmware`) and installs them in
`/usr/lib/firmware` of the root filesystem. The touch firmware is also staged
into the initramfs by [`tools/build-boot.sh`](../../../tools/build-boot.sh), from the built rootfs. See
[building.md](../../../docs/building.md).

`request_firmware()` searches that path, and
[`devices/xiaomi-thunder/wifi/wmt-daemon.py`](../wifi/wmt-daemon.py) reads the patch
headers from it.

## Licensing

These files are proprietary to Novatek, MediaTek and Xiaomi. Extracting them
from a device you own for use on that device is one thing; redistributing them
is another. Do not commit them to any repository.
