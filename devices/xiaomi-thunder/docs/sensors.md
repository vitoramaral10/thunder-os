# Brightness and sensors: slider, automatic brightness, fade, rotation, proximity

Three kernel patches on the vendor 4.19 tree, all in `devices/xiaomi-thunder/patches/` and
applied by `tools/build-kernel.sh` in the order of `devices/xiaomi-thunder/patches/series`. The "Kernel
revision" column is the kernel revision (rNN) in which the change landed.

| Patch | Kernel revision | What it gives |
|---|---|---|
| `devices/xiaomi-thunder/patches/add-mtk-lcd-backlight-class.patch` | r35 | `/sys/class/backlight/lcd-backlight`, so a desktop brightness slider works |
| `devices/xiaomi-thunder/patches/add-mtk-hf-sensors-iio.patch` | r36, r38 | the light sensor (r36), the accelerometer and the proximity sensor (r38) as IIO devices, so automatic brightness, screen rotation and blanking near the face work |
| `devices/xiaomi-thunder/patches/fix-ktd3137-brightness-fade.patch` | r38 | a ~512 ms hardware fade instead of a visible jump |

The kernel interfaces were proven on this kernel with the previous userland. On
the Arch image with systemd as PID 1, Arch's `iio-sensor-proxy` and `upower`
work unchanged (2026-10-11): `monitor-sensor` reports the accelerometer (tilt),
the ambient light sensor (lux values that follow the room) and the proximity
sensor, and `upower -i /org/freedesktop/UPower/devices/battery_battery` reports
the battery (state, percentage, voltage). The desktop parts below (slider,
automatic brightness, rotation, proximity blanking) still wait for a desktop
shell to consume them.

## The slider

The backlight was only an LED class device, `/sys/class/leds/lcd-backlight`
(0 to 2047), which writes straight to the KTD3137 backlight chip. Desktops look
for `/sys/class/backlight`, so a desktop shell had nothing to control.

The patch registers a `backlight_device` of type `raw` on top of the LED. Its
`update_status` calls `led_set_brightness_sync()`, which keeps the existing
follow-the-panel-blank logic (`devices/xiaomi-thunder/patches/fix-mtk-backlight-follow-panel-blank.patch`)
in one place. The level is clamped to a floor of `max / 100`, so a slider
dragged to zero, or automatic brightness in a dark room, cannot black the screen
out. A power daemon such as `gsd-power`, or logind's `SetBrightness`, would drive it from there.

## Automatic brightness

The light sensor (TMD2755) is not on an I2C bus the kernel owns: it sits behind
the SCP co-processor, and the only kernel-side channel is MediaTek's
`hf_manager`, which the Android sensor HAL reads through `/dev/hf_manager`.
Nothing on a Linux desktop speaks that.

`devices/xiaomi-thunder/patches/add-mtk-hf-sensors-iio.patch` adds a small in-kernel `hf_manager` client that
publishes the lux value as an IIO device named `als` with an
`in_illuminance_input` channel. udev tags it `iio-poll-als`, `iio-sensor-proxy`
picks it up, and a desktop's automatic brightness switch can use it.

The sensor is enabled on the first read, at a 200 ms period, and disabled again
after 10 seconds without reads, so it only runs while something polls it.

## Screen rotation

The accelerometer sits behind the same SCP. The same client publishes it as a
second IIO device, `accel`, with `in_accel_{x,y,z}_raw` and a scale of 0.001
(`hf_manager` reports mm/s²). udev tags it `iio-poll-accel`, `iio-sensor-proxy`
polls it, and the desktop can rotate the screen.

One sign has to change on the way. Android reports the reaction to gravity, so
a phone held upright reads +9.8 on Y; `iio-sensor-proxy` follows the IIO
convention, where upright is a negative Y. The driver negates the three axes
instead of relying on a mount matrix, since this is a convention difference
and not how the chip is mounted.

## Proximity

The TMD2755 also measures proximity. The hub reports it as a near/far state,
where 0 means near, as in MediaTek's older alsps drivers. The driver publishes
a third IIO device, `proximity`, whose `in_proximity_raw` is 10 when something is
near and 0 otherwise, with `in_proximity_nearlevel` at 5 (`iio-sensor-proxy`
ignores a near level of 0, hence the 0/10 scale). `iio-sensor-proxy` treats any
raw value above the near level as near. A phone shell can claim the sensor
during calls to blank the screen against the face, which matters once the
modem works.

## The fade

A desktop shell sets the new level in one step and has no fade of its own. The KTD3137
can fade by itself: `REG_TRANS_RAMP` (0x08) holds the transition time for I2C
changes, and the driver computes it from the device tree's `ktd,i2c-trans-dim`,
which asks for about 128 ms. That is short enough to look like a jump.

Two things had to change, and the first alone did nothing:

1. The driver clamps the requested time to at least 512 ms, which
   maps to a register value of `0x7`.
2. `ktd3137_probe()` wrote `0x01` to that same register right after configuring
   the chip, undoing the value it had just computed. The write is removed. The
   symptom was a register that read `0x1` after boot even though the log showed
   the driver deriving `0x7`.

## Checking it

```sh
cat /sys/class/backlight/lcd-backlight/max_brightness
cat /sys/devices/virtual/ktd3137/ktd/ktd_trans_ramp_reg   # 0x7 with the fade fix
monitor-sensor --light                                     # lux, needs iio-sensor-proxy
monitor-sensor --accel                                     # orientation: normal, left-up, ...
monitor-sensor --proximity                                 # near: 1 with a hand over the sensor
```
