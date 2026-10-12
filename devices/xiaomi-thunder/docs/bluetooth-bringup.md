# Bluetooth bring-up on MT6833

Bluetooth shares the connectivity subsystem (CONNSYS) with Wi-Fi: same chip,
same EMI, same `wmt_drv`. Once [the Wi-Fi work](wifi-bringup.md) was done the
expensive part was already paid for — the BT ROM patch is downloaded into EMI
at offset `0x170000` by the same sequence, and the twelve WMT/STP symbols the
BT driver needs are already exported by the loaded `wmt_drv`.

What was missing was everything *above* the radio, and it was missing in two
independent places.

**Status under Arch Linux ARM.** Every result here was measured on this kernel
with the previous userland. The Arch image boots, but the unit in
`devices/xiaomi-thunder/bluetooth/bluetooth-thunder.service` is not yet installed or enabled in the
Arch image (it also needs python3).

## Two holes, and neither is the chip

**The kernel had no Bluetooth at all.** The vendor `light_defconfig` leaves
`CONFIG_BT` and `CONFIG_RFKILL` unset, so `/sys/class/bluetooth` was not even a
directory. That is not an oversight: **Android does not use BlueZ.** Its stack
lives entirely in userspace and talks straight to `/dev/stpbt`, so the kernel
subsystem is dead weight for the shipping product.

**The chip driver was not published.** Xiaomi stripped
`drivers/misc/mediatek/connectivity/bt/` out of the source drop — checked
across all 83 089 entries of the `light-u-oss` tarball; only `common/` and
`power_throttling/` survive.

## Picking the right driver out of three

The upstream we already use for Wi-Fi
(`mt6781-devs/android_kernel_xiaomi_mt6781`, branch `lineage-23.2`) carries
three BT drivers under `bt/mt66xx/`, and they are not interchangeable:

| variant | transport | assumes | fits MT6833 |
|---|---|---|---|
| `wmt/` | STP over BTIF | `wmt_drv` (classic WMT) | **yes** |
| `btif/` | BTIF | `conninfra` | no |
| `connac2/` | BTIF | `conninfra` | no |

`conninfra` is the *next* connectivity generation (MT6893 and later) and
replaces `wmt_drv` outright. MT6833 is `SOC2_1X1` / `CONNAC_VER=1_0` and uses
classic WMT — the same `wmt_drv.ko` that already drives Wi-Fi.

The deciding signal is in the Makefiles: `btif/` and `connac2/` both set
`CONN_INFRA_SRC := .../conninfra` and add its include paths. `wmt/` mentions
conninfra nowhere and points only at
`connectivity/common/common_main/include`, which is where our `wmt_drv` lives.

`wmt/` is the exact sibling of `wmt_chrdev_wifi`: it talks to the chip through
`mtk_wcn_stp_{send,receive}_data()` on the BT queue index and powers the radio
with `mtk_wcn_wmt_func_on(WMTDRV_TYPE_BT)`. It builds as `bt_drv_6833.ko` and
exposes `/dev/stpbt`.

