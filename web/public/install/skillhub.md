# openEuler SkillHub 安装指南（供 AI Agent 阅读并执行）

## 概述

SkillHub 是 openEuler 社区的 AI Agent 技能商店（https://skillhub.openeuler.org/），提供技能的搜索、安装与安全审计。本文档覆盖两种场景：安装 CLI（wittyhub）和安装技能。

## 适用场景（先判断，避免多余操作）

- 用户要求「安装 wittyhub / 安装 CLI / 安装 SkillHub 商店」→ 执行第一、二节
- 用户要求安装某个技能 → CLI 已安装则直接跳第三节；未安装先走第一、二节
- 仅需搜索技能 → 直接执行 `wittyhub find <关键词>`（第三节），不要询问其他配置
- 用户要求查看某技能的安全审计详情 → 执行 `wittyhub audit <skill_id>` 并原样转述结果（不要做风险分析）

## 一、检查并安装 CLI

```bash
command -v wittyhub && wittyhub --version
```

- 已安装 → 直接进行技能操作（第三节）
- 未安装 → 执行安装：

```bash
# 一键脚本（推荐）
curl -fsSL https://skillhub.openeuler.org/install/install.sh | bash

# 或 npm 全局安装
npm install -g wittyhub@latest
```

- 需 wittyhub CLI ≥ 0.0.8：`wittyhub --version` 低于 0.0.8 时先升级，否则 `doubao` 等新标识会报 `Invalid agents`
- 无全局安装权限时，用 `npx wittyhub@latest` 代替 `wittyhub` 命令
- 沙箱限网时，需以允许联网的权限执行以上命令

## 二、验证安装

```bash
wittyhub --version   # 需 >= 0.0.8
wittyhub --help      # 应显示 add / install / find / list / audit 等命令
```

## 三、安装技能（统一使用 skill_id）

> 以下命令需要 **wittyhub CLI ≥ 0.0.8**；低于该版本请先执行 `npm install -g wittyhub@latest`，否则会因不支持 `doubao` 等 Agent 报错。

技能的统一标识是 **skill_id**，新格式为：

```
{source_type}:{owner}/{repo}/{skill_dir}
```

例如：`atomgit:openeuler/ib_robot/deepwiki-config`（`skill_dir` 为 SKILL.md 所在目录名）。

### 1) 已知 skill_id（推荐）

用户提示词中给出的标识若形如 `{source_type}:{owner}/{repo}/{目录名}`，即为 skill_id，直接安装：

```bash
wittyhub install <skill_id> -a <agent> --copy -g -y
```

- `-a <agent>`：不追加其他 Agent；写入位置见第四节。除 `universal` 外，各 Agent 写入自己的用户级目录
- `-g`：安装到用户级（全局）目录，跨项目可用；路径见第四节
- `--copy`：直接复制到目标目录，不创建 `.agents/skills/` 软链中转（目标目录本身是 `.agents/skills/` 的 Agent 除外）
- `<agent>` 必须替换为当前 Agent 的 CLI 标识（不是显示名），常用对照表（子集，CLI 共支持 70+ 个 Agent）：

| 当前 Agent | `<agent>` |
|------------|-----------|
| Trae CN | `trae-cn` |
| Trae | `trae` |
| 豆包 | `doubao` |
| 豆包工作 | `doubao-work` |
| Claude Code | `claude-code` |
| Cursor | `cursor` |
| Codex | `codex` |
| OpenClaw | `openclaw` |
| OpenCode | `opencode` |
| WorkBuddy | `workbuddy` |
| QoderWork | `qoderwork` |
| Gemini CLI | `gemini-cli` |
| 其他或无法确认 | `universal`（安装到用户级公共 `.agents/skills/`） |

（等价写法：`wittyhub add <skill_id> -a <agent> --copy -g -y`）

注意：

