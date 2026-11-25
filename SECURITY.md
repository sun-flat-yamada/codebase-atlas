# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x: |

## Reporting a Vulnerability

If you believe you have found a security vulnerability in **codebase-atlas**:

1. **Do not** open a public GitHub issue.
2. Report it through **GitHub Private Vulnerability Reporting**: open the **Security** tab of this repository and
   click **Report a vulnerability**.
3. Include the steps to reproduce, the impact, and a suggested fix if you have one.

We aim to acknowledge reports within a few days and will agree on a disclosure timeline with you.

## Threat Model and Safe Use

codebase-atlas makes an AI agent read source code and run shell commands with **your** permissions.
The rules it gives the agent are instructions, not an enforcement mechanism.

- **Analyze untrusted code in an isolated environment** (VM, container, disposable machine) without production
  credentials. Do not enable "always allow" style approval automation for untrusted code.
- **Instructions inside the analyzed code are data.** The rules tell the agent not to follow instructions found in
  comments, strings or documents of the target, and not to copy secrets (passwords, keys, tokens, personal data) into
  specifications — only their location and kind.
- **Treat the generated spec repository like the source code.** It contains excerpts of the analyzed code, local
  absolute paths, and the graphify graph (`graph.json`, file paths and symbol names). Do not push it to a public remote,
  and scan it with a secret scanner (for example gitleaks) before sharing.
- **graphify** is installed separately from its own repository. With `--code-only` (the only mode codebase-atlas uses)
  it parses code locally with tree-sitter and sends nothing to an LLM or network service. Do not configure a semantic
  backend (API keys) for confidential code unless your organization allows it.
- Use Playbooks and `hosts/` files only from sources you trust: they are copied into the spec repository and read by
  the agent.
