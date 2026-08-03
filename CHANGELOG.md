# Changelog

## V1.0.2 pre-deployment repair

- Replaced text-only OpenAI API scanning with active Python/workflow detection.
- Added explicit weekly region/date manifest sets and ISO-week consistency checks.
- Added calendar source references and documented offline verification boundaries.
- Preserved every rejection stage under `data/rejected/<WEEK_ID>/`.
- Added partial-package state, backfill requests, safe source paths, and expanded tests.

## 1.0.2 · Semi-Automatic Weekly Import

- 移除OpenAI Responses API和`OPENAI_API_KEY`依赖。
- 停用所有模型API定时采集GitHub Actions。
- 合并为一个ChatGPT全球计划任务：周二至周六每日采集，周一生成Codex周包。
- 新增紧凑Daily Transport Export Schema。
- 新增周包提取、校验、转换、幂等导入、拒绝记录和审计脚本。
- GitHub Actions仅保留只读测试与数据校验。
- 明确GitHub为唯一正式数据源，ChatGPT任务对话为临时收件箱。
- 加入端到端周包导入测试和零额外API费用约束。

## 1.0.1

- 曾设计OpenAI API + GitHub Actions自动写入方案；因用户明确不接受额外API费用而废弃。

## 1.0.0

- 初始全球8城市研究协议、固定评分、Schema与基线分析。