It is added to the connectivity drop as `bt_drv/` with a Makefile written for
this tree (the upstream one points at `vendor/mediatek/kernel_modules` paths
that do not exist here). It ships inside
`devices/xiaomi-thunder/connectivity/mtk-connectivity-gen4m-mt6833-2.tar.gz` and is grafted by
`tools/build-kernel.sh` (see [kernel.md](kernel.md#connectivity-graft)).

## Why there is a userspace bridge

The driver hands you a character device and nothing else. BlueZ wants an
`hci_dev` registered in the kernel.

MediaTek *has* a BlueZ variant — the vendor Kconfig describes
`CONFIG_MTK_COMBO_BT_HCI` as "MTK BT driver for BlueZ" — but **the code is not
published in any tree we can reach**: only the Kconfig option survives.
Searching the whole `bt/` tree for `hci_register_dev` finds it only in the
`btmtk_main.c` of the conninfra-based variants.

So the link is made in userspace, with the kernel's own `hci_vhci`:
`devices/xiaomi-thunder/bluetooth/stpbt-vhci-bridge.py` creates a virtual controller and shuttles
packets both ways. `devices/xiaomi-thunder/kernel-config` sets `CONFIG_BT_HCIVHCI=y` for it.

### The framing mismatch

The two sides do not have the same contract:

```
/dev/vhci   delivers and accepts ONE complete HCI packet per read()/write()
/dev/stpbt  delivers a BYTE STREAM — one read() can return two events glued
            together, or half of one
```

Copying byte-for-byte appears to work right up until the first burst of
traffic. The controller→host direction therefore reassembles H4 framing before
writing. The logic is unit-testable: `tamanho_do_pacote()` and
`corrige_features()` are pure functions.

## Three things that actually blocked it

### 1. A lying feature bit in the controller firmware

Symptom: `hci0` shows up in `/sys/class/bluetooth`, rfkill is created, the
whole init sequence runs — and `bluetoothd` still reports
`Number of controllers: 0`.

The kernel only calls `mgmt_index_added()` when the controller opens
*successfully*, so the question is why opening fails. `HCIDEVUP` answers it
with a number: **errno 56**. That is not a generic failure —
`bt_to_errno()` maps HCI status `0x01` ("Unknown HCI Command") to exactly
`EBADRQC`. With that, the last exchange on the bridge is enough:

```
-> 01 77 0c 00            Read_Sync_Train_Params (0x0C77)
<- 04 0f 04 01 01 77 0c   Command Status, status 0x01
```

and the cause is one response earlier:

```
-> 01 04 10 01 02         Read_Local_Extended_Features, page 2
<- ... 02 02 05 03 ...    features[0] = 0x05
```

`0x05` is `LMP_CSB_MASTER | LMP_SYNC_TRAIN`. The controller **claims**
Synchronization Train support; the kernel trusts that bit —
`hci_init4_req()` does `if (lmp_sync_train_capable(hdev))` — and issues the
command; the controller then rejects the very command it advertised. One
failing command anywhere in the init sequence tears down the whole open.

This never shows up under Android, because that stack never walks this path.

The bridge clears the bit instead of faking a reply to `0x0C77`: answering
"success" for an unimplemented command would leave the kernel believing in
sync-train parameters that do not exist. Clearing the bit tells the truth and
lets the kernel's own logic decide — it then never sends the command. In a
kernel driver this would be an `HCI_QUIRK_*`; the bridge plays the driver's
role, so it lives there.

### 2. `BT_read()` waits uninterruptibly

The first version of the bridge used two threads, each parked in a blocking
`read()`. That worked until the first restart. Measured state:

```
tid=NNNN  state=Z (zombie)
tid=NNNN  state=D (disk sleep)   wchan=BT_read
```

`BT_read()` waits with `wait_event()`, which is **not interruptible**. With the
controller silent nothing wakes the queue, so the thread is stuck inside the
kernel forever — `SIGKILL` will not move it. And while it is stuck:

- the file descriptors never close;
- `BT_release()` never runs, so the driver's `btonflag` stays `1` and every
  later `open("/dev/stpbt")` returns `EIO` (`BT already on!`);
- `/dev/vhci` stays open too, so `hci0` remains registered with nobody behind
  it.

Only a reboot clears it.

The fix is to never enter that state: the driver implements `poll()`, so the
bridge waits in `select()` and only calls `read()` when data is already there.
A self-pipe joins the same `select()` for shutdown, which removed the threads
entirely — one loop that can always exit and close its descriptors in order.

This is also why the systemd unit (`devices/xiaomi-thunder/bluetooth/bluetooth-thunder.service`) sets `KillSignal=SIGTERM`, `TimeoutStopSec=20` and `SendSIGKILL=no`: a `SIGKILL` would
recreate the problem.

### 3. The address was MediaTek's placeholder

The controller comes up as `00:00:46:67:61:01` — identical on every unit
running this project, which would stop two of the same phone from coexisting on
one paired host.

The real address lives in the `nvdata` partition at `/APCFG/APRDEB/BT_Addr`,
next to the Wi-Fi RF calibration. Same OUI as the Wi-Fi MAC, different value.

Byte order was **determined by experiment, not assumed**: sending the bytes as
stored makes the controller report a different address; sending them reversed
makes `Read_BD_ADDR` return exactly what is in nvram. HCI carries `BD_ADDR`
least-significant byte first, while nvram stores display order.

The `devices/xiaomi-thunder/bluetooth/bluetooth-thunder.service` unit extracts six bytes into `/lib/firmware/bt_addr.bin` on
first start (mounting `nvdata` **read-only**, always), and the bridge writes it
with the MediaTek vendor command `0xFC1A`, then reads it back to confirm. It
survives the `HCI_Reset` the kernel issues during init.

This is **per-unit data: read from the device at run time, never committed to
this repository.** Same rule as the Wi-Fi MAC.

## Result

```
Controller <MAC> (public)
        Manufacturer: 0x0046 (70)      <- MediaTek
        Version:      0x0a (10)        <- Bluetooth 5.1
        Powered: yes
```

A 25-second scan finds devices with names and with LE random addresses, so
both BR/EDR and LE work. Done with Wi-Fi associated throughout — the two
radios share CONNSYS and coexist.

After a cold boot, with no intervention:

```
wifi-thunder       started
bluetooth-thunder  started
bluetooth          started
modules: bt_drv_6833 connfem wlan_drv_gen4m wmt_chrdev_wifi wmt_drv
```

### Verified against a Linux host

Pairing (SSP, Just Works with `NoInputNoOutput` agents on both ends) completes
and both sides report `Paired: yes` / `Bonded: yes`.

An RFCOMM echo channel, 256 KiB per run:

| direction | connect | 1-byte round trip (min / median / max) | throughput each way | integrity |
|---|---|---|---|---|
| phone → host | 4.12 s | 6.5 / 28.4 / 84.5 ms | 49.6 KiB/s | 262144 / 262144 B |
| host → phone | 3.40 s | 7.0 / 24.0 / 52.1 ms | 41.1 KiB/s | 262144 / 262144 B |

Everything came back intact. This exercises discovery, SSP, ACL, L2CAP, RFCOMM
and bidirectional data — and the host→phone run exercises the **incoming**
connection path, which is different code from the outgoing one. The throughput
is modest because the test writes 1 KiB and waits for the echo, not because the
link is limited.

`connect` brings the ACL up and then drops it with
`ProfileUnavailable — No more profiles to connect to` when the two ends share
no profile. That is expected, not a radio failure.

## Audio: negotiates, but nothing plays

Worth being precise about, because "Bluetooth works" could suggest more than
was measured.

PipeWire's BlueZ plugin (`libspa-bluez5.so`, in Arch's `pipewire-audio`) is
required and was **not** pulled in by default. With it installed the phone advertises Audio Sink, Audio Source and Handsfree in both
roles, a host connects with `Active Profile: a2dp-sink`, the host's PipeWire
opens a sink for the phone and streams to it (`RUNNING`), and BlueZ on the
phone creates a real transport:

```
/org/bluez/hci0/dev_<MAC>/fd0
  UUID:  0000110b (Audio Sink)
  Codec: 255 (vendor specific)
  State: idle
```

`idle` is the point: nothing acquires the transport descriptor. PipeWire on the
phone **creates no audio node** for the connected peer — in `pw-dump` the only
Bluetooth object is `bluez_midi.server`. A sampler running on the device every
2 s for 80 s while the host streamed recorded: 40 samples, 0 Bluetooth audio
nodes, 0 hardware PCM playing. Restarting WirePlumber *while connected* does
not create it either.

The trail leaves Bluetooth and enters the session layer. On this device there
is no user session at all: `/run/user/` did not exist, PipeWire had to be
started by hand as root, there is no session D-Bus
(`telephony.c: failed to get session dbus connection`), no logind seat
(WirePlumber skips `monitor.bluez.seat-monitoring`), and no UPower. The earlier
test image ran its compositor as root outside any session, and PipeWire was not
started at boot. In that
hand-built stack local playback did not open a PCM either, so the failure is
not specific to Bluetooth.

So: the link delivers audio to the gate; the phone's audio session is not there
to receive it. That is session/audio work, not Bluetooth work.

## Still open

- **A2DP/HFP playback**, per above — blocked on the audio session, not on BT.
- **The negotiated codec is 255** (vendor specific) rather than SBC (0). Worth
  understanding.
- **Suspend/resume** and power saving are unexercised.
- The bridge is Python. It is not on any hot path — Bluetooth packet rates are
  trivial — but a small C or Rust daemon, or better a real in-kernel driver
  using `mtk_wcn_stp_set_bluez(1)`, would be the upstream-shaped answer.
