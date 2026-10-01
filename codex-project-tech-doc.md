# Project Technical Documentation

## Overview

基于 FastAPI、SQLAlchemy AsyncIO 与 PostgreSQL 的模块化后端。认证模块提供注册、邮箱验证、登录、refresh token 轮换、退出登录和当前用户接口。

## Architecture

`users` 管理账户状态，`auth` 管理 JWT、会话与一次性 token，`rbac` 在注册时创建个人组织，`email` 通过 jobs outbox 排队发送验证邮件。模块由 `core/installed_modules.py` 注册并按依赖拓扑加载。

## Key Files and Directories

- `src/kanx_ai4s_master/modules/auth/`: 认证路由、依赖、服务、模型与设置。
- `src/kanx_ai4s_master/modules/users/`: 用户模型与服务。
- `alembic/versions/foundation_0004_users.py`: 用户表迁移。
- `alembic/versions/foundation_0006_auth.py`: 会话与一次性 token 迁移。
- `tests/test_auth_contract.py`: 无外部服务依赖的认证契约测试。
- `src/kanx_ai4s_master/modules/workspace_files/`: 工作区文件模型、路由和模块注册。
- `alembic/versions/foundation_0008_workspace_files.py`: 工作区文件表迁移。

## Setup and Runbook

复制 `.env.example` 为 `.env`，运行基础设施和迁移后通过 `make dev` 启动完整 profile。默认 PostgreSQL 由 `compose.yml` 提供：宿主机端口 `55432` 映射到容器端口 `5432`，用户/密码为 `foundation`/`foundation`，数据库名为 `kanx_ai4s_master`。JetBrains 数据源应填写 Host `localhost`、Port `55432`、User `foundation`、Password `foundation`、Database `kanx_ai4s_master`，生成的 JDBC URL 为 `jdbc:postgresql://localhost:55432/kanx_ai4s_master`；截图中的 `5432` 和 `postgres` 是另一套默认值，不能直接用于本项目容器。后端使用的异步 SQLAlchemy URL 是 `postgresql+asyncpg://foundation:foundation@localhost:55432/kanx_ai4s_master`，由 `DATABASE_URL` 覆盖。前端默认 Origin 为 `http://localhost:3000`，需同时配置 `CORS_ALLOWED_ORIGINS` 与 `AUTH_ALLOWED_ORIGINS`。

本地注册验证邮件由 Mailpit 接收：完整 `make dev` 会启动 Mailpit、API、Temporal worker 和 jobs dispatcher；注册后访问 `http://localhost:8025`，打开主题为 `Verify your email` 的邮件并使用正文中的 `/verify-email?token=...` 链接。API 不返回明文验证 token，数据库也只保存摘要。

## Testing and Verification

认证专项检查：`uv run pytest -q tests/test_auth_contract.py`。工作区实现运行 `uv run pytest -q`，2026-09-30 共 9 项通过。全量 ruff/mypy 仍有既有 AI 模块问题；工作区路由支持分页、搜索、类型过滤、软删除、组织权限和 mindmap v2 JSON 校验。

## Current Decisions and Conventions

注册密码至少 12 位，注册成功与重复邮箱使用同一 202 通用响应。用户必须验证邮箱后才能登录。access token 由响应返回；refresh token 使用 `/auth` 路径的 HttpOnly cookie，CSRF token 使用可读 cookie 并要求同值 `X-CSRF-Token`。当前用户接口为 `GET /users/me`。

工作区表 `workspace_files` 以 `organization_id` 隔离数据，`deleted_at` 实现软删除；权限为 `workspace_files:read/write/delete`。路由为 `/organizations/{organization_id}/workspace-files`，列表不返回 content，详情和 PATCH 支持 mindmap 内容。新建 mindmap 的 v2 JSON 根节点文本使用去除首尾空白后的文件名。

Mindmap 数据直接存储在 PostgreSQL 的 `public.workspace_files` 表中：`file_type='mindmap'`，结构化文档保存在 `content` JSON 字段，文件名保存在 `name`，所属组织保存在 `organization_id`。前端打开文件时通过 `GET /organizations/{organization_id}/workspace-files/{file_id}` 读取，编辑后通过 `PATCH` 更新 `content`；MinIO 的 `storage` 模块用于其他上传文件，不承载当前 mindmap JSON。

## Known Issues and Follow-ups

`collaboration` 模块提供群组、成员、邀请和通知 API。组织使用 `kind=personal|group`；群组固定 `owner/admin/member` 角色，邀请 token 只保存 SHA-256 摘要并默认 7 天有效。`notifications` 支持未读计数、批量已读和软删除；群组文件继续按 `organization_id` 隔离。

用户搜索接口 `GET /users/search` 的 `q` 参数最小长度为 1，支持邀请成员时使用单字符查询。

仓库全量 `uv run pytest` 会在未启动本地 Temporal（`localhost:7233`）时于应用 lifespan 失败；认证专项测试不依赖 Temporal 或 PostgreSQL。完整端到端认证仍需 PostgreSQL、迁移、jobs dispatcher 与 SMTP/Mailpit。

数据库连接验证可运行 `docker compose -f compose.yml ps`、`docker exec kanx-ai4s-master-postgres-1 pg_isready -U foundation -d kanx_ai4s_master`，需要建表时在后端目录执行 `make migrate`。若改用外部 PostgreSQL，至少同步修改 `.env` 中的 `DATABASE_URL`、`DATABASE_USER`、`DATABASE_PASSWORD`、`DATABASE_NAME` 和 `DATABASE_HOST_PORT`，并确保 URL 与前四项一致；不要把真实密码提交到 Git。
