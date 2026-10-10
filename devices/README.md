# Devices

One folder per phone, named `<vendor>-<codename>` in lowercase (the codename is
what `fastboot getvar product` reports, or the one the vendor uses in its
kernel tree). Build with `DEVICE=<folder>`.

| Device | Folder | SoC | Kernel | Status | Maintainer |
|---|---|---|---|---|---|
| Xiaomi Redmi 10 5G / POCO M4 5G | [xiaomi-thunder](xiaomi-thunder) | MediaTek MT6833 | vendor 4.19 | boots | vitoramaral10 |

## Status levels

`DEVICE_STATUS` in `device.sh` uses one of these:

| Status | Means |
|---|---|
| `bring-up` | Builds, but does not reach the Arch userland yet. Kept so others can help |
| `boots` | Reaches OpenRC's default runlevel; SSH over USB works |
| `daily` | Display, touch, Wi-Fi and battery work in the image, not only in the kernel |

A device enters the table when its `device.sh` builds. The status is the port
maintainer's call, backed by a boot log from a real unit.

## Adding a device

See [docs/porting.md](../docs/porting.md).
