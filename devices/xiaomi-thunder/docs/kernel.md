# Kernel

The kernel is Xiaomi's vendor 4.19.191 for the MT6833 (MiCode/Xiaomi_Kernel_OpenSource
at commit `a40ceab8943703f33e935cb0b86d0f69aa2044dd`), built by `tools/build-kernel.sh`.
This page catalogues what we change on top of it and why, and lists the kernel
properties the userland has to live with. Revision numbers (`rNN`) are the kernel
revisions in which a change first shipped.

Build steps, in order: fetch and verify the tarball (sha512), graft the MediaTek
connectivity drop, apply `devices/xiaomi-thunder/patches/series` in order, strip `-Werror` from every
Makefile/Kbuild, `oldconfig` from `devices/xiaomi-thunder/kernel-config`, cross-build with
`aarch64-linux-gnu-`. Output goes to `build/xiaomi-thunder/kernel`: `boot/vmlinuz` (Image.gz),
`boot/dtbs/mediatek/mt6833.dtb`, `lib/modules/4.19.191-perf`.

## Patches, in series order

All patches apply with `patch -p1` at the root of the Xiaomi source, after the
connectivity graft.

**fix-selinux-host-includes.** `scripts/selinux/genheaders` and `scripts/selinux/mdp`
are host programs that include kernel uapi headers. On a cross build they also need
the target arch uapi directories (source and generated) on the include path, or they
fail to compile. The patch adds those two `-I` flags.

**fix-external-inline-declarations.** The vendor tree declares and defines several
functions as plain `inline` with external linkage (`ksm_flock`, `blk_crypto_flock`,
`uclamp_se_set`). Modern gcc follows C99 semantics, emits no out-of-line copy, and the
link fails with undefined references. The patch drops the `inline`.

**use-system-cpio.** `kernel/gen_kheaders.sh` expects a `cpio` binary built from
`tools/build/cpio`, which does not exist in this tree. It now uses `$CPIO`, defaulting
to the host `cpio`.

**fix-gcc-modern-compat.** Source that older gcc accepted and current gcc rejects:
a `for (int i ...)` loop in `hqsys_pcba.c` (declaration moved out) and a variable-length
array sized by a function parameter in `sia8152_regs.c` (sized with `ARRAY_SIZE`).

**fix-sia81xx-vla.** The same variable-length-array problem in the other SIA81xx
speaker-amplifier files (`sia8152s_regs.c`, `sia8159_regs.c`). The sizes are now
compile-time constants, so the speaker codec builds.

**build-mt6833-dtb.** The arm64 MediaTek DTS Makefile does not list `mt6833.dtb`.
The patch adds it so `dtbs_install` produces `boot/dtbs/mediatek/mt6833.dtb`.

**add-thunder-dt-compatible-chassis.** The root node said only `mediatek,MT6833`. It now
lists `xiaomi,thunder` first, so gmobile (Phosh) can match the phone's display data, and
sets `chassis-type = "handset"` for hostnamed. `mediatek,MT6833` stays: `clkchk-mt6833`
and `clkdbg-mt6833` check it. The bootloader rewrites `model` but leaves both alone.

**fix-u-serial-null-close.** `gs_open()` in the USB serial gadget sets
`tty->driver_data` near its end, after several early error returns. The tty layer still
calls `gs_close()` on the release path of a failed open, so `port` can be NULL there and
the kernel panicked. `gs_close()` now returns early on a NULL port.

**fix-mtk-gem-ion-null.** `mtk_gem_ion_import_dma_buf()` passed a NULL ION client to
`ion_import_dma_buf()` when no Android userspace exists, and dereferenced
`handle->buffer` before checking the returned pointer (an `ERR_PTR` on failure). It now
fails cleanly with `-ENODEV` / `-EINVAL`.

**fix-mtk-gem-prime-import-fallback.** `mtk_gem_prime_import()` accepted only DMA-BUFs
it could import through ION and returned `-EINVAL` for anything else, although the
driver advertises `DRM_CAP_PRIME` with import. A compositor that allocates a dumb
buffer, exports it and re-imports it through a second open of the node (which is what
wlroots does) therefore could not scan out. The patch falls through to the generic DRM
import when the buffer is not an ION one. See "Display and GPU findings" below.

**fix-mtk-plane-disable-without-layer-blob.** (r24) `mtk_plane_atomic_disable()` only
pushed the update when its private `state->crtc` was set, and that field is filled only
by the MediaTek layer-blob path, which the Android hardware composer drives and a plain
DRM atomic compositor never does. The patch falls back to the CRTC the plane was on
(`old_state->crtc`) and makes the CRTC update turn the layer off outside
`USER_SCEN_BLANK` too.

**fix-mtk-ipv6-optr-sysctl-netns-leak.** (r25) MediaTek registers `net.optr` (one global
int) in every network namespace and never keeps or unregisters the header. Each
namespace teardown tripped the WARN in `retire_sysctl_set()` and left the table hanging
off a freed `struct net`. The patch registers it once, in `init_net`. Anything that
sandboxes with a network namespace hit this.

