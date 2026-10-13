# Security policy

## Reporting a vulnerability

Email **security@vitormelo.dev.br**. Please do not open a public issue for a
vulnerability.

Include what you can of:

- the device and image (`uname -v`, `/etc/thunder/device`) or the commit you built;
- where the input comes from (Wi-Fi or Bluetooth in range, the USB cable network,
  a local unprivileged process, a downloaded factory ROM, the installer);
- steps or a reproducer, and the impact you see.

Fixes land on `main`; there are no release branches yet.

## Scope

Everything in this repository, including our kernel patches under
`devices/*/patches/` and the connectivity drivers we ship patched. In the vendor
kernel, only what this device's configuration builds and what the radio, the USB
link or an unprivileged process can reach. `.oss-scanner/threat_model.md`
describes the attack surface and what is known and accepted (for example the
rescue shell on the USB cable and the provisional password).
