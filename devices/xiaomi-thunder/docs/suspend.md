# Suspend on MT6833

Phone-style suspend: the phone sleeps when idle and wakes up by itself for the power key,
an alarm, the charger or, with WoWLAN, a Wi-Fi packet. Work in progress: the policy is in
the image, but `AllowSuspend=no` stays until the real suspend is tested away from the
cable (see "Still open").

## How this kernel suspends

- **`s2idle` is MediaTek's path.** The device tree asks for it (`suspend-method =
  "s2idle"`) and `mtk_lpm` sets it as the default. The last CPU enters the cpuidle state
  `s2idle` (PSCI `0x01010100`), runs `syscore_suspend` and hands over to the SPM, which
  lowers Vcore and, when nothing holds them, turns off the 26 MHz clock, the PLLs and puts
  DRAM in self-refresh. `deep` is plain PSCI `SYSTEM_SUSPEND`: it skips that path and its
  wake log, and neither MediaTek nor Xiaomi use it.
- **The cause of a wake-up** is in the kernel log (`[SPM] suspend wake up by R12_...`,
  `Suspended for`, `26M_off_pct`) and in `/sys/kernel/wakeup_reasons/last_resume_reason`.
- **Idle already goes deep.** Without suspend, the CPUs spend most of the idle time in
  `mcusysoff` (whole CPU cluster off). With the USB cable in, the USB clock keeps the 26 MHz
  and DRAM constraints blocked, idle or suspended.
- **Do not read `/sys/power/wakeup_count` with the cable in:** the musb driver holds a
  "USB suspend lock" wakeup source while the gadget is configured, and the read blocks
  until it is released.

## Wake sources (armed in the kernel)

| Source | IRQ | Notes |
|---|---|---|
| Power key, volume + | PMIC (`mt6358-irq`) | |
| Volume − | `mtk-kpd` | |
| RTC alarm | PMIC (`mt6359-rtc`) | systemd timers with `WakeSystem=yes`, `rtcwake -u` |
| Cable and charger in or out | `type_c_port0` (MT6360) | |
| Wi-Fi | `wlan0` | only with WoWLAN; without it every suspend disassociates |
| Bluetooth | `BTIF_WAKEUP_IRQ` | untested with `btmtk_stp` |
| Fingerprint sensor (on the power key) | `fingerprint` | may wake in a pocket; to measure |

## Policy

- **Who suspends:** logind (`IdleAction=suspend`), once every session is idle. The console
  profile marks its session idle when the panel blanks (`swayidle ... idlehint`); Phosh
  does it through gnome-session. gsd-power's own idle suspend is off in the Phosh profile
  (`90-thunder-phosh.gschema.override`): it would also suspend on the charger, does not
  sleep again after a wake without input, and does not re-check released inhibitors.
- **What keeps it awake:** `xiaomi-thunder-suspend-guard` holds a logind `sleep` inhibitor
  (mode `block`) while any of these holds, like an Android wakelock:
  - USB data cable to a PC (UDC `configured`): the musb driver only saves registers on
    suspend, and the rescue SSH would drop;
  - charger plugged in: the MediaTek charger re-arms an alarm every 10 to 20 s, so on the
    charger only the screen blanks;
  - SSH: an established connection on port 22 or a remote logind session (so `ssh -N`
    tunnels count too);
  - audio playing (an ALSA PCM `RUNNING`, or an active A2DP transport);
  - pacman running.
  `systemd-inhibit --list` shows which one. They are `sleep` inhibitors, not `idle`: an
  `idle` inhibitor also stops swayidle from blanking the screen. logind's `IdleAction`
  respects `sleep` inhibitors in block mode. The guard polls every 10 s, takes new
  inhibitors before it drops old ones, keeps the current state when a D-Bus query fails
  or hangs, and takes an inhibitor again if its `systemd-inhibit` died.
- **Measured with `IdleAction=suspend` (in `/run`, suspend still not allowed):** the console
  panel blanks at 60 s and marks the session idle; logind logged "System idle. Will suspend
  now." `IdleActionSec` later and retried at that interval. `IdleAction` is only armed when
  logind starts, so a `reload` does not apply it.

## Measured (2026-10-14, cable in, charging)

`/sys/power/pm_test` cycles with `echo freeze > /sys/power/state`:

| Level | Result |
|---|---|
| `freezer` | 3/3; tasks frozen in 1 ms; SSH over the cable stays up |
| `devices` | 2/4 ok; the other two aborted with `alarmtimer` returning `-EBUSY` (an alarm less than 2 s away: the charger's); the cable came back by itself, Wi-Fi reassociated in about 3 s |
| `platform` | 5/5; devices suspend in 0.47 s and resume in 0.63 s, the display (`mediatek-drm`) being the slowest at 0.66 s and 0.70 s |

`processors` and `core` are refused for `s2idle`. Earlier (docs/gpu-bringup.md), 3 of 3 real
`s2idle` cycles woken by the RTC worked with panfrost loaded; one earlier attempt did not
wake on the RTC and needed the power key. The RTC is in UTC and correct, so that was not a
time zone mix-up; the cause is unknown.

## Measured (2026-10-14, on battery, real `s2idle`)

`rtcwake -u -m freeze -s 20|30` run from a transient unit after the cable came out:

- **It always came back.** 10 of 10 cycles resumed by themselves; Wi-Fi was connected
  again within 1 to 4 s. One cycle was refused at entry by the charger's alarm
  (`alarmtimer` `-EBUSY`, see above).
- **Early wake-ups:** 3 cycles ended before the RTC (7.9 to 16.7 s), reported as
  `0 (unnamed)` or `type_c_port0-IRQ` with no cable: the MT6360 Type-C interrupt. To chase.
- **It never reaches the deep state: `26M_off_pct = 0` in every cycle.** Two causes, found
  through `/proc/mtk_lpm/lpm/rc/{bus26m,syspll,dram}/cond/state` (what blocks each level):
  - **The panel was on.** `rtcwake` suspends with the panel lit, and phoc lights it again on
    every resume; `mtk_dsi_poweroff` then logs `clk_refcnt = 2, skip power off` and the
    display clocks (`MMSYS_0`, `MMSYS_3`) block the 26 MHz. After `wlopm --off '*'` they
    are gone. Real use blanks first (swayidle, Phosh), so tests must blank too.
  - **The modem.** With the panel off and the cable out, `blocked_cond` is `0x0` but
    `blocked=1`, and the kernel logs `System LPM is blocked by md`: `SRC_REQ_STA_0` has the
    MD request bits (`0x077`). The modem is never booted here (no `ccci_mdinit`,
    `md_state = 0`), yet its clock request stays on. Rfkill on Wi-Fi and Bluetooth changed
    nothing.
- `charge_counter` stays at its full value (2851000) while discharging from 100 %: it is
  not a usable battery meter for the night test.

## Still open

- Release the modem's clock request so the SPM can turn off the 26 MHz (see "Measured, on
  battery"); without it suspend saves little.
- The early Type-C wake-ups without a cable.
- Then each wake source, then a night on battery; `charge_counter` does not move, so
  measure with the voltage or the capacity over hours.
- WoWLAN (`wifi.wake-on-wlan=any` in NetworkManager): turn on only if the measured cost in
  suspend is acceptable.
- Then `AllowSuspend=yes` with `MemorySleepMode=s2idle` in
  `sleep.conf.d/50-xiaomi-thunder.conf`, and the logind drop-in with `IdleAction=suspend`.
