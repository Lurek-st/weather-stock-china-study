# 天气与中国股票市场关系 · 实践研究

个人实践研究：每日记录北京 / 上海 / 深圳的主观天气评分（1–10）与对应股市情绪评分，长期观察两者是否存在统计关联。

## 快速开始

```bash
git clone https://github.com/Lurek-st/weather-stock-china-study.git
cd weather-stock-china-study
pip3 install -r requirements.txt
python3 build_research_workbook.py   # 可选：生成/重建 Excel 模板
```

日常只需用 Excel / WPS 打开 `天气与股市关系研究记录表.xlsx` 在「每日数据」续填，**不必每天运行 Python**。

## 数据表在哪？（最重要）

打开 **`天气与股市关系研究记录表.xlsx`** 后：

1. **第一个标签页就是「每日数据」**（不是「使用说明」）
2. **第 1 行 = 表头，第 2 行起 = 每天一行**

| 日期 | 北京天气 | 上海天气 | 深圳天气 | 北京股市 | 上海股市 | 深圳股市 | 天气均值 | 股市均值 | 备注 |
|------|:--------:|:--------:|:--------:|:--------:|:--------:|:--------:|:--------:|:--------:|:----|
| 2026-05-20 | 7 | 4 | 5 | 3 | 4 | 5 | 自动 | 自动 | … |

**股市列含义：** 北京股市 = 北证50 · 上海股市 = 上证指数 · 深圳股市 = 深证成指

若用 **Numbers / 预览** 打开，表格样式可能不明显，请换 **Excel 或 WPS**，或直接看同文件夹的 **`每日数据记录.csv`**（纯文本，一眼可见）。

## 其他标签页

| 标签页 | 内容 |
|--------|------|
| 走势图 | 折线图（随「每日数据」自动更新） |
| 统计检验 | 相关分析、H₀ 检验（α=0.05） |
| 使用说明 | 操作步骤 |

## 每日操作

在「每日数据」表**最下方空行**填写日期与 6 个 1–10 分，保存即可。不要手改「天气均值」「股市均值」列。

## 文件列表

- `天气与股市关系研究记录表.xlsx` — 主文件
- `每日数据记录.csv` — 文本版数据表（可用 Excel 打开）
- `build_research_workbook.py` — 重新生成（会覆盖 xlsx，先备份）
- `requirements.txt` — Python 依赖（openpyxl）

## V1.0.2 全球天气—市场半自动周入库

V0 Excel原型继续保留并可独立使用。仓库同时新增V1.0.2工程，用于观察北京、
上海、深圳、东京、孟买、伦敦、法兰克福和纽约的天气与主要市场指标。V1使用：

```text
ChatGPT计划任务采集
→ 周一生成自包含周包
→ Codex保存原文并校验
→ Python确定性复算评分
→ 独立分支和草稿PR
```

V1默认不调用OpenAI API，也不需要`OPENAI_API_KEY`。GitHub Actions只执行
测试和数据验证，权限为只读。

主要入口：

- `PROTOCOL.md`：研究、来源、评分和修订协议；
- `ACCEPTANCE.md`：部署、首周和20日验收标准；
- `docs/WEEKLY_OPERATIONS.md`：每周一实际操作；
- `prompts/global-scheduled-task-v1.0.2.md`：需要同步到ChatGPT任务的最新提示词；
- `scripts/import_weekly_package.py`：周包导入、拒绝留痕和部分入库；
- `schemas/`：传输、周清单和规范记录Schema。

本地验证：

```bash
python -m pip install -r requirements.txt
pytest -q
python scripts/validate_records.py tests/fixtures/valid_asia.json
python scripts/validate_transport_export.py tests/fixtures/weekly/valid_weekly_package.md
```

完整周包保存在`inbox/processed`。不完整或含拒绝项的周包保存在
`inbox/partial`并生成补录请求；所有拒绝阶段均在`data/rejected/<WEEK_ID>/`
留下结构化记录。

离线校验能够检查URL语法、来源引用闭合、交易日历来源引用和评分一致性，
但不会自动证明网页当前可访问或其内容支持对应数值。天气与市场的统计相关
不能直接解释为因果关系。

## 许可证

本项目为个人研究笔记，数据与结论不构成投资建议。
