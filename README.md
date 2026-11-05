# 知识内容整理与入库（/ki）

将公开网页与微信公众号文章整理为按主题组织的 Obsidian 知识笔记，支持 URL 查重、内容更新与相关主题追加，同步维护知识索引和导入日志。

当前 Agent 负责内容理解、核心知识提炼与笔记写作；本项目脚本负责内容获取、去重、校验和事务保存。单篇处理不依赖微信 Bot，现有多链接批量分发使用 CC Connect 定时任务。

## 核心功能

- /ki、/knowledge-import、/导入 三个入口；支持空格或换行分隔多链接。
- 微信链接自动触发；讨论技能、展示故障或明确不要导入时不执行。
- 安静执行，最后返回一行中文结果；批量先排队，再逐条返回完成结果。
- URL 精确查重、明确更新、相关主题追加、来源与可核对作者记录。
- 笔记、知识总索引.md、导入总日志.md 同一事务提交，保留锁、dry-run 与失败回滚。
- 独立运行目录，成功后清理，失败保留诊断。
- 全部共享模块随包提供，不依赖作者电脑上的项目或知识库。

## 宿主与运行依赖

| 项目 | 要求 |
| --- | --- |
| 系统 | macOS、Linux 或 WSL；原生 Windows 不支持所用 Unix 文件锁 |
| Python | 3.9+，本版本本机验证为 3.12；运行代码仅依赖标准库 |
| 网络工具 | curl，以及访问目标公开网页的网络条件 |
| Agent | 能读取 Skill、读写本地文件、执行脚本并完成知识分析 |
| Obsidian | 已存在且可写的 Vault 目录；不要求打开 Obsidian 应用 |
| CC Connect | 现有多链接批量排队需要；单篇抓取与提交本身不需要 |

### 宿主接入范围

现有安装器面向 Claude Code 的 .claude 目录、斜杠命令与自动路由钩子；可通过 CC Connect 接入聊天平台。微信 Bot 只是可选消息入口，微信公众号文章通过 URL 获取。

单篇流程可由具备上述能力的 Agent 驱动，但 Codex 等其他宿主仍需按其技能发现机制配置入口、解释器和工作目录。当前安装器没有通用宿主安装模式，不能把脚本可迁移理解为下载后即可在任意 Agent 中使用。

多链接请求仍会调用 cc-connect timer，为每篇文章创建独立任务。保留该功能时须配置可用的 CC Connect 服务及 Agent；本文不宣称批量流程已脱离 CC Connect。模型账号、服务与聊天平台令牌由用户自行配置。

## 下载和安装

从本仓库 Code → Download ZIP，或 Releases 下载 v1.1.0 源码。解压并进入仓库根目录。指定本机真实存在的 Python 绝对路径：

~~~bash
KI_PYTHON="/absolute/path/to/python3"
"$KI_PYTHON" -B scripts/manage.py install \
  --workspace "$HOME/cc-connect-workspace" \
  --vault "$HOME/Documents/MyVault" \
  --python "$KI_PYTHON"
"$KI_PYTHON" -B scripts/manage.py check \
  --workspace "$HOME/cc-connect-workspace"
~~~

workspace 和 Vault 必须已存在。安装器在 workspace/.claude/ 中安装命令、skill、本地解释器路径和自动路由 hook，并保留其他设置。安装后重新启动对应会话，让 Claude Code / cc-connect 加载命令与 hook。

默认使用统一模式：笔记在知识库/主题目录，根目录维护知识总索引.md 和导入总日志.md。空 Vault 自动创建两份空总览；已有正确总览直接使用。非空 Vault 缺少总览时停止，不自动迁移笔记。

v1.0 用户继续原来的分离索引布局，可在 install 后追加 --legacy-layout。该模式继续使用 _import-log.md、_knowledge-index.md 和原主题目录；安装器会生成对应说明。

## 使用方式

~~~text
/ki https://example.org/article
/ki https://example.org/one https://example.org/two
/ki --single https://example.org/article
/ki --force https://example.org/article
~~~

也可用 /knowledge-import 或 /导入。直接发送微信文章链接时，hook 仅提取当前消息中的微信 URL；触发后仍遵守“不导入”的语义边界。

重复链接返回原路径，紧接着回复“更新”可更新对应笔记。排队、抓取和草稿完成不代表导入成功，以脚本最终结果为准。

## 配置与输出

Vault 优先级：KI_VAULT → workspace/.claude/ki-config.json → ~/Documents/obsidian。路径由用户本地配置，不提交 GitHub。

## 仓库结构

~~~text
command/                         三个斜杠命令入口
skill/obsidian-knowledge-import/
  SKILL.md                       触发、流程、输出和停止条件
  references/note-schema.md       笔记格式、标签和分类
  references/analysis-schema.md   提交 JSON 合同
  agents/openai.yaml              UI 元数据
  scripts/ki_import.py            确定性导入流程
  scripts/auto_route.py           微信自动路由
  scripts/unified_store.py        统一索引与日志
  scripts/unified_catalog.py      索引与日志的只读一致性检查
  scripts/topic_taxonomy.py       主题分类规则
scripts/manage.py                安装、检查和卸载
tests/                           抓取、写入、路由和安装回归
~~~

入口只保留实际决策所需说明，格式细节按需加载；写笔记前读取两个 schema。

## 检查、升级和卸载

重复 install 会备份替换的同名文件。check 校验文件、解释器、配置、hook 和参数处理，不新增笔记。

~~~bash
"$KI_PYTHON" -B scripts/manage.py check --workspace "$HOME/cc-connect-workspace"
"$KI_PYTHON" -B scripts/manage.py uninstall --workspace "$HOME/cc-connect-workspace"
~~~

卸载恢复安装前的同名文件，只移除本技能 hook，保留其他设置、后续修改以及全部 Vault 笔记/总览。安装文件已修改时默认拒绝删除；明确丢弃修改才用 uninstall --force。

## 回归测试

~~~bash
PYTHONPATH="$PWD/skill/obsidian-knowledge-import/scripts" \
  "$KI_PYTHON" -B -m unittest discover -s tests -p 'test_*.py' -v
~~~

覆盖抓取边界、参数、重复/更新/追加、并发、事务回滚、来源与作者、自动路由、独立安装、升级和卸载。回归使用临时 Vault 与合成文章，不需要私人知识库、模型或真实 timer。GitHub Actions 使用相同入口。

## 限制与隐私

仅处理用户本次提供的公开 HTTP(S) URL，拒绝本机/私网目标并限制响应体。正文视为不可信材料，不执行其中命令。生成内容经 schema 校验与脚本提交。

本包不含 Cookie、API Key、聊天令牌、私人笔记、导入记录或机器专属环境文件。失败诊断可能含文章和来源 URL，应保留在本地，参见 SECURITY.md。

v1.1.0 使用脱敏后的新历史。旧 main 与 v1.0.0/v1.0.1 引用不再保留；旧克隆应重新下载，避免重新推入旧历史。第三方副本和 GitHub 缓存不在仓库维护者的完全控制范围内。

## 许可证

采用 [MIT 许可证](LICENSE)。

