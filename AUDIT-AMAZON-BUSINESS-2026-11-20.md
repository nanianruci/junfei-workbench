# 钧飞工作台 · 亚马逊业务功能实现审计

**日期**　2026-11-20
**版本**　提交 3a2e71e 加本地工作区现有改动，包含此前加入的 Codex 派单接口。
**范围**　沿网页、CLI 和 Codex 入口核对后端函数、外部执行器、保存记录及下游读取条件。本版包含源码走读、五项整改和隔离验证。真实店铺、外部服务可用性、账单和平台在售状态均需运行证据单独验证。

## 1. 这套系统实际经营哪一段业务

项目的主线是 **Amazon 自配送（FBM）商品从选品到刊登交付**。前段回答“这款值不值得做、能否找到货源”，中段生产各站点文案和各颜色图片，后段通过钧飞 ERP 建草稿、提交刊登、核对回执。各环节有独立业务代码、任务号和完成条件。账号工作区中的产品档案贯穿这些环节；跨模块衔接还需要站点、SKU、任务号和原回执。[服务路由](studio-dev/listing-workbench/server.py) · [账号工作区](studio-dev/listing-workbench/account_spaces.py)

本次检查的正式入口中，未发现订单履约、广告投放、FBA 补货、退款售后或平台结算的完整操作链。库存数量是刊登经营参数，广告与退货费用可作为利润假设输入。不能据此认定已有库存同步、广告账户管理或真实利润核算能力。[价格与数量参数](studio-dev/listing-workbench/erp_collection_inputs.py#L52) · [利润估算](studio-dev/listing-workbench/market_research.py#L282)

## 2. 业务实现清单

| 业务 | 输入、入口与实际执行 | 输出、完成依据和限制 |
| --- | --- | --- |
| 市场研究 | 网页输入站点、月份、ASIN 或筛选条件。服务端调用卖家精灵查询商品详情、销量趋势、流量词或筛选 FBM 候选；运营员可启用模型分析机会和风险。 | 报告按工具保存参数、返回数据、取得时间和警告。候选排序可用于安排核查顺序；货源、合规判断和经营利润仍需各自证据。[市场研究执行](studio-dev/listing-workbench/market_research.py#L212) |
| 循环发现和选品入库 | 从关键词、搜索结果、卖家、店铺回到下一轮关键词，逐次记录页面观察。服务端验证站点、父事件和时间顺序。实时采集另存运行及步骤记录。CLI 候选转入网页时核对原选品与货源完成记录、排重和正利润报价情景。 | 观察图可说明候选从何而来。入库后新建或复用当前账号档案，关联 CLI 作业、周期、观察事件和证据摘要。发现、通过选品、建档是三项结果。[观察链](studio-dev/listing-workbench/cyclic_discovery.py#L165) · [实时运行](studio-dev/listing-workbench/discovery_live.py#L46) · [准入和入库](studio-dev/listing-workbench/discovery_intake.py#L158) |
| 商品档案与资料采集 | 运营员可独立建档或导入候选。按站点和 ASIN 匹配档案，分配或沿用父 SKU。Amazon 来源、竞品参考、自有产品资料和 1688 供应商采集分别保存。1688 请求进入串行队列，暂停或异常时沿原任务处理。 | 工作区保存商品身份、来源网址、材质、尺寸、包装、颜色及尺码。资料检查还会检查供应商变体覆盖。网页观察只能证明采集当时的页面内容，库存及同款需要对应依据。[档案身份](studio-dev/listing-workbench/product_archive.py#L5) · [货源队列](studio-dev/listing-workbench/supplier_capture.py#L205) · [资料检查](studio-dev/listing-workbench/agent_center.py#L463) |
| 报价、物流与利润 | 后端按报价档、计费重量和处理费估算运费，再用售价、汇率、佣金、采购价和其他成本估算利润。ERP 按目的站点的币种和子 SKU 取价，可使用商品已存价格、与货源匹配的合格报价，或身份匹配的原回执。 | 报价保留来源和估算口径。按当前假设有利润可作为选品证据；交付时仍核对 CNY 采购价、目的币种及每个实际子 SKU 的售价。[运费与利润](studio-dev/listing-workbench/market_research.py#L282) · [报价落档](studio-dev/listing-workbench/discovery_intake.py#L196) · [ERP 取价顺序](studio-dev/listing-workbench/erp_collection_inputs.py#L52) |
| 竞品、关键词、评论及素材洞察 | 单次竞品分析接受本站 1–5 个有效 ASIN，可读取 Amazon 公开详情或卖家精灵数据。模型分析评论样本、素材或 ASIN 参考时读取各自来源。研究结果可保存回相应站点。 | 保留逐来源的成功或失败、关键词及勾选状态、评论依据或洞察版本。任务中心检查成功来源与已选关键词。竞品的卖点不能直接填作本款材质、性能或包装事实。[竞品读取](studio-dev/listing-workbench/competitors.py#L96) · [评论核验](studio-dev/listing-workbench/review_insights.py#L78) · [研究落档](studio-dev/listing-workbench/agent_research.py#L289) · [素材洞察](studio-dev/listing-workbench/product_insights.py) |
| Listing 文案 | 独立商品走持久文案任务，冻结商品、站点、当前文案和审核输入后调模型。选品接续走原任务，先查候选身份、选品证据、货源、版本与费用授权。翻译、字段改写、预览及试验路由各有入口。 | 独立任务按被锁字段和版本合并，备份旧稿，冲突时保留结果供采用；未知调用先核对原回执。选品接续生成的是待采用和审核的草稿，到文案环节停止。文本预算仅记录，账单以实际服务为准。[独立文案任务](studio-dev/listing-workbench/listing_jobs.py#L168) · [选品接续](studio-dev/listing-workbench/agent_handoff.py) · [任务中心文案](studio-dev/listing-workbench/agent_center.py#L980) |
| 分镜、套图与 A+ 内容制作 | 视觉页读取商品实拍或已采集参考、文案、用途、语言及张数。策划器生成分镜项目，执行器按版本、图位、来源依据、模型和尺寸提交。图像预算另有报价、费用预留及结算逻辑。 | 保存分镜、逐图任务号、生成版本、单图检查与整套审核。存在图像或 A+ 素材不等于已发布 A+ 内容。恢复时需定位原项目、图位和外部请求，避免重复计费。[分镜生成](studio-dev/listing-workbench/ivyea_pipeline.py#L399) · [图位调度](studio-dev/listing-workbench/studio_bridge.py#L261) · [费用预留](studio-dev/listing-workbench/image_budget.py#L121) |
| 变体换色与子 SKU 配图 | 使用当前母版、同色参考、供货变体及子 SKU，按配置执行换色和质检。支持本地换色任务。 | 满足条件的同色套图可自动绑定子 SKU。自动关联、人工审核和被排除变体分别保存。旧母版、缺图或同色多套发生冲突时，不能仅按颜色名称决定交付。[绑定条件](studio-dev/listing-workbench/colour_sets.py#L75) · [换色任务](studio-dev/listing-workbench/colour_sets.py#L166) |
| 预览与文件交付 | 从当前产品、站点文案、已选图集与有效变体生成预览、交付 ZIP 和商品表格。按母版及颜色匹配子 SKU，整理清单和缺项。 | 文件可以交给运营员。ERP 一键采集另行读取当前成品，导出成功只证明文件已生成。[交付组包](studio-dev/listing-workbench/delivery_export.py#L72) · [商品表格](studio-dev/listing-workbench/product_table_export.py) |
| ERP 草稿 | 从产品交付页或任务中心选择本账号的目标店铺。后端组装真实子 SKU、站点文案、逐色图库、价格和数量，冻结快照。绑定浏览器在 ERP 中查重、上传、转草稿、保存并回读。 | 同账号、商品和目的地复用原采集任务。系统检查快照、上传图片及原草稿编号。相同且已核验的记录可复用，成品变化或结果未知须对账。此处止于草稿。[成品快照](studio-dev/listing-workbench/erp_collection.py#L168) · [原任务复用](studio-dev/listing-workbench/erp_collection.py#L289) · [浏览器执行器](studio-dev/junfei-publisher/ego-collection-workflow.mjs#L17) |
| 正式发布、跨站与核验 | 现有正式路径以美国草稿为来源。服务端校验成品、目标和英国经营资料，操作员确认预检内容后执行。美国取得符合规则的发布中任务号后复制英国，核对父子 SKU、英国店铺、GBP 售价、运输模板及适用属性，提交英国。 | 保存美国发布号、英国草稿号、英国发布号、冻结输入及逐阶段回执。读取原任务和执行修复各有入口，特殊修复还依赖特定错误及原始证据。两站受理可达成交付完成口径，Amazon 可购买状态另需核验。[跨站策略](studio-dev/listing-workbench/publication_policy.py#L24) · [发布预检](studio-dev/listing-workbench/publication_flow.py) · [完成状态](studio-dev/listing-workbench/publication-tasks.js#L36) |

## 3. 沿单款商品区分五种记录

1. **发现与 CLI 作业。** 回答“为什么选这款、哪一步已经交了证据”。CLI 八阶段依次为选品、货源、研究、文案、图片、导出、发布、验证。作业提供领取、交证据、暂停和对账恢复。执行者完成外部工作后，由台账按阶段检查证据并推进。商品已关联 CLI 时，应从原 CLI 作业查询进度。[阶段领取与完成](studio-dev/automation/pipeline.py#L385) · [执行归属](studio-dev/listing-workbench/discovery_intake.py#L139)
2. **商品工作区与网页任务。** 回答“哪款、哪个站点、哪个版本”。商品、站点文案及审核在账号工作区；任务中心保存执行范围、游标、执行编号及事件。检查任务、研究接续、正式 ERP 草稿和历史产品库草稿使用不同分支。只有显式开启且通过检查的步骤才会调用相应执行器。[任务创建](studio-dev/listing-workbench/agent_center.py#L187) · [执行循环](studio-dev/listing-workbench/agent_center.py#L1273)
3. **文案与图像生产任务。** 回答“哪份文案或哪张图真的生成了”。文案原请求号和模型回执、图像项目和图位任务分别定位。恢复页面、取得候选稿、采用文案、通过图像质检和人工审核是不同事件。[文案回执](studio-dev/listing-workbench/listing_jobs.py#L97) · [图位执行](studio-dev/listing-workbench/visual_studio.py#L564)
4. **ERP 与平台回执。** 回答“哪个店铺接收了什么”。草稿编号证明草稿，发布号及受理状态证明平台接收，最新观察用于判断后续失败或在售。旧记录也有显示兼容分支，读状态时须同时识别来源及观察日期。[发布执行器](studio-dev/junfei-publisher/ego-publication-workflow.mjs#L9) · [列表显示口径](studio-dev/listing-workbench/publication-tasks.js#L36)
5. **Codex 派单。** 回答“系统让 Agent 检查什么、运营员是否确认交给执行器”。记录包含输入快照、步骤、结论、说明及下游任务编号。派单交接成功之后，要用下游原号查询业务结果。[派单创建](studio-dev/listing-workbench/codex_operations.py) · [审批交接](studio-dev/listing-workbench/codex_operations.py)

例如，商品列表在双站发布任务受理后显示「发布已受理 · 在售待核验」。两站最新观察分别匹配原发布号、全部子体和在线状态后，显示「在售已核验」。汇总数量继续按发布交付统计。[完成记录判定](studio-dev/listing-workbench/publication-tasks.js#L36)

## 4. Codex 在现有业务中的实际位置

后端调用的模型服务、内置聊天助手动作卡、外部 Codex 派单分别有独立入口。前两者用于分析、生成或在工作台内执行限定动作。外部 Codex 按账号、商品和站点配对，使用客户端领取派单。现有实现由运营员在商品页发起派单，客户端拉取队列；未发现由业务事件自动启动独立 Codex 会话的实现。[助手执行](studio-dev/listing-workbench/assistant_chat.py#L116) · [客户端](studio-dev/automation/codex_workbench.py) · [商品页](studio-dev/listing-workbench/visual-ui/CodexOperations.tsx)

| 后端可派的核对任务 | Agent 取得的主要资料 | 操作员确认后的执行方 |
| --- | --- | --- |
| 原选品任务接续研究与文案 | 候选、货源、原任务及指定竞品、预算 | 原选品接续和任务中心 |
| 独立商品文案 | 商品字段、货源及当前站点文案 | 持久文案服务 |
| 指定图位生成 | 商品、方案及图位记录，指定模型 | 视觉渲染服务 |
| ERP 草稿采集 | 商品及子 SKU、文案、图片和 ERP 目标摘要 | ERP 采集与草稿服务 |
| 原发布结果观察 | 关联的原发布任务及店铺、站点编号 | 原任务观察入口 |

每份新派单保存动作授权、冻结资料和逐项核对清单。Agent 从 `next` 领取后只能提交意见。通过结论必须覆盖清单里的每一项，说明资料不足时可以只列问题项。审批时后端再次核对版本、目标及输入，再交给该动作绑定的执行器。[派单与审核](studio-dev/listing-workbench/codex_operations.py) · [单款操作说明](.agents/skills/amazon-full-pipeline/SKILL.md)

## 5. 五项发现及整改结果

| 原发现 | 当前实现 | 运营员可看到的结果 |
| --- | --- | --- |
| 核对只到资料分组 | 后端按字段路径和原值摘要校验逐项结论。草稿证据按子 SKU 绑定来源 ASIN、售价、数量和七个有序图位。状态接口也已按真实七列变体映射取值。 | 可展开意见查看原值和阻塞项。历史已结束记录可查，旧格式待核对或待审批记录需要重新派单。[核对实现](studio-dev/listing-workbench/codex_operations.py) · [运营面板](studio-dev/listing-workbench/visual-ui/CodexOperations.tsx) |
| 草稿派单只看文案和图位状态 | 派单、回传及审批共用 ERP 成品组包函数。预检使用本地图片或缓存，读取实际字节验证格式、尺寸和哈希，兼容独立选定的成品套图。工作线程在打开 ERP 前重新核对已批准摘要及实际文件。 | 可看到具体缺项。若只有远程图片链接，须先取得本地成品再派单。既有草稿沿原快照对账。[成品组包与预检](studio-dev/listing-workbench/erp_collection.py) · [子 SKU 快照测试](studio-dev/listing-workbench/test_erp_collection_eligibility.py) |
| 审批中断可能丢失下游编号 | 执行器在登记任务的同一数据库事务中写入账号、商品、站点、派单号及下游任务号。回执查询、列表及重复审批可据此找到已交接的原任务。 | 选品任务区分接续已配置与启动已登记。仅查到审批意图时继续显示待核对，重复审批不会重发。历史中断且缺少本次关联号的记录保留未知。[交接表](studio-dev/listing-workbench/codex_handoff.py) · [原请求查询](studio-dev/listing-workbench/codex_operations.py) |
| 阶段名容易被误读为操作授权 | 五种动作分别注明后台执行终点、计费方式、ERP 写入和发布权限。动作参数由后台校验，CLI 商品沿原台账执行。 | 确认窗口可以区分付费文案、单图、可写草稿以及只读发布观察。正式发布继续使用原发布预检和确认入口。[授权与执行映射](studio-dev/listing-workbench/codex_operations.py) · [确认面板](studio-dev/listing-workbench/visual-ui/CodexOperations.tsx) |
| 金额与完成状态缺少业务口径 | 商品面板区分本站报价情景利润、文本授权预算、本款图像报价、预留与已登记结算。缺少外部账单时显示待核实。草稿回读、发布受理、在售核验使用各自的回执。 | 列表分别显示受理待核验和在售已核验，两类均计入发布交付统计。正式双站任务按各自原号和冻结子体数量判断在售。[金额来源](studio-dev/listing-workbench/image_provider.py) · [发布回执](studio-dev/listing-workbench/publication_flow.py) · [列表显示](studio-dev/listing-workbench/publication-tasks.js) |

## 6. 验证与观察范围

本轮通过 27 项 Python 隔离测试和 14 项商品状态测试。覆盖单款派单与逐项回传、资料变化、旧派单、七列变体记录、审批响应丢失后的原号查询、ERP 组包，以及两站在售观察与原发布号的匹配。前端通过 TypeScript 类型检查和 Vite 生产构建，报告的 64 处源码引用通过文件及行号范围检查。[派单测试](studio-dev/listing-workbench/test_codex_operations.py) · [草稿任务测试](studio-dev/listing-workbench/test_erp_collection.py) · [列表测试](studio-dev/listing-workbench/test_product_progress.cjs)

本次验证使用本地源码和隔离数据。逐项摘要能验证操作员看到的资料版本及 Agent 回传范围；意见本身的业务判断仍需结合原始资料。真实店铺的草稿编号、双站受理号、在售观察日期和服务商账单，均以该商品实际运行产生的对应回执为准。


## 7. 全项目复核后的五项修复

| 业务环节 | 修复后的行为 |
| --- | --- |
| 同一动作重复派单 | 后端在派单和审批事务里检查同一商品和动作目标。原任务交接结果未知时，返回原站点和请求号，超过最近 30 条的未决记录也可在列表中找到。存在准确下游关联号时按原号对账。 |
| CLI 发布与在售核验 | 发布时登记店铺、站点、发布号和全部子 SKU。核验时逐项匹配该次发布，要求 ASIN、在售状态、HTTPS 来源、原始观察及带时区的观察时间。模板沿用原发布身份。历史任务若缺上述发布记录，需要先按原始 ERP 回执核对。 |
| 便携版 ERP | 打包清单包含草稿与发布工作流引用的 Node 模块、浏览器适配器及属性校验器。后端通过固定子命令启动便携工具。 |
| CLI 商品归属 | 按账号与整款商品查询执行台账。在商品上切换 US、UK 等站点会继续显示原 CLI 台账，工作台不能另行派单。 |
| 父 SKU | 新建及修改统一使用 ERP 的 1–40 位规则，支持英文字母、数字、横线和下划线。自动编号保留 14 位时间戳。旧档案可继续保存其他字段，超长父 SKU 进入 ERP 前须先修正。 |

本轮隔离验证通过 CLI 26 项、后台协作与 ERP 65 项、便携打包 11 项，以及 Node 属性校验 6 项。虚构 US 棉布袋从选品、货源、调研、文案、图片、导出、模拟发布走到在售核验，台账留下 8 个完成事件。仅提交外部编号时核验被拒，补齐与原发布记录匹配的逐子体观察后完成。父 SKU 另行检查了 1、40、41 位边界、长用户名及历史记录。

从当前源码实际生成了临时便携分发目录，核对 35 个工作流和工具文件与配套源码逐字节一致，并在该目录成功加载草稿和发布两个 Node 入口。本机 Python 环境缺少 PyInstaller，本轮验证止于分发目录。真实店铺发布、付费服务、新电脑运行及真实在售状态均未实测。

## 8. 追加修复与验收

| 环节 | 当前行为 |
| --- | --- |
| Codex 审批中断 | 后端按账号、商品、站点和原请求串行协调审批与结案。运营员提供核查依据后可结案无交接记录的原请求，已有执行器记录的请求沿原任务继续。 |
| CLI 暂停与恢复 | 暂停后保留原凭据。运行中不能重新领取。可能重复付费或提交 ERP 的阶段，保留原任务并用同一凭据登记已核实的结果。 |
| 成品与导出 | 核验实际 Listing JSON、逐色图位、PNG/JPEG 解码、子 SKU 映射和 ZIP 哈希。发布领取前再次检查原交付 ZIP 的哈希。 |
| CLI 在售 | 从配对账号和商品对应的工作台原任务读取 ERP 回执，检查原发布号、站点、店铺、父子 SKU、ASIN 和每个子体的在售状态，保存后端观察。仅有提交编号或外部来源文字时继续待核对。 |
| 便携版 | 使用 PyInstaller 构建 Windows EXE 并在本机迁移到含中文与空格的目录。用全新数据目录测试登录、静态资源、固定工具、只读自检、原始数据隔离及无授权外部执行拦截。 |

本轮通过 153 项 automation 测试、71 项工作台与便携运行时测试、11 项打包测试、58 项 Node 发布及属性校验测试，前端 TypeScript 和 Vite 生产构建通过。便携构建标识为 `70923a2458f743a59f0e`，核验了 345 个运行文件，并通过迁移冒烟测试。

旧台账继续保留。缺少冻结成品资料的旧阶段须核对原文件。没有工作台原任务关联的外部发布继续待核对。便携版验证范围是本机迁移，仍缺少第二台干净 Windows 的实机验证。隔离验证使用虚构资料，未调用付费服务或操作真实店铺。
