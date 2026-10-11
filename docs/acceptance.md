# 验收记录

维护日期：2026-10-10。以下状态依据本机实际执行结果；外部环境未提供的项目不标为通过。

## 已执行

- 应用导入成功，默认 Agent 注册表为空。
- Ruff 格式与 lint、ty 类型检查已通过当前实现。
- 9 项基础测试通过：配置优先级、未知字段、真实 API Key／JWT 签名、用量缺失、Prompt 参数、上下文裁剪、本地对象存储、链接／路径越界、文档解析。
- 真实 PostgreSQL 18.6／pgvector 与 Redis 容器启动成功。
- Compose API、Worker、scheduler 全部在线，迁移容器退出 0；API 就绪 200，doctor Worker ping 通过，Worker 独立指标端点可采集。
- 全新独立空库完成 0001→0002→0003 全迁移及原生 LangGraph Checkpointer setup；运行角色必要表授权及线程首次写入通过。
- 5 项真实集成测试通过：RLS／连接池身份隔离；原生 StateGraph 人工暂停、恢复、线程竞争、重复响应与事件序号；原生 LlamaIndex Workflow 跨资源 Context 恢复；API Key、就绪、记忆更新与真实附件；取消、租约回收和工具副作用账本。
- OpenAI、Anthropic、Gemini 的 LangChain 与 LlamaIndex 原生模型工厂可构建；未产生收费调用。
- Compose 配置可以解析，基础端口与私有初始化配置一致；可选 Langfuse 服务、MinIO、Prometheus、Grafana 和 Collector 使用 profiles。
- Kustomize development／production overlays 渲染通过；使用本机 Kustomize 5.8.3 验证，kubeconform 0.8.0 严格校验 16 个资源全部有效。
- kind Kubernetes 1.37.0 临时集群迁移 Job 完成，两个 API 副本、Worker、scheduler 正常运行；startup、live、ready 探针通过，API 缩容至零顺利退出，临时集群及网络连接验收后清理。
- Dockerfile 使用固定 Python 运行镜像及固定 uv 版本；完整生产镜像已构建成功。构建依赖下载遇到网络超时后通过本机代理重试完成。

## 持续验收

- 真实模型、Embedding、Tavily、MCP、S3 和观测服务的配置依赖验收。
- LlamaIndex Workflow 与 LangChain create_agent 原生运行、恢复和取消。
- 实际模型服务商调用、真实 usage、费用和重试；需要有效服务商凭据。
- 实际 Embedding 导入、更新、删除与多空间重建；需要有效向量服务商凭据。
- Tavily、外部 MCP、S3 及双后端 OTLP 关联；需要相应服务及授权配置。
- 双后端观测中的真实 trace、评估反馈和正文脱敏验收，需要实际后端授权。

CI 执行相同静态规范与基础测试。集成项目必须使用真实依赖，跳过和未执行结果须保留。测试内临时原生图只验证框架契约，不注册到交付运行服务。
