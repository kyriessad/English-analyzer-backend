# English-analyzer-backend Project Rules

## 项目性质

FastAPI 后端，服务微信小程序 English-study-miniapp。

## 常用目录

- app/routers/reviews.py — 复习 session、overview、history 相关接口
- app/routers/cards.py — 卡片 CRUD
- tests/test_reviews_phase2_api.py — 复习相关测试

## 首页统计语义

首页"今日任务 / 今日已完成"采用 unique card count：

- 今日任务 = 当前今日建议任务的有效唯一卡片数
- 今日已完成 = 当前今日任务中已完成反馈的有效唯一卡片数
- 不统计已删除卡片
- 不按回炉次数累计
- 不使用 review session dynamic steps
- ReviewLog 可以存在孤儿历史，但首页 overview 不应统计已删除卡片
- completed_suggested 统计必须排除已删除卡片和孤儿 ReviewLog
- is_all_done 不应把首页 dashboard 计数覆盖为 0

## 强制边界

- 不要修改数据库 schema，除非用户明确要求。
- 不要改选卡逻辑、回炉逻辑，除非任务明确涉及。
- 不要改接口字段语义，除非用户明确要求。
- hotfix 默认只改用户指定模块。
- 不要跨仓库联动大改，除非任务明确要求。

## 验证命令

- `python -m pytest tests/ -q`
- `git diff --check`
- `git status --short`

## Git 规则

- 不要自动提交，除非用户明确要求。
- 提交前必须输出修改文件、验证结果、git status。
