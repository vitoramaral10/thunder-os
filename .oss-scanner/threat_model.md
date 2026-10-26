# Threat model

## What this project does

Thunder OS turns an Android phone with an unlocked bootloader (fastboot) into an Arch
Linux ARM system. The first device is the Xiaomi Redmi 10 5G (`devices/xiaomi-thunder`):
MediaTek MT6833, the vendor's Linux 4.19 kernel with our patches, systemd 262 as PID 1.

Two halves run in different places:

- **On the phone**, as root: the kernel, our initramfs (`initramfs/init`), the systemd
  units and helpers in `rootfs/overlay/`, the MediaTek connectivity daemon
  `thunder-wmt` (`rootfs/packages/thunder-mediatek-wmt/thunder_wmt.py`), the kernel
  update hook (`rootfs/overlay/usr/lib/thunder/boot-deploy`) and `thunder-wifi`.
- **On the user's PC**, as the user: the build tools in `tools/` and the Flutter
  installer in `installer/`, which download images, parse the phone's factory ROM, and
  talk to the phone over fastboot and a USB network link.

## Where untrusted input enters

On the phone:

- **Over the air:** Wi-Fi frames into the gen4m driver (`drivers/misc/mediatek/connectivity/wlan_drv_gen4m`,
  grafted from `devices/xiaomi-thunder/connectivity/`) and Bluetooth into `btmtk_stp`
  (`devices/xiaomi-thunder/patches/add-btmtk-stp-hci-driver.patch`) and the BlueZ stack.
  An attacker in radio range, before or without authentication, is the most important case.
- **The USB cable network** (RNDIS gadget, phone 172.16.42.1, host 172.16.42.2): sshd,
  a telnet login on 172.16.42.1:23 (`thunder-telnet-usb.service`, only in emergency and
  rescue mode or after sshd has been down for 2 minutes), and udhcpd. Whoever holds the
  cable reaches these. Wrong passwords lock root too (pam_faillock `even_deny_root`).
- **Unprivileged local processes** reaching the kernel: our patches add syscall
  backports (`backport-statx-mount-id`, `backport-waitid-pidfd`, `backport-close-range`,
  `backport-utimensat-empty-path`, `backport-faccessat2`), an nf_tables user-namespace
  gate, procfs and DRM changes, panfrost for the Mali GPU (`add-drm-panfrost-mt6833.patch`),
  and the WPA3 nl80211 change. A bug there is a local privilege escalation.
- **Data read from the phone's own partitions** by root daemons: `thunder-wmt` reads the
  factory NVRAM from the `nvdata` ext4 partition (through debugfs, without mounting) and
  firmware headers; the kernel update hook reads the installed kernel package.

On the PC:

- **The factory ROM** the user downloads: `tools/rom-extract-partition.py` parses the
  `.tgz`, Android sparse images and the logical-partition (super) layout without
  external tools. Treat the archive as untrusted.
- **Output of the phone** (fastboot `getvar`, adb, the USB network, the diagnostic UDP
  beacon on port 9999): `tools/install.sh` and the installer (`installer/lib/fastboot.dart`,
  `installer/lib/backend.dart`).
- **What the user types in the installer** (user name, passwords, hostname, Wi-Fi name and
  password), passed to the shell tools through the environment. Shell or keyfile injection
  from these fields is in scope.

## Components that matter most / least

1. Most: our kernel patches (`devices/xiaomi-thunder/patches/`) and the connectivity
   drivers we ship patched (gen4m Wi-Fi, WMT, `btmtk_stp`), because they face the radio and
   unprivileged userspace.
2. Root code on the phone: `thunder_wmt.py`, `boot-deploy`, `usb-gadget`, `thunder-wifi`,
   `make-swapfile`, `initramfs/init`, `initramfs/thunder-mkboot`.
3. PC side: `tools/rom-extract-partition.py`, `tools/*.sh`, `installer/`.
4. Least: the rest of the vendor kernel. It is compiled and in scope, but report it only
   when it is reachable from the radio, the USB link or unprivileged userspace on this
   device's configuration (`devices/xiaomi-thunder/kernel-config`). Drivers for hardware
   this phone does not have, or code not built with this config, are out of scope.

## How to exercise it

- The kernel tree, patched and built: `build/xiaomi-thunder/kernel-src` (objects in
  `out/`); installed kernel and modules: `build/xiaomi-thunder/kernel`. It is an aarch64
  kernel; there is no phone in the scanner, so reasoning and reproducers are at source level.
- `rootfs/packages/thunder-mediatek-wmt/test_thunder_wmt.py`: unit tests of the WMT
  daemon, with fakes for the driver and the partitions.
- `installer/test/`: the installer's unit and widget tests (`flutter test`).
- `tools/rom-extract-partition.py` runs on the host; it can be fed crafted archives.
- `build/thunder-reboot/thunder-reboot-host` is a host build of `rootfs/src/thunder-reboot.c`.

## How we rate severity

- **Critical:** code execution or memory corruption reachable from the radio (Wi-Fi or
  Bluetooth) without authentication; code execution on the PC from a crafted factory ROM.
- **High:** local privilege escalation to root or the kernel from an unprivileged process
  on the phone; code execution reachable from an associated Wi-Fi network or a paired
  Bluetooth device; command injection from installer input into a root context.
- **Medium:** denial of service from the radio (crash, reboot); memory corruption that needs
  root or the USB cable to reach; information leaks of other users' or kernel data.
- **Low:** denial of service that needs local root or the cable; hardening gaps.
- A memory corruption without a demonstrated path from one of the inputs above is capped
  at medium.

## Anything to leave alone (known and accepted)

- **The USB cable is trusted as physical access.** The initramfs rescue shell on telnet
  172.16.42.1:23 has no password by design (it only runs when booting fails or with
  `thunder.debug=1`), and the diagnostic beacon sends pstore and dmesg to 172.16.42.2:9999.
- **The password** (`THUNDER_PASSWORD`, required by the build, no default; the same for root
  and the user) is documented and meant to be changed with `passwd`.
- **Physical possession**: the data partition is not encrypted and the bootloader is
  unlocked, and the `console` profile has no lock screen (documented in the README).
- Code under `build/` other than the kernel tree is generated. Firmware blobs are not in
  the repository and come from the user's phone.
- Kernel 4.19 is end of life upstream and MediaTek published no newer BSP for this chip;
  "upgrade the kernel" is not an actionable fix. Prefer patches against this tree.
