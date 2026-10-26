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
plus `setsid` (util-linux). The app talks to the phone itself
(`lib/fastboot.dart`): fastboot, adb and ssh must be on the PATH.

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

### Windows

The Windows build (`flutter build windows`, only on a Windows host; CI does it in
`.github/workflows/installer-windows.yml`) has no bash, so for now it only
**flashes** images that were built elsewhere:

- next to `thunder-os-installer.exe` goes `catalogue.json`, the output of
  `tools/installer-info.sh` (CI adds it);
- the images go in `build/<device>/out/` next to it (`boot.img`,
  `userdata.img`, as `tools/install.sh` leaves them on Linux);
- `fastboot` and `adb` come from the PATH or, if missing, the app downloads
  Google's Platform-Tools (pinned version, checked by SHA-256) into
  `%LOCALAPPDATA%\ThunderOS`;
- in fastboot, Windows needs a USB driver (Google USB Driver, "Android
  Bootloader Interface"); the device screen explains it. SSH to Thunder OS uses
  the `ssh.exe` that ships with Windows 10 and 11.

Building the image on Windows is not done yet.

## Layout

```
lib/backend.dart    calls tools/*.sh and parses their JSON and ::step markers;
                    detects the phone and reboots it to fastboot
lib/fastboot.dart   fastboot/adb output parsing and the flash done by the app
lib/platform_tools.dart  fastboot/adb from the PATH or Google's Platform-Tools
lib/config.dart     the user's choices, validation, environment for install.sh
lib/wizard.dart     the step list and navigation
lib/steps/          one screen per step
test/               unit tests (flutter test)
```
