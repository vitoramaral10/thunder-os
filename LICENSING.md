# Licensing

This repository mixes material under different licenses. Each file keeps the
license it came with.

| Path | License | Origin |
|---|---|---|
| `kernel/patches/*.patch`, `kernel/config-thunder.aarch64` | GPL-2.0 | derived from Linux kernel source |
| `kernel/connectivity/mtk-connectivity-gen4m-mt6833-2.tar.gz` | GPL-2.0 | MediaTek/Xiaomi kernel source, assembled from public vendor trees (657 files, no binaries). The `bt_drv/` directory comes from `mt6781-devs/android_kernel_xiaomi_mt6781` @ `lineage-23.2`, path `drivers/misc/mediatek/connectivity/bt/mt66xx/wmt/`; its `Makefile` was written for this project |
| `kernel/connectivity/Makefile-mt6833`, `kernel/connectivity/gen4m-mt6833-kernel419.diff` | GPL-2.0 | derived from `MiCode/Xiaomi_Kernel_OpenSource` and the connectivity drop |
| `rootfs/packages/glib/*.patch` | LGPL-2.1-or-later | patches to GLib |
| `rootfs/packages/openrc/PKGBUILD` | GPL-3.0 | written for this project; builds OpenRC (BSD-2-Clause) |
| `kernel/build.sh`, `initramfs/`, `rootfs/`, `device/`, `tools/`, `docs/` | GPL-3.0 | written for this project |

The kernel source this project builds against is
[`MiCode/Xiaomi_Kernel_OpenSource`](https://github.com/MiCode/Xiaomi_Kernel_OpenSource),
commit `a40ceab8943703f33e935cb0b86d0f69aa2044dd`, published by Xiaomi under
GPL-2.0.

**No proprietary firmware is distributed here.** See
[docs/vendor-firmware.md](docs/vendor-firmware.md).
