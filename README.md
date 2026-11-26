# 钧飞工作台 · Junfei Workbench

亚马逊商品档案、选品研究、Listing 文案、商品套图、颜色变体、智能修图与钧飞 ERP 交付的本地工作台。

## 当前源码更新（2026-11-26）

商品页的“运营状态与 Codex”可复制当前商品的原任务、图库确认和刊登回执。任务中心支持提交前淘汰和归档轮转。仓库复核记录列出了本次修复、测试结果及实机验收范围。[更新与上传前复核](studio-dev/listing-workbench/CHANGELOG.md#2026-11-26)

- 商品作图使用“首色套图 / 颜色变体 / 智能修图”三个入口，接当前账号自己的 Codex Tools。
- 默认七图为白底主图 1 张、细节 2 张、种草 3 张、尺码 1 张。细节和种草使用原模板；种草场景、模特及展示方式须有区别。适用的颜色共用有真实依据的尺码图。
- 原图位置可直接“关联颜色变体”，选择当前商品已保存的真实同色照片。账号、商品、真实子体、参考图片及文件哈希共同校验；关联参考不授予 SKU 成品图库或刊登权限。
- 任务中心“启动任务 · 配置店铺”保存有序紫鸟店铺名称、对应 ERP 账号及 ERP 内各站店铺名称，并生成交给 Codex 的接续说明。保存名称不代表身份已核对，也不会自动启动 Codex 或发布。
- 店铺依次轮转，每家每轮 3 款新商品；例如店 1 做新款 1–3，店 2 做 4–6，店 3 做 7–9，再回店 1 做 10–12。按完整父商品及目标站点真实发布记录计数，不按颜色、尺码计数。
- 未知模型或 ERP 提交保留原意图和回执，先对账；不手工修图、不篡改验收，不把候选生成成功当正式图库交付完成。

## 目录

- `studio-dev/listing-workbench/`：Python 后端、账号隔离、产品、图片与刊登任务。
- `studio-dev/listing-workbench/visual-ui/`：React / TypeScript 界面。
- `studio-dev/junfei-publisher/`：ERP 采集、草稿、图库回读及刊登适配。
- `studio-dev/automation/`：工作流说明、研究和浏览器执行入口。
- `.agents/skills/amazon-full-pipeline/`：Codex 当前聊天的总控工作流。
- `.agents/skills/junfei-product-images/`：“钧飞商品作图”项目技能。
- `studio-dev/packaging/`：现有便携构建与验证脚本。

## 本地源码启动

需要 Python 3.12、Node.js 22+；浏览器执行需另行配置本机 EGO 和紫鸟。
在仓库根目录运行：

```powershell
if (-not (Test-Path -LiteralPath studio-dev/automation/config.json)) {
    Copy-Item studio-dev/automation/config.example.json studio-dev/automation/config.json
}
python -m venv .venv
.venv/Scripts/python -m pip install -r studio-dev/listing-workbench/cloud_requirements.txt
Push-Location studio-dev/listing-workbench/visual-ui
npm ci
npm run build
Pop-Location
Push-Location studio-dev/junfei-publisher
npm ci
Pop-Location
$env:JUNFEI_PROJECT_ROOT = (Resolve-Path studio-dev).Path
$env:JUNFEI_ACCOUNT_DATA = Join-Path $env:LOCALAPPDATA 'JunfeiWorkbenchSource'
$env:JUNFEI_PORT = '18769'
$env:NUMBA_CACHE_DIR = Join-Path $env:LOCALAPPDATA 'JFRun/numba'
.venv/Scripts/python -B -X utf8 studio-dev/listing-workbench/desktop_launcher.py
```

已有本地 `config.json` 时保留原文件。源码和笔记本试跑默认使用 `http://127.0.0.1:18769`，独立便携版默认使用 `18768`。可用 `JUNFEI_PORT` 指定工作台端口。只读诊断用 `python -B studio-dev/listing-workbench/runtime_environment.py --json`，检查项目配置、端口、EGO 绑定和图片资源文件。模型设置使用本机自己的 Codex Tools，常用代理地址 `http://127.0.0.1:8787/v1`。新机器自行登录和配置预算、店铺及浏览器绑定。

## 完整作图资源与笔记本试跑

本仓库上传源码、测试、示例及工作流；未上传离线安装器、模型权重、第三方模板配图、完整原始模板库、虚拟环境、构建产物或任何真实账号和业务数据库。源码检出不等于完整离线安装包。

完整图片功能还需要经过清单验证的 240 张原模板及原文、24 项修图配置、11 个滤镜资源，以及本地分割/抠像权重和固定依赖。使用单独交付的 **20261124 Windows x64 笔记本试跑包**运行 Setup.cmd、Check-Tools.cmd、Start-Workbench.cmd；资源目录由各自 manifest 核验。缺少资源时保留明确错误，不以简化蓝图、弱化选区检查或放宽验收替代。

模型 runtime.json 由目标机器安装后生成，不进 Git；保留 worker、依赖及权重的固定哈希。原电脑的运行路径、密钥、登录状态和发布授权不能迁移为新电脑的权限。

## 工作流与实际能力

先读 [工作台自动化入口与接续](studio-dev/automation/工作台自动化入口与接续.txt)、[店铺轮转规则](studio-dev/automation/店铺轮转发布规则.txt)及 [MODULES.md](studio-dev/listing-workbench/MODULES.md)。在此项目打开 Codex，使用项目总控或“钧飞商品作图”技能。

Codex 聊天负责持续编排，任务中心保存配置和状态，原生模块保存任务、输入与回执。新图片候选到正式图库/SKU 的通用自动交接尚未统一；工作台本身不是完整无人值守的多店轮转执行器。原正式图库、SKU、身份、预算与发布检查继续执行。示例关闭生图和自动发布，循环模板关闭。

### Agent 接续和正式交付

从商品页的“运营状态与 Codex”配对并派单。复制本款接续信息可带上原任务、正式图库确认回执和 ERP 外部编号，配对码单独生成。每个会话使用独立配对码领取核对任务，运营员可以撤销本款配对码。使用 `studio-dev/automation/codex_workbench.py` 的 `next`、`review`、`status` 和 `receipt`。连接其他端口时按接续信息设置 `JUNFEI_CODEX_URL`。母版使用远程原图时，先在图片引擎中关联母版颜色，把已核对的原图保存在本机。首色七图派单经运营员确认后进入原生图片引擎，原请求可查询任务与质检回执。

核对时间较长时，领取方可通过 `heartbeat <原派单编号>` 延长三分钟核对租约。原配对码和派单的有效期继续适用。

新版服装套图可在商品交付页选择过检的逐色套图，也可使用沿所选母版生成的六个过检颜色图位。所有颜色沿用母版第七张真实尺码图。系统重新校验实际颜色、子 SKU、图序、文件哈希和尺寸数据，并记录图库版本。ERP 草稿保存和正式发布分别确认。

正式发布可仅选美国，或者在本机已启用跨站策略并核对英国设置后接续英国。正式发布和写入类恢复需预检当前快照并重新输入工作台密码。新建的单机工作空间会先要求运营员设置发布确认密码。只读观察沿原工作台任务及 ERP ID 回查。CLI 在售核验读取原工作台回执，核对完整子 SKU 和 ASIN。

任务中心的“轮转检查点”保存当前轮次、目标和商品，按已核对的原生发布回执计数。运营员可淘汰尚未发起刊登、且无待核对提交的当前商品，继续本店的同一位置。归档后可用已保存配置开始新轮转，原回执和排重历史继续可查。每次外部发布仍走商品交付页。

## 验证与维护

退出工作台并结束业务任务后，可用 `python -B studio-dev/listing-workbench/workspace_backup.py create <数据目录> <新快照目录>` 创建本机数据快照。用同一工具的 `verify <快照目录>` 检查文件哈希和 SQLite 内容。快照含本机业务数据，应保存在受保护的存储中。独立数据目录、EGO 登录资料和系统凭据使用各自的迁移流程。

源码迁移前，便携版已完成本机离线安装、固定模型代码/本机库/权重哈希、279 项模板资源与隔离 HTTP 启动检查。此次源码快照另通过 9 项颜色来源测试和 12 项店铺配置测试。这些记录不代表笔记本实际 GPU、付费出图或 ERP 刊登已验收。

测试用隔离数据和模拟服务，按模块运行；TSX/TS 修改后须重新构建。保留未知提交及原外部任务号，不重置重发。

## 许可与来源

保留仓库原有 AGPL-3.0-only 许可、[来源声明](studio-dev/listing-workbench/SOURCE-NOTICE.md)及各组件许可。独立模板资产、模型与第三方依赖按各自来源和许可管理。
