# `skillcrawler discover` 逻辑说明
本文档基于当前仓库代码，说明 discover 流程的完整执行逻辑，入口有两种：

1. CLI 命令（手动执行）：
```bash
python skillcrawler/main.py discover
```
> 命令用法、参数和示例见 [`skillcrawler/README.md`](../../skillcrawler/README.md)。

2. 后台自动调度（API 服务内置 `discover_scheduler` 定时任务，每天 / 每周固定触发一轮全量 discover）。

两者共用同一执行路径（扫描结束后均补建缺失 embedding）；差异（仓库列表来源）见 §2.1。

内容覆盖两种入口、仓库来源、Git 同步、Skill 发现、版本扫描、分类、安全检测、索引创建、数据库写入和错误处理。

| 序号 | 章节 |
|---|---|
| [§1](#1-总体流程) | [总体流程](#1-总体流程) |
| [§2](#2-skill仓库来源) | [skill仓库来源](#2-skill仓库来源) |
| [§3](#3-git-仓库同步) | [Git 仓库同步](#3-git-仓库同步) |
| [§4](#4-仓库级短路分支) | [仓库级短路分支（no_skill / removed / unchanged）](#4-仓库级短路分支no_skill--removed--unchanged) |
| [§5](#5-可扫描-skill-规则) | [可扫描 Skill 规则](#5-可扫描-skill-规则) |
| [§6](#6-latest-和-tag-版本扫描) | [latest 和 Tag 版本扫描](#6-latest-和-tag-版本扫描) |
| [§7](#7-单个-skill-的构建顺序) | [单个 Skill 的构建顺序](#7-单个-skill-的构建顺序) |
| [§8](#8-category-分类) | [Category 分类](#8-category-分类) |
| [§9](#9-skillspector-安全检测) | [SkillSpector 安全检测](#9-skillspector-安全检测) |
| [§10](#10-数据库写入时机) | [数据库写入时机](#10-数据库写入时机) |
| [§11](#11-仓库状态和结果类型) | [仓库状态和结果类型](#11-仓库状态和结果类型) |
| [§12](#12-删除与统计自愈) | [删除与统计自愈](#12-删除与统计自愈) |
| [§13](#13-错误处理和退出码) | [错误处理和退出码](#13-错误处理和退出码) |

阅读建议：

- 快速了解总体流程：第 1 节（流程图 + 调用链）。
- 定位模块实现代码：第 1.1 节关键代码索引。
- 排查 Git 同步 / Tag 拉取问题：第 3、6 节。
- 排查安全检测问题：第 9 节。
- 排查索引创建问题：第 2.1 节。
- 查数据表字段 / 写入行为：第 10 节（含 10.1 数据模型约定）。
- 查删除与统计自愈（批量删除、统计值维护）：第 12 节。
- 查错误处理和退出码：第 13 节。

## 1. 总体流程

```mermaid
flowchart TD
    A1["入口1: CLI 命令<br/>skillcrawler/main.py discover"] --> B{解析 discover 参数}

    B -->|"默认 / -p <platform>"| C["从 openEuler-skills 仓库（catalog）<br/>各平台 skill.yaml 构造请求列表<br/>（全部平台或指定平台，按 URL 去重）"]
    B -->|--url <skill.yaml>| D["只读取指定 skill.yaml<br/>构造其登记的请求"]
    A2["入口2: 后台自动调度<br/>discover_scheduler 定时任务"] --> C
    C --> E[逐仓串行循环]
    D --> E
    E --> F[clone / fetch 仓库]
    F --> F1["检查 HEAD 是否存在可扫描 SKILL.md"]
    F1 -->|无| F2{"DB 有仓库记录?"}
    F2 -->|无| F3["结果 no_skill<br/>保留 clone 目录"]
    F2 -->|有| F4["删除仓库记录及关联数据（立即 commit）<br/>结果 removed"]
    F1 -->|有| F5["创建或读取仓库记录<br/>创建仓库记录时设置status=init"]
    F5 --> F6{"已有记录且<br/>HEAD commit == repository_commit_id?"}
    F6 -->|是| F8["结果 unchanged<br/>（衍生字段缺口由 remediation 全局兜底）"]
    F6 -->|否| F10["需要扫描：状态设为 discovering"]
    F10 --> Z[扫描仓库]

    subgraph SCAN[扫描仓库内部]
        direction TB
        S1["收集 Git 元数据<br/>HEAD + 两阶段拉取最近 N 个 Tag"] --> S2[构造版本快照]
        S2 --> S3["记录 branch / local_path（flush 不 commit）"]
        S3 --> S4["预加载已有 Skills / SkillVersions<br/>（取上次的 category 和安全检测结果，供复用）"]
        S4 --> S5["扫描 latest Skills<br/>（每个 Skill：如果上次已有分类和安全检测结果：<br/>优先复用已有结果，否则新提交 Jenkins）"]
        S5 --> S6["扫描 Tag SkillVersions<br/>（复用 latest Skill 的结果：<br/>分类按 skill_id 直接复用，<br/>安全检测按 tree hash 一致才复用）"]
    end
    Z --> S1

    S6 --> T["同步 Skills / SkillVersions<br/>（删 / 插 / 更新 / 保留）"]
    T --> U["upsert 新触发的 SecurityAudit 记录"]
    U --> V["更新仓库 HEAD、数量、status=done<br/>一次 commit + 统计自愈（见 §12）"]
    V --> W["记录结果行<br/>created / rediscovered"]
    F3 --> W
    F4 --> W
    F8 --> W
    W --> Y[下一仓库]
    Y -->|全部仓库完成| Y2["补跑衍生字段缺口（remediation）<br/>security 风险分 → category 分类 → embedding 向量"]
    Y2 --> Y3["输出结果表和 summary<br/>（counters 说明 + skill 统计 + 补跑结果，见 §11.1）"]
```

### 1.1 关键代码索引

| 模块 | 作用 |
|---|---|
| `skillcrawler/main.py` | CLI 参数、模式路由、逐仓库循环、结果汇总 |
| `skillcrawler/config.py` | openEuler-skills catalog 与仓库列表加载 |
| `skillcrawler/core/skill_manager.py` | 仓库同步、状态、扫描与存储编排 |
| `src/utils/git_operations.py` | clone、fetch、Tag 两阶段拉取、tree hash、blob 物化 |
| `src/utils/skill_parser.py` | SKILL.md 查找、跳过规则、URL/ID 辅助逻辑 |
| `skillcrawler/core/skill_scanner.py` | latest/Tag 扫描、分类、安全检测触发 |
| `src/ai/category_classifier.py` | Category 规则和模型调用 |
| `src/models/repository.py` | Skills、SkillVersions、SecurityAudit 持久化 |
| `src/security/detector.py` | Jenkins 客户端和后台报告 collector |
| `src/api/main.py` | API 生命周期中启动和停止 collector |

### 1.2 SkillManager 调用链

```
discover_configured_skill_repository(request)            # 主入口
├── _sync_git_repository(clone_dir, ...)                 # clone / fetch 仓库
│   ├── update_existing_repository()（已克隆）或 clone_repository()（新克隆）
├── _list_skill_paths(clone_dir)                         #  发现 SKILL.md
│   └── 空 → 查 DB 删除记录 / 返回 None
├── _get_or_create_skill_repository(request, repo_name)  # 查 DB 或新建记录
├── _is_commit_unchanged(repo, clone_dir)                # 对比commit_id
│   └── 未变 → 标记 _unchanged 返回                        # 衍生字段缺口由 remediation 全局兜底
└── 已变 → _discover_and_store_skills(...)                # 扫描并存储 Skills / SkillVersions

_discover_and_store_skills()
├── _discover_skills()                                      # 扫描并返回 Skill/SkillVersion 列表
├── _count_unique_skills(latest_skills)                     # 去重计数
├── skill_repository.store_skills_and_versions(...)         # skills/skill_versions表入库
├── _store_to_security_audits()                             # security_audits表upsert
└── update_skill_repository(DONE, commit_id, skill_num)     # skill_repos表更新状态、skill_num

_discover_skills()
├── git_ops.collect_repository_git_metadata(...)         # 获取仓库最新commit_id和Tag的commit_id
│   ├── get_repository_head_commit_id()                     # 获取 HEAD commit_id
│   ├── _get_repository_latest_tags()                       # 获取最近 N 个 Tag
│   ├── _get_git_ref_commit_id(per tag)                     # 获取tag的commit_id
├── update_skill_repository(branch, local_path)             # skill_repos表更新branch、local_path
└── scanner.start_scan(...)                              # 开始扫描 Skills / SkillVersions
```

### 1.3 SkillScanner 扫描层

```
start_scan(...)
├── load_scan_records(repo.id)                           # DB：已有 skills + versions
├── _scan_latest_skills
│   ├── get_skill_tree_hashes(..., 'HEAD')               # 1 次批量 rev-parse
│   └── per skill：git show → build_skill_id → 分类 → 确定安全检测结果（优先复用）
└── _scan_tagged_skills（per tag）
    ├── list_skill_paths_for_ref(..., ref)               # 1 次 ls-tree -r 独立发现该 Tag 的 SKILL.md
    ├── get_skill_tree_hashes(..., ref)                  # 1 次批量 rev-parse
    └── per skill：git show → 分类 → 确定安全检测结果（优先复用）
```

## 2. skill仓库来源
### 2.1 触发来源
> 命令示例和参数约束见 [`skillcrawler/README.md`](../../skillcrawler/README.md)。

触发方式有两种：CLI 命令和后台定时任务

触发后读取skill仓库配置，根据配置构造 discover 请求列表。
- 先同步`https://atomgit.com/openeuler/openEuler-skills` 到
`<storage.local_path>/skill-repositories/openEuler-skills`，
扫描其中 `community`、`enterprise`、`personal` 分组下的 `*/skill.yaml`。
- 再解析出全部 discover 请求：入口**build_configured_discover_requests**，
  同时收集两种登记方式——`skill_repos` 整仓仓库与 `skills[].skill_url`
  单 Skill 贡献（见 §2.3）。CLI 与自动调度共用该入口，两边收集范围一致。

1. CLI 命令（`discover`；`--url <skill.yaml>` 时只读取指定 skill.yaml，不读全量配置）
**命令模式**（手动执行 `skillcrawler discover`）。不同模式的 commit 去重行为相同，
仓库列表来源不同：

| 命令 | 仓库来源 | commit 未变化时是否跳过 | 说明 |
|---|---|---:|---|
| `discover` | 配置文件 | 是 | 默认模式，全部平台 |
| `discover -p enterprise` | 配置中的指定列表 | 是 | 只读取指定平台列表 |
| `discover --url <skill.yaml>` | 单个 skill.yaml | 是 | 只扫描该文件登记的 `skill_repos` / `skills[]`，不读全量配置 |

2. 后台自动调度（`discover_scheduler` 定时任务）
**自动调度**（API 服务内置定时任务：服务启动时按 `discover_scheduler.enabled`
决定是否拉起，实现见[`src/jobs/discover_scheduler.py`](../../src/jobs/discover_scheduler.py)）：

- **每天 / 每周固定时刻（`discover_scheduler.interval` / `time` / `weekday`）触发一轮
  全量 discover**（`time` 为 **UTC** 时间，如 `08:00` 即北京时间 16:00）：同步
  openEuler-skills catalog、构建全部请求，逐仓库调用
  `SkillManager.discover_configured_skill_repository`（与 CLI 相同的执行路径）；
  **服务启动时额外立即执行一轮**（重启后当天触发点已过也不必等下个周期）；
- 调度基于纯 asyncio（每轮跑完 sleep 到下一个触发点），每轮执行完毕后才计算下一个触发点，
  克隆扫描耗时较长时下一轮自动顺延，不会叠加执行；单轮整体异常只记录日志，
  不终止调度循环；
- **每轮扫描结束后补跑衍生字段缺口**：调 `run_remediations()`（实现见
  [`src/jobs/remediation.py`](../../src/jobs/remediation.py)），按
  security → category → embedding 顺序补三类缺口（三类缺口互不影响，顺序仅
  为固定执行顺序：risk_score 缺口重提 Jenkins 检测、category 缺口补跑分类、
  embedding 缺口补建向量），各任务自持
  `pg_advisory_lock` 互斥（embedding 的锁另与全量重建脚本
  `scripts/reindex_embeddings.py` 互斥），单任务锁被占用或失败只影响该任务。
  失败不重试，漏掉的行由下轮 discover 或 API 启动时的补齐自愈（见
  [`src/jobs/remediation.py`](../../src/jobs/remediation.py)）；
- 每轮结果（每仓 outcome / 耗时 / 汇总计数 / skill 统计 / 补跑结果）写入
  `result_dir`（默认 `<storage.local_path>/logs`）下的 `discover-<时间戳>.json`，
  结果分类口径与 CLI discover 一致，输出结构见 §11.1。

> **注意**：CLI `skillcrawler/main.py discover` 与自动调度对齐——扫描结束后
> 同样补跑衍生字段缺口（`_run_remediations_safe()`），并写入
> 同一结构的 `discover-<时间戳>.json` 结果文件（构建与保存由
> [`skillcrawler/main.py`](../../skillcrawler/main.py) 的
> `build_discover_result_document()` / `save_discover_result_file()` 提供，
> 两个入口共用同一实现，输出完全一致）。语义搜索未启用或锁被全量重建占用
> 时跳过对应补跑，失败只记日志不影响退出码；若需强制全量重建，手动执行
> `python scripts/reindex_embeddings.py`。

### 2.2 仓库配置读取

配置格式（`openEuler-skills/{community,enterprise,personal}/*/skill.yaml` 的目录结构和示例）见 [`skillcrawler/README.md`](../../skillcrawler/README.md)。

本节描述读取实现（[config.py `load_crawler_config`](../../skillcrawler/config.py#L60-L88)）：

1. 加载结果为 `{"community": [...], "enterprise": [...], "personal": [...]}` 三组列表；
2. 每组的每个 `skill.yaml` 贡献其 `skill_repos` 条目；`community` 组的条目会被补上
   `sig_name`（取 `skill.yaml` 所在目录名），供 author 字段使用（见 §8.1）；
3. 按目录顺序（`sorted(root.glob("*/skill.yaml"))`）读取，YAML 文件本身不能作为来源；
4. 构造请求时校验每个条目必须包含非空 `url`，非法条目抛 `ValueError`；
5. 读取可选的 `branch`；
6. 在单个列表之间按 URL 去重；
7. 默认扫描全部平台时允许某个列表为空（warning 后跳过）；显式指定 `--platform` 时，
   对应列表为空同样 warning 后跳过，不产生请求。

使用 `--url` 单文件模式时，只构建指定 skill.yaml 登记的请求（入口
[main.py `build_requests_from_skill_yaml`](../../skillcrawler/main.py)）：
`platform` / owner 取自文件目录结构 `<catalog>/<platform>/<owner>/skill.yaml`
（不再由 URL 推断）；路径先按本地文件解析，不存在时按 catalog 根
（`-r` / 自动同步的 openEuler-skills）解析相对路径；文件名必须恰好是
`skill.yaml` 且位于三个平台分组之下，否则抛 `ValueError`。

仓库请求进入 discover 前会规范化 URL：`gitcode.com` 是 `atomgit.com` 的旧域名，
HTTPS/HTTP 和 SSH 地址中的精确 host `gitcode.com` 都会自动转换为
`atomgit.com`。后续 clone、`repo_name`、数据库 `url`、source/skill ID 生成和
platform 推断均使用转换后的 AtomGit 地址。

### 2.3 单 Skill URL 贡献（`skills[].skill_url`）

`skill.yaml` 除 `skill_repos` 整仓登记外，还支持 `skills[]` 单 Skill 登记
（只贡献某个具体 SKILL.md，不扫描整个仓库，见
[`skillcrawler/README.md`](../../skillcrawler/README.md) 的配置格式）。实现链路：

1. 收集：[config.py `load_catalog_skill_url_entries`](../../skillcrawler/config.py#L91-L112)
   读取各组 `skill.yaml` 的 `skills[]` 条目；`skill_name` / `skill_url` 必填，
   缺失抛 `ValueError`；
2. 解析：[skill_parser.py `parse_skill_md_url`](../../src/utils/skill_parser.py#L42-L67)
   把 `skill_url`（`<repo>/blob/<ref>/<path>/SKILL.md`）拆为
   `(repo_url, ref, skill_path)`。`ref` 取 `/blob/` 后第一段，约定为单段分支名
   （含 `/` 的 ref 无法与路径消歧，不支持），作为 clone 分支；`repo_url` 与
   `skill_repos` 同口径规范化（去 `.git`、gitcode→atomgit）；
3. 合并（[main.py `_build_requests_from_skill_urls`](../../skillcrawler/main.py#L118-L176)）：
   - 同一仓库的多条贡献合并为一个请求，`skill_paths` 取并集（部分扫描按请求
     同步入库，拆成多个请求会互相删掉对方的 skill）；
   - 同仓库多条贡献必须同 `ref`，否则抛 `ValueError`；
   - 仓库已被 `skill_repos` 登记时跳过：整仓扫描已覆盖这些 skill；
4. 收窄扫描：请求 `skill_paths` 非空时，HEAD 与 tag 版本扫描都只收录登记的
   SKILL.md（`skill_scanner.py` 的 `skill_path_filter`），入库同步的删除/更新
   只作用于登记 skill。

结果语义：该仓库 `skill_repos.skill_num` 只统计登记的 skill；贡献的 SKILL.md
从仓库删除后，下次扫描走 `removed` 语义清理该仓库记录（级联删除其登记 skill）。

## 3. Git 仓库同步

### 3.1 本地目录

仓库保存目录为：

```text
<storage.local_path>/skill-repositories/<repo_name>
```

例如：

```text
/opt/wittyhub/skill-repositories/github.com_anthropics_skills
```

`repo_name` 的生成方式：

```text
删除 http:// 或 https://
删除末尾 .git
将 / 替换为 _
```

### 3.2 仓库同步流程

```mermaid
flowchart TD
    A[计算 clone_dir] --> B{检查目录是 Git 仓库?}
    B -->|是| C[更新 origin URL]
    C --> D{有目标分支?<br/>请求 branch 字段或本地检出分支}
    D -->|是| E["fetch origin 目标分支<br/>浅仓库时 --depth 1"]
    E --> F["git update-ref refs/heads/分支 FETCH_HEAD<br/>只移动分支指针，不 checkout"]
    D -->|否，detached HEAD| G["fetch origin<br/>浅仓库时 --depth 1"]
    B -->|否但目录存在| H[删除非 Git 目录]
    H --> I["git clone --depth 1 --no-checkout --filter=blob:none"]
    B -->|目录不存在| I
```

- 新仓库使用浅克隆 + partial clone：`git clone --depth 1 --no-checkout --filter=blob:none`（请求带 branch 时加 `--branch`），仓库从建立之初就只有 commit/tree 元数据，blob 按需拉取，不再执行 unshallow（见 §3.3）。
- 已有仓库先更新 origin URL；有目标分支（请求 branch 字段或本地检出分支）时 `fetch origin <branch>`（浅仓库时加 `--depth 1`），随后 `git update-ref refs/heads/<branch> FETCH_HEAD` 把本地分支指针移到新提交；detached HEAD 等无目标分支场景只执行 `fetch origin`。**全程不 checkout 工作区**——仓库以 `--no-checkout` 建立，后续扫描（`git ls-tree` / `git show`）直接读对象库。
- Git 操作默认最多执行 2 次，单次超时 180 秒；所有 git 子进程注入 `GIT_TERMINAL_PROMPT=0` 等非交互环境变量，无 TTY 时认证提示直接失败而不是挂死。
- GitHub 仓库（且配置了 token）的 clone/fetch：先按原始 URL 执行（含 2 次重试），失败后改用带 token 的 URL（SSH URL 也会转换为 `https://<user>:<token>@github.com/...`）再执行（又含 2 次重试）；token 在异常信息中被掩码。

### 3.3 partial clone 和 `blob:none`

Git 仓库主要包含以下对象：

| 对象 | 内容 |
|---|---|
| `commit` | commit ID、作者、时间和父提交等元数据 |
| `tree` | 某个 commit 对应的目录结构和文件列表 |
| `blob` | 文件的实际内容 |
| `tag` | Tag 信息及其指向的对象 |

普通的完整克隆会下载 commit、tree 和全部 blob。  

discover 克隆仓库使用：
```bash
git clone --depth 1 --no-checkout --filter=blob:none [--branch <branch>] <url> <dir>
```

其中：
- `--depth 1`：浅克隆： 只取目标分支最新一个 commit；
- `--no-checkout`：不展开工作树；Skill 元数据通过 `git show <ref>:<path>` 从对象库读取；
- `--filter=blob:none`：partial clone：只下载 commit（--depth 1又指定最新一个）和tree，blob不下载。

三个参数缺一不可：
- 省略 `--depth 1` 会拉取完整历史（大仓库可达数 GB，如 ClickHouse约 2GB，浅克隆后约 200MB）；
- 省略 `--no-checkout` 会展开与仓库文件总量相同的工作区 （如 PostHog 约 500MB）；
- 省略 `--filter=blob:none` 则需下载全部 blob，下载量与工作区相当。

三者组合后本地通常只占几 MB 的 commit/tree 元数据。

克隆完成后 Git 自动把 `origin` 标记为 promisor remote（写入
`remote.origin.promisor=true` 和 `remote.origin.partialclonefilter=blob:none`）。
后续读取缺失 blob 时 Git 自动向远端按需拉取，例如：

```bash
git show HEAD:skills/example/SKILL.md
```

扫描用到的命令对 Git 对象的依赖：
- `git ls-tree` / `git rev-parse` 只需要tree/commit 对象，在 partial clone 中本地已存在；
- `git show <ref>:<path>` 需要blob，读取时自动触发按需拉取。
- `git ls-tree <ref>` 直接读取 ref 的 tree 对象，始终可用。


并非所有 Git 服务都支持 partial clone filter。服务端不支持时，Git 通常只输出
warning（如 `filtering not recognized by server`）并退化为完整传输，克隆仍能成功，
只是对象下载量变大；Tag 元数据阶段的 `tree:0` 探测会主动识别该 warning 并降级为
Tag 名自然排序（见 §6.2.1）。

#### blob 预物化（materialize）

SkillSpector 容器以只读方式挂载缓存目录且无网络访问，扫描前必须把 Skill 路径下的
全部 blob 提前拉取到本地。`materialize_skill_objects` 在爬虫宿主机（缓存可写、
网络可用）执行：

```bash
# 1. 列出 Skill 目录下全部对象
git ls-tree -r --format='%(objectname) %(objecttype)' <ref> -- <skill-path>

# 2. 逐个读取 blob，触发 Git on-demand 懒拉取
git cat-file --batch-check
```

`cat-file --batch-check` 读取缺失 blob 时触发 promisor 懒拉取；结束后校验无
`missing` 对象，否则记录 warning 并返回失败。物化在安全链路中的位置（触发
Jenkins 的前置步骤）与失败时的处理（跳过本次审计、不缓存结果、下轮自动重试）见
[`security-audit-feature-spec.md`](security-audit-feature-spec.md) 3.1.1 节。

#### 目录 tree hash

安全结果复用以 Skill 目录的 tree hash 为 key（见 §7、§9.1），通过一次批量命令
获取：

```bash
git rev-parse <ref>:<skill-dir-1> <ref>:<skill-dir-2> ...
```

tree hash 只依赖 tree 对象（partial clone 中已存在），不需要下载任何 blob；批量
查询失败时对应目录结果为 `None`，扫描继续。

### 3.4 中断传输与 `tmp_pack_*` 清理

Git 流式接收 pack 数据时，会先把数据写入 `.git/objects/pack/tmp_pack_XXXXXX`，
传输成功后才 rename 为正式 pack 文件并构建索引。如果 fetch 在传输过程中因超时、
网络中断或被 kill 而终止，`tmp_pack_*` 文件会永久残留在磁盘上，且不会被任何
pack 索引引用。对大仓库反复执行失败的 fetch 会让这些孤儿文件累积，浪费磁盘
空间（例如 `/opt/wittyhub/skill-repositories/github.com_clickhouse_clickhouse/.git/objects/pack/tmp_pack_yqbJ9L`）。

代码中需要在以下场景主动清理 `tmp_pack_*`：

| 场景 | 清理位置 | 说明 |
|------|---------|------|
| Tag 元数据或精确拉取失败 | `_cleanup_orphaned_tmp_packs` | `_get_repository_latest_tags` 在两阶段 `git fetch` 抛出 `CalledProcessError` / `TimeoutExpired` 后调用 |
| 其他 fetch 失败 | 当前未统一处理 | 可在排查磁盘占用时手动 `rm tmp_pack_*` |

实现要点（[git_operations.py `_cleanup_orphaned_tmp_packs`](../../src/utils/git_operations.py#L424-L440)）：

- 只匹配 `tmp_pack_*` 前缀，不误删 `pack-*.pack` / `pack-*.idx`；
- 通过 `glob` 列举，逐个 `unlink`，失败时只记录 warning 不抛出；
- 在没有 pack 目录（如全新 clone 之前）时直接返回。

> 注意：清理必须在没有其他 fetch 并发执行时进行，否则可能删除正在写入的 pack。

## 4. 仓库级短路分支（no_skill / removed / unchanged）

### 4.1 仓库没有可扫描 Skill

按 §5 的可扫描规则没有找到文件时：

- 数据库没有仓库记录：返回 `no_skill`。
- 数据库已有仓库记录：删除该仓库数据库记录，返回 `removed`。
- 本地 clone 目录都会保留，便于排查或下次更新。

### 4.2 commit 未变化

对于配置模式和 `--url` 模式，已有仓库会比较：

```text
本地仓库当前 HEAD commit
        vs
数据库 repository_commit_id
```

相同后直接标记 `_unchanged` 返回：

```text
result = unchanged
error  = commit unchanged
```

并跳过：

- Skill 解析；
- 分类调用；
- Jenkins 安全检测；
- Skills/SkillVersions 写入；
- SecurityAudit 更新。

历史版本曾在此分支内联为 `risk_score IS NULL` 的记录重提 Jenkins 检测
（`_retry_unscored_security_audits`，结果 `security_retried`）；现已抽离到
统一补齐框架 [`src/jobs/remediation.py`](../../src/jobs/remediation.py)：
安全补审计改为**全局扫描**所有 `risk_score` 缺口行（不限于本轮扫到且 commit
未变的仓库），在每轮 discover 末尾与 API 侧低频补齐循环中执行，覆盖面更大、
且与 category / embedding 补齐同构。未评分记录的重试流程（还原 `SKILL.md`
相对路径 → 重新提交 Jenkins → 更新 `extra_metadata.security_audit` →
upsert `security_audits` → 保持 `risk_score=NULL` 等 collector 回填）不变，
见 [`security-audit-feature-spec.md`](security-audit-feature-spec.md) 3.1 节与
3.2 节；触发失败的记录仍保持 NULL，由下一轮补跑重试。

## 5. 可扫描 Skill 规则

只扫描仓库内名为 `SKILL.md` 的文件。

以下目录中的 `SKILL.md` 会被跳过：

```text
template, templates
example, examples
demo, demos
test, tests
fixture, fixtures
docs, doc
archive, archives
legacy
```

判断只针对 `SKILL.md` 之前的目录段。例如：

```text
skills/foo/SKILL.md               扫描
examples/foo/SKILL.md             跳过
skills/foo/tests/SKILL.md         跳过
```

扫描文件按路径排序，因此日志中的 Skill 顺序通常是稳定的。

目录名匹配大小写不敏感（`Template/`、`Docs/` 同样跳过）；文件名匹配为
大小写敏感的**精确** `SKILL.md`（根级 `SKILL.md` 或 `*/SKILL.md`），因此
`PRE-SKILL.md`、`backupSKILL.md`、`_cannbot_SKILL.md` 这类近似文件名**不会**
被扫到，`skill.md` / `Skill.md` 同样不会。

路径合法后还有三道收窄：

- **元数据门禁**：frontmatter 必须同时声明 `name` 与 `description`，
  缺任一字段或值为空的 `SKILL.md` 跳过并告警（不算 skill）；
- **内容门禁**：正文 strip 后不足 100 字符的 `SKILL.md` 跳过（多为占位/桩文件）；
- **skill_id 去重**：同仓库不同路径推导出同一 `skill_id`（目录同名）时，
  只保留排序靠前的第一个，其余跳过并告警。

## 6. latest 和 Tag 版本扫描

> **要点**：当前分支和最近若干 Tag 都会扫描；Tag 数量由 `crawler.max_tags_per_repo` 控制，默认是 5。Tag 拉取采用「列出全部 Tag → `tree:0` 浅拉取 Tag/commit 元数据 → 按 `creatordate` 取前 N → `blob:none` 精确补齐所选 Tag 的 tree」的两阶段策略，既覆盖不规则 Tag 名，也避免 `git fetch --tags` 全量下载；远端不支持元数据过滤时降级为按 Tag 名自然排序。fetch 失败会清理 `.git/objects/pack/tmp_pack_*` 孤儿文件。Tag 版本行按 `(skill_id, tree_hash)` 内容去重：Skill 目录内容未变的旧 Tag 不产生独立版本行（见 §6.2.2）。

### 6.1 latest Skill

当前分支（HEAD）的每个可扫描 `SKILL.md` 会生成一个 `Skill`：

```text
version = latest
version_source = branch_head
commit_id = 仓库当前 HEAD commit（repository_commit_id）
tree_hash = Skill 目录的 tree hash
```

- `commit_id` 直接使用仓库 HEAD commit，单独保存在 `skill_repos.repository_commit_id`；
- `tree_hash` 通过一次批量 `git rev-parse HEAD:<skill-dir> ...` 获取（见 §3.3），批量
  查询失败时对应目录为 `None`，扫描继续；
- 安全结果是否复用按 `tree_hash` 判断（见 §9.1），不再按「目录最后修改 commit」判断。

如果仓库记录没有 branch，会检测当前分支或远端默认分支，并回写数据库。

### 6.2 Tag SkillVersion

Git 元数据优先按 Tag 创建时间选择最近的若干 Tag，数量来自：

```yaml
crawler:
  max_tags_per_repo: 5
```

#### 6.2.1 标签拉取流程

为避免 Tag 名自然排序漏掉 `release-final`、`stable-summer` 等不规则新标签，同时避免
对包含大量标签的仓库执行 `git fetch --tags`，标签拉取采用“元数据筛选 + 精确补齐”
的两阶段流程：

```mermaid
flowchart TD
    A["git ls-remote --tags --refs origin"] --> B["得到全部远程 Tag 名称"]
    B --> B1{"有远程 Tag?"}
    B1 -->|否| I["返回空列表，跳过 Tag 扫描"]
    B1 -->|是| C["先对 1 个 Tag 探测 tree:0 支持"]
    C -->|支持| C1["depth=1 + filter=tree:0<br/>拉到 refs/crawler/tag-candidates/*"]
    C -->|不支持或失败| E["降级：Tag 名自然排序"]
    C1 -->|成功| D["for-each-ref 读取 creatordate"]
    C1 -->|失败| E
    D --> F["按 creatordate 倒序<br/>同时间按自然顺序稳定排序<br/>未取到时间的 Tag 追加在尾部"]
    E --> G["取前 N 个"]
    F --> G
    G --> J["--refetch + filter=blob:none<br/>精确拉取前 N 个 Tag"]
    J -->|成功| K["返回最近 N 个 Tag"]
    J -->|失败| L["清理 tmp_pack_* 后返回空列表"]
```

各步骤说明：

| 步骤 | 命令 | 作用 |
|------|------|------|
| 列出远程 Tag | `git ls-remote --tags --refs origin` | 只下载 ref 列表，不下载对象；`--refs` 排除 annotated Tag 的 `^{}` peeled 条目 |
| 能力探测 | 对自然排序首个 Tag 执行一次 `depth=1 + filter=tree:0` 精确 fetch | Git 服务不支持 filter 时可能只给 warning 且退出码为 0；代码识别该 warning 并降级，避免直接对全部 Tag 发起无过滤传输。探测最多涉及一个 Tag |
| 元数据拉取 | `git fetch --force --no-tags --depth=1 --filter=tree:0 origin '+refs/tags/*:refs/crawler/tag-candidates/*'` | 探测成功后，全部 Tag 只获取 tip tag/commit 元数据，不获取 tree/blob；使用临时 ref 命名空间，不污染正式本地 Tag |
| 时间排序 | `git for-each-ref --format='%(creatordate:unix) ...'` | annotated Tag 使用 tagger date；lightweight Tag 使用目标 commit 的 committer date；相同时间用自然排序作为稳定次序；未取到 creatordate 的 Tag 追加在排序结果尾部 |
| 残留清理 | `_cleanup_candidate_refs(clone_dir)`（排序开始前执行） | 删除上一轮残留的 `refs/crawler/tag-candidates/*`；残留 ref 会让下一次 `tree:0` fetch 在部分服务端触发 `pack-objects` 断言（Git 客户端 bug） |
| 兼容降级 | `_tag_sort_key(tag)` | 元数据 fetch 或时间读取失败时按 Tag 名自然排序（正则拆分数字段并补零比较，`v10 > v9`），保证不因服务端不支持 `tree:0` 而中断整个仓库扫描 |
| 精确拉取 | `git fetch --force --no-tags --refetch --filter=blob:none origin refs/tags/<t>:refs/tags/<t> ...` | 只为前 N 个 Tag 补齐 commit/tree；`--refetch` 防止 Git 因 commit 已在元数据阶段存在而跳过缺失 tree；需要 Git ≥ 2.36 |
| 失败清理 | `_cleanup_orphaned_tmp_packs(clone_dir)` | Git 流式传输把 pack 写到 `tmp_pack_XXXXXX`，成功后才 rename；超时/中断留下孤儿文件，需主动删除 |

> `creatordate` 是统一排序字段，不是可信发布时间。Git 时间戳可由提交者设置；如果
> 业务以后要求“平台 Release 发布时间”，需要另接 GitHub/GitLab/AtomGit API。

元数据阶段的传输规模主要与 Tag 数量和各 Tag tip commit 数量相关，而不是与仓库
文件总体积相关。即使仓库历史包含大型二进制文件，`tree:0` 也不会在这一阶段下载
目录树或文件 blob。元数据阶段的 `--depth=1` 同时把 refetch 的传输量约束在各 Tag
tip 一层——即使 Tag 指向历史深处的 commit，refetch 后本地也只有每个 Tag 的 tip
commit + tree。该约束依赖元数据阶段成功执行（Tag tip 已写入 shallow 边界）；
探测降级为自然排序的路径下 Tag tip 没有写入 shallow 边界，refetch 传输量由服务端
对 `blob:none` 的支持情况决定，可能退化为拉取完整历史。选中前 N 个 Tag 后，
`blob:none` 只批量补齐扫描所需 tree，实际 `SKILL.md` blob 在 `git show` 时按需
获取。

#### 6.2.2 版本快照与扫描

每个 Tag 形成一个版本快照：

```text
ref = tag
version = tag
commit_id = 该 Tag 指向的 commit（tag commit，缺失时回退为仓库 HEAD commit）
version_source = tag
tree_hash = Skill 目录在该 Tag ref 下的 tree hash
```

Tag 扫描还有两个限制：

1. 每个 Tag 通过 `git ls-tree -r --name-only <tag>` 独立列出该 ref 下存在的
   Skill 路径，不复用 HEAD 的路径集合——只存在于历史 Tag、后来被删除或改名的
   Skill 也会作为版本导入。
2. 去重分三级：
   - `(skill_id, tree_hash)` 的**内容去重**发生在记录组装之前，命中即跳过该
     条目，不再执行分类调用与安全审计解析。同一 Skill 在多个 Tag 之间目录内容
     未变（例如仓库只改了 README 后连续打 tag）时，只有内容首次出现的 Tag
     （即最新的 Tag，Tag 按创建时间降序迭代）会构建版本记录，其余旧 Tag 不
     写入 `skill_versions`。即使保留下来的版本与 HEAD 的 latest 行 tree_hash
     相同，也仍保留这一条版本行。
   - `(skill_id, commit_id)` 的 commit 预去重发生在记录组装之前，命中即跳过
     该条目，不再执行分类调用与安全审计解析（多个 Tag 指向同一 commit 时只有
     第一个 Tag 会构建记录）；
   - `(skill_id, version)` 的 version 去重发生在组装之后，因为最终 `version`
     可能由 frontmatter 兜底填充，跨 Tag 会出现最终 version 相同的记录。

## 7. 单个 Skill 的构建顺序

```mermaid
flowchart TD
    A[读取 frontmatter 和正文] --> B[生成 source_url 和 skill_id]
    B --> C[读取本 ref 已批量计算的目录 tree hash]
    C --> D[解析 author/source/platform]
    D --> E["分类 category<br/>（按 skill_id 优先复用 category_cache：<br/>DB 已有分类 / 本轮已分类直接复用）"]
    E --> F{安全检测结果复用判定<br/>（相同 tree hash 且已有 risk_score?）}
    F -->|是| G[复用 risk_score 和 audit metadata]
    F -->|否| H[触发 Jenkins SkillSpector]
    G --> I[运行时标记 audit_triggered=false]
    H --> J[保存新 build number metadata]
    I --> K[构建 Skill 或 SkillVersion 对象]
    J --> K
    K --> L[加入内存列表]
    L --> M[输出 Discovered skill 日志]
```

图中 F/H 分支是安全结果三层复用（DB 记录 → 扫描级缓存 → 新提交 Jenkins）的简化
视图，完整判定流程与缓存规则见
[`security-audit-feature-spec.md`](security-audit-feature-spec.md) 3.1 节（另见 §9.1）。

`Discovered skill` 日志只表示对象已加入内存列表，不表示已经写入数据库。

## 8. Category 分类

分类优先级：

```mermaid
flowchart TD
    A[整仓预加载已有 Skills 和 SkillVersions] --> B{category_cache 有结果?}
    B -->|是| D[复用数据库或本轮 category]
    B -->|否| E{DeepSeek 分类器可用?}
    E -->|否| F[使用 SKILL.md metadata.category]
    E -->|是| G{metadata.category 合法?}
    G -->|是| H[直接使用 metadata category]
    G -->|否| I[调用模型分类]
```

模型分类调用失败按错误类型分流（`category_classifier.py`）：

- **瞬时故障**（httpx 传输层错误：读超时、连接被重置；HTTP 429 / 5xx）：
  最多重试 `CATEGORY_RETRY_ATTEMPTS=2` 次（间隔 2s）；重试耗尽后**降级**——
  `metadata.category` 经 `_normalize_category` 校验（在类别表内，大小写不敏感，
  归一为标准写法）合法则沿用；非法或缺失则返回 `None`（落库为空，
  下轮扫描重新分类），不中断扫描。网络抖动不再导致整仓回滚。
- **配置 / 凭据类错误**（4xx、响应解析失败等非瞬时异常）：抛出
  `CategoryClassificationError`，保持 fatal 语义。该异常在
  [skill_manager.py](../../skillcrawler/core/skill_manager.py) 的 except 块中被捕获后：

1. 当前仓库执行 rollback 并标记为 `failed`（单独 commit）；
2. 如果异常类型是 `CategoryClassificationError`，**直接 `raise` 原异常**（不包装为 `ValueError`），保留 fatal 语义；
3. 其他异常仍包装为 `ValueError(...) from exc` 上抛。

因此 [main.py](../../skillcrawler/main.py) 的 `isinstance(exc, CategoryClassificationError)` fatal 判定能正确识别：
当前仓库标记为 `failed`，停止处理后续仓库，CLI 返回退出码 1。

### 8.1 `author` 字段解析链

`author` 按以下优先级解析，前一级为空时才使用下一级（[skill_manager.py:301](../../skillcrawler/core/skill_manager.py#L301)、[skill_scanner.py:345-346](../../skillcrawler/core/skill_scanner.py#L345-L346)）：

```mermaid
flowchart TD
    A["① catalog sig_name<br/>（community 组 skill.yaml 所在目录名，见 §2）"] -->|空| B["② community 缓存映射<br/>（openeuler-community 仓库 sig/*/sig-info.yaml）"]
    B -->|空| C["③ 仓库 owner<br/>（derive_skill_source 从 repo.url 提取）"]
    A -->|非空| H[写入 skills.author]
    B -->|非空| H
    C --> H
```

第 ② 级只对 `platform=community` 生效，从 openEuler community 仓库缓存构建
`repo_name → sig_name` 映射（[openeuler_sig.py](../../skillcrawler/core/openeuler_sig.py)）：

- community 仓库缓存目录：`/opt/wittyhub/openeuler-community`
- 扫描 `sig/*/sig-info.yaml` 的 `repositories[].repo`，只读取 `openeuler/<repo>`，
  不读取 `src-openeuler/<repo>`
- 映射示例：`openeuler/PilotGo-plugin-llmops` → `atomgit.com_openeuler_PilotGo-plugin-llmops` → `sig_name`

## 9. SkillSpector 安全检测

安全检测的完整设计（触发条件、Jenkins 交互参数、API 后台 collector、风险评分与存储
规则）见 [`security-audit-feature-spec.md`](security-audit-feature-spec.md)。本节只
描述 discover 阶段与扫描相关的行为。

### 9.1 discover 阶段

安全检测启用且 Jenkins 配置有效时，新 Skill、目录 tree hash 已变化的 Skill，以及
`risk_score IS NULL` 的 Skill 会触发 Jenkins Job。目录 tree hash 相同且已有非空
`risk_score` 时复用已有结果，不提交新 Job；相同 tree hash 的结果也会在 latest 与
各 Tag 版本之间复用（跨版本 security_cache）。

跨版本复用由扫描级缓存 `security_cache`（`dict[tree_hash → SecurityResolution]`）
实现，在单仓库扫描（latest 与全部 Tag）内共享，解析顺序为：DB 已有评分结果 →
同轮缓存 → 提交新 Jenkins Job（三层复用的完整判定流程见
[`security-audit-feature-spec.md`](security-audit-feature-spec.md) 3.1 节）。
两条关键规则：

- **失败结果不缓存**：Jenkins 审计失败（`risk_score=None` 且非异步 pending）不写入
  缓存，相同内容的后续版本会重新提交重试，避免失败传播；
- **异步模式正确性**：缓存命中时保留 `audit_triggered=True` 与原 `build_number`，
  复用记录仍会写入各自 SecurityAudit 行；结果回来后按 `audit.id` 逐行回填，
  同 `build_number` 的行都能被独立更新。

扫描完成时输出缓存统计（`skill_manager` 记录日志）：

```text
Discover: scan completed for <repo>: latest_skills=N, tagged_skills=M,
security_cache_hits=X, security_audits_submitted=Y, security_audits_pending=Z
```

各计数器的精确口径（`X + Y` 与 `_resolve_security_result` 调用次数的等式关系、
空跑计入 `Y`、version 后置去重的放大效应）见
[`security-audit-feature-spec.md`](security-audit-feature-spec.md) 3.1.2 节。

是否产生新 Jenkins 任务使用 ORM 对象上的运行时属性
`_security_audit_triggered` 表示。该属性不写入 `extra_metadata`，也不会持久化到
数据库。`_store_to_security_audits()` 通过 `_has_new_security_audit()` 只选择该属性
为 `true` 的记录，避免把复用的旧 build 重新重置为 pending。属性为 `true` 时，
Scanner 已保证 `extra_metadata.security_audit` 中存在本轮的新 build number。

```mermaid
sequenceDiagram
    participant D as discover
    participant J as Jenkins
    participant DB as PostgreSQL

    D->>J: 获取 crumb
    D->>J: POST skill-scanner/buildWithParameters
    J-->>D: Queue Location
    loop 每 1 秒，最长 60 秒
        D->>J: 查询 Queue Item
        J-->>D: 等待 executable/build number
    end
    J-->>D: build number
    D->>D: 保存 async/build_number 元数据
    Note over D,J: discover 不等待 Build 执行完成
    D->>DB: 整仓扫描完成后写 SecurityAudit pending
```

提交参数（`GIT_URL`、`REF`、`SKILL_PATH`、`SCANNERS`）的定义见
[`security-audit-feature-spec.md`](security-audit-feature-spec.md) 第九节。

当前 Jenkins Queue 轮询间隔为 1 秒，最长等待 60 秒。executor 繁忙时，discover 会在这里串行等待 build number。

如果 Jenkins 触发失败：

- 记录错误日志；
- 返回的 build number 为 `None`；
- Skill 仍会继续构建和入库；
- 不会创建可供 collector 收集的 SecurityAudit pending 记录。

本轮写入的 pending `SecurityAudit` 由 API 侧后台 collector 异步回填，不属于 discover
进程；收集流程（30 秒轮询、Build 终态处理、report 下载重试）见
[`security-audit-feature-spec.md`](security-audit-feature-spec.md) 3.2 节。discover
退出后，只要 API 正常运行，collector 仍可继续收集报告。

## 10. 数据库写入时机

单个仓库的扫描结果先保存在内存中：

```text
latest_skills: list[Skill]
tagged_skills: list[SkillVersion]
```

扫描开始时先用两次查询一次性加载本仓库已有的 `skills` 和 `skill_versions`，用于
category、安全结果和版本复用；不再为每个 Skill 单独查询已有记录。只有 latest 和
Tag 全部扫描完成后，才调用：

```python
store_skills_and_versions(
    repo.id, latest_skills, tagged_skills, commit=False,
)
```

因此：

- 中途看到 `Discovered skill` 时，新数据尚未落库。
- 当前仓库扫描中途按 `Ctrl+C`，本轮新 Skill 数据不会写入。
- 已经提交给 Jenkins 的 Build 不会因数据库事务回滚而撤销；中途退出可能留下没有对应 `security_audits` 记录的孤立 Build。
- 当前仓库扫描中途异常，旧 Skill 数据通常仍保留。
- 多仓库模式是每个仓库完成后分别提交，不需要等所有仓库完成。

### 10.1 数据模型约定

discover 写入路径涉及三张关键表，字段约定如下，供 §11 仓库状态和后文更新规则对照。

#### `skill_repos`

`skill_repos` 记录一个 Git 仓库的 discover 状态。

| 字段 | 含义 |
|------|------|
| `repo_name` | 由仓库 URL 生成的本地唯一名称，如 `atomgit.com_openeuler_wittyhub-cli` |
| `source` | 仓库来源，如 `atomgit` / `github` |
| `platform` | 平台分类，如 `openeuler` / `enterprise` / `personal` |
| `branch` | 当前扫描分支 |
| `url` | Git 仓库 URL |
| `local_path` | 本地 clone 绝对路径 |
| `repository_commit_id` | 仓库 HEAD commit，用于判断 repo 是否更新 |
| `skill_discover_status` | `init` / `discovering` / `done` / `failed` |
| `skill_num` | 当前 repo 下 latest skill 数量 |

#### `skills`

`skills` 保存前端展示和下载时使用的 latest skill。

| 字段 | 含义 |
|------|------|
| `skill_id` | 全局唯一 Skill ID，格式类似 `atomgit:openeuler/wittyhub-cli/find-skills` |
| `skill_repo_id` | 所属 `skill_repos.id` |
| `commit_id` | `SKILL.md` 所在 skill 文件夹的最新 commit，不是 repo HEAD |
| `platform` | 优先继承 `skill_repos.platform` |
| `author` | SIG 名或仓库 owner，三级解析链见 §8.1 |

> 注意：repo HEAD commit 存在 `skill_repos.repository_commit_id`，不要写入
> `skills.extra_metadata.repository_commit_id`。

#### `skill_versions`

`skill_versions` 保存 tag 扫描得到的版本快照。每次扫描会重建当前 repo 的版本快照
（见 §10.3）。

### 10.2 latest Skills 更新规则

批量写入时：

1. 数据库存在、本次不存在的 Skill 会删除。
2. 本次新增的 Skill 会插入。
3. 已存在的 Skill 优先按技能目录 `tree_hash` 判断内容是否变化；缺少
   `tree_hash` 时回退比较 `commit_id`。
4. 内容变化的 Skill 会更新，并刷新 `updated_at`；内容相同的 Skill 跳过更新，
   `updated_at` 保持不变。
5. `download_count` 在更新时保留。

### 10.3 Tag 版本更新规则

`SkillVersion` 按 `(skill_id, version)` 进行增量同步：

1. 数据库存在、本次扫描不存在的版本会删除。
2. 本次新增的版本会插入。
3. 已存在的版本优先按技能目录 `tree_hash` 判断内容是否变化；缺少
   `tree_hash` 时回退比较 `commit_id`。
4. 内容相同的版本跳过写入，保留原记录及 `updated_at`。
5. 内容变化的版本在原记录上更新，刷新 `updated_at`、清空旧的 embedding，
   并保留 `download_count`。

安全结果复用发生在持久化之前。扫描器读取已有版本记录；当 `tree_hash` 相同且已有
评分时，复用原 `risk_score` 和 audit metadata，避免重复触发安全扫描。相同内容还可
通过本轮基于 `tree_hash` 的安全结果缓存跨 latest 和 tag 版本复用。

### 10.4 提交边界

单仓库成功写入的事务边界为：

```mermaid
flowchart LR
    A[flush Skills/SkillVersions] --> B[upsert SecurityAudits]
    B --> C[更新 Repo HEAD/skill_num/status=done]
    C --> D[一次 commit]
    A -.异常.-> E[rollback]
    B -.异常.-> E
    C -.异常.-> E
```

branch 和 local_path 在扫描过程中只 flush；最终 Repo 更新负责一次 commit。因此
Skills、SkillVersions、本轮新 SecurityAudit，以及 Repo 的最终 HEAD、数量和 done
状态要么一起成功，要么一起回滚。开始前写入的 `discovering` 状态和失败后的
`failed` 状态仍各自单独提交，以便外部观察任务状态；统一补齐
（[`src/jobs/remediation.py`](../../src/jobs/remediation.py)）是扫描
结束后的独立短事务批次，不进入本事务。

只有运行时 `_security_audit_triggered=true` 且真正获得新 Jenkins build number 的
记录才会写入或重置 `security_audits`。

## 11. 仓库状态和结果类型

仓库状态：

| 状态 | 含义 |
|---|---|
| `init` | 仓库记录刚创建 |
| `discovering` | 正在扫描 |
| `done` | 扫描和存储完成 |
| `failed` | 扫描流程发生异常 |

skill仓库扫描结果：

| result | 含义 |
|---|---|
| `created` | 新建仓库记录并完成扫描 |
| `rediscovered` | 已有仓库 commit 变化并完成重扫 |
| `unchanged` | HEAD commit 与数据库相同，跳过扫描 |
| `removed` | 已有仓库不再包含可扫描 Skill，删除 DB 仓库记录 |
| `no_skill` | 新仓库没有可扫描 Skill，不创建 DB 仓库记录 |
| `failed` | 当前仓库处理失败 |

### 11.1 每轮结果文件输出

自动调度每轮结束把结果写入 `result_dir`（默认 `<storage.local_path>/logs`）下的
`discover-<时间戳>.json`，结构如下（实现见 [`src/jobs/discover_scheduler.py`](../../src/jobs/discover_scheduler.py)）：

| 字段 | 说明 |
|---|---|
| `counters` | 仓库级统计，各项含义见下表（与 §11 结果类型口径一致） |
| `skill_stats` | skill 级统计（口径：`skills` 表 latest Skill，不含 Tag 版本） |
| `remediation_run` | 本轮补齐三类衍生字段缺口的执行结果（`security` / `category` / `embedding`），见 §11.1.3 |
| `repos` | 每仓明细：`url` / `outcome` / `skill_num` / `duration_seconds`，失败仓另含 `error` |

`counters` / `skill_stats`（含 `security_audit`、`index_creation` 子对象）/
`remediation_run`（各任务子对象）的每个字段均内嵌 `<key>_note` 文字说明
（如 `created` → `created_note`），随文件输出便于离线解读：

#### 11.1.1 counters 各项含义

| 项 | 说明 |
|---|---|
| `created` | 新建仓库记录并完成扫描 |
| `rediscovered` | 已有仓库 commit 变化并完成重扫 |
| `unchanged` | HEAD commit 与数据库相同，跳过扫描 |
| `removed` | 已有仓库不再包含可扫描 Skill，删除 DB 仓库记录 |
| `no_skill` | 新仓库没有可扫描 Skill，不创建 DB 仓库记录 |
| `failed` | 当前仓库处理失败（clone/fetch 或扫描异常），详见 `repos[].error` |

#### 11.1.2 skill_stats 结构

| 字段 | 说明 |
|---|---|
| `total_skills` | skills 表 Skill 总数 |
| `category_distribution` | 分类分布 `{category: count}`，按数量降序；NULL / 空 / `other` / `others` 统一归入 `others` |
| `security_audit.audited` | 安全检测完成（`risk_score` 非空）的 Skill 数 |
| `security_audit.pending` | 异步检测进行中（`security_audits` 有 `skillspector_collected` 为空的记录）的 Skill 数 |
| `security_audit.failed` | 安全检测失败：`risk_score` 为空且无异步检测进行中（触发失败或终态无报告） |
| `security_audit.failed_skills` | 失败明细（`skill_id` / `name` / `source_url`），最多 50 条，超出由 `failed_skills_truncated` 标记 |

#### 11.1.3 补齐结果（remediation_run）

`remediation_run` 是本轮补跑衍生字段缺口（§1 流程图 Y2 节点）的执行结果，
含 `security` / `category` / `embedding` 三个任务各自的统计（实现见
[`src/jobs/remediation.py`](../../src/jobs/remediation.py)）：

| 任务 | 正常执行输出 | 跳过/异常输出 |
|---|---|---|
| `security` | `SecurityBackfillStats.as_dict()`：`total` / `triggered` / `failed` / `skipped` | `{"skipped": "..."}`（锁被占用、检测关闭、skillspector 不可用） |
| `category` | `BackfillStats.as_dict()`：`total` / `filled` / `unclassified` / `failed` / `skipped`（见 [`category_backfill.py`](../../src/ai/category_backfill.py)） | `{"skipped": "..."}`（锁被占用、分类器不可用） |
| `embedding` | `ReindexStats.as_dict()`：`total` / `indexed` / `failed_batches` / `failed_rows` / `skipped_empty` / `stale_skipped`（见 [`src/ai/embedding_indexer.py`](../../src/ai/embedding_indexer.py)） | `{"skipped": "..."}`（锁被占用、语义搜索未启用）；任务级异常为 `{"error": "..."}` |

单任务锁被占用或失败只记录在该任务的统计里，不影响其余任务执行。

`skill_stats.index_creation` 是索引状态的存量口径（全库），四项为互斥三分法，
`indexed + missing + not_required` 恒等于 `total`：

| 字段 | 说明 |
|---|---|
| `total` | skills 表 Skill 总数 |
| `indexed` | 已建 embedding 的 Skill 数（不论是否已审计） |
| `missing` | 已审计但 embedding 缺失的 Skill 数（**索引创建失败**，等下轮 discover 或 API 启动补跑自愈） |
| `not_required` | 未审计且未建索引（`risk_score` 与 `embedding` 均为空）的 Skill 数，暂不建索引属预期状态 |

## 12. 删除与统计自愈

删除仓库记录（CLI `delete` 命令、仓库内 SKILL.md 全部消失时的单仓删除）与
扫描落库后的统计值维护（每个仓库扫描完成后执行）。

### 12.1 批量删除

`SkillRepoRepository.delete_skill_repositories(ids)`（`src/models/repository.py`）
是集合式批量删除，整批只有 4 次数据库往返：

```text
delete_skill_repositories(ids)
├── SELECT skills.id / skill_versions.id                  # 收集两类 UUID
│        WHERE skill_repo_id IN (...)                     #（tag 版本审计同样
│                                                        #  按版本 UUID 落软关联）
├── delete_skill_related_records(resource_ids)            # 批量清软关联表，
│                                                        #   返回清理总行数
│   ├── DELETE security_audits WHERE resource_id IN (...)
│   └── DELETE download_history WHERE resource_id IN (...)
└── DELETE FROM skill_repos WHERE id IN (...)            # 外键 CASCADE 删除
                                                         #   skills + skill_versions
```

- 单条删除 `delete_skill_repository(id)` 委托批量入口（CLI `delete` 命令、
  仓库内 SKILL.md 全部消失时的单仓删除共用同一实现）。
- `security_audits` / `download_history` 通过 `resource_type='skill' + resource_id`
  软关联（无外键），必须显式清理；`security_audits` 对 tag 版本（SkillVersion）
  同样落记录，因此清理集合是 skill 与 skill_versions UUID 的并集。`skills` /
  `skill_versions` 对 `skill_repos` 是 `ondelete="CASCADE"` 外键，删除仓库行时
  自动级联。
- 删除成功后清理本地 clone 目录（`SkillManager._cleanup_local_clone`，失败仅告警）。
- CLI `delete` 命令删除完成后触发一次统计自愈（见 12.2），立即修正
  `contributors.skill_count` / `skill_repos.skill_num`。

批量删除前后输出日志，便于排查线上问题：

```text
Batch delete skill repos: repo_count=N, repo_ids=[...]              # 执行前
Batch delete skill repos done: skill_repos_deleted=N,               # 执行后
    related_skill_count=N, related_records_deleted=N
```

`skill_repos_deleted` 与 `repo_count` 不一致说明有仓库被并发删除；
`related_records_deleted` 明显偏少说明软关联数据本就有缺失。

### 12.2 统计自愈

`_reconcile_skill_statistics()`（`skillcrawler/core/skill_manager.py`）按实际数据
重算两处统计值，`WHERE 旧值 <> 新值`，天然幂等（每个仓库扫描完成后、
以及 CLI `delete` 删除仓库后各执行一次）：

- `contributors.skill_count`：risk_score 非空的可见 skill 计数（与查询口径一致）；
- `skill_repos.skill_num`：仓库下的 skill 行数。

修正完成后输出日志 `Discover: statistics reconciled after scan: contributors=N, skill_repos=N`
（两类修正数均为 0 时不输出）；失败仅告警，不影响已提交的扫描结果。

## 13. 错误处理和退出码

| 场景 | 当前行为 | 退出码 |
|---|---|---:|
| 全部成功 | 输出 summary | 0 |
| commit unchanged / no_skill | 视为正常结果 | 0 |
| Jenkins 触发失败 | Skill 继续扫描，安全审计缺失 | 通常 0 |
| 扫描阶段异常（配置批量模式） | rollback + DB 状态设为 failed（单独 commit），记 failed 行，继续下一个仓库 | 1 |
| clone/fetch 异常（配置批量模式） | 异常在 try 块外上抛，DB 状态不更新，记 failed 行，继续下一个仓库 | 1 |
| CategoryClassificationError | rollback + 状态设为 failed（单独 commit），原异常直接上抛，停止后续仓库（见 §8） | 1 |
| 参数或配置错误 | 异常上抛到 main | 1 |
| `Ctrl+C` | 记录 Canceled | 130 |

批量模式只要 `failed_count > 0`，最终退出码就是 1；
`CategoryClassificationError` 会额外停止后续仓库（见 §8）。
