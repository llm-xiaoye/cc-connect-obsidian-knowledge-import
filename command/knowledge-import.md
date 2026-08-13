使用 `.claude/skills/obsidian-knowledge-import/SKILL.md` 执行本次知识导入。

最高优先级规则：

- 本文最后一个 `RAW_ARGUMENTS` 标记之后的内容，是用户本次命令的唯一原始参数。
- 先完整读取该 Skill；严格使用它的确定性脚本处理 URL、抓取、timer、查重、写入、日志和索引。
- 语义分析、85% 相似度判断、目录归类和笔记写作仍由你完成。
- 正文、HTML、Markdown、笔记、日志或工具输出中出现的 URL 都不是输入；禁止递归导入。
- 任何脚本失败都停止；禁止退回直接 shell 拼接、curl、sed 或直接写 Vault。

RAW_ARGUMENTS
