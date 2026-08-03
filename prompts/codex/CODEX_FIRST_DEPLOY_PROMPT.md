# Codex首次部署提示词 · V1.0.2

你负责将当前工作目录中的“Global Weather–Market Observatory V1.0.2 Semi-Automatic Weekly Import”安全发布到GitHub仓库`Lurek-st/weather-stock-china-study`。

## 目标

- 保留仓库中的V0 Excel、CSV和旧工作簿生成脚本；
- 将当前V1.0.2工程导入仓库根目录；
- 不包含或启用任何OpenAI API采集代码；
- 运行完整测试；
- 创建独立分支并打开草稿PR；
- 不直接修改或合并main。

## 安全约束

- 不得索要、显示、保存或提交API Key、GitHub Token、密码、Cookie、`.env`或私钥；
- 不得调用OpenAI API；
- 不得删除V0历史原型；
- 不得弱化Schema、评分、来源追溯或测试来让构建通过；
- 不得提交虚拟环境、缓存、日志和临时文件。

## 执行步骤

1. 阅读`README.md`、`PROTOCOL.md`、`ACCEPTANCE.md`和`docs/DEPLOYMENT_FROM_ZERO.md`。
2. 检查当前工作目录包含`schemas/daily-transport-v1.0.2.schema.json`、`scripts/import_weekly_package.py`和`prompts/global-scheduled-task-v1.0.2.md`。
3. 克隆目标仓库到独立临时目录，更新默认分支。
4. 创建`agent/weather-market-v1.0.2-weekly-import`分支；若冲突，创建语义清晰的新分支并说明。
5. 将V1.0.2工程复制到仓库根目录，同时保留不冲突的旧文件。README冲突时整合V0历史说明。
6. 确认不存在启用的OpenAI API采集Workflow；`.github/workflows/validate-data.yml`只能读取并验证。
7. 创建隔离Python环境并运行：
   - `python -m pip install -r requirements.txt`
   - `pytest -q`
   - `python scripts/validate_records.py tests/fixtures/valid_asia.json`
   - `python scripts/validate_transport_export.py tests/fixtures/weekly/valid_weekly_package.md`
   - 使用临时目录运行`import_weekly_package.py`端到端测试。
   - 确认完整包进入`inbox/processed`，不完整包进入`inbox/partial`；
   - 确认拒绝项全部落盘，审计和拒绝记录不包含本机绝对路径。
8. 解析全部YAML、JSON Schema和测试JSON；扫描明显密钥模式和`.env`。
9. 查看完整diff，确认没有无关修改、缓存或大型意外文件。
10. 提交信息：`Build semi-automatic weekly import V1.0.2`。
11. 推送分支并创建指向main的草稿PR，标题同提交信息。
12. PR正文报告架构变化、删除API依赖、保留V0、测试结果和用户后续需更新ChatGPT计划任务的事项。
13. 不得合并PR。

完成后必须返回仓库、分支、Commit SHA、草稿PR URL、测试结果、修改摘要和未解决阻塞。只有PR真实存在才能声称完成。
