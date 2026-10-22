#!/usr/bin/env bash
# Requirements: public synthetic manifest and a single pinned public project only.
# Ambiguity: this smoke does not prove the 1,257-project private-input source graph.
# Research: ensure selected repo launcher/init/parser/sync work on current runners.
# Optimization: shallow one-project sync; bounded 180-second init and sync phases.
# Risks: upstream/network availability; no secrets or private files may be read.
# Validation: launcher digest, repo implementation SHA, exact bionic checkout.
set -euo pipefail
: "${RUNNER_TEMP:?}" "${GITHUB_WORKSPACE:?}"
smoke_root="$(mktemp -d "${RUNNER_TEMP}/selene-public-smoke.XXXXXXXX")"
read -r repo_ref repo_commit launcher_digest < <(python3 - "${GITHUB_WORKSPACE}/selene.lock.json" <<'PY'
import json,sys
r=json.load(open(sys.argv[1]))['repo_tool']
print(r['version'],r['commit'],r['launcher_sha256'])
PY
)
mkdir "${smoke_root}/bin" "${smoke_root}/manifest" "${smoke_root}/android"
curl --fail --silent --show-error --location --retry 2 --max-time 60 \
  "https://raw.githubusercontent.com/GerritCodeReview/git-repo/${repo_commit}/repo" --output "${smoke_root}/bin/repo"
printf '%s  %s\n' "${launcher_digest}" "${smoke_root}/bin/repo" | sha256sum --check --status
chmod 0700 "${smoke_root}/bin/repo"
export PATH="${smoke_root}/bin:${PATH}"
export GIT_TERMINAL_PROMPT=0 GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_COUNT=2
export GIT_CONFIG_KEY_0=user.name GIT_CONFIG_VALUE_0=selene-smoke
export GIT_CONFIG_KEY_1=user.email GIT_CONFIG_VALUE_1=selene-smoke@local.invalid
cat > "${smoke_root}/manifest/default.xml" <<'XML'
<manifest>
  <remote name="aosp" fetch="https://android.googlesource.com"/>
  <default remote="aosp" revision="refs/tags/android-12.1.0_r4"/>
  <project name="platform/bionic" path="bionic" revision="11fa611f2c52e17e23f679869724278e2becc862"/>
</manifest>
XML
git -C "${smoke_root}/manifest" init --initial-branch=main --quiet
git -C "${smoke_root}/manifest" add default.xml
git -C "${smoke_root}/manifest" commit --quiet -m 'Public synthetic manifest fixture'
cd "${smoke_root}/android"
timeout --signal=TERM --kill-after=20s 180s repo init \
  --manifest-url="file://${smoke_root}/manifest" --manifest-branch=main \
  --manifest-name=default.xml --groups=all --depth=1 --no-clone-bundle \
  --no-use-superproject --repo-rev="${repo_ref}" < /dev/null
[[ "$(git -C .repo/repo rev-parse HEAD)" == "${repo_commit}" ]]
repo manifest -o "${smoke_root}/parsed.xml"
timeout --signal=TERM --kill-after=20s 180s repo sync -c -j1 \
  --no-clone-bundle --no-tags --fail-fast --retry-fetches=2
[[ "$(git -C bionic rev-parse HEAD)" == 11fa611f2c52e17e23f679869724278e2becc862 ]]
printf 'PUBLIC_REPO_INIT_SYNC_SMOKE=PASS\n'
