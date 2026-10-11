# Interface profiles

A profile decides what the phone shows on its screen. It lives in
`rootfs/profiles/<name>.sh` and is chosen with `THUNDER_PROFILE` (or in the
installer). Every profile gets the same base: Arch Linux ARM, OpenRC, SSH over
the USB cable, the device's firmware and services.

| Profile | Status | What it is |
|---|---|---|
| `none` | tested | No interface. SSH and telnet over the USB cable |
| `console` | experimental | Full-screen terminal with an on-screen keyboard |
| `phosh` | planned | GNOME's phone shell |
| `plasma-mobile` | planned | KDE's phone shell |

`planned` profiles show up in the installer with what is missing, and
`tools/build-rootfs.sh` refuses them unless `THUNDER_ALLOW_PLANNED=1` (for whoever is
working on them).

## Profile file

| Variable | Meaning |
|---|---|
| `PROFILE_ORDER` | Position in the installer list |
| `PROFILE_TITLE`, `PROFILE_DESCRIPTION` | Shown in the installer |
| `PROFILE_STATUS` | `tested` (validated on a real phone), `experimental` (builds, not validated) or `planned` (missing base) |
| `PROFILE_BLOCKERS` | For `planned`: what is missing, in one sentence |
| `PROFILE_PACKAGES` | Arch packages installed in the image |
| `PROFILE_SERVICES` | OpenRC services to enable. `name` goes to the `default` runlevel; `name:runlevel` picks another one (`udev:sysinit`) |
| `PROFILE_GROUPS` | Supplementary groups of `THUNDER_USER` (`seat video input render`). Groups that do not exist yet are created with `groupadd -r` |
| `PROFILE_LOCAL_PACKAGES` | Our own packages, `rootfs/packages/<name>/PKGBUILD`, built in order in the build chroot (each one is installed there before the next, so they can depend on each other) |
| `PROFILE_BUILD_DEPENDS` | Arch packages the build chroot needs for them |

Optional files next to it, in `rootfs/profiles/<name>/`: `overlay/` (copied over
`/` after the core overlay and before the device's) and `configure.sh` (run in
the chroot at the end, with `THUNDER_USER` set).

## The graphical base

Every graphical session needs pieces that systemd normally provides, and
systemd 262 (Arch's) wants kernel features the 4.19 vendor kernel lacks. The
decision (build/research/gui/PLAN.md, 2026-10-11): **the kernel learns what
systemd 262 needs, and Arch's binaries run unchanged.** Nothing below has been
measured on a phone yet; the console profile is `experimental` until the plan's
phone tests pass.

**Kernel backports** (`devices/xiaomi-thunder/patches/`), three small commits:

1. `backport-statx-mount-id.patch`: `statx` with `STATX_MNT_ID` and
   `STATX_ATTR_MOUNT_ROOT` (upstream `fa2fcf4f1df1` + `80340fe3605c`). Without it,
   systemd's `chase()` fails with `EUNATCH`.
2. `backport-waitid-pidfd.patch`: `waitid(P_PIDFD)` (`3695eae5fee0`). sd-event
   reaps udev workers with it; without it the udevd loop dies on the first worker exit.
3. `backport-close-range.patch`: `close_range(2)` with `flags=0` only
   (`278a5fbaed89`). Every udev `PROGRAM` and `RUN{program}` needs it before `exec`.

Changing the kernel struct changes the MODVERSIONS CRCs, so the out-of-tree
modules are rebuilt with the kernel, as always.

**Common base:**

1. **udev**: Arch's own `systemd-udevd` 262 (`/usr/lib/systemd/systemd-udevd`) as
   the OpenRC services `udev` (supervise-daemon, sysinit) and `udev-trigger`
   (oneshot: `udevadm trigger --action=add`, then `udevadm settle`). libinput needs
   udev's database (`/run/udev/data`) to accept a device, and the `input_id`
   builtin tags the NT36672C touchscreen as `ID_INPUT_TOUCHSCREEN` by itself. The
   rule `80-net-setup-link.rules` is masked (symlink to `/dev/null` in the overlay)
   so udev never renames `usb0` or `wlan0`; whether `99-default.link` also has to
   be masked is to be confirmed on the phone. Because libudev stays Arch's, every
   package (wlroots, libinput, libgudev, NetworkManager, upower) sees a full udev.
2. **seatd** as the seat manager (OpenRC service, `seat` group), and
   `dbus-run-session` for the session bus.
3. The GPU: panfrost is in the kernel build but not autoloaded in the image yet;
   Mesa's `panfrost` driver comes from the `mesa` package.
4. A logind is still missing for Phosh and Plasma (elogind is not in Arch ARM, and
   KWin needs logind over D-Bus).

If the udevd turns up another missing API on 4.19, plan B is libudev-zero loaded
only by the session (see the plan).

**console**: phoc + squeekboard + foot, under seatd, in a `dbus-run-session`,
started at boot by the `thunder-console` service as `THUNDER_USER`. cage does not
work: it has neither layer-shell nor input-method-v2, which squeekboard needs. The
keyboard can be forced visible with `SQUEEKBOARD_DEBUG=force_show`.

**phosh**: Arch's gnome-session 51 requires `systemd --user` (its builtin
service manager was removed in 49). Alpine and postmarketOS stay on
gnome-session 48 for that reason, so this profile needs gnome-session 48
packaged against elogind, plus greetd.

**plasma-mobile**: plasma-workspace still has the classic boot without systemd
(`systemdBoot=false` in `~/.config/startkderc`). It needs elogind, the GPU, and
a measurement of whether it fits in 4 GB of RAM.
