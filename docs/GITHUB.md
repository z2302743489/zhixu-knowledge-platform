# 发布到 GitHub

项目仓库：[z2302743489/zhixu-knowledge-platform](https://github.com/z2302743489/zhixu-knowledge-platform)，默认可见性为私有。仓库简介与 README 使用中文。

以下为将源码发布到其他账号或仓库时可参考的步骤；当前仓库的日常更新只需提交并推送新增改动。

1. 在 GitHub 创建空仓库，第一次可以先设为 private。不要额外创建 README，以免首次推送产生无关合并。
2. 在此项目目录初始化并提交源码：

```sh
git init -b main
git add .
git diff --cached --stat
# 确认没有 .env、data/、.venv、node_modules、企业文件或私密日志
git commit -m "feat: 完成企业知识导入、混合检索与可追溯问答"
git remote add origin https://github.com/YOUR_ACCOUNT/zhixu-knowledge-platform.git
git push -u origin main
```

将 `YOUR_ACCOUNT` 替换为自己的账户。仓库里包含 CI：前端构建、Python 自动化测试、数据库迁移和前后端镜像构建。推送后观察 Actions，尚未运行的 CI 不能标为绿色通过。

建议仓库描述：

> 企业知识库与 RAG 问答平台，支持文档导入、结构化分块、混合检索、来源引用与多服务商模型配置。React / FastAPI / LangChain / Qdrant。

建议标签：`rag`、`langchain`、`fastapi`、`react`、`knowledge-base`、`qdrant`。

交付源码包排除了环境配置、真实文件、数据库、缓存、依赖目录和 Git 元数据。原型时期的截图和验收记录属于历史阶段；当前功能与边界以 README 和 docs/TESTING.md 为准。
