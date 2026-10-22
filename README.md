# AOSP Builder

Android 构建编排工程。selene 的新入口是 [LineageOS selene (Native, locked LOS20)](.github/workflows/lineageos-selene-native.yml)，参考 [twrp_builder](https://github.com/Grinch27/twrp_builder) 的 Native 路线，使用 [lineageos_selene](https://github.com/Grinch27/lineageos_selene) 的固定 LOS20 图。两个项目的源码、overlay、镜像检查和交付证书独立。

## selene 固定图构建

| 项目 | 实现 |
| --- | --- |
| 私有输入 | `lineageos_selene@5027df9dab99f47ce2962a49b169a62ce27a0948` 的 `default.xml`，仅读取这一个文件 |
| 身份 | [selene.lock.json](selene.lock.json)：commit、Git blob、字节数、1,257 个精确 revision 和 44 个 linkfile |
| 平台 | LOS20 / Android 13 历史图；China MIUI `V14.0.4.0.TKUCNXM` 是适配研究目标 |
| 编译 | Non-container；`lunch lineage_selene-userdebug`；`mka -j4 bootimage bacon target-files-package` |
| Runner | 统一 runner-image Job；构建使用 `needs.runner-image.outputs.RUNNER_IMAGE` |
| 工具 | 固定 Repo v2.54 和实现 SHA；JDK/Clang 来自锁定源码，不切换全局 Java/Python |
| 源码检查 | `repo init -g all`；逐项目 HEAD、Git 根、洁净状态、copy/link；同步前后核对 Repo 解析 |
| 失败 | 同步/编译时限和真实退出码；不换源、不抄旧输出、不新增缺依赖或 SELinux 绕过 |
| 产品 | boot v2/page 2048/64 MiB 上限、AVB 自洽、AB OTA/payload 哈希、target-files CRC/分区声明及 boot 一致性 |
| 交付 | 仅成功后的 CMS 密文；含 boot、ROM、target-files、报告及 SHA256SUMS；Artifact 保留 7 天 |

**部署与静态验证不等于完整构建成功。** 固定图尚未证明全量可取/可构建；旧 ROM 和历史安装失败不能作为新入口的成功证据。实际状态见 [SELENE_BUILD.md](SELENE_BUILD.md)。

## 触发和设置

Push/PR 只做不含 Secrets 的单元测试、Bash/Actionlint，以及一个公开 bionic 项目的 Repo init/sync smoke；不读取私有输入或同步完整 Android。手动默认 `build=false`，执行同样的公开验证。

完整构建前，在 Settings → Secrets and variables → Actions 配置：

| 类型 | 名称 | 要求 |
| --- | --- | --- |
| Secret | `LINEAGE_SOURCE_TOKEN` | Fine-grained PAT，仅授予 Grinch27/lineageos_selene 的 Contents: read；默认 GITHUB_TOKEN 不能跨仓库读私有输入 |
| Secret | `SELENE_DELIVERY_CERT` | PEM X.509 公共接收证书；本地必须有匹配私钥，不得把私钥交给 GitHub/聊天 |
| Variable | `SELENE_BUILD_RUNNER` | 可选，已批准且已配置的高资源 x86_64 GitHub-hosted Runner 标签；空值使用共享 Action 解析的普通标签 |
| Variable | `SELENE_WORK_BASE` | 可选，Runner 上已有真实绝对目录；空值使用 RUNNER_TEMP；所在文件系统须满足空间预检 |

保守资源策略为 **400 GiB 可用空间、64 GiB 物理内存**，尚非实测 LOS20 最低需求。标准 Ubuntu Runner 的官方配置为 16 GB RAM / 14 GB SSD，不满足本策略；预检在私有下载和依赖安装前停止，不清理磁盘、创建 LVM、格式化或修改宿主。高资源标签只影响手动构建，PR 测试仍在普通托管 Runner 上运行。

高资源 Runner 的套餐、标签和费用未确认。**本次未授权购买、开启收费资源或付费构建。** 使用前须确定费用上限和资源授权；不支持直接使用 self-hosted/生产宿主。任务总上限 350 分钟，同步 150 分钟、编译 170 分钟；源图不可取或首个失败时结束当前尝试，不自动换源、增加项目或恢复 Release 旧镜像。

确认资源、私有输入处理与本地接收私钥后，选择新入口、分支 main，唯一输入 `build=true`。每次使用新工作目录。当前只交付密文，没有公开镜像、Release 或设备命令。

Token 只在 fetch 步骤可用；同步/编译子进程采用环境白名单，不继承 Token、Actions Runtime Token、代理或全局 Git 配置。公开日志仅输出固定阶段、退出码和非敏感提示。私有 manifest、源码、原始日志及明文镜像不进入公开 Artifact。失败日志仅在临时 Runner 内，结束后不可恢复；需要跨会话诊断时先另行约定私有出口。

## 本机解密

先在本地确认接收证书与私钥匹配。成功下载 Artifact 中的 `selene-output.cms` 后，在本机使用实际证书和私钥文件：

```bash
# Requirements: OpenSSL, local recipient credentials and a new output filename.
# Ambiguity: decryption does not establish stock or device compatibility.
# Research: independent AVB trust, partition geometry, firmware/FBE and device gates.
# Optimization: decrypt once; inspect the tar before extracting into an empty directory.
# Risks: experimental plaintext ROM files; protect the local private key.
# Validation: CMS decryption, expected file list, checksums and NOT_TESTED fields.
test ! -e selene-output.tar
openssl cms -decrypt -binary -inform DER -in selene-output.cms \
  -recip recipient.pem -inkey recipient.key -out selene-output.tar
tar -tf selene-output.tar
```

固定内容为 `boot.img`、`rom.zip`、`target-files.zip`、`static-validation.json`、`SHA256SUMS`；解压到新空目录后运行 `sha256sum -c SHA256SUMS`。CMS 用于保密交付，内部摘要用于解密后核对，不是独立发布签名。

AVB 自洽不证明 stock key/rollback 信任。China 固件来源、super/slot 几何、FBE、Virtual A/B 安装/merge、启动和数据安全未验收。原设备树有 BUILD_BROKEN/SELinux 绕过和旧 Global fingerprint；新编排不修改这些来源，也不将其提升为已通过适配。报告保持 `stock_compatibility=NOT_TESTED`、`device_boot=NOT_TESTED`、`flash_readiness=NOT_ACHIEVED`。不执行 ADB/Fastboot。

## 开发验证

Python >=3.11、Git、Bash；CMS 往返测试还需要 OpenSSL。

```bash
python3 scripts/selene.py validate-lock
python3 -m unittest discover -s tests -p 'test_selene.py' -v
bash -n scripts/selene-build.sh tests/selene-repo-smoke.sh
actionlint -shellcheck='' .github/workflows/lineageos-selene-native.yml
git diff --check
```

有权限的本地归档副本可运行 `python3 scripts/selene.py check-manifest --manifest /实际路径/default.xml`；仅本地校验，不提交私有 fixture。测试 Git、OTA、boot、证书及 Token 字符串均为临时合成数据。

## 原有入口与参考

旧 lineageos-dev/stable/testing、call-build、lineageos-selene-fastboot 和其他实验入口保持独立。Call Build 调用 lineageos-dev，不调用新入口。旧 selene-fastboot 设备/vendor/kernel 来源与固定图不同，不可视为等价构建；本次不删除或迁移这些入口。

- [GitHub 工作流语法](https://docs.github.com/zh/actions/reference/workflows-and-actions/workflow-syntax)
- [Repo manifest 格式](https://gerrit.googlesource.com/git-repo/+/HEAD/docs/manifest-format.md)
- [GitHub-hosted Runner 资源](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [AOSP 环境与资源](https://source.android.com/docs/setup/start/requirements)
- [参考 TWRP Native](https://github.com/Grinch27/twrp_builder/blob/6cd2d8f7f2313747f31e32a104097bef18abff95/.github/workflows/twrp_native.yml)
