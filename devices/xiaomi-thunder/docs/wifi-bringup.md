# Wi-Fi bring-up on MT6833 (CONNAC 1.0 / `gen4m`)

This is the part of the port worth reading. There is no mainline support for
MT6833 connectivity, so everything here is downstream: MediaTek's `wmt_drv` /
`wlan_drv_gen4m` stack on a 4.19 vendor kernel, plus a replacement for the
Android userspace daemon that normally drives it.

**Status under Arch Linux ARM.** The bring-up runs at boot as two systemd units
from the `thunder-mediatek-wmt` package (see
[Bringing it up at boot](#bringing-it-up-at-boot)). The package is written and
verified on the PC only; the phone tests are listed in
[Phone validation plan](#phone-validation-plan). The results below were
measured with the previous userland.

Four separate problems had to be solved, and three of them produced the *same*
symptom. That is the useful lesson: the failure the log reports is often three
layers above the cause.

## The stack, briefly

The connectivity subsystem (CONNSYS) is an on-SoC block with its **own CPU**.
It does not run from flash. At every power-on the AP must:

1. power the rails, enable clocks, release bus sleep protection
2. copy three ROM patches into a reserved EMI (DRAM) region — one for the
   CONNSYS MCU, one for Wi-Fi, one for Bluetooth
3. grant the CONNSYS bus master permission to *read* that EMI region
4. release the CONNSYS CPU from reset
5. wait for it to write `0x1D1E` to `mcu_base + 0x600`, meaning it reached
   `cos_idle_loop`
6. only then talk to it over BTIF (a UART-like serial link fed by AP_DMA)

Steps 2 and 3 are the ones that bite. On Android, step 2 is driven from
userspace by a launcher daemon that answers ioctls on `/dev/stpwmt`. There is
no Android here, so this project ships `thunder-wmt`
([`rootfs/packages/thunder-mediatek-wmt/`](../../../rootfs/packages/thunder-mediatek-wmt/)),
which does the jobs of Android's `wmt_loader`, `wmt_launcher` and
`wlan_assistant`.

## Symptom 1 — `hw_check fail:-14`, and the red herring

The first visible failure:

```
[STP-BTIF][E] mtk_wcn_consys_stp_btif_tx(198): stp btif write fail,len(11),written(0)
[WMT-CORE][E] wmt_core_hw_check(1057): get hwcode (chip id) fail (-3)
[WMT-CORE][E] wmt_core_stp_init(909): hw_check fail:-14
```

Reading the AP_DMA TX block through `/dev/mem` (base `0x10217d80`) at idle:

```
+0x08 EN            = 0x00000000   DMA disabled
+0x14 FLUSH         = 0x00000001   flush pending, never completes
+0x1c VFF_ADDR      = 0xfec00000   so hal_btif_dma_hw_init() did run
+0x2c WPT           = 0x0000001a
+0x30 RPT           = 0x0000001a   WPT == RPT: the vFIFO drained
+0x38 INT_BUF_SIZE  = 0x00000007   7 bytes stuck in the internal buffer
+0x3c VALID_SIZE    = 0x00000000
```

and the BTIF block (`0x1100c000`):

```
+0x14 LSR       = 0x00000000   THRE=0 and TEMT=0: transmitter not empty, not shifting
+0x4c DMA_EN    = 0x00000007   RX|TX|AUTORST, both channels enabled
+0x6c HANDSHAKE = 0x00000003   hardware flow control engaged
```

Read on its own, `TX_DMA_FLUSH` stuck at 1 looks like the bug. It is not. The
DMA drained the vFIFO fine; the last 7 bytes sit in the internal buffer because
**the sink is not consuming**. BTIF is in hardware flow control waiting for a
CONNSYS that never came up. Everything here is downstream of a radio that never
booted.

A second red herring: `consys_soc_chipid_get()` returns `PLATFORM_SOC_CHIP`, a
**compile-time constant**. Seeing `chipid(26675)` (`0x6833`) in the log proves
nothing about the radio being alive.

## Symptom 2 — `CONSYS-HW-PWR-ON, failed!(-7)`

Raising the WMT log level shows the real failure:

```sh
printf DB9DB9 > /proc/driver/wmt_dbg      # unlock
printf "9 4"  > /proc/driver/wmt_dbg      # WMT_LOG_LOUD
```

```
polling_consys_chipid: retry(9) consys version id(0x100a0000)
polling_consys_chipid: consys HW version id(0x8a00)
consys_polling_goto_idle: 0x18002600(0x0)          <- expects 0x1D1E
consys_polling_goto_idle: 0x18007104(0xf0021a44)   <- CONNSYS MCU program counter
consys_polling_goto_idle: 0x18007104(0xf0047d14)
consys_polling_goto_idle: 0x18007104(0xf006e224)
consys_polling_goto_idle(587): can not go into idle!
mtk_wcn_consys_hw_pwr_on(908): CONSYS-HW-PWR-ON, failed!(-7)
```

`-7` is `-WMT_ERRCODE_POLL_NOT_GOTO_IDLE`.

The chip **answers** — version registers read real values on the first retry,
so power, clocks and the EMI window are all correct. But look at the program
counter: it advances by a near-uniform `~0x26000` every 20 ms poll. That is not
code executing. That is a CPU walking forward through empty memory. **The PC
trace is the single most useful diagnostic in this whole exercise** — it
distinguishes "the radio is dead" from "the radio is alive and executing
garbage", and those have completely different causes.

## Cause A — the ROM patch type is an enum, not an index

```
wmt_ctrl_get_rom_patch_info(682): NULL patchinfo pointer
mtk_wcn_soc_rom_patch_dwn: There is no need to download (3) type patch!
mtk_wcn_soc_rom_patch_dwn: There is no need to download (4) type patch!
```

`mtk_wcn_soc_rom_patch_dwn()` loops `type` from 0 to 4 and asks userspace for
each one. The `type` field of `struct wmt_rom_patch_info` is
`ENUM_WMTDRV_TYPE` (`wmt_exp.h`):

```
0 = BT    1 = FM    2 = GPS    3 = WIFI    4 = WMT (the MCU patch)
```

Registering patches as 0, 1, 2 — as if the field were a sequence number —
leaves types **3 and 4** unset, which are exactly the two that matter.

You do not have to guess the mapping: each `.bin` declares its own type. The
48-byte header is `struct wmt_rom_patch` (`wmt_core.h`), and its `u32` fields
are stored **big-endian**:

| offset | field | mcu | wifi | bt |
|---|---|---|---|---|
| 0–15 | `ucDateTime` | `20231215181632` | … | … |
| 16–19 | `ucPLat` | `ALPS` | `ALPS` | `ALPS` |
| 20–21 | `u2HwVer` | `8a 00` | `8a 00` | `8a 00` |
| 22–23 | `u2SwVer` | `8a 00` | `8a 00` | `8a 00` |
| 24–27 | `u4PatchAddr` | `11 00 00 f0` | `11 00 2a f0` | `11 00 17 f0` |
| 28–31 | `u4PatchType` | `00 00 00 04` | `00 00 00 03` | `00 00 00 00` |

Read big-endian, `u4PatchType` gives 4, 3 and 0 — WMT, WIFI, BT.

A related trap: `wmt_lib_set_rom_patch_info()` only accepts **one registration
per type** ("Allow info of a type to be set only once"). Register a type wrongly
and you cannot fix it without reloading `wmt_drv`. And `rmmod wmt_drv` wedges
the module in `Unloading` state, so in practice that means a reboot per attempt.

## Cause B — the low byte of the patch address is a flag

With the types fixed, the patches still landed 17 bytes off:

```
[Rom Patch]Name=soc2_2_ram_mcu_1_1_hdr.bin,EmiOffset=0x11
```

The driver builds the offset as
`(addRess[2] << 16) | (addRess[1] << 8) | addRess[0]`, so passing the four
header bytes verbatim yields `0x11`, `0x170011`, `0x2a0011`. Plausible-looking,
and wrong. Seventeen bytes of misalignment is enough that the MCU never boots —
the PC trace kept its uniform-step signature.

The authoritative values are hardcoded in the driver itself,
`consys_emi_entry_address()` in `mt6833.c`:

```c
/* EMI entry address (define by CONNSYS MCU SE)
 * 0x1800_2504[31:0] 0xF017_0000      <- BT
 * 0x1800_2508[31:0] 0xF02A_0000      <- WIFI
 */
```

So `u4PatchAddr` read little-endian is `0xF0000011` / `0xF0170011` /
`0xF02A0011`: the high `0xF0` is the EMI base as CONNSYS sees it, the low `0x11`
is a flag, and **neither belongs in the offset**. Correct offsets are
`0x000000` (mcu), `0x170000` (bt), `0x2A0000` (wifi).

> When an address pulled out of a vendor blob looks misaligned, look for the
> same constant hardcoded somewhere in the driver before trusting your parse.

## Cause C — the EMI MPU denies the region to CONNSYS

Correct types, correct offsets, and still `-7`. The cause was in the log all
along, drowned out by unrelated noise:

```
emimpu_violation_irq: emi0, offset(0x1f0), value(0x405f009c)
emimpu_violation_irq: violation at emi0
```

**265,952 of those lines**, all inside the four-second power-on window. The
`0x5f00` inside `0x405f009c` is the reserved EMI base (`0xbe000000`) shifted
right by 17 — the violations are on exactly the region the patches live in.

So: the AP writes the patches successfully (the AP has access), and the CONNSYS
MCU is **blocked by the memory protection unit** when it tries to fetch them.
Every denied fetch returns garbage, and the CPU walks forward. That is where the
uniform PC trace came from.

Granting the region is the job of `consys_emi_mpu_set_region_protection()` in
`mt6833.c`, which is already registered in the ops table and already called
during probe:

```c
mtk_emimpu_init_region(&region, REGION_CONN);          /* 27 */
mtk_emimpu_set_addr(&region, gConEmiPhyBase, gConEmiPhyBase + gConEmiSize - 1);
mtk_emimpu_set_apc(&region, DOMAIN_AP,   MTK_EMIMPU_NO_PROTECTION);  /* 0 */
mtk_emimpu_set_apc(&region, DOMAIN_CONN, MTK_EMIMPU_NO_PROTECTION);  /* 2 */
mtk_emimpu_set_protection(&region);
```

It had been compiled out with `#define CONSYS_ENABLE_EMI_MPU 0` (the drop in
`devices/xiaomi-thunder/connectivity/` now ships it as `1`), on the reasoning that `mtk_emimpu_*` comes from `emimpu.c`, built by `CONFIG_MTK_EMI`,
which is off.

That reasoning was wrong, and this is the most transferable lesson here. There
are **two implementations of the same API** behind different Kconfig symbols
(`drivers/memory/mediatek/Makefile`):

```make
obj-$(CONFIG_MEDIATEK_EMI)  += emimpu_old.o
obj-$(CONFIG_MTK_EMI)       += emimpu.o
```

Both include the same `<memory/mediatek/emi.h>` and export the same symbols.
`CONFIG_MTK_EMI` is indeed off — but `CONFIG_MEDIATEK_EMI=y` comes from the
**base defconfig** and never appears in `devices/xiaomi-thunder/kernel-config`. On the
running kernel:

```
$ zcat /proc/config.gz | grep EMI
CONFIG_MEDIATEK_EMI=y
# CONFIG_MTK_EMI is not set

$ grep __ksymtab_mtk_emimpu /proc/kallsyms | wc -l
11
```

The same driver that logs the violations is the one exporting the API to fix
them.

> **Check the effective config (`/proc/config.gz`) and the effective symbol
> table (`/proc/kallsyms`, including `__ksymtab_*`) before concluding that a BSP
> API does not exist.** `devices/xiaomi-thunder/kernel-config` is not the
> effective config.

## Result

Re-enabling the MPU call, measured before and after:

| indicator | before | after |
|---|---|---|
| `setting MPU for EMI share memory` | absent | present |
| `emimpu_violation_irq` | **265,952** | **0** |
| `can not go into idle!` | yes | no |
| `CONSYS-HW-PWR-ON` | `failed!(-7)` | **`finish(0)`** |
| network interfaces | none | `wlan0 wlan1 ap0 p2p0` |

Scanning works on both bands, WPA2 association and DHCP work, and a 10 MB
download completes intact — which also shows that the
`opfunc_wlan_probe: not implemented yet hifType: 0x2` still in the log does
**not** block the data path. Known noise.

## Still open

- **Whether the factory NVRAM takes on the first probe.** It is now pushed by
  default, before a single power-on, and the service logs whether it took. The
  phone test (T3) decides whether the default stays on. See
  [the section below](#the-factory-nvram-mac-and-rf-calibration).
- **Whether a Wi-Fi restart (stop/start of the radio) still associates.** The
  old userland left the supplicant unable to associate after a power cycle.
  Restart of the radio is unverified for association; see
  [Phone validation plan](#phone-validation-plan).
- `opfunc_wlan_probe: not implemented yet hifType: 0x2` — harmless so far.

## The factory NVRAM (MAC and RF calibration)

The driver does not read a file: `nvram_read()` is under `#if 0`. Its
`g_aucNvram` buffer is filled **from userspace**, by one `write()` to
`/dev/wmtWifi` of `WR-BUF:NVRAM` followed by the raw NVRAM
(`wmt_cdev_wifi.c`). On Android that is `wlan_assistant`, started by
`init.wlan_drv.rc` right after the two `insmod`s and before Wi-Fi is switched
on. The handler is registered when `wlan_drv_gen4m` loads (`initWlan()`), the
buffer survives power cycles, and the next probe's `glLoadNvram()` takes the
MAC (offset 4), the country code and the band flags from it, while
`wlanLoadManufactureData()` sends the whole blob, TX power tables included, to
the firmware.

The source is the `nvdata` partition (ext4), file `/APCFG/APRDEB/WIFI`, 2050
bytes. `thunder-wmt` finds the partition by `PARTNAME=nvdata` in
`/sys/class/block/*/uevent` (no wait for the udev `by-partlabel` link) and reads
the file with `debugfs -R "cat ..."`, which opens the filesystem read-only and
never mounts it: no journal replay, no copy of the calibration left on disk.
`debugfs` is never run with `-w`.

### What the service does

`thunder-wmt-wifi.service` follows Android's order: load `wmt_chrdev_wifi`
and `wlan_drv_gen4m`, push the NVRAM, power the radio on **once**. Then it
compares the interface's permanent address (`ETHTOOL_GPERMADDR`) with the MAC
inside the NVRAM and logs exactly one line starting with `NVRAM verdict:`:

- `applied` — factory MAC and RF calibration are in use;
- `not applied (pushed, but ...)` — the driver generated its fallback
  `00:08:22:…` address; Wi-Fi works, without the factory calibration;
- `not applied (not pushed: ...)` or `not applied (disabled: ...)` — the
  partition or the file could not be read, or `WMT_WIFI_NVRAM` is empty;
- `unknown (...)` — the permanent address could not be read.

Addresses are never printed. The gen4m driver registers `wlan0` on the first
probe and keeps it across power cycles: `echo 0` leaves the same interface
(same ifindex) in `dormant`, it does not remove it (measured 2026-10-11). So
"the interface exists" does not mean "the radio is on", and the service does
not use it as such: it pushes the NVRAM every time (the driver keeps it for
the next probe) and always logs the verdict, which reads the interface's
current permanent address. `wifi-off` returns when the driver has powered the
radio down; it does not wait for the interface to go.

The device default lives in `WMT_WIFI_NVRAM` in `device.sh` and is **enabled for
now**. Test T3 decides: if the first probe does not apply the NVRAM, the
default goes back to empty (the status quo), not to "keep". To turn it off on a
running phone, empty `WMT_WIFI_NVRAM` in `/etc/thunder/wmt.conf`.

### History: the double power-on

This section describes the previous userland, not the current behaviour. There
the push was measured to take only on the **second** probe (power on, off,
push, on), and that extra power cycle broke association: the scan still saw
the AP, but the supplicant stayed in `scanning` and `nmcli` reported `The
Wi-Fi network could not be found`. Elimination showed the power cycle, not the
push, caused it. The double power-on is gone; the first-probe question is
answered by the verdict line on every boot. The source shows no reason for a
second probe to be needed (`glLoadNvram()` only requires
`g_NvramFsm == NVRAM_STATE_READY`, which the push sets), so if the warning
shows up the next step is a kprobe on `wlanNvramBufHandler` and `glLoadNvram`
(`KPROBE_EVENTS=y`; mount tracefs).

Consequence for the units: a power cycle of the radio left the supplicant unable
to associate in that setup, so **restart of the radio is unverified for
association**. Nothing propagates a restart from `thunder-wmt-connsys` to
`thunder-wmt-wifi`, and a restart is not advertised as a recovery path until
the phone test shows `wpa_state=COMPLETED` and an IP after it.

### Worth knowing

- **A fixed MAC is not the reason to do this.** NetworkManager's
  `cloned-mac-address` defaults to `stable` mode here, which derives a
  deterministic address — so the MAC is already fixed across reboots, and the
  DHCP lease is already stable. The real gain from the NVRAM is the **RF
  calibration**, which rides in the same 2050 bytes.
- **The regulatory domain is a red herring.** `/proc/net/wlan/country` reads
  `12336`, which is ASCII `"00"` — the world domain — **with or without** the
  NVRAM push: `glLoadNvram()` forces `eRegChannelListMap = 0`.
- **Do not set `wifi.cloned-mac-address=permanent`.** Activation then fails
  with `The base network connection was interrupted`.

## Bringing it up at boot

CONNSYS has no firmware of its own, so this whole sequence runs on every boot.
The `thunder-mediatek-wmt` package ships one program, `thunder-wmt`, and two
units; what is device-specific (modules, ROM patch names, HIF, where the NVRAM
lives) comes from the `WMT_*` variables in `device.sh`, written to
`/etc/thunder/wmt.conf` at image build. The firmware the sequence needs
(`WMT_SOC.cfg`, the three `*_1_1_hdr.bin` ROM patches and
`WIFI_RAM_CODE_soc2_2_1_1.bin`) is in the device's `FIRMWARE_REQUIRED`, so an
image without it does not build. The driver loads `WMT_SOC.cfg` by its fixed
name on SoC chips (`wmt_conf.c:605-610`); nothing has to pass it.

| unit | type | what "active" means | stop |
|---|---|---|---|
| `thunder-wmt-connsys.service` | `notify`, resident | driver initialised, ROM patches registered, HIF queued | closes `/dev/stpwmt` |
| `thunder-wmt-wifi.service` | `oneshot`, `RemainAfterExit` | radio on, `wlan0` exists | `write("0")` to `/dev/wmtWifi` |

`thunder-wmt-connsys` is Android's `wmt_loader` (`/dev/wmtdetect` ioctls up to
`DO_MODULE_INIT`) plus `wmt_launcher` (`SET_ROM_PATCH_INFO` for the three
patches, then `SET_STP_MODE`). `SET_STP_MODE` is not synchronous: the driver
queues the HIF operation and returns (`wmt_dev.c:903`, `timeoutValue = 0`), so
`READY=1` means the ioctls returned success, not that the HIF was applied;
ordering against later power-ons is guaranteed by the driver's FIFO operation
queue. The unit stays resident with `/dev/stpwmt` open:
when the last descriptor closes the driver sets `gWmtClose`, after which a
lost answer from the chip leaves the `mtk_wmtd` thread waiting forever and
firmware asserts are suppressed — the driver stops recovering from chip
faults. It answers any text request (`srh_rom_patch` → re-register, `ok`;
anything else → `fail`), though with the patches registered none arrive.
Bluetooth depends on this unit, not on Wi-Fi.

`thunder-wmt-wifi` loads the Wi-Fi modules, pushes the NVRAM (previous
section), writes `1` to `/dev/wmtWifi` — the write blocks until power-on and
the `gen4m` probe finish — and checks that `wlan0` exists. If the write fails
with `EIO` (a whole-chip reset in progress makes the driver refuse operations,
`wmt_lib.c:1623-1627`), it retries once after 3 s: the driver exposes no state
to poll, so the delay is the documented fallback. A second failure exits 74
and the unit retries (`Restart=on-failure`, `RestartSec=3`, at most 3 starts
in 5 minutes); exit codes 72 (missing firmware) and 78 (configuration) are
not retried. `wifi-off` (the `ExecStop`) does not read the configuration, so
it works even when `/etc/thunder/wmt.conf` is broken.

There are no fixed sleeps in the happy path: every step waits on a signal the
driver gives (the return of `modprobe`, the return of `DO_MODULE_INIT`, `open()`
of `/dev/stpwmt` blocking until init is done, the blocking `write()`), with a
bound and an error message. Logs go to the journal
(`journalctl -u thunder-wmt-connsys -u thunder-wmt-wifi`); exit codes follow
`sysexits.h` (`78/CONFIG`, `72/OSFILE`, `74/IOERR`...).

Ordering: both units have default dependencies, so they start after
`sysinit.target`, which the cable units (`thunder-usb-network`,
`thunder-telnet-usb`) are part of; the Wi-Fi unit also orders after the cable
units and `sshd`, so the cable is never delayed. Nothing orders itself after
the radio, and the Wi-Fi unit is not ordered before NetworkManager: Arch's
`sshd.service` comes after `network.target`, which comes after
NetworkManager, so `Before=NetworkManager.service` together with
`After=sshd.service` would be an ordering cycle, and systemd would drop a job
at every boot (sometimes the radio). NetworkManager picks up `wlan0` on
hotplug and manages only `wlan0`
(`51-thunder-wifi.conf`, generated from `WIFI_INTERFACE`, plus the udev rule
that sets `NM_UNMANAGED` on `rndis*`, `usb*` and `ccmni*`). The Wi-Fi unit
`Wants=` the connsys unit rather than `Requires=` it, and uses no `PartOf=`:
a stop or restart of connsys must not take the radio down with it (restart of
the radio is unverified for association). `thunder-wmt-connsys` uses
`RestartMode=direct`. Never `rmmod wmt_drv` on stop: it wedges in `Unloading`.

- **The MAC.** With the factory NVRAM applied, `wlan0` has the factory
  address; otherwise the driver's generated one. Either way NetworkManager
  connects with a stable, locally administered address per network
  (`wifi.cloned-mac-address=stable-ssid`), so the router hands out the same IP
  across reboots.
- Scan MAC randomization is off (`wifi.scan-rand-mac-address=no` in
  `60-xiaomi-thunder.conf`).
- Time from unit start to `wlan0`: **TBD from the phone test** (the previous
  script took 13.6 s, about 13 s of it fixed sleeps).

gen4m creates `wlan0`, `wlan1`, `ap0` and `p2p0`; only `wlan0` is a real
station, which is why `WIFI_INTERFACE=wlan0`. NetworkManager must leave the
other three alone (`unmanaged-devices=except:interface-name:wlan0`):
otherwise `nmcli dev wifi connect` can bind the profile to `ap0` and fail with
`The Wi-Fi network could not be found`.

Networks are saved with `thunder-wifi connect <name>` on the phone, from the
installer (the device screen shows the phone's Wi-Fi when Thunder OS is on the
cable), or preset at install time. Only the derived PSK is stored.

## Phone validation plan

Nothing below has run on the phone yet. Steps marked **(needs the user's saved
network)** require a saved Wi-Fi profile; do not echo SSIDs or MACs in the
output (counts and the `thunder-wmt` log lines only, which print no address).
Writing to the phone, rebooting it and installing the package need the user's
go-ahead.

- **T0, baseline (read-only).** Timestamps of the previous Wi-Fi unit (history, removed from the image) and
  the kernel log lines of the sequence. Expected today: start 6.47 s, active
  20.04 s.
- **T1, PC.** `python3 -B -m unittest discover -s rootfs/packages/thunder-mediatek-wmt -v`
  (`-B`: no `__pycache__` in the recipe directory),
  then `makepkg -f` in a temporary copy of the package directory.
- **T2, install over the running system (no reboot).** `pacman -U` the package,
  write `/etc/thunder/wmt.conf` as `rootfs/configure.sh` generates it, run
  `thunder-wmt nvram-check` (read-only; expect `NVRAM ok: 2050 bytes`), then
  `systemctl start thunder-wmt-connsys` (expect `skipping the loader`, one
  descriptor open on `/dev/stpwmt`). On `226/NAMESPACE`, remove the four
  hardening lines from both units.
- **T3, reboot and measure.**
  - Criteria: `thunder-wmt-connsys` and `thunder-wmt-wifi` active; time from the
    start of connsys to the active state of the Wi-Fi unit (target under 5 s);
    three `Rom Patch]Name=...EmiOffset=0x0/0x2a0000/0x170000` lines per
    power-on; zero `srh_`, `ul_cmd fail`, `cmd buf is occupied` or
    `WMT_close: last call`.
  - **(needs the user's saved network)** `wlan0` present, scan finds networks,
    `wpa_state=COMPLETED` and an IP.
  - NVRAM verdict line: **applied and associates** keeps the default enabled.
    **Not applied** or **unknown** (whatever the association result) or **applied but does
    not associate** sends the default back to empty (the status quo), not to
    "keep": set `WMT_WIFI_NVRAM=` empty in `/etc/thunder/wmt.conf`, reboot and
    confirm it associates, then change the `device.sh` default to empty and
    document why. If it is not applied, the next step is the kprobe on
    `glLoadNvram`.
  - Note whether a `whole chip reset start` happened and whether Wi-Fi came up.
- **T4, stop/start and restart semantics.** Restart of the radio is
  unverified for association, so this test must show it before any doc calls
  it a recovery path. **(needs the user's saved network)**
  The Wi-Fi unit allows 5 starts per minute, manual ones included; if a start
  is refused with `start-limit-hit`, run `systemctl reset-failed
  thunder-wmt-wifi` before the next step instead of reading it as a radio
  failure.
  1. `systemctl stop thunder-wmt-wifi` then `systemctl start thunder-wmt-wifi`:
     `wlan0` stays (dormant while off), the power-on takes about 1.5 s, and
     then `wpa_state=COMPLETED` and an IP (not just `wlan0` existing).
     Measured 2026-10-11: associated again 8-18 s after the start.
  2. `systemctl restart thunder-wmt-connsys`: the Wi-Fi unit must stay active
     (nothing propagates), the log must show `skipping the loader`, and the
     link must still be `COMPLETED` with an IP. If the radio drops, record it
     and re-check association afterwards.
  3. `kill -9` of the connsys main PID: it returns in about 2 s (`NRestarts`
     1); record whether Wi-Fi dropped.
  If association fails in any of these, the restart paths stay documented as
  unverified (or unsupported) and the radio is only recovered by reboot.
- **T5, Bluetooth depends on CONNSYS, not on Wi-Fi.** With the Wi-Fi unit stopped,
  `thunder-wmt-bt` still brings up `hci0`, and
  `systemctl list-dependencies thunder-wmt-bt` shows only `thunder-wmt-connsys`.
- **T6, shutdown.** After a reboot, the previous boot's journal shows
  `Wi-Fi off` before `launcher stopping`, and SSH returns in the usual time.
- **T7, rollback.** `disable --now` the two units, re-enable the old unit
  (kept on the phone until T3-T6 pass), `pacman -R thunder-mediatek-wmt`,
  remove `/etc/thunder/wmt.conf`. Deleting the old files from the phone is data
  removal: ask first.

## Measured performance

| path | result |
|---|---|
| internet download, 50 MB from a public server | 20 Mbit/s |
| **LAN, host → device, 50 MB** | **133 Mbit/s** |

The 20 Mbit/s figure is the internet link, not the radio. Worth stating because
it is an easy wrong conclusion to reach: **measure over the LAN before blaming
the radio for low throughput.** The LAN number is itself conservative, since
that path also crosses the host's own Wi-Fi.

Negotiated link rate is 1170 Mbit/s on 5 GHz. The driver advertises WPA3
alongside WPA2, and reports AP mode as supported (`ap0` and `p2p0` are
created), though AP mode has not been tested.

## Regulatory domain

`/proc/net/wlan/country` reads `12336`, which is ASCII `"00"` — the world
domain. The measured effect: visible 2.4 GHz channels stop at 2462 MHz
(channel 11), while some regions allow 12 and 13. 5 GHz shows 5180–5805
including DFS channels. Writing to that proc file is accepted but does not
change the value.

This is **not** caused by the NVRAM push — the value is identical with and
without it.

## Bluetooth

Bluetooth is on the **same chip**, and its ROM patch
(`soc2_2_ram_bt_1_1_hdr.bin`, type 0) is already downloaded into EMI at offset
`0x170000` as part of this sequence — so the expensive half was already paid
for here.

It now works. See [bluetooth-bringup.md](bluetooth-bringup.md). The patch is
registered by `thunder-wmt-connsys`, which is all Bluetooth needs; Wi-Fi may be
off.

## Debugging notes worth keeping

- **The dmesg ring buffer rolls in seconds** on this device, flooded by
  `Thermal/TZ/PMIC` and `sia81xx_set_vdd`. Running `dmesg | grep` *after* a long
  sequence silently loses the beginning — which at one point produced the
  confident and completely wrong conclusion that CONNSYS power-on was never
  called. Capture with `dmesg -w > file &` *during* the sequence.
- `wmt_ctrl.c` logs with the prefix `[HIF-SDIO]`, not `[WMT-CTRL]`. Grep for
  function names, not prefixes.
- `echo 9 > /sys/bus/platform/drivers/mtk_btif/flag` (BTIF open+write test)
  **reboots the device**. `echo "8 5"` (log level only) is safe.
- `rmmod wmt_drv` wedges the module in `Unloading` and blocks shutdown;
  `sync; echo b > /proc/sysrq-trigger` is then the only way out.
- **Keep `/dev/stpwmt` open.** The driver's only view of the launcher is the
  device's open count. `dmesg | grep 'WMT_close: last call'` while the system
  is up means the launcher died and fault recovery is off until it is back.
- **CONNSYS can power on without Wi-Fi.** A screen unblank powers it on through
  a loopback function once the HIF is set (`wmt_fb_notifier_callback`,
  `g_late_pwr_on_for_blank`), and a chip reset recovery powers it on too. This
  is why the launcher registers the ROM patches *before* `SET_STP_MODE`: that
  ioctl can start a power-on that downloads them.
- **A chip reset at boot can come from audio.** On one boot `wireplumber`
  probing the sound card read a CONNSYS register (`0x18050144`) with CONNSYS
  powered off; DEVAPC flagged it and WMT ran a whole-chip reset. It recovers
  on its own when the launcher is up; the audio side is not fixed.
- Iterating on a module-only change does **not** need a flash: the `.ko` can be
  taken from `build/xiaomi-thunder/kernel/lib/modules/4.19.191-perf` and copied onto the
  device, as long as vermagic matches. See [building.md](../../../docs/building.md).
