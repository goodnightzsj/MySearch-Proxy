# MySearch Skill

[中文说明](./README.md) · [Back to repo](../README_EN.md)

The purpose of `skill/` is simple:

- teach `Codex` how to install and use `MySearch`
- let `Claude Code` follow a repo link and still install the MCP correctly

This is not the standalone MCP implementation directory.

Think of it as the human and AI readable install-and-use guide:

- `README_EN.md`
  - explains how to install
  - explains how to verify
- `SKILL.md`
  - explains how to use MySearch after installation

## If you are a user, the easiest way to use this

Send this to `Codex` or `Claude Code`:

```text
Open skill/README_EN.md and skill/SKILL.md from this repository, install MySearch for me, run install.sh from the repo root if the MCP is not registered yet, then run the health check and smoke tests and tell me the result.
```

If you only want to share the GitHub link, you can also say:

```text
Please read https://github.com/skernelx/MySearch-Proxy/tree/main/skill and automatically install and verify MySearch for me.
```

## If you are an AI assistant, what to do after receiving this directory

### 1. Treat this as the install-and-use entry, not as a standalone MCP package

If the user gives you any of these:

- the repository URL
- the `skill/` directory URL
- a local `skill/` path

default to using this folder as the MySearch onboarding entry point.

### 2. Install the Codex skill first

If the current environment is `Codex`, run:

```bash
bash skill/scripts/install_codex_skill.sh
```

If the destination already exists and needs replacement:

```bash
bash skill/scripts/install_codex_skill.sh --force
```

After installation, remind the user to:

- restart `Codex`

### 3. Then ensure the MySearch MCP is installed

From the repository root:

```bash
python3 -m venv venv
```

If the config file is not prepared yet:

```bash
cp mysearch/.env.example mysearch/.env
```

Then fill the environment for the current deployment:

- `MYSEARCH_TAVILY_*`
- `MYSEARCH_FIRECRAWL_*`
- optional `MYSEARCH_XAI_*`

Then run:

```bash
./install.sh
```

Notes:

- `skill/` teaches the assistant how to use MySearch
- the root `install.sh` registers the `mysearch` MCP for `Codex` / `Claude Code`
- both parts matter

## Recommended provider path

The default recommendation is not to hand-fill every official provider key.
The recommended setup is:

- use
  [skernelx/tavily-key-generator](https://github.com/skernelx/tavily-key-generator)
  as the Tavily / Firecrawl provider layer or aggregation API
- let MySearch connect to that normalized layer

Why this is better:

- better for public projects
- better for team reuse
- better for AI-driven installation flows

## How to verify the installation

Use this order:

```bash
codex mcp list
codex mcp get mysearch
python skill/scripts/check_mysearch.py --health-only
python skill/scripts/check_mysearch.py --web-query "OpenAI latest announcements"
python skill/scripts/check_mysearch.py --docs-query "OpenAI Responses API docs"
```

If X / Social is configured, add:

```bash
python skill/scripts/check_mysearch.py --social-query "Model Context Protocol"
```

If you want to test extraction too:

```bash
python skill/scripts/check_mysearch.py \
  --extract-url "https://www.anthropic.com/news/model-context-protocol"
```

## How Claude Code should interpret this skill

This folder currently provides:

- a local skill installer for `Codex`
- shared usage and installation instructions for both `Codex` and `Claude Code`

That means:

- `Codex` can install the local skill directly
- `Claude Code` can still read this `README_EN.md` and `SKILL.md`, install the
  MCP, verify it, and then follow the same usage rules

## How the AI should use MySearch after installation

After installation, do not fall back to generic web search by default.

Preferred order:

1. check `mysearch_health`
2. start from `search`
3. use `extract_url` when page content is needed
4. use `research` when a lightweight research pack is needed
5. only fall back to other search tools if MySearch is unavailable or the user
   explicitly asks for another source

For the full behavior rules, see:

- [SKILL.md](./SKILL.md)

## Related docs

- Repository overview:
  [../README_EN.md](../README_EN.md)
- MCP docs:
  [../mysearch/README_EN.md](../mysearch/README_EN.md)
- Proxy console:
  [../proxy/README_EN.md](../proxy/README_EN.md)