**fix-mtk-gauge-kernel-algo.** (r26) Gauge Master 3.0 computes the state of charge in
Android's `fuelgauged` daemon, which reaches the driver over netlink. Without the
daemon the capacity reads `-1`. The driver carries a complete in-kernel copy of the
algorithm (`mtk_battery_recovery.c`) that only runs in recovery. The patch adds
`CONFIG_MTK_GAUGE_KERNEL_ALGO` and routes the five daemon-or-recovery decisions in
`mtk_battery.c` through it. The algorithm reports through the same `SET_KERNEL_UISOC`
command the daemon would use, so nothing above the driver changes.

**fix-mtk-gauge-charge-full-uisoc.** (r31) With the in-kernel algorithm the level stopped
at 99% on the charger: the charger raises its charge-full event, but the recovery code
had no handler for `FG_INTR_CHR_FULL`, and a full battery draws too little current to
take the last coulomb-counter step. The patch handles the event and reports 100% while
the charger is present, as `fuelgauged` does. The counter-based level is left alone.

**fix-mtk-backlight-follow-panel-blank.** (r27) The panel driver blanks the display but
never touches the backlight; on Android the lights HAL writes 0 to `lcd-backlight`.
Without it the backlight stays lit over a black panel. The LED driver now follows the
vendor DRM blank notifier chain: chip off on `DRM_BLANK_POWERDOWN`, level restored on
`DRM_BLANK_UNBLANK`, and any level written while blanked is held until the panel is back.

**fix-nvt-touch-follow-panel-blank.** (r28) The NT36672C touch controller resets with the
panel and needs `nvt_ts_resume()` once the panel is back, but its driver listens only for
framebuffer blank events, which no DRM compositor emits. The touch driver now follows the
same DRM blank chain: suspend on blank; on unblank, resume and reload the firmware
(about 120 ms). The burst of checksum errors after each blank is gone because the
controller is put to sleep instead of losing power under the driver.

**fix-panel-mode-clock-refresh.** (r29) The Tianma NT36672C panel driver declared mode
clocks that do not match `htotal * vtotal * vrefresh`: the 60 Hz mode was advertised at
48.96 Hz and the 90 Hz mode at 73.13 Hz, so compositors paced frames against the wrong
rate. Each `.clock` is now computed from the mode's own timings (60 Hz: 308356 kHz).
The DSI link takes its rate from the panel's `pll_clk`/`data_rate` params, so only the
advertised rate changes. Timing vblanks with `DRM_IOCTL_WAIT_VBLANK` gives 60.5 Hz.

**fix-panel-pwm-gpio-optional.** The Tianma and JDI panel drivers take a `pwm` GPIO on
prepare and unprepare, but the DT has no `pwm-gpios`. The error pointer from
`devm_gpiod_get()` went to `gpiod_set_value()` ("invalid GPIO") and `devm_gpiod_put()`,
which WARNs with a backtrace: one per screen-off. Both drivers skip the GPIO when the
lookup fails.

**backport-procfs-fdinfo-ptrace-read.** (r30) Backport of upstream 5.14 `7bc3fa0172a4`
and `1927e498aee1`. A process that drops privileges without `exec()` becomes
non-dumpable, its `/proc` files turn root-owned, and it cannot read its own
`/proc/self/fdinfo`. polkit 124+ identifies a subject through a pidfd and reads its PID
from fdinfo, so every caller resolved to "Process not found". With the backport, fdinfo
access is checked against `PTRACE_MODE_READ` instead of the file mode.

**backport-drm-prime-ioctls-no-auth.** Backport of Linux 5.2 "drm: allow render capable
master with DRM_AUTH ioctls" and "drm: remove DRM_AUTH from PRIME_TO/FROM_HANDLE
ioctls". wlroots allocates scan-out on a non-master fd of `card0` and exports it through
PRIME; on 4.19 that returns `EACCES` for every user except root.

**add-drm-panfrost-mt6833.** Panfrost for the Mali-G57 MC2 on 4.19: the 5.15.194 driver
with its own drm_sched, shmem helper and io-pgtable, the G57 hardware support from 6.12,
power handled through MediaTek's gpufreq (functions exported by this patch), and the GPU
node in the device tree switched to `arm,mali-valhall-jm`, which the vendor kbase driver
does not match. Shipped in r32. Details: [gpu-bringup.md](gpu-bringup.md).

**fix-devfreq-trans-stat-overflow.** Backport of the upstream `trans_stat_show` fix. The
N x N transition table was written with unbounded `sprintf` and could overflow the sysfs
page with many OPPs; a `cat` of it oopsed in `iov_iter_advance`. With the panfrost
devfreq on the MT6833 the table fits in one page, but the bound is now guaranteed.

**add-mtk-lcd-backlight-class.** Registers the `lcd-backlight` LED also as
`/sys/class/backlight/lcd-backlight` (type raw), so brightness controls that expect the
backlight class find it. Writes go through the LED core, so both views stay in sync and
the panel-blank hold (r27) still keeps the level at 0. A floor of about 1% stops a
slider at zero from turning the screen off.

**add-mtk-hf-sensors-iio.** The ambient-light and proximity sensor (TMD2755) and the
accelerometer, all behind the SCP, as IIO devices `als` (`in_illuminance_input`, lux),
`proximity` (`in_proximity_raw`, 10 = near) and `accel` (`in_accel_{x,y,z}_raw`), read
through `hf_manager` the way the Android HAL does. Each sensor is enabled only while
read and disabled 10 s later. Details: [sensors.md](sensors.md).

