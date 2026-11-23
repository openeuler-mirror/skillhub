# 贡献你的 Skill

> 想把你的 AI 技能分享给更多的开发者使用？在 `openEuler-skills` 仓库里登记一个配置文件，就能把你的 Skill 上架到 SkillHub。

## 发布前，你需要准备什么

在提交之前，你的 Skill 本身需要是一个"能被识别"的单元：它通常位于一个 Git 仓库中，且包含一个 `SKILL.md` 作为"名片"（写清它叫什么、能做什么、怎么用），必要时附上相关的依赖文件。

准备好之后，你无需把 Skill 文件搬进登记仓库，只需要**写一个登记配置文件 `skill.yaml`**，告诉平台你的技能在哪儿。

## 上架方式：提交 PR 登记 Skill

SkillHub 贡献仓库是 **`https://atomgit.com/openeuler/openEuler-skills`**（镜像于 atomgit 的 `openEuler/openEuler-skills`）。它按照贡献来源分为三个目录，你在其中新建对应的一种 `skill.yaml` 即可：

```
openEuler-skills/
├── community/   # 社区 SIG 提交到这里
├── enterprise/  # 企业组织提交到这里
├── personal/    # 个人开发者提交到这里
└── templates/   # 三种 skill.yaml 的空模板（照着填即可）
```

### 个人开发者：在 personal/ 下登记

1. **Fork 并 Clone** 上述贡献仓库到本地。
2. 在 `personal/` 目录下新建一个以你的用户名命名的目录（如 `personal/你的用户名/`），并在其中创建 `skill.yaml`。
3. 参照 `templates/personal-skill-spec.yaml` 填写。写法很简单，例如：

   ```yaml
   author:
     name: 你的用户名
   skill_repos:
   - url: https://atomgit.com/你的用户名/你的仓库名
   skills: []
   ```

   - `author`：你的基本信息；
   - `skill_repos`：包含你 Skill 的**仓库链接**（可选）；
   - `skills`：直接指向某个具体 `SKILL.md` 的**URL 列表**（可选）。

   你也可以把 Skill 目录直接放进 `personal/你的用户名/<技能名>/SKILL.md`。

4. 提交一个 **PR**（Pull Request），等待审核合入。

### 社区 SIG 与企业：在 community/ 或 enterprise/ 下登记

- **社区 SIG** 在 `community/<sig_name>/skill.yaml` 登记，`<sig_name>` 需是你所在 SIG 的名称。
- **企业组织** 在 `enterprise/<组织名>/skill.yaml` 登记，额外填写企业的组织信息与维护者列表。
- 两者都使用 `templates/` 下对应的模板（`community-skill-spec.yaml` / `enterprise-skill-spec.yaml`），通过 `skill_repos` 或 `skills` 引用你的能力来源。

填写完成后同样提交 **PR**，审核合入后平台会自动认领你登记的 Skill。

## 提交之后会发生什么

你的 PR 通过审核、登记配置合入后，剩余的事**全部由平台自动完成**：

1. **扫描识别**：自动前往你登记的仓库 / SKILL.md 地址，读取能力信息，包括名称、简介、版本、作者。
2. **智能归类**：如果没指定，系统会自动帮你的 Skill 分到合适的领域（如"开发构建""监控运维"）。
3. **安全审查**：自动对你的 Skill 做安全评估（详见[安全评估：你的 Skill 安不安全](./skillhub-security-audit.md)）。
4. **上架展示**：评估完成的 Skill 会**同步出现在首页**，进入公开列表，供其他开发者搜索、浏览和安装。
5. **热度统计**：平台根据仓库热度与下载情况估算排行，用于排序展示。

## 如何管理已上架的 Skill

发布之后，保持更新同样轻松：

- **改进了 Skill？** 平台会自动察觉到仓库的更新，拉到最新版本；如果内容没变，就不会做多余的动作。
- **发现评估没完成？** 平台会在后台自动重试，直到拿到可靠的安全结论。
- **需要调整登记？** 修改你在 `community/`、`enterprise/` 或 `personal/` 下的 `skill.yaml`，再次提交 PR 即可。

> 提示：想深入了解平台怎么对 Skill 做安全把关，请看[安全评估：你的 Skill 安不安全](./skillhub-security-audit.md)。

## 一些让 Skill 更容易被使用的小建议

- **写清楚 `SKILL.md`**：名称、简介、使用说明越清楚，越容易被人检索到、用起来。
- **保证仓库可访问**：公开、稳定，且确实存在 `SKILL.md`。
- **归入合适的分类**：如果属于企业组织或社区 SIG，登记到对应的分组，展示更清晰。
- **持续更新**：保持仓库活跃，平台每次同步都会捕获到你的最新成果。
