# 提交数据格式

## 新建

```json
{
  "current_url": "与 plan.current_url 完全一致",
  "action": "new",
  "relative_path": "知识库/AI/Agent/记忆管理/写入与更新/示例主题.md",
  "title": "文章标题",
  "article_date": "2026-08-13",
  "type": "实践案例",
  "tags": ["agent", "workflow", "tool-use", "实践案例"],
  "top_level": "AI",
  "second_level": "Agent",
  "insight": "不超过30字的核心结论"
}
```

新建 `relative_path` 须以 `知识库/` 开头，其后为 2–4 级主题目录。`top_level`、`second_level` 分别对应前两级主题，此处为 `AI`、`Agent`。提交器会生成 frontmatter，并在文件冲突时自动选择 `-2` 后缀。

## 追加

```json
{
  "current_url": "与 plan.current_url 完全一致",
  "action": "append",
  "relative_path": "知识库/AI/Agent/记忆管理/写入与更新/已有笔记.md",
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
@KI_PYTHON@ -B .claude/skills/obsidian-knowledge-import/scripts/ki_import.py finish --plan-file <artifacts.plan_file>
```

finish 自动读取本次绑定的 analysis/body 文件，先 dry-run，再事务提交；两阶段结果分别保存在 commit-dry.json 和 commit.json。最终只回复 message。禁止编辑脚本生成的笔记、日志或索引来规避验证错误。