**fix-ktd3137-brightness-fade.** The KTD3137 I2C brightness transition now takes about
512 ms (the device tree asks for about 128 ms). The probe wrote `0x01` to
`REG_TRANS_RAMP` right after configuring the chip, which undid the computed value; that
write is removed. Userspace changes the level in one step and the chip does the fade.

**fix-mt6360-transmit-buffer-overflow.** `mt6360_transmit()` builds the USB Power
Delivery TX block in a 30-byte stack buffer but writes 31 bytes when a message carries
seven data objects (byte count, two-byte header, 7 x 4). A USB PD 3.0 source asks for
Discover Identity, the reply has seven objects, and the stray byte hits the stack canary:
`Kernel panic - not syncing: stack-protector: Kernel stack is corrupted in:
mt6360_transmit`, about 13 s after boot. Plugged into such a port, the phone boot-looped
every ~26 s; on a port without PD it never showed. The panic was read from pstore with
the initramfs UDP beacon (`thunder.debug=1`).

**fix-u-ether-linearize-tx.** MediaTek's `u_ether` sets `NETIF_F_SG`, which this 4.19
tree accepts without checksum offload, so TCP queues payload in page fragments. The
single-packet TX path sends `skb->data` with `skb->len` bytes, so everything after the
linear head went out as whatever followed it in memory. The host saw bad TCP checksums,
conntrack marked the segments INVALID and the firewall dropped them: ping and the first
~1 KB of each write passed, then TCP stalled (SSH never finished its key exchange over
USB). The patch linearizes the skb on that path. Measured after the fix: 50 MB over ssh
in 2.7 s.

### Backports for Arch's systemd 262 userland

Arch's `systemd-libs` 262 (libudev, sd-event) and `systemd-udevd` assume a newer kernel
and have no fallback for these four features. Without them libudev enumerated no
devices on 4.19 (`udev_enumerate_scan_devices` returned `-EUNATCH`) and udevd failed
on 192 devices, the touchscreen among them. With them, Arch's own udevd runs under
OpenRC and tags the touchscreen (`ID_INPUT_TOUCHSCREEN=1`). Each was found and checked
on the phone with a probe (`statx-mntid.py`, `waitid-pidfd.py`, `close-range.py`) or
strace, and written from the upstream commit with two adversarial reviews.

- **backport-statx-mount-id** (upstream fa2fcf4f1df1 + 80340fe3605c, 5.8): `statx`
  returns `STATX_MNT_ID` and `STATX_ATTR_MOUNT_ROOT`. systemd's `chase()` needs the
  mount ID to verify paths and does not fall back to `name_to_handle_at`.
- **backport-waitid-pidfd** (3695eae5fee0, 5.4): `waitid(P_PIDFD)`. sd-event reaps
  udevd's workers through pidfds.
- **backport-close-range** (278a5fbaed89, 5.9, flags 0 only): `close_range` as
  syscall 436, native and compat. Any non-zero flag returns `-EINVAL`, so callers that
  ask for `CLOSE_RANGE_CLOEXEC` (OpenRC) fall back.
- **backport-utimensat-empty-path** (44a3b8744405, 5.8): `utimensat(fd, "", ...,
  AT_EMPTY_PATH)`. udevd touches tag files and device nodes this way; 4.19 returned
  `EINVAL` and udevd dropped the whole event.

- **backport-faccessat2** (94704515 + c8ffd8bcdd28, 5.8): syscall 439. Harmless for udevd
  (glibc falls back), but required for systemd as PID 1: the executor checks every
  `ExecStart=` with `faccessat(fd, "", X_OK, AT_EMPTY_PATH)`, which glibc 2.43 sends to
  syscall 439 and fails with EINVAL when it is missing, so every service died with
  `203/EXEC`.

Still missing and harmless: `epoll_pwait2` (sd-event falls back), `openat2` (bubblewrap is
rebuilt with its runtime fallback, see `rootfs/packages/bubblewrap-thunder`), the new
mount API and per-mount procfs (systemd units that need them get drop-ins, see the
systemd notes in the image).

Kernel config for systemd as PID 1 (r40): `FHANDLE`, `CGROUP_PIDS`, `AUTOFS_FS`,
`CHECKPOINT_RESTORE`, `NET_SCH_FQ_CODEL`, no `UEVENT_HELPER`; and for iwd, AF_ALG
(`CRYPTO_USER_API_HASH/SKCIPHER/AEAD`) and `KEY_DH_OPERATIONS`. nf_tables is on since rB (next section).

**fix-vendor-kthreads-idle-sleep.** Vendor kthreads that poll forever (`wdtk-0..7`,
`gauge_timer_thr`, `mivr_thread`, `hang_detect`, `ccci_poll1`, `mddp_rx`, `disp_echk`) slept
in `TASK_UNINTERRUPTIBLE`, which counts in the load average: an idle phone showed ~16.
They now sleep in `TASK_IDLE`, which is not counted, with the same timing:
`usleep_idle_range()` (mainline 5.16) and `msleep_idle()` (ours) in
`kernel/time/timer.c`, `wait_event_idle()` and `wait_event_idle_timeout()`. `mdrt_thread` was
created and never woken, so it sat in `kthread()`; it is now started at init and parks
itself until the first MDRT stall. Measured: load 2.1 five minutes after boot.

