# ADR-0009：来源登记与原始文件归档（阶段 2B.1）

- 状态：已接受
- 日期：2026-08-07
- 决策人：InsightForge 项目

## 决策

1. **RawArtifact 与 SourceRecord 分成两个独立表**。
   - RawArtifact 是不可变字节归档，与来源无关，按 SHA-256 内容寻址唯一；SourceRecord 是一次来源登记，引用 artifact_id。
   - 同一原始文件可对应多个来源记录（例如同一份 PDF 被多个来源 URL 指向）。
2. **RawArtifact 使用 SHA-256 内容寻址与不可变写入**。
   - 存储布局 `sha256/ab/cd/<64位hex>.pdf`；写入采用临时文件 + `os.replace` 原子替换；已存在的同哈希文件不覆盖。
3. **SHA-256 唯一去重由数据库保证**。
   - `content_sha256` 唯一约束 + `INSERT ... ON CONFLICT DO NOTHING RETURNING`，并发下以最先提交的事务为准；service 层在冲突时回退查询已存在行。
4. **业务主键统一由 Python 层 uuid4 生成**。
   - `raw_artifacts.artifact_id` 与 `source_records.source_id` 均使用模型 `default=uuid.uuid4`；不使用 DB 端 `gen_random_uuid()` server_default（真实 DB 中 `artifact_id` 的 `column_default` 为空）。
   - 曾临时引入 server_default 排查 psycopg 空字节报错；该报错真正根因是 storage_key CHECK 对 `chr(0)` 的求值（见第 13 条），**与 UUID 参数绑定无关**，故回滚该临时改动。
5. **安全 URL 获取采用受限 fetcher，不使用通用爬虫**。
   - httpx AsyncClient：`follow_redirects=False`，手动最多 5 次重定向，**每次重定向后重新执行 `is_url_allowed` 域名校验**；`trust_env=False` 不使用代理环境变量；`Content-Length` 预检与流式读取**双重大小上限**；仅接受 HTTP 2xx 与 `application/pdf`。
6. **下载/上传期间不持有 AsyncSession**。
   - 网络 I/O 与文件 I/O 发生在数据库事务之外；数据库写入收敛在短事务内，避免长连接占用。
7. **来源 URL 必须通过 Source Registry 域名校验**。
   - `source_url` 必须满足 Provider `allowed_domains` 的 `is_url_allowed`；URL 导入的每次跳转同样受限。
8. **Provider 能力校验前置**。
   - 登记来源要求 Provider 具备 `company_announcement` / `issuer_ir` / `document_download` 之一，且 `enabled` 为真。
9. **replay 语义按 (provider_key, source_url, artifact_id) 判定**。
   - 完全相同时返回已存在记录，HTTP 200 + `Source-Replayed: true`；不同 URL 同内容则新建来源记录并共享同一 artifact。
10. **阶段 2B.1 不解析 PDF 正文**。
    - 只归档字节与登记来源；不创建 DocumentChunk、EvidenceCard、Claim、Report，不接入 Embedding/Chroma。
11. **阶段 2B.1 不主动执行外网请求**。
    - 不抓取公告、不同步公司目录、不轮询任何外部服务；URL 导入仅在用户显式调用接口时发生。
12. **日志与错误输出脱敏**。
    - 不输出完整 URL query、响应正文、绝对路径、数据库 URL、Provider 完整 allowed_domains。
13. **storage_key 的 CHECK 约束不使用 `chr(0)` 检查空字节**。
    - PostgreSQL 文本类型在 bind 阶段即拒绝含 NUL 的参数（`ProgramLimitExceeded: null character not permitted`，SQLSTATE 54000），因此空字节防护由数据库层天然提供；
    - `chr(0)` 在 CHECK 表达式中求值**自身即抛出同样的 54000 错误**，导致任何提供全部列的 INSERT 都失败——该检查冗余且有害，必须移除；
    - 该根因与 UUID 参数绑定无关，主键契约见第 4 条。
14. **SourceRecord 保存 Provider 能力快照**。
    - `source_records.provider_capabilities_snapshot`（JSONB NOT NULL + `jsonb_typeof = 'array'`）在登记时写入**获取当时的完整能力列表**（稳定排序的字符串数组）；
    - 历史记录不随 Provider 后续策略修改而变化（快照不可变），与 `authority_tier_snapshot`、`critical_claim_eligible_snapshot` 同一契约。
15. **raw_storage 是 readiness 第 5 项检查**。
    - `/health/ready` 返回 `configuration`、`database`、`chroma`、`checkpoint`、`raw_storage` 五项；raw_storage 失败返回 `503`；
    - `check_ready()` 在根目录创建并 fsync 随机字节探测文件后删除，不产生 DB 记录、不修改已有归档；失败时仅记录异常类型，不输出绝对路径；
    - `/health/live` 不探测任何依赖。
16. **测试级真实网络隔离**。
    - source ingestion 测试模块以 autouse fixture 将 `httpx.AsyncHTTPTransport.handle_async_request` 替换为抛 `AssertionError("real external HTTP is forbidden in tests")`；
    - httpx.MockTransport（自带 handle_async_request）与 FastAPI TestClient（ASGI transport）不受影响；PostgreSQL/Chroma 本地测试不受影响；
    - 测试 helper 不再允许 `fetcher=None` 回退到真实 fetcher——默认注入**永不让步的 MockTransport**，URL 导入忘记注入即立即失败。
17. **Docker 原始归档 volume 以非 root 用户运行**。
    - backend 镜像以 `appuser`（uid 10001）运行；`/app/data/raw` 在镜像内预创建并 `chown appuser`，named volume 首次挂载时继承所有权，确保容器内可写；
    - readiness 的 volume 可写性由 `check_ready()` 探测覆盖。

## 后果

- Source → Evidence → Claim 证据链的 **Source 输入阶段**具备可追溯、可去重的归档基础。
- 文件系统存储与数据库登记分离，后续可平滑迁移到对象存储（仅改 LocalRawArtifactStore 实现）。
- 测试覆盖：存储层、fetcher 层、service 层、API 层与集成层合计 326 项（265 单元 + 61 集成）；URL 导入测试全部使用 httpx MockTransport，不访问外网。
- 遗留边界：PDF 正文解析、DocumentChunk 与 Evidence 建模在后续阶段实现。
