# Interface profiles

A profile decides what the phone shows on its screen. It lives in
`rootfs/profiles/<name>.sh` and is chosen with `THUNDER_PROFILE` (or in the
installer). Every profile gets the same base: Arch Linux ARM, OpenRC, SSH over
the USB cable, the device's firmware and services.

| Profile | Status | What it is |
|---|---|---|
| `none` | tested | No interface. SSH and telnet over the USB cable |
| `console` | planned | Full-screen terminal with an on-screen keyboard |
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
| `PROFILE_SERVICES` | OpenRC services added to the `default` runlevel |
| `PROFILE_LOCAL_PACKAGES` | Our own packages, `rootfs/packages/<name>/PKGBUILD`, built in order in the build chroot (each one is installed there before the next, so they can depend on each other) |
| `PROFILE_BUILD_DEPENDS` | Arch packages the build chroot needs for them |

Optional files next to it, in `rootfs/profiles/<name>/`: `overlay/` (copied over
`/` after the core overlay and before the device's) and `configure.sh` (run in
the chroot at the end, with `THUNDER_USER` set).

## Why the graphical profiles are not ready

Every graphical session needs pieces that systemd normally provides, and
systemd 262 does not run on the 4.19 vendor kernels. Researched on 2026-10-10
(Alpine/postmarketOS, Artix and upstream sources); nothing below has been
measured on a phone yet.

**Common base, in order:**

1. Check on the phone whether Arch's `libsystemd` 262 loads at all on 4.19
   (`sd_bus_open_system`, `sd_pid_get_session`). The answer decides whether
   Arch's binary packages work as they are or need rebuilding against
   `libelogind`, as Alpine and Artix do.
2. **udev**: eudev 3.2.15 (maintained by Alpine, Devuan and Gentoo), packaged
   with its own rootprefix and without its libudev, plus OpenRC `udev` and
   `udev-trigger` services. libinput needs udev's `ID_INPUT_TOUCHSCREEN` to see
   the touchscreen. Then check that `libinput list-devices` (libudev 262)
   reads eudev's database.
3. **elogind** 257.16 (`-Dcgroup-controller=openrc`) with its udev rules
   (`70-uaccess`, `71-seat`, `73-seat-late`), a `logind.conf` like
   postmarketOS's and `pam_elogind`. Needed by Phosh and Plasma; KWin only
   talks to logind over D-Bus.
4. The GPU: panfrost is backported to the thunder kernel but not loaded in
   the image yet.

**console**: phoc + squeekboard + foot, under seatd, in a `dbus-run-session`.
cage does not work: it has neither layer-shell nor input-method-v2, which
squeekboard needs. The keyboard can be forced visible with
`SQUEEKBOARD_DEBUG=force_show`.

**phosh**: Arch's gnome-session 51 requires `systemd --user` (its builtin
service manager was removed in 49). Alpine and postmarketOS stay on
gnome-session 48 for that reason, so this profile needs gnome-session 48
packaged against elogind, plus greetd.

**plasma-mobile**: plasma-workspace still has the classic boot without systemd
(`systemdBoot=false` in `~/.config/startkderc`). It needs elogind, the GPU, and
a measurement of whether it fits in 4 GB of RAM.