### rB: nf_tables, netfilter at 4.19.325, userns gate, no static usermode helper

Design and evidence: `build/research/nft/DESIGN.md` (with R1 stable sweep, R2 CVE map, R4
applicability). Flashed and tested on the phone on 2026-10-12; the acceptance list, the test plan and
the results are in `netfilter.md`.

**What.** Nine series entries, appended after `fix-vendor-kthreads-idle-sleep`:

- **backport-netfilter-4.19.325.** The netfilter code of the tree becomes that of
  `v4.19.325`: 191 stable commits, 109 files, +2669/-1483 (`net/netfilter`,
  `net/{ipv4,ipv6}/netfilter`, `net/bridge/netfilter`, `br_netfilter_*`, `include/net/netfilter`,
  `include/net/netns/nftables.h`, `include/{linux,uapi/linux}/netfilter*`). Left out: the 23 generic
  `net/netlink` commits (a later revision, rD) and the DECnet removal (the vendor still has
  DECnet). `uname` keeps saying 4.19.191. The patch header carries the exact recipe, the
  list of commits and the sha256 of the body. Invariant: tree + patch compared with `v4.19.325`
  on those paths differs only in the vendor files (`xt_quota2`, `xt_IDLETIMER`, `rcu_read_lock` in
  `nf_reinject`, a non-static `nf_nat_ipv4_fn`, Kconfig, Makefile) and DECnet.
- **add-netfilter-userns-restrict.** The gate, below.
- **backport-nf-reject-ipv6-ll-max-header** (4ed234fe793f, CVE-2024-50256),
  **backport-nf-ct-tcp-rst-direction** (bed6e04be8e6, CVE-2026-63913) and
  **backport-nf-ct-reasm-mac-header** (3b08fed5b7e0, CVE-2026-72250): one to three lines
  each, no 4.19.y fix.
- **fix-mtk-default-no-dccp-act-ipt.** Removes three `select` lines of
  `drivers/misc/mediatek/Kconfig.default` (`NF_CT_PROTO_DCCP`, `NF_NAT_PROTO_DCCP`,
  `NET_ACT_IPT`). A `select` beats `# CONFIG_X is not set`, so the config alone would not do.
- **backport-cgroup-v1-release-agent-capable** (stable 4.19.229, CVE-2022-0492): see Risks.
- **backport-reboot-hw-protection-shutdown** (dfa19b11385d, 5.14) and
  **fix-mtk-charger-battery-missing-poweroff**: the forced fallback of the missing-battery
  shutdown, see Risks.

**Config.** `kernel-config` gains the nftables group (`NF_TABLES`, inet/ip/ip6/arp/bridge families,
ct, nat, masq, redir, reject, compat, fib, ...; 10 `nft_*` symbols are modules), and loses
`STATIC_USERMODEHELPER`. `NF_CT_PROTO_DCCP` (CVE-2023-53333, remote stack OOB; the upstream fix does not fit the 4.19 layout and nobody
uses DCCP), `NET_ACT_IPT` and `NF_CONNTRACK_TIMEOUT` are off; `NF_NAT_PROTO_DCCP` follows
`NF_CT_PROTO_DCCP`. Not enabled: `NF_TABLES_NETDEV`, flow tables, `NFT_SOCKET/TPROXY/OSF/TUNNEL/DUP`, IPVS, ipset,
legacy ebtables, `NF_CONNTRACK_SIP`. No NAT in the `inet` family (5.2): NAT goes in `ip`/`ip6`
tables; firewalld does not run (needs 5.3); `iptables` works through its nft backend.

**The gate.** Upstream accepts `CAP_NET_ADMIN` over the netns (`ns_capable(net->user_ns)`), and with
`USER_NS` and `NET_NS` any user gets that from `unshare -Urn`. With
`net.netfilter.nf_userns_restrict=1` (the default, in the code from boot) configuring netfilter also needs
`CAP_NET_ADMIN` in the initial user namespace. Covered: `nfnetlink_rcv()` (nf_tables, ctnetlink,
nfqueue, nflog, ipset), `nfnetlink_bind()` (event subscription and module autoload) and the sockopts of
`ip_tables`, `ip6_tables`, `arp_tables`, `ebtables`, `ipset`, `ipvs`. Root, NetworkManager,
systemd-networkd and `nftables.service` see no difference; an admin of a child user namespace gets `EPERM`.
Because `act_ipt` (tc action `xt`) runs xtables targets with only `netlink_ns_capable` and no gate, it is
compiled out instead. The sysctl exists only in `init_net`, range 0..1, and has its own `proc_handler`:
`net_ctl_permissions()` lets any `CAP_NET_ADMIN` holder write network sysctls like root, so the handler
requires `capable(CAP_SYS_ADMIN)` for writes (EPERM otherwise). Opt-out, owner decision only:
`net.netfilter.nf_userns_restrict = 0` in `/etc/sysctl.d`. What the gate breaks at `=1`: podman rootless with
bridge networking (netavark), Docker rootless, `nft`/`iptables` inside unprivileged containers (LXC/Incus,
`systemd-nspawn --private-users`); bubblewrap and `unshare` itself keep working.

