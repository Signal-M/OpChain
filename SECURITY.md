# 安全政策（Security Policy）

## 支持版本

| 版本 | 是否安全维护 |
|---|---|
| `main` | ✅ 是 |

## 上报漏洞

**请勿通过公开 GitHub Issue 上报安全漏洞。**

请通过以下私下渠道上报，我们会尽快响应：

- GitHub Security Advisories（推荐）：在仓库页面 → **Security → Advisories → Report a vulnerability**
  直达链接：https://github.com/Signal-M/OpChain/security/advisories/new
- 或直接邮件联系维护者（见仓库 Profile）。

请尽量提供：
- 漏洞类型与影响面；
- 复现步骤（最小可复现）；
- 可能的修复建议（如有）。

我们会与你协商披露时间，避免在修复前公开细节。

## 敏感信息提醒

OpChain 在本地运行，但请务必注意：

- **不要**在 Issue / PR / 日志中粘贴密钥、API Key、`BRAIN_API_KEY`、会话凭证或个人数据。
- 运行产物目录 `runs/`、`data/`、`captures/` 已在 `.gitignore` 中排除，**请勿手动 `git add` 这些目录**。
- 连接云端模型（`BRAIN=cloud`）时，密钥请通过环境变量注入，不要写进代码或提交到仓库。
- 本项目用于自动化操作第三方应用，**请遵守目标平台的服务条款与当地法律法规**（见 README「合规声明」）。
