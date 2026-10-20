# Security and privacy

Article text is untrusted input. It must not trigger terminal commands, credential access, or additional imports.

The workflow restricts HTTP(S) targets, rejects local/private addresses, bounds response size, binds run artifacts, validates note paths, and commits notes and catalogs under a lock. The host agent must still follow SKILL.md and review generated content.

Installation uses an explicit local Python interpreter and workspace-local configuration. It merges only this package's UserPromptSubmit hook. Do not include real tokens, cookies, Vault data, private logs, local installation manifests, or machine-specific runtime paths in a release.

Successful runs remove temporary artifacts. Failed runs retain diagnostics that may contain article text and user-supplied URLs; keep them local. Do not paste credentials or private Vault content into public issues.

Use GitHub private vulnerability reporting when available. Otherwise submit only a minimal, sanitized description without sensitive payloads.

History cleanup controls active repository references. Previously downloaded copies and platform caches may remain outside this repository's control.