**CVE policy** (86 CVEs with compiled code and no 4.19.y fix, R2):
- nfnetlink (30) and sockopt (21) entry points: held by the gate; root is needed after it. `MODULE_SIG` is off, so
  root and kernel are already one boundary.
- Packet-path CVEs: per CVE. Fixed by the three backports; DCCP conntrack removed; conntrack helpers
  (h323, irc, amanda, ...) need a `ct helper` rule and `nf_ct_auto_assign_helper` is false;
  `connlimit` CVEs need a `connlimit` rule; `ct timeout` code is not compiled. **Policy for any ruleset we
  ship: no `ct helper`, no `connlimit`, no `ct timeout`.**
- Bridge CVEs (CVE-2024-27415, 35839, 2026-52912, 72255 and the like): any user can create a bridge in
  its own netns, and `br_netfilter` hooks (`BRIDGE_NETFILTER=y`, forced by the vendor `select`) run in every netns.
  So these are not limited to Waydroid or a hotspot; what holds them is the gate (configuring netfilter,
  nfqueue and the ebtables sockopts from a user namespace is refused) together with `NET_ACT_IPT` being
  off. If the gate is switched to 0, they are open. The gate does not cover the other case: when root keeps a
  bridge in the initial netns with conntrack on (Waydroid's `waydroid0` with NAT, a bridged hotspot),
  CVE-2024-27415 (bridge multicast with conntrack, adjacent attack vector) is reachable by traffic from the
  neighbouring network.
- CVE-2024-26921 (inet_defrag UAF, local, with an IPv6 conntrack in the initial netns) is **not** fixed: it is
  core IP code, planned as rB.1. Until then do not enable a stateful IPv6 firewall by default (the
  NetworkManager `shared` hotspot creates one).
- The CNA data misses CVEs before Feb/2024, and nf_tables bugs without CVE exist: the gate is what carries the security
  of rB; the slice and the backports are defence in depth against root and the packet path.

**`STATIC_USERMODEHELPER=n`.** What works again: module autoload (`/sbin/modprobe`, so the `NFT_*=m` load on
demand, as root only past the gate), the `core_pattern` pipe, `orderly_poweroff/reboot` (`/sbin/poweroff`,
which is `systemctl`), `/sbin/request-key`. The bridge STP helper stays absent and `UEVENT_HELPER` is already off.

**core_pattern.** 4.19 knows `%d` and `%I` but not `%F` (pidfd, 6.16). systemd 262 ships
`kernel.core_pattern=|.../systemd-coredump %P %u %g %s %t %c %h %d %F %I`; without `%F` the
split collapses the spaces and `%I` lands in the pidfd slot. systemd-coredump requires only up to
`%c`. `tools/package-kernel.sh` therefore adds `usr/lib/sysctl.d/51-thunder-coredump-no-pidfd.conf` to the kernel
package whenever the kernel version is below 6.16 (generic, not tied to the device; it sorts after the
systemd `50-coredump.conf` and overrides only `kernel.core_pattern`, with `%P %u %g %s %t %c %h %d`).
`update-kernel.sh` installs only that package, so the file reaches the phone with the kernel. The script
fails if the file is not in the package; the presence of the file in the package is an acceptance check.

**Risks.**
- **Protection poweroff goes through systemd.** With the static helper off, `orderly_poweroff(true)`
  (`thermal_core.c`, the missing-battery check of `mtk_charger.c`) runs `/sbin/poweroff`, i.e.
  `systemctl poweroff`. `run_cmd()` uses `UMH_WAIT_EXEC`, so the kernel forces the power off only when the
  exec fails; before, the helper did not exist and it always did. Both paths keep a forced fallback 30 s after
  the orderly request, so a hung systemd no longer keeps the phone on: thermal through
  `THERMAL_EMERGENCY_POWEROFF_DELAY_MS=30000`, the battery check through `hw_protection_shutdown()`
  (the two patches above). The forced power off does not sync: a shutdown that takes longer than 30 s
  loses what was not written. No safe trigger exists to test either path.
- **cgroup v1 `release_agent` (CVE-2022-0492).** The vendor tree lacks the fix, and with the static
  helper off the `release_agent` runs again (as root, in the initial namespaces). A user who mounts an
  existing v1 hierarchy inside `unshare -UrmC` could point it at a program of his. The image only mounts
  cgroup2, but LXC, Waydroid or any `mount -t cgroup` by root create a v1 hierarchy. Closed by
  `backport-cgroup-v1-release-agent-capable.patch` (stable 4.19.229), part of rB.
- `modprobe_path` and `core_pattern` are again final steps of an arbitrary kernel write. Accepted: whoever has that
  already owns the credentials.
- Autoload is again reachable by unprivileged code where the kernel asks for no capability (about 14 modules plus
  the `nft_*`, which pass the gate).
- The sysctl at 0 reopens everything. Only real root writes it (`CAP_SYS_ADMIN`).
- `struct net` changes (`netns/nftables.h` shrinks), so every module's CRC changes: all modules must come from
  the same build as the boot image (they do through the kernel package).
- The rest of the kernel stays at 4.19.191.
- The gate was compiled but, as of this writing, never run on the device.

### Bluetooth core at 4.19.325

**backport-bluetooth-4.19.325**, right before `add-btmtk-stp-hci-driver`. Same recipe as
the netfilter slice: `net/bluetooth` and `include/net/bluetooth` become those of `v4.19.325`,
104 stable commits, 23 files, +886/-400. The vendor tree is identical to `v4.19.191` on those
paths, so tree + patch equals `v4.19.325` there. `drivers/bluetooth` is left out: none of its
drivers is built (the transport is `btmtk_stp`). The patch header has the recipe, the commit
list and the sha256 of the body; the CVE map is in `build/research/bt/REPORT.md`.

- **Closes**, among others, the over-the-air L2CAP bugs CVE-2022-42896 (LE connect with an
  invalid SPSM, use after free), CVE-2022-42895 (uninitialized EFS, kernel memory leak),
  CVE-2022-45934 (CONF_REQ overflow), CVE-2022-3640 (A2MP, built because `BT_HS=y`) and
  CVE-2021-47620 (advertising report out of bounds while scanning).
- **Still open:** dozens of Bluetooth CVEs never fixed in 4.19.y (L2CAP, RFCOMM, SMP, HIDP).
  With the radio not discoverable, not connectable and not pairable (the BlueZ default), a
  stranger cannot open a connection; the exposure is while pairing or connected.
- **ABI:** `struct hci_dev` changes (`name` becomes `const char *`), so `btmtk_stp.ko` must come
  from the same build (the kernel package does that). No config change.

### Vibration and flashlight

- **add-thunder-regulator-haptic.** A `regulator-haptic` node in `mt6833.dts` on the
  MT6359P VIBR LDO (2.4 to 3.0 V), with `CONFIG_INPUT_REGULATOR_HAPTIC`. The vendor
  `mediatek,vibrator` driver (its node comes from the factory dtbo) only offers Android's
  "vibrator" LED; feedbackd wants a force-feedback input, and its udev rules already know
  `regulator-haptic`. Measured: a 1 s rumble enables the LDO at 2.8 V and turns it off at
  the end. `CONFIG_MTK_VIBRATOR` is off: the vendor driver writes the LDO enable bit
  straight to the PMIC (on suspend and overcurrent), behind the regulator's back.
- **add-mt6360-torch-led-class.** The rear LED is FLED1 of the MT6360 PMIC (the
  `ktd3137_flash` LED belongs to the backlight chip). The vendor reaches it only through the
  camera HAL's `/dev/flashlight`; the patch adds a `white:torch` LED class device, which
  Phosh's torch button finds. Brightness 1 to 15 is 25 to 200 mA in torch mode; the chip
  goes to 400 mA, but torch mode has no hardware timeout. Flash (strobe) mode is not exposed. It shares
  FLED1 with MediaTek's flashlight core without a common lock; harmless while nothing uses
  the camera HAL.

### gen4m: bounds on frames from the air

Two fixes in the Wi-Fi driver, found by a review of what a stranger in radio range reaches
without joining a network (2026-10-14; second opinion from another model family).

- **fix-gen4m-rnr-parse-bounds.** The Reduced Neighbor Report walk in a scan kept its offset
  in a u8. A beacon with one field of 4 TBTT entries of 63 bytes made the step 256, the
  offset wrapped to 0 and the loop never ended: a remote hang without association. The
  offset is now a u16 and the walk stops at the end of the element.
- **fix-gen4m-he-cap-bounds.** The HE Capabilities element from the AP (association
  response) was checked only for its fixed part; the MCS maps and PPE thresholds were read
  past a short element. It is now ignored when shorter than what its PHY bits announce.
- **Left as is:** an extension element (ID 255) of length 0 makes about 26 call sites read
  the extension ID one byte past the element, still inside the RX buffer; the byte is only
  compared, and the HE Capabilities path checks the length again.
- MediaTek's 2024 bulletins list remote out-of-bounds writes in the WLAN driver and firmware
  (CVE-2024-20101, CVE-2024-20103) without naming functions; this BSP predates them and no
  newer one exists for MT6833.

## Connectivity graft

The Wi-Fi, Bluetooth and WMT drivers are not in the Xiaomi tree for this SoC in a usable
form. `tools/build-kernel.sh` replaces five directories under
`drivers/misc/mediatek/connectivity/` (`wmt_drv`, `wmt_chrdev_wifi`, `wlan_drv_gen4m`,
`connfem`, `bt_drv`) with the ones from `devices/xiaomi-thunder/connectivity/mtk-connectivity-gen4m-mt6833-2.tar.gz`.

- `common/` has to come from the same drop. The `struct wmt_platform_bridge` layout
  changes between generations, and mixing them fails silently at runtime. The build
  copies `connectivity_build_in_adapter.{c,h}` and `wmt_build_in_adapter.{c,h}` from the
  drop's `wmt_drv` into `common/`.
- The Makefile that ships in the Xiaomi tree hard-codes the wrong chip
  (`MTK_COMBO_CHIP=CONNAC`, `WLAN_CHIP_ID=6765`). `devices/xiaomi-thunder/connectivity/Makefile-mt6833`
  replaces it. It is based on the connectivity Makefile of the "camellian" drop (Xiaomi's
  4.14 kernel for the real MT6833 platform), and every local change is explained in
  a comment in the file.
  Main points:
  - `-Werror` removed from the four lines that had it (the drop is 4.14 code built on
    4.19; the build also strips `-Werror` tree-wide).
  - clkbuf include points at `base/power/include/clkbuf_v1`, the tree that implements
    `clk_buf_ctrl()`. The only consumer is behind `CONNADP_HAS_CLOCK_BUF_CTRL`, which does
    not list `CONFIG_MACH_MT6833`, so this is inert for now.
  - Include paths for `wmt_chrdev_wifi` and `wlan_drv_gen4m` are redirected from the
    vendor-repo layout (`connectivity/common/common_main/...`) to the grafted
    `connectivity/wmt_drv/` location. `test.o` is built only with `CONFIG_CONN_PWR_DEBUG`.
  - The per-chip dispatch block runs unconditionally (module and built-in builds both need
    the exported `MTK_COMBO_CHIP`, `WLAN_CHIP_ID`, `CONNAC_VER`), the vendor symlinks and
    `$(error)` are gone, and `CONFIG_WLAN_DRV_BUILD_IN` stays off on purpose: each
    sub-Kbuild uses it to choose between `obj-y` and `obj-m`, and these come out as
    modules. The MT6833 path sets `BT_PLATFORM` so `bt_drv` builds `bt_drv_6833.ko`.
  - `wlan_service` is turned on. The factory `wlan_drv_gen4m.ko` contains it (189 function
    names from `wlan_service/` match with `readelf -sW`), and with the service off the
    tree does not link, because nothing in the drop includes the service headers outside
    `#if CONFIG_WLAN_SERVICE == 1`.
- `devices/xiaomi-thunder/connectivity/gen4m-mt6833-kernel419.diff` is applied inside the connectivity
  directory (`patch -p1 -d`). It adapts the drop to this kernel: a stub for
  `wlanoidListMode` when `CONFIG_WLAN_SERVICE != 1`; local definitions for
  `NL80211_WPA_VERSION_3` and `LIST_SEG_MAX`, which this tree lacks; the pm_qos mapping
  with the `cpu_ctrl.h`/`topo_ctrl.h` includes and `DDR_OPP_*` defaults that MT6833 lacks
  in `mtk-pm-qos.h`; and the substitution of `set_task_util_min()` (0..1024 scale,
  exported by `kernel/sched/core.c`) for `set_task_util_min_pct()`. It touches
  `wlan_oid.c`, `gl_cfg80211.c`, `gl_qa_agent.h`, `plat/mt6833/plat_priv.c` and
  `mtk_wcn_consys_hw.c`.

Bring-up of the userland side: [wifi-bringup.md](wifi-bringup.md),
[bluetooth-bringup.md](bluetooth-bringup.md).

## Kernel facts that shape the userland

- **`CONFIG_STATIC_USERMODEHELPER` is off since rB** (it used to point at an Android binary and
  neutralised every usermode helper). Module autoload, the `core_pattern` pipe and a clean
  `orderly_poweroff` work again; the `core_pattern` needs the override that
  `tools/package-kernel.sh` ships (no `%F`), and the poweroff caveat is in the rB section.
- **The initramfs must be gzip.** `CONFIG_RD_GZIP` is the only initramfs decompressor
  (bzip2, lzma, xz, lzo and lz4 are off). The LK bootloader also accepts only a compressed
  kernel (Image.gz).
- **No systemd.** The kernel lacks `close_range()` (5.9) and `waitid(P_PIDFD)` (5.4), and
  `CONFIG_FHANDLE` is off. Since systemd 260 (baseline 5.10) there is no fallback, so
  systemd 262 cannot run here. Hence OpenRC as PID 1.
- **`waitid(P_PIDFD)` is missing but `pidfd_open()` and `CLONE_PIDFD` exist** (backported
  into the Android 4.19 tree). A pidfd can be created and polled, but cannot be reaped
  with `waitid`: the call returns `EINVAL`. `kernel/fork.c` handles `CLONE_PIDFD` while
  `include/uapi/linux/wait.h` defines only `P_ALL` and `P_PID`. glib's child watch needs a
  fallback to the SIGCHLD path (see `rootfs/packages/glib/`); without it a caller gets
  wait status `-1` and children stay zombies.
- **Other syscalls missing for a 4.19 base:** `openat2()` (5.6) returns `ENOSYS`, so
  bubblewrap built with `-Dassume_kernel` at a newer kernel fails every bind mount.
  `faccessat()` with `AT_SYMLINK_NOFOLLOW` returns `EINVAL` (`faccessat2()` is 5.8), which
  makes glib report existing files as missing.
- **No Landlock.** pacman must run with `DisableSandboxFilesystem`.
- **nf_tables is on (rB), for the initial user namespace only:** configuring netfilter needs
  `CAP_NET_ADMIN` in the initial userns (`net.netfilter.nf_userns_restrict=1`). No NAT in the `inet`
  family (5.2), no firewalld (5.3); `iptables` uses the nft backend and `iptables-legacy` still works for root.
- **No `AF_ALG` and no `KEY_DH`.** iwd cannot work, so Wi-Fi uses wpa_supplicant.
- **The NCM USB gadget is broken on this MUSB controller.** Only RNDIS works, so the USB
  network is RNDIS.
- **`CONFIG_FRAMEBUFFER_CONSOLE` caused a bootloop,** so there is no text console on the
  screen.
- **pstore works.** `console-ramoops` is the panic log; after a crash and reboot read
  `/sys/fs/pstore`.
- **Vendor kthreads sleep in state D by design:** `mdrt_thread`, `mivr_thread`,
  `gauge_timer_thr` and the `wdtk-*` watchdog kickers. They hold the load average near 15
  on an idle device. It is not a hang; `khungtaskd` names them.
- **Debugging a device that resets:** logs written up to the reset do not survive on the
  device. `/tmp` is tmpfs,
  and a file newly created on the rootfs is discarded as an orphan inode by the boot-time
  fsck. A pre-created file survives but its output stays in the pipe buffer. Streaming each
  line off the device over the network was the only method that captured the failures
  below.

### Display and GPU findings

- **Dumb-buffer PRIME re-import.** The display driver advertised `DRM_CAP_PRIME` with
  import while accepting only ION buffers. A 30-line reproducer (create dumb buffer,
  export, re-import) succeeds on the same file descriptor, because that only hits the
  per-file PRIME handle cache, and fails on a second descriptor of the same device.
  Swapchain test failures went from 2012 to 0 with
  `fix-mtk-gem-prime-import-fallback.patch`.
- **Plane disable is a no-op without the layer-blob path.** On this driver, disabling a
  plane from a plain atomic compositor never reaches the hardware, which keeps scanning
  out the last buffer it was given. That is harmless while the compositor feeds it and
  fatal once the compositor blanks and frees it: the overlay engine
  (`L1_OVL_2L_RDMA0`) raises `mtk_iommu_isr <<TRANSLATION FAULT>>` on the freed IOVA and
  the device dies between 50 s and 5 min later, with nothing in pstore. Legacy KMS is no
  way out (`WLR_DRM_NO_ATOMIC=1` fails identically). Measured on r24: DPMS off for 45 s
  and back on survives, and power-button blanking works. The vendor idle manager
  (`/proc/mtkfb`, `enable_idlemgr`) never goes through drm_atomic and is not involved; an
  A/B on it looked conclusive and was wrong.
- **No `pwm-gpios` for the panel.** The panel driver tries a `pwm` GPIO on unprepare, but
  no device tree defines `pwm-gpios`, not even Xiaomi's overlay. That is the
  `gpiod_set_value: invalid GPIO` warning on every blank. The backlight is only the LED
  class device `/sys/class/leds/lcd-backlight` (0 to 2047) in front of the KTD3137 chip.
- **Touch after blank.** `nvt_ts_resume()` is only called from framebuffer blank events.
  By hand, `echo 4 > /sys/class/graphics/fb0/blank` followed by `echo 0` wakes the touch
  controller; the `4` matters, because without a suspend first the resume is skipped as
  `Touch is already resume`.
- **Rendering on the CPU costs the compositor.** Before the GPU (r32), everything was
  composed on one thread; the panel's wrong advertised refresh rate (r29 fix) and fractional
  scaling made the interface lag. The GPU arrived with `add-drm-panfrost-mt6833.patch`.

### Power and battery findings

- **The gauge reports `capacity=-1` without the daemon,** which a power manager reads as
  0%. With the battery then discharging above the 500 mA the USB port supplies, UPower's
  critical-battery chain (`CanHybridSleep`, `CanHibernate`, `PowerOff`) shut the phone
  down 8 to 60 seconds into a session. That looked like a silent hang: the log stops, no
  kernel output, stale pstore (a real power-off loses RAM), and
  `androidboot.bootreason=usb` because the phone powers back on only because the cable is
  in. Measured on r26: `capacity` goes from `-1` to `100` (`Full` at 4.41 V on the
  cable); unplugged it counts down (100 to 99% at about 13 minutes of uptime,
  `Discharging` at 451 mA); after a reboot it resumes from the value kept in the RTC
  (98%) instead of starting over. `dbus-monitor` on the system bus is what caught the
  `PowerOff` call.
- **Charge full.** The charger does fire `battery full!`; handling it is
  `fix-mtk-gauge-charge-full-uisoc.patch`. On discharge, `fgr_bat_int2_l_handler()` already
  scales its steps by `ui_soc / soc`, so the two levels converge again.
- **Liveness monitors can fool you.** `ps aux | grep -c "[n]ame"` run from a shell whose
  own command line contained the string counted itself. `pgrep -x <name> | wc -l` is
  the correct form.

### Unrelated kernel bug, not fixed

Tearing down the USB gadget through configfs panics the kernel with a NULL dereference in
`sysfs_remove_file_ns`:

    sysfs_remove_file_ns -> device_remove_file -> gadgets_drop
    -> configfs_rmdir -> vfs_rmdir2 -> do_rmdir -> unlinkat

Do not remove the gadget at runtime. This fits the USB networking being unreliable on this
device. No patch yet.
