# InsightForge 项目级协作规范

## 项目定位与主线

- InsightForge 是面向 A 股上市公司的**证据驱动基本面研究与事实审核系统**。
- 核心证据链：**Source → Evidence → Claim → Report → Audit**，所有结论必须可追溯到证据。
- 长期只做 A 股基本面研究；**禁止**扩展自动交易、技术分析、短期预测和买卖建议。

## 架构约束

- **LangGraph 是未来唯一顶层编排器**；不得引入其他 Agent 框架作为顶层编排。
- 确定性任务交给代码，判断与综合交给 Agent。
- API 只处理协议；业务逻辑进入 services。
- Agent 不直接写数据库；持久化通过 repositories/services。
- PostgreSQL 保存业务数据并承担 LangGraph Checkpointer；ChromaDB 用于 Chunk 向量索引。

## 协作规则

- 默认使用**中文**沟通和输出报告；代码标识保持英文。
- 不得未经任务许可：安装新依赖、扩大范围、提交 Git 或 push。
- 不提交 `.env`、密钥、生成文件和运行时数据。
- 开始修改前先说明：任务理解、涉及文件、风险、验证方法。
- 完成后报告：修改文件、运行命令、测试结果、遗留问题。
- 所有 Python 命令必须明确使用 `insightforge` Conda 环境（`conda run -n insightforge ...`）。
- 未达到当前阶段验收门槛，不提前实现下一阶段。
