# 提交数据格式

## 新建

```json
{
  "current_url": "与 plan.current_url 完全一致",
  "action": "new",
  "relative_path": "AI/Agent/工程实践/示例主题.md",
  "title": "文章标题",
  "article_date": "2026-08-13",
  "type": "实践案例",
  "tags": ["agent", "workflow", "tool-use", "实践案例"],
  "top_level": "AI",
  "second_level": "Agent",
  "insight": "不超过30字的核心结论"
}
```

新建 `relative_path` 至少应包含顶层和二级目录。提交器会生成 frontmatter，并在文件冲突时自动选择 `-2` 后缀。

## 追加

```json
{
  "current_url": "与 plan.current_url 完全一致",
  "action": "append",
  "relative_path": "AI/Agent/工程实践/已有笔记.md",
  "title": "可填写本次文章标题",
  "article_date": "2026-08-13",
  "type": "实践案例",
  "tags": ["agent", "workflow", "实践案例"],
  "top_level": "AI",
  "second_level": "Agent",
  "insight": "不超过30字的补充洞察"
}
```

追加时，提交器以原笔记的 `title/date/type/tags` 为索引基准；本 JSON 的新 tags 会与旧 tags 合并。

## 命令

```bash
python3 .claude/skills/obsidian-knowledge-import/scripts/ki_import.py commit \
  --plan-file <plan.json> \
  --analysis-file <analysis.json> \
  --body-file <body.md> \
  --dry-run \
  --output <commit-dry.json>
```

dry-run 成功后，去掉 `--dry-run` 并改用新的输出文件。禁止编辑脚本生成的笔记、日志或索引来规避验证错误。
