---
name: cyclic-product-discovery
description: Review an existing cyclic-discovery candidate or continue a verified selection in Junfei Workbench, preserving the original candidate, marketplace, sourcing and ranking evidence.
---

# 循环选品与原候选接续

在工作台选品入口保留候选站点、ASIN、货源与排名证据。通过现有入库流程关联原报告和任务。

候选入库后，使用项目中的 `studio-dev/automation/codex_workbench.py next` 接收后端派发的选品核对任务。按派单核对 `selection.reference`、`selection.tasks`、`supplier`，沿用原任务、预算与竞品 ASIN。发现冲突、材料缺失或未知回执时，交回 `blocked` 并指出需要核对的原记录。

逐项结果按 `assignment.checks` 绑定原路径、摘要及 `ready` 或 `blocked`。选品利润采用报价情景，文本预算只记录授权，本次接续止于文案。

如 `selection.cliTask` 有值，向运营员提供原 CLI 任务编号和阶段，使用对应台账继续。遇到 `unavailable` 时先核对原台账。
