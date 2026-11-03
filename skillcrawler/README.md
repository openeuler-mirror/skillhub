# skillcrawler

`skillcrawler` 是 WittyHub 的 Skill 仓库发现工具。它从一组明确的 Git 仓库完成 clone/fetch、扫描 `SKILL.md`、分类和入库。

> 实现细节（总体流程、Git 同步策略、Tag 两阶段拉取、commit 去重、安全检测集成、数据库写入、错误处理）见 [`doc/design/skillcrawler-discover-design.md`](../doc/design/skillcrawler-discover-design.md)，建议从「§1 总体流程」开始阅读。

---

## 1. CLI 命令

入口文件：

```bash
python skillcrawler/main.py <command>
```

| 命令 | 用途 |
|------|------|
| `query` | 查询 `skill_repos` 记录 |
| `discover` | 注册、clone/fetch、扫描并同步 Skill |
| `delete` | 删除 `skill_repos` 记录及本地 clone |

### query

查询所有 skill repo：

```bash
python skillcrawler/main.py query
```

按 id 查询：

```bash
python skillcrawler/main.py query --id <repo_id>
```

支持参数：

- `-i` / `--id`：查询单个 skill repo

### discover

`discover` 是唯一扫描入口。

支持参数：

| 参数 | 说明 |
|------|------|
| `-p` / `--platform` | 选择 `community` / `enterprise` / `personal` 仓库列表；不传时读取全部列表 |
| `-r` / `--repository-path` | 指定已有的 openEuler-skills 本地目录；默认下载到 `<storage.local_path>/skill-repositories/openEuler-skills` |
| `-u` / `--url` | 只扫描指定的单个 skill.yaml（不读全量配置）；传本地文件路径，或相对 openEuler-skills catalog 的路径 |

默认扫描全部配置列表：

```bash
python skillcrawler/main.py discover
```

扫描指定平台配置列表：

```bash
python skillcrawler/main.py discover --platform community
python skillcrawler/main.py discover --platform enterprise
python skillcrawler/main.py discover --platform personal
```

使用已有的本地 openEuler-skills 仓库：

```bash
python skillcrawler/main.py discover --repository-path /path/to/openEuler-skills
```

只扫描单个 skill.yaml（catalog 相对路径或本地文件路径）：

```bash
python skillcrawler/main.py discover --url personal/leon-wang2021/skill.yaml
python skillcrawler/main.py discover --url /path/to/openEuler-skills/personal/leon-wang2021/skill.yaml
```

单文件模式说明：

- 只扫描该 skill.yaml 登记的 `skill_repos`（整仓）与 `skills[]`（单 Skill）条目；
- `platform` / owner 取自目录结构（`<catalog>/<platform>/<owner>/skill.yaml`），
  无需传 `--platform`；
- 文件必须位于 `community` / `enterprise` / `personal` 之下两级目录且名为
  `skill.yaml`，否则报错。

### delete

删除仓库记录和本地 clone：

```bash
python skillcrawler/main.py delete --id <repo_id>
```

支持参数：

- `-i` / `--id`：必填，指定要删除的 skill repo

---

## 2. 仓库来源配置

默认仓库列表来源：

```text
openEuler-skills/{community,enterprise,personal}/*/skill.yaml
```

示例：

```yaml
# community/Infrastructure/skill.yaml
name: Infrastructure
skill_repos:
  - url: https://atomgit.com/openeuler/IB_Robot
skills:                                          # 可选：单 Skill URL 贡献
  - skill_name: skill-name
    skill_url: https://github.com/example/skills/blob/main/skills/skill-name/SKILL.md
    description: 一句话说明该 Skill 的用途
    category: Development and Build
```

两种登记方式：

| 字段 | 扫描范围 | 说明 |
|------|---------|------|
| `skill_repos` | 整仓 | 克隆仓库后收录其中全部 SKILL.md |
| `skills[]` | 单 Skill | 只收录 `skill_url` 指向的 SKILL.md（含其 tag 版本），`skill_name` / `skill_url` 必填 |

`skills[]` 约定：

- `skill_url` 格式为 `<repo>/blob/<ref>/<path>/SKILL.md`，`ref` 取 `/blob/`
  后第一段，应为单段分支名（作为 clone 分支）；
- 同一仓库的多条 `skills[]` 条目合并为一次扫描（路径取并集），且 `ref` 必须一致；
- 仓库已被 `skill_repos` 登记时，其 `skills[]` 条目被整仓扫描覆盖，不再单独处理。

平台映射：

| 目录分组 | 写入 platform |
|----------|---------------|
| `community` | `community` |
| `personal` | `personal` |
| `enterprise` | `enterprise` |

---

## 3. 输出与日志路径

默认本地存储来自 `config.yaml`：

```yaml
storage:
  local_path: /opt/wittyhub/
```

仓库 clone 路径：

```text
<storage.local_path>/skill-repositories/<repo_name>
```

示例：

```text
/opt/wittyhub/skill-repositories/atomgit.com_openeuler_wittyhub-cli
```

`repo_name` 的生成方式：

```text
删除 http:// 或 https://
删除末尾 .git
将 / 替换为 _
```

日志路径：

```text
<storage.local_path>/logs/skillcrawler.log
```

---

## 4. 推荐使用方式

推荐从项目根目录执行：

```bash
cd wittyhub
python skillcrawler/main.py query
python skillcrawler/main.py discover
```

也支持从 `skillcrawler/` 目录执行：

```bash
cd wittyhub/skillcrawler
python main.py query
python main.py discover
```

查看帮助：

```bash
python skillcrawler/main.py --help
python skillcrawler/main.py query --help
python skillcrawler/main.py discover --help
python skillcrawler/main.py delete --help
```
