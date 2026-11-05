## 这个 PR 做了什么

<!-- 一两句话。说明动机，不逐条复述 diff -->

## 类型

- [ ] 修复
- [ ] 新功能
- [ ] 重构（不改变行为）
- [ ] 文档
- [ ] 数据库迁移

## 验证

<!-- 贴命令与输出，不接受「本地试过」 -->

```bash
python -m compileall -q src/app
python run.py check_db
python run.py eval --category violation
```

```
（粘贴关键输出）
```

## 红线自查

- [ ] 没有新增付款、开票、确认、审批、删除类工具
- [ ] 模型仍无法确认草稿（`confirm_draft` 未注册为工具）
- [ ] `build_review_draft` 的前置条件没有放宽
- [ ] 没有输出密钥或真实业务数据

## 影响与回滚

- 是否需要迁移：
- 是否有对应 `sql/down/` 脚本：
- 回滚方式：
