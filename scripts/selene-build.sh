#!/usr/bin/env bash
# Requirements: pristine locked LOS20 sources; fresh OUT_DIR; real boot/ROM/target-files.
# Ambiguity: compiler success does not establish China TKUCNXM compatibility.
# Research: legacy host ABI and firmware/AVB/FBE need independent validation.
# Optimization: fixed bounded jobs; JDK/Clang supplied by the locked Android tree.
# Risks: historical source bypass flags remain; no additional bypasses are allowed.
# Validation: exact lunch identity, all targets exit zero, static product verification.
set -eo pipefail
umask 077
: "${BUILD_ROOT:?}" "${OUT_DIR:?}" "${SELENE_BUILD_JOBS:?}"
[[ "${OUT_DIR}" == "${BUILD_ROOT}/out" && ! -e "${OUT_DIR}" ]]
[[ "${SELENE_BUILD_JOBS}" =~ ^[1-8]$ ]]
[[ -s build/envsetup.sh && -s prebuilts/jdk/jdk11/linux-x86/lib/modules ]]
unset ALLOW_MISSING_DEPENDENCIES PYTHONPATH PYTHONHOME JAVA_HOME
export BUILD_USERNAME=github-actions BUILD_HOSTNAME=github-actions
# envsetup/lunch are not nounset-compatible. Do not use roomservice/breakfast/brunch.
source build/envsetup.sh
lunch lineage_selene-userdebug
[[ "${TARGET_PRODUCT}" == lineage_selene && "${TARGET_BUILD_VARIANT}" == userdebug ]]
mka -j"${SELENE_BUILD_JOBS}" bootimage bacon target-files-package
