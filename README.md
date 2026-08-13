# cc-connect Obsidian Knowledge Import

通过 cc-connect 在聊天软件中发送 `/ki <网页链接>`，让 Claude Code 抓取、分析、查重、分类，并把结构化笔记安全写入本机 Obsidian Vault。

仓库包含可安装的 Claude Code Skill、`/ki` 自定义指令、确定性提交脚本，以及安装、检查和卸载工具。安装器不会读取或复制你的 cc-connect 令牌。

## 能做什么

- 单链接：抓取网页，生成结构化中文笔记并写入 Obsidian。
- 多链接：交给 cc-connect timer 逐条排队；每条任务使用独立 `ki-run-*` 工作目录，避免并发污染。
- 重复保护：按来源 URL 查重；回复“更新”后才允许覆盖原笔记。
- 相似内容追加：模型判断主题重叠度，选择新建或追加到已有笔记。
- 一致性提交：笔记、`_import-log.md`、`_knowledge-index.md` 一起更新；失败自动恢复。
- 安全边界：只抓取公开 HTTP(S) 网页，拒绝本机、私网和链路本地地址，限制响应大小，并把网页正文视为不可信数据。

## 系统要求

- macOS、Linux 或 WSL。当前版本使用 Unix 文件锁，不支持原生 Windows Python。
- Python 3.9 或更高版本，仅使用标准库。
- `curl`。
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview)。
- [cc-connect](https://github.com/chenhg5/cc-connect)。本项目在 cc-connect 1.3.3、Claude Code 2.1.195、Python 3.9 上通过测试。
- 一个已经存在的 Obsidian Vault 目录；Obsidian 应用本身不是脚本运行的必要条件。

## 1. 安装并配置 cc-connect

### 安装

任选一种官方方式：

```bash
npm install -g cc-connect
```

macOS 也可以使用 Homebrew：

```bash
brew install cc-connect
```

确认命令可用：

```bash
cc-connect --version
claude --version
python3 --version
curl --version
```

### 创建专用 workspace

建议为机器人创建独立工作目录，不要把整个主目录或包含私密代码的仓库作为 `work_dir`：

```bash
mkdir -p "$HOME/cc-connect-workspace"
```

### 配置项目与聊天平台

先生成官方注释配置，再编辑它：

```bash
mkdir -p "$HOME/.cc-connect"
cc-connect config example > "$HOME/.cc-connect/config.toml"
```

然后在 `~/.cc-connect/config.toml` 中新建或修改一个项目，至少设置：

- Agent 类型：`claudecode`
- `work_dir`：上一步创建的绝对路径
- 一个平台：微信、Telegram、飞书、Discord、Slack 等
- 平台的 `allow_from`：只允许你自己的用户 ID

部分较新构建还提供 `cc-connect web` 图形配置界面；先在 `cc-connect --help` 中确认存在该子命令再使用。它只负责配置，不会启动机器人。本文给出的 TOML 方式可用于本项目实际测试的 cc-connect 1.3.3。

微信可先使用官方向导：

```bash
cc-connect weixin setup
```

Telegram 的最小脱敏示例见 [`examples/config.toml`](examples/config.toml)。cc-connect 按 `--config`、当前目录 `config.toml`、`~/.cc-connect/config.toml` 的顺序查找配置。

> 安全提醒：`/ki` 需要执行 Python、网络抓取和写入 Vault。无人值守场景通常需要 Claude Code 的 `bypassPermissions` 模式；它权限很高，只应配合专用 workspace、仅本人可访问的平台账号和严格的 `allow_from` 使用。不要把 `admin_from` 设为 `"*"`。

只导入真正公开的 URL，不要提交带访问令牌、临时签名或私密查询参数的链接。默认抓取流程会把 URL 发送给公共 Jina Reader 服务。

### 启动 cc-connect

前台试运行：

```bash
cc-connect
```

或安装为后台服务：

```bash
cc-connect daemon install
cc-connect daemon status
```

后台日志可用 `cc-connect daemon logs` 查看。官方完整说明见 [安装文档](https://github.com/chenhg5/cc-connect/blob/main/INSTALL.md) 和 [配置示例](https://github.com/chenhg5/cc-connect/blob/main/config.example.toml)。

## 2. 安装 `/ki` Skill

克隆仓库：

```bash
git clone https://github.com/llm-xiaoye/cc-connect-obsidian-knowledge-import.git
cd cc-connect-obsidian-knowledge-import
```

把下面两个路径替换为你的真实绝对路径：

```bash
python3 scripts/manage.py install \
  --workspace "$HOME/cc-connect-workspace" \
  --vault "$HOME/Documents/My Obsidian Vault"
```

安装器会：

1. 把三个指令安装到 `<workspace>/.claude/commands/`；
2. 把 Skill 安装到 `<workspace>/.claude/skills/obsidian-knowledge-import/`；
3. 在 `<workspace>/.claude/ki-config.json` 保存 Vault 的绝对路径；
4. 把被替换的同名文件备份到 `<workspace>/.claude/backups/obsidian-knowledge-import/`。

运行只读自检：

```bash
python3 scripts/manage.py check --workspace "$HOME/cc-connect-workspace"
```

自检不会写入 Vault。检查通过后重启正在运行的 cc-connect：

```bash
cc-connect daemon restart
```

如果使用前台进程，则停止后重新运行 `cc-connect`。cc-connect 会自动发现 workspace 中的 `.claude/commands/*.md`，不需要额外添加全局 `[[commands]]` 配置。

## 3. 使用

在连接的聊天平台中发送：

```text
/ki https://example.com/article
```

也可以使用别名：

```text
/knowledge-import https://example.com/article
/导入 https://example.com/article
```

一次发送多个 URL 时会进入队列：

```text
/ki https://example.com/one https://example.com/two
```

如果来源 URL 已经导入，机器人会提示重复。紧接着回复：

```text
更新
```

才会覆盖该 URL 原来对应的笔记。

成功时返回类似记录：

```text
新建：
| 2026-08-13 10:30 | https://example.com/article | [AI/Agent/示例文章.md](AI/Agent/示例文章.md) | 新建 |
```

首次成功导入后，Vault 中会出现：

```text
_import-log.md
_knowledge-index.md
<分类目录>/<笔记>.md
```

## 更新与卸载

更新仓库后重新运行安装器即可；原安装会被备份：

```bash
git pull --ff-only
python3 scripts/manage.py install \
  --workspace "$HOME/cc-connect-workspace" \
  --vault "$HOME/Documents/My Obsidian Vault"
python3 scripts/manage.py check --workspace "$HOME/cc-connect-workspace"
cc-connect daemon restart
```

卸载 Skill 并恢复安装前的同名文件：

```bash
python3 scripts/manage.py uninstall --workspace "$HOME/cc-connect-workspace"
```

如果安装文件被手工修改，卸载器会停止，避免误删；确认放弃这些修改时可加 `--force`。卸载器不会删除 Vault 中已经生成的笔记。

## 故障排查

### 聊天中没有 `/ki`

1. 确认 cc-connect 项目的 `work_dir` 与安装时的 `--workspace` 完全相同。
2. 确认 `<workspace>/.claude/commands/ki.md` 存在。
3. 重启 cc-connect；后台运行时查看 `cc-connect daemon logs`。
4. 重新运行 `scripts/manage.py check`。

### 提示权限不足

`/ki` 需要执行 Bash/Python 并写 Vault。检查 Claude Code 模式及 macOS/Linux 对 Vault 目录的权限。若启用 `bypassPermissions`，务必先落实专用 workspace 与 `allow_from` 限制。

### 抓取失败

只支持公开的 HTTP(S) 网页。登录墙、付费墙、验证码、纯 PDF/图片/视频、本机地址和私网地址不会导入。脚本优先读取网页正文，并在必要时尝试 Jina Reader 和 curl 回退。

### 修改 Vault 路径

最稳妥的方式是重新运行安装器并提供新的 `--vault`。也可以设置环境变量 `KI_VAULT` 临时覆盖本地配置；该变量必须对 cc-connect 进程可见。

## 仓库结构

```text
command/                          Claude Code 自定义指令
skill/obsidian-knowledge-import/  Skill、格式约束和确定性工作流脚本
scripts/manage.py                 安装、检查、卸载
tests/                            工作流与安装器回归测试
examples/config.toml              脱敏的 cc-connect 配置示例
```

运行全部测试：

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

安全模型、漏洞报告和运维建议见 [`SECURITY.md`](SECURITY.md)。

## License

[MIT](LICENSE)
