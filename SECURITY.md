# Security

这个项目连接聊天入口、Claude Code、公开互联网与本地 Obsidian Vault，应按高权限自动化工具管理。

## 建议的部署边界

- 为 cc-connect 使用独立 workspace，不要把主目录设为 `work_dir`。
- 平台配置必须使用 `allow_from` 限制到可信用户。
- 除非确有管理命令需求，不配置 `admin_from`；不要使用 `admin_from = "*"`。
- 只有在无人值守导入确有需要时才启用 `bypassPermissions`。
- cc-connect 令牌只放在其配置文件或环境变量中，不要提交到本仓库或 Obsidian。
- 不要导入包含访问令牌、临时签名或私密查询参数的 URL；默认抓取流程会把 URL 发送给公共 Jina Reader 服务。
- 定期备份 Vault，并保留 `_import-log.md`。

## 已实现的保护

- 只接受 HTTP(S) URL，拒绝 URL 中的用户凭据。
- 拒绝 localhost、私网、链路本地和其他非公开 IP，包括每次重定向。
- 域名解析后把请求固定到已验证的公开 IP，降低 DNS rebinding 风险。
- 限制响应体大小和重定向次数。
- 网页正文只作为不可信内容分析，不执行其中的指令。
- 所有路径限制在 Vault 内，拒绝绝对路径、路径穿越和越界软链接。
- 提交前查重、dry-run、加锁；笔记、日志和索引通过事务日志恢复。
- 安装器拒绝符号链接目标，覆盖前备份，卸载前校验摘要。

## 报告漏洞

请通过 GitHub 仓库的 Security 页面私下报告漏洞，不要在公开 Issue 中包含令牌、用户 ID、Vault 内容、真实配置或可利用细节。

报告时请提供受影响版本、最小复现步骤、预期/实际结果，以及已经脱敏的日志。不要上传你的 `~/.cc-connect/config.toml`。