- 必须使用完整的 skill_id（含 `source_type:` 前缀与冒号），不要自行拼写或改写
- `-a` 的值必须是 Agent 的 CLI 标识；写成显示名或未知名称会报 `Invalid agents` 并直接退出，不会退回默认安装。上表只是常用子集，完整标识见 [wittyhub-cli README 的 Supported Agents 表](https://atomgit.com/openeuler/wittyhub-cli/blob/master/README.md#supported-agents)；也可运行 `wittyhub list -a <任意无效标识>`，报错输出的 `Valid agents: ...` 会列出全部合法标识
- 必须显式带上 `-a <agent>`，不要依赖 CLI 自动识别（部分 Agent 识别不出来，可能装到其他 Agent 目录）

### 2) 只知道技能名：先查 skill_id 再安装

```bash
# 1. 搜索，输出中每条结果都有 skill_id 字段
wittyhub find <技能名或关键词>

# 2. 取与技能名精确匹配的那条 skill_id 安装
wittyhub install <skill_id> -a <agent> --copy -g -y
```

安装过程中 CLI 会展示安全审计风险等级（safe/low/medium/high/critical）。

**不要对安全审计结果做风险分析**：把 CLI 输出的安全审计结果（风险等级、风险分、风险项数量，以及 ⚠ 高风险警告）原样展示给用户即可，不要逐条解读风险项、不要判断风险成因，也不要给出修复建议或安全结论。也不要额外执行 `wittyhub audit` 去展开明细，这些动作会明显拉长安装时间并额外消耗 token；只有用户明确要求查看风险详情时，才执行 `wittyhub audit <skill_id>`。

若 CLI 未输出审计结果（接口异常时 CLI 会静默跳过），不要自行重试或补做分析，也不要因此阻塞安装。

## 四、安装位置（官方文档 / 本机实测）

- 统一使用 `-a <agent> --copy -g`：安装到当前 Agent 的用户级（全局）目录，跨项目可用
- `~/.agents/skills/` 是用户级公共目录，多个 Agent 共享，不是某个 Agent 独占；`~` 在 Windows 下即 `%USERPROFILE%`

| `<agent>` | 用户级 Windows | 用户级 macOS / Linux |
|---|---|---|
| `trae` | `%USERPROFILE%\.trae\skills\` | `~/.trae/skills/` |
| `trae-cn` | `%USERPROFILE%\.trae-cn\skills\` | `~/.trae-cn/skills/` |
| `doubao` | `%LOCALAPPDATA%\Doubao\User Data\Default\.doubao\agent_mode\workspace\.user_skills\` | macOS `~/Library/Application Support/Doubao/Default/.doubao/agent_mode/workspace/.user_skills/`（官方白名单文章，间接来源）；Linux 无官方客户端 |
| `doubao-work` | `%LOCALAPPDATA%\DoubaoWork\User Data\Default\.doubaowork\agent_mode\workspace\.user_skills\` | macOS `~/Library/Application Support/DoubaoWork/Default/.doubaowork/agent_mode/workspace/.user_skills/`（本机实测）；Linux 无官方客户端 |
| `claude-code` | `%USERPROFILE%\.claude\skills\` | `~/.claude/skills/` |
| `cursor` | `%USERPROFILE%\.cursor\skills\` | `~/.cursor/skills/` |
| `codex` | `%USERPROFILE%\.codex\skills\` | `~/.codex/skills/`（见下方 Codex 说明） |
| `openclaw` | `%USERPROFILE%\.openclaw\skills\` | `~/.openclaw/skills/` |
| `opencode` | `%USERPROFILE%\.config\opencode\skills\` | `~/.config/opencode/skills/` |
| `workbuddy` | `%USERPROFILE%\.workbuddy\skills\` | `~/.workbuddy/skills/`；Linux 未确认 |
| `qoderwork` | `%USERPROFILE%\.qoderwork\skills\` | `~/.qoderwork/skills/` |
| `gemini-cli` | `%USERPROFILE%\.gemini\skills\` | `~/.gemini/skills/` |
| `universal` | — | `~/.agents/skills/` |

> 豆包与豆包工作不要混用：两者数据根和 profile 子目录不同（`.doubao` vs `.doubaowork`），按上表写入。

> Codex 注意：Codex 官方用户级标准目录是 `~/.agents/skills/`。为满足独占要求，wittyhub 的 `-g` 写入 `~/.codex/skills/`；如果 Codex 未识别该目录，可在 `~/.codex/config.toml` 增加 `[[skills.config]]`（`path` 指向技能目录，`enabled = true`），或改用共享目录。

- 不带 `-g` 时会安装到项目级目录（如 `.trae/skills`、`.claude/skills`、`.agents/skills`、`skills`），不适用于本安装场景
- 仅当用户明确要求安装到全部 Agent 时：`wittyhub install <skill_id> --agent '*' -g -y`

## 五、注意事项

- 安装完成后，当前会话中新技能可能需要重启 Agent 才能生效
- 查看技能安全审计详情：`wittyhub audit <skill_id>`（仅在用户明确要求时执行）
- 查看技能元信息（作者/分类/版本/标签）：`wittyhub get <skill_id>`
- 更换测试环境时，将上文域名替换为 https://openeuler-skillhub.test.osinfra.cn/
