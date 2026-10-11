# Wi-Fi bring-up on MT6833 (CONNAC 1.0 / `gen4m`)

This is the part of the port worth reading. There is no mainline support for
MT6833 connectivity, so everything here is downstream: MediaTek's `wmt_drv` /
`wlan_drv_gen4m` stack on a 4.19 vendor kernel, plus a replacement for the
Android userspace daemon that normally drives it.

**Status under Arch Linux ARM.** The results below were measured on this
kernel with the previous userland. The Arch image boots, but the Wi-Fi
service is not wired into it yet (see
[Bringing it up at boot](#bringing-it-up-at-boot)).

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
no such daemon here, so this project ships
[`devices/xiaomi-thunder/overlay/usr/local/bin/wmt-daemon.py`](../overlay/usr/local/bin/wmt-daemon.py).

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

- **The factory MAC and RF calibration are available but not enabled by
  default.** See [the section below](#the-factory-mac-solved-mechanism-unusable-in-practice).
- `opfunc_wlan_probe: not implemented yet hifType: 0x2` — harmless so far.

## The factory MAC: solved mechanism, unusable in practice

The driver never gets the factory MAC, so `wlan0` comes up with a fallback or
locally-administered address and the factory RF calibration is never applied.

### How the driver actually wants it

`platform.c` defines

```c
#define WIFI_NVRAM_FILE_NAME   "/data/nvram/APCFG/APRDEB/WIFI"
```

which is an Android path that does not exist here. But that file read is dead
code — it sits under `#if 0` in `kalCfgDataRead()`. In practice the driver's
`g_aucNvram` buffer is filled **from userspace**, by writing to `/dev/wmtWifi`:

```
WR-BUF:NVRAM<2050 raw bytes>
```

a 12-byte prefix followed by the raw NVRAM (`wmt_cdev_wifi.c`). It must be a
**single `write()`** — the driver slices the same user buffer into prefix and
payload. The handler is registered in `initWlan()`, so the module only needs to
be loaded, not powered on.

This is the same shape of problem as the ROM patches: another piece of the
Android launcher that is missing here.
[`devices/xiaomi-thunder/overlay/usr/local/bin/wifi-nvram-push.py`](../overlay/usr/local/bin/wifi-nvram-push.py)
implements it.

The source is the `nvdata` partition (ext4), file `/APCFG/APRDEB/WIFI`, 2050
bytes, with the MAC at offset 4. **Mount it read-only** — it holds the factory
RF calibration.

### It works, and then it does not

Pushed before power-on, the MAC comes out correct and survives a reboot:
`wlan0` gets the factory address and `wlan1` a driver-derived variant.

Two problems stack up:

1. **The MAC only takes on the second probe.** Reproduced three times from a
   freshly loaded module: push the NVRAM and power the radio on once and you
   get the fallback MAC; power on, power off, push, power on again and you get
   the factory one. The cause is not isolated — the handler is registered from
   `initWlan()`, the write returns success, and `glLoadNvram()` only requires
   `g_NvramFsm == NVRAM_STATE_READY`, which the push just set. This build of
   gen4m emits no `DBGLOG` at all, even with `/proc/net/wlan/dbgLevel` set to
   `0x00:0xff`, so the path could not be instrumented.

2. **That extra power cycle breaks association.** The scan still sees the AP at
   100% signal on both bands, but the supplicant stays in `scanning` and
   `nmcli` reports `The Wi-Fi network could not be found`. Confirmed by
   elimination: with the NVRAM push disabled but the double power-cycle still
   in the script, association kept failing; restoring a single power-on brought
   the connection straight back.

So the feature is **opt-in and off by default**:

```sh
touch /etc/thunder-wifi-nvram     # factory MAC + calibration, but will not associate
rm    /etc/thunder-wifi-nvram     # default: working connection
```

### Worth knowing before you chase this

- **A fixed MAC is not the reason to do this.** NetworkManager's
  `cloned-mac-address` defaults to `stable` mode here, which derives a
  deterministic address — so the MAC is already fixed across reboots, and the
  DHCP lease is already stable. It is simply not the hardware address. The real
  gain from the NVRAM is the **RF calibration**, which rides in the same 2050
  bytes.
- **The regulatory domain is a red herring.** `/proc/net/wlan/country` reads
  `12336`, which is ASCII `"00"` — the world domain — **with or without** the
  NVRAM push. It is not caused by the NVRAM and is not related to the
  association failure. Writing to that proc file is accepted but does not
  change the value.
- **Do not set `wifi.cloned-mac-address=permanent`.** This driver exposes no
  permanent address through ethtool, and activation then fails with
  `The base network connection was interrupted`.

### The open question

Why is a second probe needed? If the NVRAM could be made to take on the first
one, the extra power cycle disappears and so does the association failure.
Instrumenting `glLoadNvram()` with a plain `printk` is the obvious next step,
since the driver's own logging is unavailable.

## Bringing it up at boot

The bring-up is not something the kernel does on its own — the CONNSYS
subsystem has no firmware of its own and the whole sequence in this document
has to run on every boot. In the Thunder OS image (since 2026-10-10):

- `devices/xiaomi-thunder/overlay/etc/init.d/wifi-thunder` runs
  `/usr/local/bin/sobe-wifi.sh` (log in `/run/sobe-wifi.log`) and declares
  `provide wifi-hw`. It starts **after** `sshd` and the USB network, so the
  ~30 s of bring-up never delay access over the cable.
- The core services take over from there: `wpa-supplicant` waits for `wlan0`,
  then `dhcpcd-wifi` gets an address on `wlan0` only (the USB link is left
  alone). NetworkManager is not used: without udev it does not manage the
  interface.
- Measured: SSH back about 26 s after power-on, Wi-Fi associated with an IP at
  about 30 s, on 5 GHz.
- **The MAC is random on every boot** (the NVRAM push is off, see above). The
  `wpa-supplicant` service replaces it with a stable, locally administered
  address derived from `/etc/machine-id`, so the router hands out the same IP
  across reboots. `wpa_mac="keep"` in `/etc/conf.d/wpa-supplicant` disables it.

Success of `wifi-thunder` is judged by **`/sys/class/net/wlan0` existing**,
not by the script's exit code: `sobe-wifi.sh` has expected failures along the
way (the second `SET_PATCH_NUM` ioctl legitimately returns EPERM, for instance).

gen4m creates `wlan0`, `wlan1`, `ap0` and `p2p0`; only `wlan0` is a real
station, which is why `WIFI_INTERFACE=wlan0`. If NetworkManager is used one
day, it must leave the other three alone
([`docs/networkmanager-unmanaged.conf`](networkmanager-unmanaged.conf)):
otherwise `nmcli dev wifi connect` can bind the profile to `ap0` and fail with
`The Wi-Fi network could not be found`.

Networks are saved with `thunder-wifi connect <name>` on the phone, from the
installer (the device screen shows the phone's Wi-Fi when Thunder OS is on the
cable), or preset at install time. Only the derived PSK is stored.

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

It now works. See [bluetooth-bringup.md](bluetooth-bringup.md).

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
- `rmmod wmt_drv` wedges the module in `Unloading` and blocks OpenRC shutdown;
  `sync; echo b > /proc/sysrq-trigger` is then the only way out.
- Iterating on a module-only change does **not** need a flash: the `.ko` can be
  taken from `build/xiaomi-thunder/kernel/lib/modules/4.19.191-perf` and copied onto the
  device, as long as vermagic matches. See [building.md](../../../docs/building.md).
