# 每周一操作手册

## 周一08:00左右

1. 打开ChatGPT中的“全球天气市场采集与周度Codex交接”。
2. 找到最新周一输出。
3. 从`BEGIN_CODEX_WEEKLY_IMPORT_PACKAGE`开始，一直复制到`END_CODEX_WEEKLY_IMPORT_PACKAGE`。
4. 打开Codex，并进入`weather-stock-china-study`本地项目或已连接仓库。
5. 新建任务，粘贴完整周包。
6. Codex应自动保存周包、校验、导入、运行测试、创建分支并打开草稿PR。
7. Codex若只分析而没有创建PR，追加发送`prompts/codex/CODEX_WEEKLY_IMPORT_FALLBACK.md`中的文字。

## 检查Codex结果

必须有：

- 分支名；
- Commit SHA；
- 草稿PR URL；
- accepted；
- skipped_identical；
- revised；
- rejected；
- pytest和数据校验结果。

## 检查PR

重点查看：

- `inbox/raw/YYYY-Www.md`保留原始周包；
- 成功周包复制到`inbox/processed/`；
- 不完整或含拒绝项的周包复制到`inbox/partial/`并生成补录请求；
- `data/raw`新增对应区域日期；
- `data/audits`存在周度导入报告；
- 拒绝项进入`data/rejected`；
- 旧Excel、CSV和旧生成脚本没有被删除；
- 没有密钥、`.env`、缓存或无关文件。

## 冲突处理

若已有同日期记录且内容不同，Codex必须默认拒绝。先比较来源和字段，再决定是否允许：

```bash
python scripts/import_weekly_package.py <周包文件> --allow-revision
```

只有明确确认新数据更可靠时才能建立新revision。

## 缺失处理

周包出现`WEEKLY_AGGREGATION_INCOMPLETE`时：

1. 不要强行要求Codex把缺失数据补成完整；
2. 先让Codex导入完整且合格的记录；
3. 根据周包`unresolved`或仓库审计生成补录请求；
4. 将补录请求发给ChatGPT；
5. 下一次交给Codex单独补录。

周包清单必须使用`expected_records`、`found_records`和
`unresolved_records`明确列出region/date。合并本次修复版后，需要同步更新
ChatGPT计划任务提示词；旧提示词只提供数量清单，不能满足严格集合校验。
