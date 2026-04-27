# selene LineageOS fastboot test notes

This note matches the workflow file .github/workflows/lineageos-selene-fastboot.yml.

The current device probe already confirmed these facts on the attached phone:

- adb comes up as selene
- fastboot connectivity works from this host
- fastboot reports product selene
- current slot is a
- boot, dtbo, and vbmeta all have A/B slots

That means the host-side fastboot path is good. What is still unknown before you try a real image is whether this Xiaomi bootloader accepts fastboot boot for a temporary in-memory boot. Some Xiaomi MTK devices do, some reject it. The commands below are ordered so that the first test is non-destructive.

## 1. Trigger the GitHub Actions build

Use GitHub CLI if you want to dispatch the workflow from a shell:

```bash
export REPO="OWNER/aosp-builder"

gh workflow run lineageos-selene-fastboot.yml \
  -R "$REPO" \
  -f manifest_branch=lineage-20 \
  -f lunch_target=lineage_selene-userdebug \
  -f build_targets=bacon \
  -f device_branch=lineage-20 \
  -f common_branch=lineage-20 \
  -f vendor_branch=lineage-20 \
  -f common_vendor_branch=lineage-20 \
  -f kernel_branch=thirteen-stable
```

Watch the latest run:

```bash
gh run list -R "$REPO" --workflow lineageos-selene-fastboot.yml --limit 5
```

## 2. Download the build artifacts

```bash
export REPO="OWNER/aosp-builder"
export RUN_ID="$(gh run list -R "$REPO" --workflow lineageos-selene-fastboot.yml --limit 1 --json databaseId -q '.[0].databaseId')"

rm -rf /tmp/selene-artifacts
mkdir -p /tmp/selene-artifacts

gh run download "$RUN_ID" \
  -R "$REPO" \
  -n lineageos-selene-fastboot \
  -D /tmp/selene-artifacts

cd /tmp/selene-artifacts
ls -lh
cat SHA256SUMS.txt
```

Expected files usually include some or all of these:

- boot.img
- recovery.img
- dtbo.img
- vbmeta.img
- vbmeta_system.img
- vbmeta_vendor.img
- lineage-\*.zip
- build.log
- pinned-manifest.xml

## 3. Install local platform-tools without sudo

Use this if the host does not already have adb and fastboot in PATH.

```bash
mkdir -p "$HOME/tools"
cd "$HOME/tools"
rm -rf platform-tools platform-tools-latest-linux.zip
curl -fsSLO https://dl.google.com/android/repository/platform-tools-latest-linux.zip
unzip -q platform-tools-latest-linux.zip

export PATH="$HOME/tools/platform-tools:$PATH"
adb version
fastboot --version
```

## 4. Record the current phone state before testing

```bash
mkdir -p "$HOME/selene-logs/preboot"

adb wait-for-device
adb devices -l
adb shell getprop ro.product.device
adb shell getprop ro.build.fingerprint
adb shell getprop ro.boot.slot_suffix
adb shell getprop ro.boot.verifiedbootstate
adb shell getprop ro.boot.vbmeta.device_state
adb shell getprop ro.boot.dynamic_partitions
adb shell getprop ro.build.ab_update

adb shell getprop > "$HOME/selene-logs/preboot/getprop.txt"
adb shell logcat -b all -d > "$HOME/selene-logs/preboot/logcat-all.txt"
```

## 5. Reboot to bootloader and verify fastboot state

```bash
mkdir -p "$HOME/selene-logs/fastboot"

adb reboot bootloader
fastboot devices
fastboot getvar product 2>&1 | tee "$HOME/selene-logs/fastboot/product.txt"
fastboot getvar current-slot 2>&1 | tee "$HOME/selene-logs/fastboot/current-slot.txt"
fastboot getvar has-slot:boot 2>&1 | tee "$HOME/selene-logs/fastboot/has-slot-boot.txt"
fastboot getvar has-slot:dtbo 2>&1 | tee "$HOME/selene-logs/fastboot/has-slot-dtbo.txt"
fastboot getvar has-slot:vbmeta 2>&1 | tee "$HOME/selene-logs/fastboot/has-slot-vbmeta.txt"
fastboot getvar all 2>&1 | tee "$HOME/selene-logs/fastboot/all.txt"
```

