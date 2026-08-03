# Codex周度导入备用提示词

当ChatGPT周包内已经包含`CODEX_OBJECTIVE`和`CODEX_REQUIRED_STEPS`时，优先执行周包自身指令。本文件仅用于Codex没有正确继续时补充：

请把我随后粘贴的完整`BEGIN_CODEX_WEEKLY_IMPORT_PACKAGE`至`END_CODEX_WEEKLY_IMPORT_PACKAGE`内容保存为周包文件，并严格运行仓库的`validate_transport_export.py`和`import_weekly_package.py`。不要手工抄写JSON，不要自行猜测缺失数据，不要直接推送main或合并PR，不要调用OpenAI API。完整包进入`inbox/processed`；不完整或含拒绝项的部分包进入`inbox/partial`并生成补录请求。完成后给出分支、Commit SHA、草稿PR URL、import_status、接受/跳过/修订/拒绝清单和测试结果。
