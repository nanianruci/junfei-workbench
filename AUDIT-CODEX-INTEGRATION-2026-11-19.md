# 固定后端与 Codex 联动审计

2026 年 11 月 19 日。审计对象为 `3a2e71e` 加当前本地工作区改动。沿单款商品检查选品、货源、研究、文案、套图、ERP 草稿、正式发布和状态核验，重点核对 Codex 能读到什么、能触发什么，以及后端依据什么放行。

## 审计结论

工作台已有可复用的业务校验、任务快照和外部回执。缺口在于 Codex 缺少可随项目交付的操作契约，也缺少按商品和站点返回当前状态、阻塞原因与允许动作的统一入口。操作规则分布在前端、CLI、任务中心与发布服务。即使 Codex 理解运营目标，选错入口仍会遇到一套不同的校验条件。

## 发现与优先级

| 优先级 | 发现与影响 | 整改验收 |
| --- | --- | --- |
| P0 | 正式发布启动校验同账号美国 ERP 目标、原草稿与当前成品摘要、英国经营参数，并冻结输入。当前接口的显式确认仍是调用方提交 `confirmed: true`。从本次检查的启动路径中未发现单独绑定该预检快照的操作员确认记录。同一网页会话中的自动操作与手工点击可传入相同的标记。`continue` 同时承载观察和修复。([启动](studio-dev/listing-workbench/publication_flow.py#L541) · [恢复](studio-dev/listing-workbench/publication_flow.py#L623)) | 独立提供「核对原发布结果」。发布与有副作用的恢复按账号、商品、目的地、动作和预检摘要校验服务器记录的确认。资料或店铺变更使原确认失效。通过原对账 ID 恢复。 |
| P0 | 付费文案存在多条入口。选品接续检查原候选身份、选品结论、资料版本和费用确认，预算仅记录。通用持久文案入口允许省略 `expectedRevision` 和 `requestId`，单次启动的费用弹窗在前端。兼容生成路由还可以直接调用模型。用通用入口做选品接续时，选品链约束没有随动作一起传递。([选品接续](studio-dev/listing-workbench/agent_handoff.py#L57) · [持久文案](studio-dev/listing-workbench/listing_jobs.py#L252) · [兼容生成](studio-dev/listing-workbench/server.py#L875)) | 将「选品接续」和「独立商品建档」定义为不同业务动作。给 Codex 的付费动作必带版本、请求标识和明确费用范围，复用持久任务。未知结果只核对原任务。预算字段明确标为估算，除非服务真能强制封顶且通过对应测试。 |
| P1 | 技能目录展示 `$amazon-full-pipeline` 和 `$cyclic-product-discovery`。安装标记只检查工作台服务进程所见目录下的 `SKILL.md`。仓库未发现根级 `AGENTS.md`、随项目分发的对应 Skill、已注册的项目 MCP Server 或版本化的 Codex 接口合同。工作台规则库、内置助手动作卡、名为 `Codex Tools` 的模型配置、卖家精灵 MCP 客户端属于各自的功能入口。([技能目录](studio-dev/listing-workbench/workbench_skills.py#L32) · [助手动作](studio-dev/listing-workbench/assistant_chat.py#L116) · [模型设置](studio-dev/listing-workbench/provider.py#L63) · [卖家精灵](studio-dev/listing-workbench/sellersprite.py#L1)) | 在干净检出及测试账号中验证 Codex 会话实际发现项目 Skill、连接指定环境、核对会话账号并读到一款商品。工作台显示可验证的连接结果。由后端强制工具范围。 |
| P1 | CLI 八阶段流水线、网页任务中心、工作区、文案任务、图像任务、ERP 采集和正式发布各有状态。选品入库已显式关联部分 CLI 证据，后续各模块也有原任务号，但还没有贯穿单款运营的状态和可执行动作视图。通用 `GET /api/state` 甚至可能补写 SKU 或供应商资料并增加版本，真正只读需要显式走 `readOnly=1`。([CLI 阶段](studio-dev/automation/pipeline.py#L17) · [入库](studio-dev/listing-workbench/discovery_intake.py#L222) · [任务中心](studio-dev/listing-workbench/agent_center.py#L1459) · [工作区读取](studio-dev/listing-workbench/server.py#L370)) | 同一账号、商品和站点在页面与 Codex 返回一致的阶段、阻塞原因、允许动作、数据版本和原任务引用。读取不产生副作用。关闭会话后可用原 ID 找回任务。并发提交同一请求只创建一份外部意图。 |
| P1 | 商品档案、工作区快照、研究、套图和交付使用多组字段表达身份、事实、参考和审核。通用工作区保存已有记录冲突、基础字段、SKU、锁定和主色校验，但没有为 Codex 声明每个字段的权威来源和允许写入动作。选品参考商品可以先建档，图片可用性、人工审核以及 ERP 草稿结果又分别留有记录。([档案创建](studio-dev/listing-workbench/product_archive.py#L22) · [工作区校验](studio-dev/listing-workbench/workspace_state.py#L57) · [图像预检](studio-dev/listing-workbench/agent_image_preparation.py#L104) · [交付快照](studio-dev/listing-workbench/erp_collection.py#L168)) | 工具直接返回字段来源、站点、确认状态和引用。采集观察、模型建议、人工确认及系统派生各自带出处。逐项校验字段和允许的转换，通用工作区写入不作为 Codex 工具。 |
| P2 | 页面、批处理和 Codex 缺少共享的动作展示规则。批处理已经限制动作范围，并核对绑定会话。用户仍须跨页面判断该补资料、查原回执，还是启动下一阶段。([批处理说明](studio-dev/automation/WORKBENCH-BATCH.md#L1) · [文案页面](studio-dev/listing-workbench/listing-jobs.js#L151) · [发布页面](studio-dev/listing-workbench/visual-ui/PublicationFlow.tsx#L76)) | 依据后端动作清单实现按钮。应用渐进披露，默认展示当前阶段和一个主要动作，详情展开显示证据和历史。用一致的按钮尺寸、间距和状态词。被阻塞的主动作显示原因和补齐入口。 |

## 单款商品的字段和证据

| 阶段 | 当前权威记录 | 给 Codex 的约束 |
| --- | --- | --- |
| 定位操作范围 | 登录账号由服务端识别，商品档案有 `productId`，目标站点在任务或目标配置中，正式 ERP 通过本账号 `destinationId` 解析店铺。 | 始终同时展示账号、商品、站点。涉及交付时明确店铺。以 ID 执行，商品名和店名供人识别。 |
| 选品 | 站点与候选 ASIN 配对保存在 `selectionResearch.reference` 或入库来源，选品规则和证据由选品模块检查。入库可能创建 `IMP-{market}-{asin}` 档案并分配父 SKU。 | 候选、竞品、子变体来源 ASIN 分开引用。选品入库结果仍须经过资料检查。([档案匹配](studio-dev/listing-workbench/product_archive.py#L5) · [选品检查](studio-dev/listing-workbench/agent_selection.py#L380)) |
| 货源与变体 | `sourceUrl`、货源采集属性、材质、尺寸、包装、已选颜色尺码和变体核验。任务中心检查资料完整性和供应商变体覆盖。交付使用实际选中的子 SKU，校验完整性及唯一性。 | 货源页面或人工核验记录是事实依据。缺项应指出来源要求。保留父 SKU 和已选子 SKU，改动身份须走明确动作。([资料检查](studio-dev/listing-workbench/agent_center.py#L463) · [交付快照](studio-dev/listing-workbench/erp_collection.py#L168)) |
| 研究与文案 | 研究报告和关键词按站点保存，当前文案在 `copy[productId + '-' + market]`。持久文案任务记录来源版本、输入摘要和结果。选品接续保留任务草稿与审核交接。 | 生成、应用和审核分别留痕。查完原版本后执行。报告里的竞品信息作为参考，锁定字段保留。([研究应用](studio-dev/listing-workbench/agent_research.py#L289) · [文案启动](studio-dev/listing-workbench/listing_jobs.py#L252)) |
| 分镜与套图 | 视觉方案及图位、现有图片任务记录、产品图色绑定 `assetColorBindings`、主色和逐色子 SKU。ERP 快照保留图位、颜色、摘要及人工审核来源。 | 以方案版本、图位和颜色定位素材。可用图、模型质检通过、人工审核和允许交付分别表达。([图像预检](studio-dev/listing-workbench/agent_image_preparation.py#L104) · [成品组包](studio-dev/listing-workbench/erp_collection.py#L168)) |
| ERP 草稿 | 当前成品读取已选变体和图集。售价按市场、币种和子 SKU 解析，保留计价依据，采购价区分 CNY。采集任务保存目标和原草稿回执。 | 区分账户、目标店铺、父子 SKU、站点、售价币种与采购币种。从原采集任务取 `draftId`。([价格解析](studio-dev/listing-workbench/erp_collection_inputs.py#L52) · [采集入口](studio-dev/listing-workbench/erp_collection.py#L250)) |
| 正式发布和核验 | 美国原草稿、快照、已提交回执、美国 `publishId`、英国目标和经营参数。美国原任务进入发布中后按策略接续英国，留存同一任务链。 | 草稿、已提交、发布中及已在售各用自己的状态。未知时查原回执。英国复用原父子 SKU，经营参数按英国目标校验。([发布策略](studio-dev/listing-workbench/publication_policy.py#L24) · [英国参数](studio-dev/listing-workbench/publication_inputs.py#L223)) |

## 实施顺序

1. **先做单款只读状态。** 由后端组合现有记录，以账号、商品、站点定位业务上下文。返回阶段、阻塞列表、版本、允许动作及原始任务引用。内部继续复用各模块自己的回执和字段。
2. **打通选品通过到研究与文案。** 项目 Skill 指向固定工具。工作台和 Codex 共用选品接续服务。覆盖缺货源、选品未通过、已保存版本变化、无费用确认、重复提交和结果未知的场景。单款通过后扩到批量。
3. **接套图、草稿和发布。** 先接图位、产出及审核，再接 ERP 草稿和原任务查询。正式发布绑定预检与操作员确认。保留现有外部任务号和恢复语义。对于已登录浏览器才可完成的步骤，由后端明确所需会话及对应账号，执行前核对。
4. **按状态整理前端。** 用渐进披露决定页面提供哪些按钮，用既有设计系统统一视觉。货源缺失时主按钮是「补齐货源」，任务排队时是「查看任务」，回执未知时是「核对原任务」，只有发布预检完成且具备所需确认入口时才显示发布操作。以相同状态对照页面和 Codex 的动作，作为功能验收。

## 本轮验证

- 隔离账号数据下，Python 专项 **108 项通过**，包括选品接续、持久文案、选品入库、ERP 草稿、正式发布及原任务恢复。首次使用 Windows 默认编码运行时有 16 项读取 UTF-8 测试回执时报 GBK 解码错误。按项目回归配置加 `-X utf8` 后全部通过。该差异影响本地运行测试的方式。
- Node 批处理运行时 **2 项通过**，覆盖请求结果未知时不重发、账号不符时停止。
- 本报告依据源码和隔离测试。缺少发生错误时的原 Codex 对话、操作日志及运营员客户端安装状态，无法定位哪次实际操作选错了入口。真实店铺联调和外部服务账单未在本轮验证，项目源码未在本轮修改。
