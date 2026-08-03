# V1.0.2 验收说明

## A. 部署验收

- [ ] V1.0.2已通过独立分支和草稿PR进入仓库；
- [ ] V0 Excel、CSV和旧生成脚本仍保留；
- [ ] 仓库中没有真实API Key、Token、`.env`或私钥；
- [ ] 不存在调用OpenAI API的启用工作流；
- [ ] `pytest -q`全部通过；
- [ ] `validate_records.py`和`validate_transport_export.py`通过测试样例。

## B. 计划任务验收

- [ ] 仅有一个天气市场任务启用；
- [ ] 北京时间周一至周六07:00运行；
- [ ] 周二至周六输出一个完整Daily Transport Export；
- [ ] 周一输出一个完整Codex Weekly Import Package；
- [ ] 周包清单列出预期、找到、补录、未解决和损坏记录；
- [ ] 清单的明确region/date集合与实际记录完全一致；
- [ ] 周包无法完整生成时明确标记`WEEKLY_AGGREGATION_INCOMPLETE`。

## C. 第一周周包验收

- [ ] 从开始标志到结束标志可以一次性复制；
- [ ] 周包中的所有Daily Export均为合法JSON；
- [ ] 日期覆盖与上一完整周一致；
- [ ] 每个非空指标都有来源；
- [ ] 没有预测值冒充历史观测；
- [ ] 缺失和补录情况与每日任务记录一致；
- [ ] 没有因周包过长而截断。

## D. Codex入库验收

Codex应返回：分支、Commit SHA、草稿PR URL、接受/跳过/修订/拒绝数量和测试结果。

仓库应出现：

```text
inbox/raw/YYYY-Www.md
inbox/processed/YYYY-Www.md
inbox/partial/YYYY-Www.md
data/raw/<region>/YYYY-MM-DD.json
data/audits/weekly-import-YYYY-Www.json
```

被拒绝记录应进入`data/rejected/YYYY-Www/`，不得静默丢失。

不完整或含拒绝项的周包必须标记`import_status=partial`并进入
`inbox/partial`；只有完整成功的周包进入`inbox/processed`。审计、拒绝记录
和补录请求不得包含用户主目录或输入文件绝对路径。

来源验收验证URL语法和引用闭合，不把URL可访问性或网页内容真实性描述为
已经由程序自动证明。交易日历核对必须保存`calendar_source_ids`。

## E. 五个运行日工程试验

- 计划任务产出率：100%为目标，最低95%；
- Transport Schema/语义结果均有明确通过或拒绝原因；
- Python评分复算一致率100%；
- 来源覆盖率100%；
- 严重日期和时区错误0；
- 重复规范文件0；
- 周一Codex PR创建成功；
- OpenAI API费用0。

## F. 20个交易日正式验收

五日试验通过后冻结正式起始日期。20个交易日内不得修改城市、主要指数、评分函数、Schema或主要假设。

通过标准：

- 预期区域记录入库率≥95%；
- 强制市场字段完整率≥95%；
- 强制天气评分字段完整率≥90%；
- Schema与评分复算通过率100%；
- 非空指标来源覆盖率100%；
- 可恢复缺失补录率≥90%；
- 重复文件、严重日期错误、虚构数据均为0。

## G. 自动验收报告

先由Codex根据官方交易日历生成期望清单，例如：

```json
{"asia":["2026-08-03"],"europe":["2026-08-03"],"us":["2026-08-03"]}
```

然后运行：

```bash
python scripts/acceptance_report.py \
  --start 正式开始日期 \
  --end 正式结束日期 \
  --expected-json analysis/results/expected-region-dates.json
```

只有官方日历清单存在且所有阈值通过时，报告中的`v1_engineering_acceptance_pass`才会为`true`。
