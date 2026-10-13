# Bluetooth bring-up on MT6833

Bluetooth shares the connectivity subsystem (CONNSYS) with Wi-Fi: same chip,
same EMI, same `wmt_drv`. Once [the Wi-Fi work](wifi-bringup.md) was done the
expensive part was already paid for — the BT ROM patch is downloaded into EMI
at offset `0x170000` by the same sequence, and the twelve WMT/STP symbols the
BT driver needs are already exported by the loaded `wmt_drv`.

What was missing was everything *above* the radio, and it was missing in two
independent places.

**Status under Arch Linux ARM (2026-10-12).** Bluetooth is in the image and comes up at
boot: the `btmtk_stp` kernel driver registers `hci0` with BlueZ, `thunder-wmt-bt`
programs the factory address, and `bluetooth.service` (bluez) runs on top. The
earlier userspace bridge (`stpbt-vhci-bridge.py` over `hci_vhci`) is gone; what it
taught is kept below, because the driver repeats it.

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

## The HCI driver: `btmtk_stp`

The vendor driver hands you a character device and nothing else; BlueZ wants an
`hci_dev` registered in the kernel. MediaTek *has* a BlueZ variant — the vendor Kconfig
describes `CONFIG_MTK_COMBO_BT_HCI` as "MTK BT driver for BlueZ" — but **the code is not
published in any tree we can reach**.

`wmt_drv` still carries the hook it used: an STP **BlueZ mode**
(`mtk_wcn_stp_set_bluez(1)`). In it, `stp_core.c` hands every BT packet straight to the
callback registered with `mtk_wcn_stp_register_if_rx()` instead of queueing it for
`/dev/stpbt`. `btmtk_stp.ko` (patch `add-btmtk-stp-hci-driver`, built next to `bt_drv` in
the connectivity graft) is a regular `hci_dev` on top of that:

- **open/close:** `mtk_wcn_wmt_func_on/off(WMTDRV_TYPE_BT)` (the BT ROM patch download
  happens there) and the BlueZ mode on/off.
- **rx:** the callback runs in the `btif_rxd` kthread. Both directions carry H4 framing,
  and STP may split a packet. The kernel's own `h4_recv_buf()` reassembles it, under a
  spinlock shared with close.
- **tx:** the H4 type byte goes in front, and the packet goes into the driver's own
  queue; a work item hands it to `mtk_wcn_stp_send_data()` in order. STP returns 0
  while its queue is full, and the work waits and tries again. Dropping the packet
  instead would cost a controller credit for good: the core counts an ACL packet once
  `send()` returns, whatever it returned.
- **CONNSYS whole-chip reset (WMT message):** `hci_reset_dev()`, which closes and reopens
  the device.
- **Lifetime:** `wmt_drv` calls the rx and reset callbacks through plain pointers, with
  no synchronisation against unregistering them. So they are registered on the first
  open, after `hci_register_dev()` succeeded, and never taken back; the module has no
  `module_exit`: a late call never runs freed code.

It replaces `bt_drv_6833` (`/dev/stpbt`): both drive the BT function of the same chip,
so only one of them may be loaded. `CONFIG_BT_HCIVHCI` is off again.

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

The fix clears the bit instead of faking a reply to `0x0C77`: answering "success"
for an unimplemented command would leave the kernel believing in sync-train
parameters that do not exist. Clearing the bit tells the truth and lets the kernel's
own logic decide — it then never sends the command. 4.19 has no quirk for it, so
`btmtk_stp` edits the Read_Local_Extended_Features page 2 reply on its way in (the
kernel log says `controller claims sync train, cleared` once per power-on).

### 2. `BT_read()` waits uninterruptibly (bridge only)

The bridge sat on `/dev/stpbt`, whose `BT_read()` waits with `wait_event()`: not
interruptible. A bridge thread blocked there could not be killed, kept `btonflag` set
(every later `open()` returned `EIO`) and left `hci0` registered with nobody behind it,
until a reboot. The bridge had to wait in `select()` and never be sent `SIGKILL`.
`btmtk_stp` does not use the character device at all, so this is history.

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

