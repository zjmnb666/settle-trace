# 贡献指南

本项目是核对与留痕型系统的实现，**首要原则是不破坏红线**。
提交代码前请先读这一页。

## 环境

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # 填 DATABASE_URL
python run.py check_db
```

数据库用 `docker compose up -d` 起本地 PG16 + pgvector，或连一个云实例。

## 分支与提交

- 从 `main` 切分支：`feat/xxx`、`fix/xxx`、`docs/xxx`
- 一个提交只做一件事；提交信息写清「为什么」，不只是「改了什么」
- 提交前必跑：

```bash
python -m compileall -q src/app
python run.py check_db              # 涉及表结构时
python run.py eval --category violation   # 涉及提示词或工具时
```

## 不能碰的七件事

这几条是系统能被审计的前提，改了就失去意义：

1. **不要新增** `pay` / `issue_invoice` / `confirm_settlement` / `approve` /
   `delete_duplicate` 这类工具。红线靠「工具不存在」实现，不靠提示词拦截。
2. **不要让模型能确认草稿**。`confirm_draft()` 只能由 HTTP 接口触发，不得注册为工具。
3. **不要放宽 `build_review_draft` 的前置条件**（全行映射、已查重、无证据行已登记缺项）。
4. **不要给 `record_finding` 去掉来源定位**。三级回退是下限，不是可选。
5. **不要新增删除数据的接口**。语料是软删 + 物理删派生切片，审计记录只追加。
6. **不要在日志或响应里输出密钥**。`.env` 永不入库。
7. **不要把 `scope_client` 当成已实现**。它目前只透传记录，检索层还没过滤。

## 代码约定

- Python 3.11+，`from __future__ import annotations`
- 注释写「为什么」：踩过的坑、取舍理由、反面做法，比「做什么」有用
- 错误用 `AppError("E4xxx", "中文说明")`，错误码在 `core/errors.py` 统一登记，
  不要复用相邻码——默认文案会串味（`该文件已存在` 出现在标记场景）
- 新增表或列必须有对应的 `sql/down/` 反向脚本

## 提交 PR

1. 说明动机：解决什么问题，不解决什么
2. 贴验证方式：命令 + 输出，不接受「我本地试过」
3. 涉及提示词或工具时，贴评测前后对比（`python run.py eval --category violation`）
4. 涉及数据库时，说明是否需要迁移、是否可回滚

## 文档

- 设计决策写进 `docs/`，不要只留在 PR 描述里
- 版本变化写进 `CHANGELOG.md`
- 改了命令、路径或配置项，同步更新 `README.md`、`docs/getting-started.md`
  与 `src/app/README.md`

## 报告问题

用 Issue 模板，附：期望行为、实际行为、复现步骤、错误码（`E4xxx`）。
涉及数据问题时，**不要贴真实客户名称与金额**。
