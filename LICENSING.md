# Licensing

This repository mixes material under different licenses. Each file keeps the
license it came with.

## Core

| Path | License | Origin |
|---|---|---|
| `rootfs/packages/glib/*.patch` | LGPL-2.1-or-later | patches to GLib |
| `initramfs/`, `rootfs/`, `tools/`, `docs/` | GPL-3.0 | written for this project |

## Devices

Each `devices/<name>/` follows the same rule: kernel patches, kernel configs
and kernel source shipped in the device folder are **GPL-2.0** (derived from the
Linux kernel); everything else written for the port (`device.sh`, overlays,
services, daemons, docs) is **GPL-3.0**. A device folder lists any exception
below.

### xiaomi-thunder

| Path | License | Origin |
|---|---|---|
| `patches/*.patch`, `kernel-config` | GPL-2.0 | derived from Linux kernel source |
| `connectivity/mtk-connectivity-gen4m-mt6833-2.tar.gz` | GPL-2.0 | MediaTek/Xiaomi kernel source, assembled from public vendor trees (657 files, no binaries). The `bt_drv/` directory comes from `mt6781-devs/android_kernel_xiaomi_mt6781` @ `lineage-23.2`, path `drivers/misc/mediatek/connectivity/bt/mt66xx/wmt/`; its `Makefile` was written for this project |
| `connectivity/Makefile-mt6833`, `connectivity/gen4m-mt6833-kernel419.diff` | GPL-2.0 | derived from `MiCode/Xiaomi_Kernel_OpenSource` and the connectivity drop |
| everything else | GPL-3.0 | written for this project |

The kernel source this device builds against is
[`MiCode/Xiaomi_Kernel_OpenSource`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource),
commit `a40ceab8943703f33e935cb0b86d0f69aa2044dd`, published by Xiaomi under
GPL-2.0.

**No proprietary firmware is distributed here.** Each device documents how to
copy it from your own phone, for example
[devices/xiaomi-thunder/docs/vendor-firmware.md](devices/xiaomi-thunder/docs/vendor-firmware.md).
