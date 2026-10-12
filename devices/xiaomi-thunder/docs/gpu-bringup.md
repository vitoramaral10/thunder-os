# GPU bring-up: panfrost on the Mali-G57 (MT6833)

The Mali-G57 MC2 in MT6833 is a Valhall **Job Manager** GPU (arch 9.0.9,
`GPU_ID` 0x9093). The vendor kernel drives it with Arm's proprietary kbase
(`/dev/mali0`, DDK r32p1), and its only userspace is the Android blob. Nothing
free talks to kbase, so a desktop session would render on the CPU.

This port runs the mainline **panfrost** DRM driver instead, backported to the
vendor 4.19 kernel. Mesa's panfrost gallium driver then works unmodified, and
a compositor (phoc, in the tests below) composes with GLES through Mesa's
`kmsro` path: the display stays on `mediatek-drm` (`card0`), rendering goes to
panfrost (`renderD128`).

**Status under Arch Linux ARM.** The measurements below were taken on this
kernel with the previous userland and its Mesa build. On the Arch image (systemd
and Arch's udev), panfrost is a module that udev loads from the device tree's
modalias, with no `modules-load.d` entry, and the console profile's phoc renders
through Arch's Mesa `panfrost` driver with GLES (2026-10-11).

## Why panfrost and not the alternatives

| Option | Why not |
|---|---|
| Arm's Linux `libmali` | no public build for Valhall JM G57. Rockchip's mirrors carry G610 (CSF) and Bifrost only; MediaTek's Genio build (DDK r41) sits behind an NDA |
| The Android blob through libhybris | the ABI matches the kernel, but phoc takes EGL/GBM from Mesa, so it would not accelerate the compositor, and it needs binder and gralloc |
| Mesa talking to kbase directly | the only prior work covers Bifrost/Midgard JM and G610 CSF, never G57 |
| panfrost | the upstream driver already runs this exact GPU (MT8370, G57 MC2 JM) and Mesa supports it |

## What the backport consists of

`devices/xiaomi-thunder/patches/add-drm-panfrost-mt6833.patch` adds `drivers/gpu/drm/panfrost` as a module:

- **panfrost from Linux 5.15.194**, with the G57 feature and errata tables and
  the reset, power sequencing and MMU lock-region fixes from 6.12. `GPU_ID`
  0x9093 masks to 0x9003, the same entry MT8192 uses.
- **Its own `drm_sched`** (`gpu-sched.ko`, from 5.15): the vendor kernel does not
  build the 4.19 scheduler, and the 5.15 driver wants the newer one anyway.
- **Its own shmem GEM helper and `ARM_MALI_LPAE` page tables**, with the public
  io-pgtable symbols renamed so they never bind to the kernel's copy, whose
  structures have a different layout.
- A compatibility header for what 4.19 lacks: `dma_resv` (still
  `reservation_object`, and not embedded in `drm_gem_object`), XArray (4.20),
  `dma_buf_map`, sg_table helpers, `drm_gem_object_funcs` (the hooks go on
  `drm_driver`), and `drm_gem_fence_array_add`.
- **Power and DVFS through MediaTek's gpufreq.** The GPU node has no clocks,
  regulators or power domains; the vendor `gpufreq` driver owns the MFG power
  domains, the BG3D clock and the VGPU buck. `panfrost_mtk.c` calls the same
  `mt_gpufreq_power_control()` kbase called, and turns gpufreq's OPP table into
  devfreq OPPs. The patch exports the seven gpufreq functions it needs.
- **The device tree node** becomes `"mediatek,mali", "arm,mali-valhall-jm"`.
  kbase only matches `arm,mali-valhall`, so it stays built but never binds.

## Bugs that took the time

Each of these looked like a panfrost problem and was a 4.19 difference:

1. **`DRIVER_PRIME` is still required on 4.19.** 5.4 made it implicit and
   dropped the flag; without it `DRM_IOCTL_PRIME_FD_TO_HANDLE` returns `EINVAL`
   before reaching the driver, and `gbm_bo_create` for scanout fails.
2. **`DRM_AUTH` on the PRIME ioctls.** wlroots exports scanout buffers through
   a non-master `card0` fd. 4.19 demands authentication for that, so a regular
   user gets `EACCES`. Root passes, because 4.19 authenticates
   `CAP_SYS_ADMIN` automatically, which hid the problem in every test run as
   root. `devices/xiaomi-thunder/patches/backport-drm-prime-ioctls-no-auth.patch` removes the flag as Linux 5.2
   did.
3. **Heap BOs have sparse page arrays.** 4.19's `drm_gem_put_pages` does not
   skip `NULL` pages, so freeing a heap BO oopsed.
4. **panfrost 5.4.0 mapped a BO into one address space only.** When phoc
   imported a client's buffer the second `gem_open` hit
   `WARN(is_mapped)`, the GPU faulted, and closing the file corrupted the
   `drm_mm` tree. The `panfrost_gem_mapping` rework, backported to 5.4.y,
   fixes it.
5. **The IRQs are named in capitals** (`"GPU"`, `"MMU"`, `"JOB"`) in the vendor
   device tree; panfrost asks for lowercase names.
6. **`devfreq`'s `trans_stat` overflows a page** with gpufreq's 34 OPPs and
   oopses on `cat`. `devices/xiaomi-thunder/patches/fix-devfreq-trans-stat-overflow.patch` bounds it, and the
   driver registers 12 of the 34 OPPs.
7. **The 5.15 file operations use the core `drm_gem_mmap`**, which on 4.19
   leaves the VMA `PFNMAP` and never reaches the helper, so the first page
   fault on a mapped BO hit `BUG` in `vm_insert_page`. The driver uses the
   helper's file-level mmap instead.

## Measured

All of this was measured with the previous userland (see the status note above).

- Mesa 26.2 reports `Mali-G57 MC2 (Panfrost)`, OpenGL ES and OpenGL 3.1.
- phoc in GLES with a full-screen EGL client: 60 fps (302 page flips in 5 s),
  with phoc at 2–11% CPU. With pixman it held one core at 100%.
- glmark2 (DRM), panfrost 5.4.302 against 5.15: `build` 701 → 983 fps,
  `shading=phong` 453 → 540 fps, `texture` unchanged. The gain comes from
  queueing two jobs per slot.
- DVFS: 390 MHz idle, 955 MHz under load, within gpufreq's thermal and battery
  limits (it goes through `mt_gpufreq_target(..., KIR_POLICY)`).
- Idle power: the MFG block is cut on runtime suspend. Forced on, the idle GPU
  cost about 7 mA of battery current.
- A shader that never finishes is killed by the job timeout in about 0.5 s,
  the GPU resets, and the next client renders normally (5 out of 5).
- 20 minutes of glmark2 on panfrost 5.4.302: no oops or fault, 33.5 °C peak.
- No GPU memory leak on 5.15: over 15 runs of glmark2, one minute each with
  the process exiting in between, `Shmem` stayed at 6.2 MB and `Slab`
  returned to its baseline after each run.
- System suspend (`s2idle`, woken by the RTC): with panfrost loaded and a
  compositor rendering in GLES, 3 of 3 cycles suspended and resumed, and the GPU
  rendered afterwards. panfrost suspends in about 0.5 ms and resumes in about
  1 ms. The same cycle also works with panfrost unloaded and with the
  compositor on pixman.

## Still open

- **Vulkan.** Mesa's PanVK builds its Job Manager backend for Bifrost only;
  Valhall v9 (G57) is skipped on purpose. It is Mesa work, not kernel work.
- **One suspend never woke up.** The very first attempt, with panfrost loaded
  and the GPU idle, stayed asleep until a long press of the power button; the
  RTC alarm did not wake it. It did not happen again in seven later cycles
  across every combination above, and a hang leaves nothing in pstore, so the
  cause is not known.
