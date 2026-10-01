# 开发协作与交接

协作分工的唯一规则入口是 [AGENTS.md](../AGENTS.md)。主控负责架构、计划与审查，具体执行由规定的 gpt-6-luna（max）子代理承担；主控亲自实施的单次例外须来自用户明确授权。本文不重复维护另一套型号或执行方式规则。

## 任务交接

每次交接写明本次目标、文件归属、需要保留的现有改动、相关契约、验收标准和实际验证方式。执行端须知道工作区还有其他协作者，不撤销他人改动。验收不通过时在原任务修正；通过后的新实施任务按 AGENTS 启动新的指定子代理。

开始前检查现有模块与 Git 状态；报告“已编码、已集成、已验证”的具体范围，区分构造测试、真实资料、本地 mock、在线调用和历史回放。不得把工具或依赖在旧环境可用写成当前环境已就绪。

## 按任务读文档

- 启动、当前能力与不足：[README](../README.md)。
- 实现与接口契约：[architecture.md](architecture.md)。
- 数据与评测边界：[data-policy.md](data-policy.md)。
- 工程里程碑：[roadmap.md](roadmap.md)；产品任务与交付顺序：[product-delivery-roadmap.md](product-delivery-roadmap.md)。
- 旧流程兼容：[current-schema-audit.md](current-schema-audit.md)，仅为历史结构参考。
- 既往验收与环境快照：[verification-history.md](verification-history.md)。
- 文档审阅与维护方式：[documentation-review.md](documentation-review.md)。

`tem/FINTRACE_Codex_Development_Brief.md` 保持原样，是旧阶段建议；其中任务顺序与“Codex 工作规则”不覆盖当前 AGENTS、本次任务或后续已实现能力。

## 范围与兼容

当前首版不做 OCR、向量库、GraphRAG、模型训练、用户系统和云部署。用户明确安排的新范围按任务更新路线，不能用历史“暂不做”否定后续授权。

保留旧预检 API 和历史记录的兼容；已有原始资料与归档不改写。修复旧实现不等于改写历史结果，必要的 API 行为修正应说明兼容影响并验证。

计划书可按用户任务完善；视频脚本与制作交接给视频负责人，软件侧交付可录系统与证据。上传由团队指定提交负责人。材料编写、视频制作和软件实现分别验收。
