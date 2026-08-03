# 从零部署V1.0.2

本版本不需要OpenAI API Key，不需要在GitHub添加`OPENAI_API_KEY`，也不会调用额外付费模型API。

## 第一次部署

1. 下载并解压V1.0.2工程包。
2. 在Codex中打开内层项目文件夹。
3. 打开`prompts/codex/CODEX_FIRST_DEPLOY_PROMPT.md`，复制全部内容给Codex。
4. 允许Codex访问当前项目目录、运行终端、访问GitHub和创建分支/PR。
5. GitHub要求网页登录时，登录`Lurek-st`并授权目标仓库。
6. Codex必须返回分支、Commit SHA、草稿PR URL和测试结果。
7. 打开PR，检查V0 Excel/CSV/旧脚本仍在，且没有API Key或启用的模型API Workflow。
8. 确认测试通过后合并PR。

## 更新ChatGPT任务

使用`prompts/global-scheduled-task-v1.0.2.md`作为唯一天气市场计划任务提示词。时间为北京时间周一至周六07:00。

部署前修复版新增了明确region/date周清单和`calendar_source_ids`。合并PR后
必须用仓库中的最新提示词更新现有ChatGPT任务；旧V1.0.2备份缺少这些字段，
其周包无法满足严格一致性校验。

- 周二至周六：每日采集；
- 周一：生成上一完整周的Codex周包。

旧的第二个天气任务必须暂停，防止重复采集和占用任务名额。

## 不需要做的事

- 不创建OpenAI API Key；
- 不设置`OPENAI_API_KEY` GitHub Secret；
- 不开启GitHub Workflow写权限；
- 不运行任何自动模型采集Actions。

GitHub Actions只对PR和提交执行测试，权限为`contents: read`。