`thunder-wmt bt-on` (package `thunder-mediatek-wmt`, unit `thunder-wmt-bt`) reads the
six bytes with `debugfs`, without mounting `nvdata`, as it does for the Wi-Fi NVRAM. It
sets them through BlueZ's mgmt interface (Set Public Address); the driver's
`set_bdaddr` programs them. Two details took measuring:

- **The write needs a reset first.** Sent right after the BT power-on, `0xFC1A` does
  not stick; after an `HCI_Reset` it does, and then survives the resets of the HCI
  init. `set_bdaddr` therefore sends `HCI_Reset`, then `0xFC1A` with its status
  checked, then `Read_BD_ADDR`, and fails if the address differs.
- **Every BT power-on loses it.** `func_on` reloads the firmware, and so does a CONNSYS
  reset. With `HCI_QUIRK_NON_PERSISTENT_SETUP` the core runs the driver's `setup()` on
  every open, and `setup()` programs the configured address again. Two power cycles
  in a row keep it. If that write fails, the power-on fails, so the controller never
  comes up with the placeholder; the next power-on tries again.
- **Except on the configuration power-on.** The power-on that follows Set Public
  Address (`HCI_CONFIG`) calls `set_bdaddr`, which never fails it: in 4.19 a failed
  open there leaves the index out of mgmt until a reboot. On a failed write the
  controller comes up with the placeholder, `bt-on` reads it back, and power-cycles it
  (up to twice) so `setup()` writes again.
- **Not inside the auto-off window.** After its first setup the core leaves the
  controller up for 2 s (`HCI_AUTO_OFF`), and mgmt already calls it powered off. A Set
  Public Address in that window marks `HCI_CONFIG`, but 4.19's `hci_power_on()` sees
  `HCI_UP` and only powers on through mgmt without reopening. The configuration never
  runs, and the index stays out of mgmt until a reboot. `HCIGETDEVINFO` hides `HCI_UP`
  during that window, so `bt-on` waits for `HCI_RUNNING` to drop instead. It finishes
  only when Read Controller Information returns the factory address. It runs before
  `bluetooth.service`, because Set Public Address needs the controller off.

This is **per-unit data: read from the device at run time, never committed to
this repository.** Same rule as the Wi-Fi MAC.

## Result

With the `btmtk_stp` driver, cold boot, no intervention (2026-10-12, kernel #1 Mon Oct 12
18:03:12):

```
thunder-wmt-bt: hci0: factory address set and read back        (10 s after boot)
Controller <factory address> (public)
        Manufacturer: 0x0046 (70)      <- MediaTek
        Version:      0x0a (10)        <- Bluetooth 5.1
        Powered: yes
```

`bluetooth.service` starts only after `thunder-wmt-bt`. A 10-second scan finds 19
devices, with Wi-Fi associated throughout: the two radios share CONNSYS and coexist.
Three `power off`/`power on` cycles keep the factory address. No unit fails.

### Verified against a Linux host (with the earlier bridge)

Not repeated yet with `btmtk_stp`; the radio, ACL and pairing code paths are the same
controller and the same BlueZ, only the transport changed.

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

- **Pairing and RFCOMM with `btmtk_stp`**: measured only with the bridge (above).
- **A2DP/HFP playback.** The blocker above, no audio session, is gone: the Phosh image
  has a logind seat, a user PipeWire and a working card (docs/audio.md). Not re-tested.
- **The negotiated codec is 255** (vendor specific) rather than SBC (0). Worth
  understanding.
- **Suspend/resume** and power saving are unexercised.
- **A failure of the configuration power-on itself** (the WMT function on, or the HCI
  init, during the `HCI_CONFIG` open) still leaves the index out of mgmt until a
  reboot: 4.19 never clears `HCI_CONFIG` on that path and the module cannot be
  reloaded. Upstream has the same path. `bt-on` then fails and Bluetooth stays off.
- **A CONNSYS whole-chip reset** has no safe trigger; the reset path
  (`hci_reset_dev()`, then `setup()` reprograms the address) is untested.
