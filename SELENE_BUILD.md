# selene LOS20 Native：部署与验收记录

日期：2026-10-23，Asia/Shanghai。当前任务是部署构建编排，不是设备适配或刷写。

## 已核验依据

| 对象 | 基线 | 用途 |
| --- | --- | --- |
| aosp-builder | `61cfd532ae30c238539de32337a19b8071cf031c` | 部署前 main；16 个工作流及调用关系已遍历 |
| twrp_builder | `6cd2d8f7f2313747f31e32a104097bef18abff95` | Native 结构、共享 Action、官方 ncurses 包、公开 smoke、私有日志/加密交付方法 |
| lineageos_selene | `5027df9dab99f47ce2962a49b169a62ce27a0948` | LOS20 固定图；default.xml blob `c67b5f254cdedfb99038b4c76e6cb887d26299d2`，219,803 bytes |

归档的研发暂停说明适用于历史归档任务；当前新指令授权本仓库实现，不据此启动私有输入构建或修改归档。没有复制私有原文、日志、旧镜像、固件或 Release 内容到公开仓库。

旧 selene-fastboot 的 common/vendor/kernel 与归档图不同；新入口独立保留全部 1,257 个精确 SHA 和 44 个指令，不混入 TWRP Android 12.1 图或设备 overlay。

## 实际验证

| 检查 | 结果与边界 |
| --- | --- |
| 私有 manifest 本地身份、大小、项目/指令 | PASS：1,257 / 44；仅本地读取 |
| 合成边界测试 | PASS：30 项；含失败/超时、路径、checkout/link、OTA 设备/哈希/截断、boot 一致性、重复 ZIP 项和 CMS 解密往返 |
| Bash、Python、JSON/YAML、Actionlint | PASS：新文件；不代表修复原有其他工作流 |
| Action 分支和入口 | checkout、upload-artifact、Grinch27/github-actions 默认 main；已探测 main/master 并读实际 action.yml |
| GitHub 公开 Repo smoke | 待部署后查看实际 Actions；不读取私有输入 |
| 完整 R1 / R2 | NOT_TESTED：未下载 1,257 项、未运行完整编译 |
| 真实 R3 产品 | NOT_TESTED：合成产品检查不等于真实镜像验收 |
| TKUCNXM / R4 / R5 | NOT_TESTED / NOT_ACHIEVED：无设备操作或刷写证明 |

## 后续条件与停止点

1. 查看新入口 Push 的公开 CI；失败时仅据公开证据修正本次实现。
2. 完整构建须配置只读 Token、公共接收证书，并在本地确认匹配私钥可用；不回显 Secrets。
3. 资源策略是 400 GiB 空间 / 64 GiB 内存，普通 Runner 将在预检停止。高资源托管标签、套餐、费用上限未确认，当前没有付费授权；先确定资源，再手动 build=true。
4. 每个 SHA 可取性、旧工具链兼容和完整耗时待验。身份不符、不可取、资源不足或首个失败时结束尝试，不自动换源、禁用检查、增加项目或复制历史输出。
5. 编译后才有真实 boot/ROM/target-files 静态验收及密文；失败日志未持久化，未来须跨会话保留时先批准私有出口。
6. 历史 Global fingerprint、BUILD_BROKEN/SELinux 绕过和 firmware/AVB/分区差距仍存在；本次不修改。stock trust、安装/merge、FBE、启动与设备安全独立验收。

当前最大不确定性：完整锁定图可取/可构建性，以及满足预算的获准 Runner。
用户可能遗漏：公开 Artifact 不是私有成品出口；必须持有接收私钥，且密文只保留 7 天。
Q1：下一入口是核对 Push CI 的真实状态。
Q2：ROM 构建前确定资源、费用边界、Token 和接收证书。
Q3：设备兼容与刷写另行研究和批准，不能由 CI 成功推定。
