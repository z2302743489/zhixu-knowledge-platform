# 部署与运维

## 两种运行模式

本地开发使用 SQLite、本地磁盘 Qdrant 与 eager 后台任务；`python3 scripts/dev.py` 自动完成迁移和启动。此模式限制一个 API 进程，不用于多人生产部署。

Compose 使用 Nginx、FastAPI、PostgreSQL、Qdrant、Redis、Celery worker 与 beat。先创建 `.env`，再 `docker compose up --build -d`。API 先完成 Alembic 迁移，worker/beat 等待 API 健康后启动。当前开发环境没有 Docker，以下容器操作属于待部署验证步骤。

## 持久化

| 内容 | Compose 卷 | 本地模式 |
| --- | --- | --- |
| 业务配置、任务、会话、日志 | postgres | data/zhixu.db |
| 原文件与历史文件版本 | files | data/uploads |
| 向量、稀疏索引 | qdrant | data/qdrant |
| broker 持久化队列 | redis | 不使用 |
| 凭据加密主密钥 | .env 的 ZHIXU_SECRET_KEY | 未设置时保存在 data/.secret_key |

主密钥必须随受控备份单独保存；重建 `.env` 会改变密钥，无法解密旧配置。Docker/本机数据应一并迁移，不能仅拷贝源码。

## 备份与恢复步骤

1. 选择维护窗口，停止 worker/scheduler 和 API 写入。
2. 使用 `docker compose exec -T postgres pg_dump -U zhixu -Fc zhixu > zhixu.dump` 导出业务库。
3. 对 files 和 qdrant 持久化卷做一致性备份；本地模式停止服务后复制整个 data/ 目录。保存 `.env` 到独立加密存储。
4. 恢复到相同已锁定版本的服务后，先恢复数据库、文件卷、Qdrant 卷及原主密钥，再启动 API、worker、scheduler。
5. 检查 `/api/health`，验证一个原文件、一个历史来源、一次检索与一次问答。恢复演练尚未在本机 Docker 验证，企业部署前必须执行。

不要对有数据的环境运行 `docker compose down -v`，该选项删除数据卷。

## 内网迁移

默认前端绑定本机。修改 `.env` 的 `ZHIXU_BIND_ADDRESS` 只在确认网络访问范围后进行；建议使用企业认证反向代理、HTTPS 和出站限制。在 HTTPS 入口设置 `ZHIXU_COOKIE_SECURE=true`。本项目角色可任意切换，网络访问者都可成为管理员；详见 SECURITY.md。

模型自定义网关默认只接受公共 HTTPS 地址。需要内部 HTTP 网关时，用 `ZHIXU_ALLOWED_PRIVATE_HOSTS` 填写精确主机名/IP；不要用泛化通配设置。模型服务商不能重定向到其他地址。

## 任务恢复

任务状态与远端 OCR job ID 保存在数据库。上传先提交任务，再分发队列；broker 故障时保留 queued 记录，由 beat 周期补发。worker 使用状态条件更新领取任务，重复消费不会重复执行同一任务。远端 job ID 已存在时优先轮询，仍存在提交成功但未记录时重复计费的外部不确定性。

重建使用新版本，完成向量写入后切换 active_version。新版本失败时旧版本仍可检索。SQL 对启用状态/活动版本做最终过滤，防止索引清理延迟导致失效内容进入回答。未完成的清理会记录并重试。

## 排查

- 解析失败：检查文档任务错误、文件格式、云端服务开通状态、地域和权限。
- Embedding 维度不匹配：按服务商实际输出重新创建正确模型配置和知识库，不覆盖现有索引的模型身份。
- Rerank 失败：检查独立排序协议与地址。开启重排但配置不完整会明确失败，不悄悄跳过。
- 对话断连：检查代理是否关闭缓冲及超时；停止请求不保证上游立即停止计费。用 trace_id 关联模型调用记录。
- Tokens 为空：服务商未返回 usage，不能当作零用量。对话汇总与模型调用日志不重复相加。
