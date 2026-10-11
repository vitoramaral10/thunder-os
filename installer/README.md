# Thunder OS installer

A desktop app (Flutter, Linux) that installs Thunder OS on a phone over the USB
cable, in the steps of a desktop Linux installer:

1. **Device**: detects the phone in fastboot, in the stock Android (adb) or
   already running Thunder OS (SSH), and matches it to a folder in `devices/`.
2. **Factory ROM**: you attach the vendor's fastboot package; the proprietary
   firmware is copied out of it (`tools/extract-firmware.sh`).
3. **Interface**: one of the profiles in `rootfs/profiles/`, plus extra Arch
   packages.
4. **Users**: your account (with sudo), the root password, hostname and an
   optional SSH key.
5. **Region**: locale and timezone, defaulting to the host's.
6. **Summary**: image size and whether to flash at the end. Flashing asks for
   confirmation, since it erases the root partition.
7. **Installation**: runs `tools/install.sh`, with each step's progress and the
   live log, which is also saved to `build/<device>/install-<date>.log`.

The app does no building itself: it calls the scripts in `tools/` and reads
their output, so everything it does can also be done by hand
([docs/building.md](../docs/building.md)). Passwords go to the build through
the process environment and are never written to a file on the host.

## Running

The host needs everything in [docs/building.md](../docs/building.md#host-requirements),
plus Python 3 and `setsid` (util-linux).

```sh
cd installer
flutter run -d linux
```

or build once and start the binary from anywhere inside the repository:

```sh
flutter build linux --release
build/linux/x64/release/bundle/thunder-os-installer
```

It finds the repository by walking up from the current directory or the
binary; set `THUNDER_OS_REPO` to run it from elsewhere.

## Layout

```
lib/backend.dart    calls tools/*.sh and parses their JSON and ::step markers
lib/config.dart     the user's choices, validation, environment for install.sh
lib/wizard.dart     the step list and navigation
lib/steps/          one screen per step
test/               unit tests (flutter test)
```
