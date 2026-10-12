# Audio on xiaomi-thunder

MT6833 audio front end (AFE) with the MT6359 codec. The speaker amplifier on this
unit is a **sia81xx** on the left lineout. The factory ROM also carries an Awinic
AW87389 config (`aw87xxx_*`), but that is another variant of the phone: the
kernel exposes only the sia81xx controls. The ALSA card is `mt6833-mt6359`. Routes
come from the ROM's `/vendor/etc/audio_device.xml` and gains from
`PlaybackVolAna_AudioParam.xml`.

## What ships

- **UCM** in `overlay/usr/share/alsa/ucm2/conf.d/mt6833-mt6359/`. The `HiFi` verb has
  four devices:
  - Speaker: DL1, mono;
  - Headphones: DL1, stereo;
  - Mic: main analog mic, AIN0;
  - Headset: headset mic, AIN1.

  Playback is on `hw:0,0` (DL1) and capture on `hw:0,10` (UL1/VUL12). The verb holds
  the digital routes (DL1 to ADDA, ADDA to UL1). These are DPCM PCMs: the front end's
  `hw_params` fails with `EINVAL` when no back end is connected, and PipeWire probes
  the PCMs before it picks a device. `FixedBootSequence` turns the amplifier off and
  sets the factory gains at every boot (`alsa-utils`, through `alsactl` from udev).
  The driver default is +8 dB on the lineout. `alsa-restore.service` is masked in the
  overlay: with no `asound.state` stored at shutdown, the boot restore falls back to
  the UCM init instead of bringing back an old state (high gain, amplifier on). A new
  image never has that file. The udev rule still restores one that already exists, so
  on a phone set up before this change delete `/var/lib/alsa/asound.state` once.
- **Volume is software only.** `Lineout Volume` and `Headset Volume` declare their dB
  scale inverted: "0%" puts the hardware at the maximum. The UCM never hands them to
  PipeWire. The analog gains stay at the factory values through the
  `*_PGA*_GAIN` enums (index 0 = +8 dB ... 8 = 0 dB, 10 = -2 dB, 19 = mute):
  - speaker -2 dB;
  - headphones -1 dB;
  - mic PGA +12 dB.
- **Kernel**, three patches at the end of the series:
  - `fix-sia81xx-speaker-amp-dapm`: the amplifier is now part of DAPM (`LINEOUT L` ->
    `SpkrLeft IN` -> `SPKR DRV` -> `SPKR`). It powers up with the playback stream and
    down after it, and the UCM never touches `Sia81xx Power`. Before, the vendor left
    the routes commented out, and the machine driver's hook was dead code
    (`CONFIG_SND_SOC_SIA8109`, with no implementation).
  - `fix-mt6359-vow-enable-stuck`: opening and closing `VOW_Capture` (`hw:0,50`) used
    to leave the codec's `vow_enable` set. Every later capture then ran the ADC in the
    low-power voice-wakeup mode, and the mics recorded silence until a reboot.
  - `fix-mt6833-afe-no-s32`: the front ends no longer offer `S32_LE`. The AFE forces
    the 8_24 layout for 32-bit data (`memif_32bit_supported` = 0, `CPU_HD_ALIGN` = 0),
    so "S32_LE" was really `S24_LE`: capture came in 256 times too low and playback
    was garbage.
- **WirePlumber rules** in `overlay/usr/share/wireplumber/wireplumber.conf.d/`:
  - `60-xiaomi-thunder-alsa.conf`: no Pro Audio profile on this card. Most of its ~40
    PCMs are internal DSP ports (hostless, SCP VOW, a BTCVSD with 255 channels).
  - `60-xiaomi-thunder-v4l2.conf`: the v4l2 monitor ignores the video decoder
    (`1602f000.vdec`, `/dev/video1`). Its `open()` is refused, the device activation
    never finishes, and the stuck asynchronous hook holds WirePlumber's event queue:
    no audio node gets a session item and there is no default sink.

## Tested (2026-10-12, kernel #1 Mon Oct 12 11:18:07)

- PipeWire plays a 1 kHz tone at -20 dBFS on the speaker (sink at 40%), and the
  internal mic picks it up: rms 55, with the 1 kHz component on top, against 5 in
  silence.
- Speaker and Internal Microphone come up as defaults, also after a reboot.
- The amplifier is off while idle. The kernel log shows `sia81xx_resume` at stream
  start and `sia81xx_suspend` after.
- After an `arecord` on `hw:0,50`, the mic still records (`vow_enable 0`).
- Listened to on the phone: a short melody at -12 dBFS plays clean through the speaker.
- Headphones (a 3-pole set, which accdet reports as `HEADPHONE(3-pole) PlugIn`), selected
  by hand: the melody plays in both ears, the speaker path and the amplifier stay off.

## Not done

- **Headphone jack detection.** `ACCDET` is an input device (`SW_HEADPHONE_INSERT`,
  `SW_MICROPHONE_INSERT`), not an ALSA jack control, so PipeWire does not switch
  ports by itself. Headphones and the headset mic are selected by hand. The fix is
  an `snd_soc_jack` in the machine driver fed by accdet. The headset mic is not
  tested yet.
- **Earpiece and calls.** `RCV Mux`/`Handset_PGA_GAIN` are mapped in
  `audio_device.xml`. callaudiod skips the card ("lacks speaker and/or earpiece
  port") until the UCM has an `Earpiece` device and a `VoiceCall` verb. Calls need
  the modem.
- Second mic (AIN3, noise reduction) and DMIC modes are unused.
