# Interface profiles

A profile decides what the phone shows on its screen. It lives in
`rootfs/profiles/<name>.sh` and is chosen with `THUNDER_PROFILE` (or in the
installer). Every profile gets the same base: Arch Linux ARM, systemd, SSH over
the USB cable, the device's firmware and services.

| Profile | Status | What it is |
|---|---|---|
| `none` | tested | No interface. SSH and telnet over the USB cable |
| `console` | experimental | Full-screen terminal with an on-screen keyboard |
| `phosh` | experimental | GNOME's phone shell |
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
| `PROFILE_SERVICES` | systemd unit names with suffix (`foo.service`, `bar.socket`) enabled with `systemctl enable`; `{user}` is replaced by `THUNDER_USER`. The old `name:runlevel` form is gone |
| `PROFILE_GROUPS` | Supplementary groups of `THUNDER_USER` (`video input render`). Groups that do not exist yet are created with `groupadd -r` |
| `PROFILE_LOCAL_PACKAGES` | Our own packages, `rootfs/packages/<name>/PKGBUILD`, built in order in the build chroot (each one is installed there before the next, so they can depend on each other). Device packages (`DEVICE_LOCAL_PACKAGES`) come first |
| `PROFILE_BUILD_DEPENDS` | Arch packages the build chroot needs for them. Missing `depends`/`makedepends`/`checkdepends` of each recipe are also installed automatically |

Optional files next to it, in `rootfs/profiles/<name>/`: `overlay/` (copied over
`/` after the core overlay and before the device's) and `configure.sh` (run in
the chroot at the end, with `THUNDER_USER` set).

## The graphical base

Every graphical session needs pieces that systemd provides, and Arch's systemd
262 runs as PID 1 on the 4.19 vendor kernel once the kernel learns what it
needs. The decision (build/research/gui/PLAN.md, 2026-10-11): **the kernel
learns what systemd 262 needs, and Arch's binaries run unchanged.** The systemd
boot is untested on a phone; the console profile is `experimental` until the
plan's phone tests pass.

**Kernel backports** (`devices/xiaomi-thunder/patches/`), small commits:

1. `backport-statx-mount-id.patch`: `statx` with `STATX_MNT_ID` and
   `STATX_ATTR_MOUNT_ROOT` (upstream `fa2fcf4f1df1` + `80340fe3605c`). Without it,
   systemd's `chase()` fails with `EUNATCH`.
2. `backport-waitid-pidfd.patch`: `waitid(P_PIDFD)` (`3695eae5fee0`). sd-event
   reaps udev workers with it; without it the udevd loop dies on the first worker exit.
3. `backport-close-range.patch`: `close_range(2)` with `flags=0` only
   (`278a5fbaed89`). Every udev `PROGRAM` and `RUN{program}` needs it before `exec`.
4. `backport-faccessat2.patch`: `faccessat2(2)`, which systemd and recent glibc
   call for access checks.

Changing the kernel struct changes the MODVERSIONS CRCs, so the out-of-tree
modules are rebuilt with the kernel, as always.

**Common base**, all from Arch's own systemd and packages:

1. **udev**: `systemd-udevd` 262 with the stock rules. libinput needs udev's
   database (`/run/udev/data`) to accept a device, and the `input_id` builtin
   tags the NT36672C touchscreen as `ID_INPUT_TOUCHSCREEN` by itself. The rule
   `80-net-setup-link.rules` is masked (symlink to `/dev/null` in the overlay)
   so udev never renames `rndis0` or `wlan0`; whether `99-default.link` also has
   to be masked is to be confirmed on the phone. Because libudev stays Arch's,
   every package (wlroots, libinput, libgudev, NetworkManager, upower) sees a
   full udev.
2. **logind**, **dbus-broker** and **`systemd --user`** come with the same
   package. The graphical session runs as a logind session (seat0, tty1).
   seatd and `dbus-run-session` are no longer used.
3. The GPU: panfrost is a kernel module that udev loads from the device tree;
   Mesa's `panfrost` driver comes from the `mesa` package.

If the udevd turns up another missing API on 4.19, plan B is libudev-zero loaded
only by the session (see the plan).

**console**: phoc + squeekboard + foot, as a logind session on tty1, started at
boot by `thunder-console@USER.service` (in `rootfs/profiles/console/overlay`)
as `THUNDER_USER`. Local tweaks such as `WLR_RENDERER` go through
`systemctl edit thunder-console@USER`. cage does not work: it has neither
layer-shell nor input-method-v2, which squeekboard needs. The keyboard can be
forced visible with `SQUEEKBOARD_DEBUG=force_show`.
The session turns the panel off after 60 s idle (`swayidle` + `wlopm`, through
phoc's output power management) and back on at the first touch or key;
`THUNDER_IDLE_TIMEOUT` in `systemctl edit thunder-console@USER` changes the delay. A
logind drop-in makes a short press of the power key only wake the screen; holding it
powers off (logind's default powered the phone off on the first press).
There is no lock screen: waking the panel shows the terminal of the logged-in user.
`swaylock` takes no input from squeekboard, so locking would lock the owner out too;
a touch-capable locker is due before the profile is promoted to tested.

**phosh**: Arch's Phosh with gnome-session 51 on `systemd --user`. At boot,
`thunder-phosh@USER.service` (in `rootfs/profiles/phosh/overlay`) runs it as
`THUNDER_USER`: it is upstream's development `phosh.service` as a per-user template, a
PAM `login` session on tty1 running `phosh-session` under `capsh --noamb`. There is no
greeter: without `GDMSESSION=phosh`, Phosh starts locked, and the user's password
unlocks it (PAM `phosh`, from the package). The on-screen keyboard is squeekboard
(`stevia` conflicts with it). The display scale is phoc's own, computed from the
panel's physical size (3 on xiaomi-thunder, 360x803 logical); a device whose panel
reports a wrong size ships `/etc/phosh/phoc.ini` in its overlay. Notch and rounded
corners come from gmobile's panel data, picked by the first DT compatible (the clock
moves off a notch; `sm.puri.phosh shell-layout` must stay `device`); a device whose file
is not upstream yet adds it as a patch to `rootfs/packages/libgmobile-thunder`, which
replaces Arch's libgmobile until a release carries it. Modem, calls,
Bluetooth, audio and vibration come later. An image is built for one profile, so
going back to the console means installing a console image; on a system where both
profiles were installed by hand,
`systemctl disable --now thunder-phosh@USER && systemctl enable --now thunder-console@USER`
switches between them.

**plasma-mobile**: plasma-workspace still has the classic boot without systemd
(`systemdBoot=false` in `~/.config/startkderc`). It needs the GPU, validation under
`systemd --user`, and a measurement of whether it fits in 4 GB of RAM.
