# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| main（最新提交） | ✅ |
| 历史 tag / 旧版本 | ❌ |

MultiRAG 目前以滚动方式发布：安全修复只落在 `main` 分支，不向历史版本回传。

## Reporting a Vulnerability

如发现安全漏洞，请**不要**通过公开 issue 披露，改用以下渠道之一：

1. GitHub [Private Vulnerability Reporting](https://github.com/yuehong136/MultiRAG/security/advisories/new)（首选）；
2. 邮件：du13013901711@163.com（标题注明 `[SECURITY]`）。

报告请尽量包含：受影响的组件/端点、复现步骤或 PoC、影响评估。
我们会在 7 天内确认收到，并在修复发布后与你协调披露时间。

## Dependency & Secret Scanning

仓库 CI 持续运行以下供应链检查（配置见 `.github/workflows/ci.yml`）：

- **osv-scanner**：对 `uv.lock` 全依赖树做漏洞扫描（必过门禁；
  豁免清单及理由见根目录 `osv-scanner.toml`，只出不进）；
- **gitleaks**：工作树泄密扫描（规则见 `.gitleaks.toml`）；
- **uv lock --check**：锁文件一致性。