## 6. First try: non-destructive temporary boot

selene uses recovery-as-boot, and the device tree builds a standard boot.img with boot header v2. So boot.img is the first image to try.

```bash
cd /tmp/selene-artifacts

fastboot boot boot.img
```

If boot.img is not present but recovery.img is, you can also try:

```bash
cd /tmp/selene-artifacts

fastboot boot recovery.img
```

Interpret the result like this:

- If the device boots, the bootloader accepts temporary fastboot boot and you can continue adb-side debugging.
- If fastboot returns unknown command or a remote refusal, this bootloader does not allow temporary boot.
- If the command succeeds but the phone hangs or reboots, the image booted far enough for the bootloader to accept it, but the kernel or ramdisk is not yet stable.

## 7. If temporary boot succeeds, collect first-boot logs

```bash
mkdir -p "$HOME/selene-logs/booted"

adb wait-for-device
adb shell getprop ro.product.device
adb shell getprop ro.build.fingerprint
adb shell getprop sys.boot_completed

adb shell logcat -b all -d > "$HOME/selene-logs/booted/logcat-all.txt"
adb shell dmesg > "$HOME/selene-logs/booted/dmesg.txt" || true
adb shell 'for f in /sys/fs/pstore/*; do echo "===== $f"; cat "$f"; echo; done' > "$HOME/selene-logs/booted/pstore.txt" || true
```

## 8. If fastboot boot is blocked, use the inactive slot as the fallback test path

This writes partitions, so it is not temporary. The point is to keep the currently active slot untouched and test the new images on the other slot.

```bash
cd /tmp/selene-artifacts

current_slot="$(fastboot getvar current-slot 2>&1 | awk -F': ' '/current-slot/ {print $2}')"
if [[ "$current_slot" == "a" ]]; then
  test_slot="b"
else
  test_slot="a"
fi

echo "current_slot=$current_slot"
echo "test_slot=$test_slot"

fastboot --disable-verity --disable-verification flash "vbmeta_${test_slot}" vbmeta.img
fastboot flash "dtbo_${test_slot}" dtbo.img
fastboot flash "boot_${test_slot}" boot.img
fastboot set_active "$test_slot"
fastboot reboot
```

If the device fails to boot, go back to bootloader and switch back to the original slot:

```bash
fastboot set_active "$current_slot"
fastboot reboot
```

## 9. Basic failure triage after a bad boot

If the phone returns to bootloader or never reaches adb:

```bash
fastboot getvar all 2>&1 | tee "$HOME/selene-logs/fastboot/failure-all.txt"
fastboot reboot bootloader
```

If adb does come back after a failed or partial boot:

```bash
mkdir -p "$HOME/selene-logs/failure"

adb wait-for-device
adb shell logcat -b all -d > "$HOME/selene-logs/failure/logcat-all.txt"
adb shell 'for f in /sys/fs/pstore/*; do echo "===== $f"; cat "$f"; echo; done' > "$HOME/selene-logs/failure/pstore.txt" || true
adb shell cat /proc/partitions > "$HOME/selene-logs/failure/partitions.txt"
```

## 10. What to inspect first if the image does not boot

For this device family, the first checks are usually:

- boot.img packing versus boot header v2
- dtbo mismatch
- vbmeta rejection if you flashed instead of fastboot boot
- fstab and metadata encryption mismatches
- Xiaomi mi_ext overlays and proprietary blob version skew
- kernel branch mismatch against the device tree

The current workflow is aligned to these repositories and branches:

- device/xiaomi/selene -> selene-devs/android_device_xiaomi_selene lineage-20
- device/xiaomi/mt6768-common -> LineageOS/android_device_xiaomi_mt6768-common lineage-20
- vendor/xiaomi/selene -> atharvnegi/vendor_xiaomi_selene lineage-20
- vendor/xiaomi/mt6768-common -> mt6768-dev/proprietary_vendor_xiaomi_mt6768-common-legacy lineage-20
- kernel/xiaomi/selene -> TelegramAt25/android_kernel_xiaomi_selene thirteen-stable
