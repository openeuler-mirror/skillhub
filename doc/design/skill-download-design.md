# Skill 本地打包下载设计

## 1. 背景

WittyHub 的爬取阶段已经将 Skill 所属 Git 仓库克隆到本地，并在数据库中记录了：

| 数据 | 来源 | 用途 |
|------|------|------|
| `skills.skill_id` | Skill 索引记录 | 标识 Skill（格式 `{source}:{owner}/{repo}/{skill_name}`） |
| `skills.commit_id` | Skill 索引记录 | 锁定下载内容对应的 Git 提交 |
| `skills.source_url` | Skill 索引记录 | 从浏览 URL 反推 Skill 在仓库中的相对路径 |
| `skills.version` | Skill 索引记录 | 作为 ref 候选项参与路径解析 |
| `skills.name` | Skill 索引记录 | 生成 ZIP 内根目录名和下载文件名 |
| `skills.skill_repo_id` | Skill 索引记录 | 关联所属仓库 |
| `skill_versions.commit_id` | Skill 版本记录 | 指定版本下载时锁定 Git 提交 |
| `skill_versions.source_url` | Skill 版本记录 | 指定版本下载时反推仓库相对路径 |
| `skill_versions.version` | Skill 版本记录 | 指定版本下载时的版本标识 |
| `skill_versions.name` | Skill 版本记录 | 指定版本下载时生成 ZIP 文件名 |
| `skill_versions.skill_repo_id` | Skill 版本记录 | 关联所属仓库 |
| `skill_repos.url` | 仓库记录 | 解析 `owner/repo`，校验 `skill_id` 前缀 |
| `skill_repos.source` | 仓库记录 | 校验 `skill_id` 前缀 |
| `skill_repos.branch` | 仓库记录 | 作为 ref 候选项参与路径解析 |
| `skill_repos.local_path` | 仓库记录 | 定位本地 Git 仓库绝对路径 |

`storage.local_path` 是后端和爬虫共享的运行时数据根目录。当前配置为：

```text
/opt/wittyhub/
```

目录布局：

```text
/opt/wittyhub/
├── skill-repositories/      # 爬虫 clone 的 Git 仓库
├── download-cache/          # 下载接口生成的 ZIP 缓存
└── logs/                    # skillcrawler 日志
```

下载接口不再返回远端仓库 URL，而是由后端从本地 Git 仓库导出单个 Skill 目录，生成 ZIP 后直接返回给前端。

支持通过 `version` 查询参数下载历史版本。不传 `version` 时从 `skills` 表取最新版本；传入 `version` 时从 `skill_versions` 表查找对应版本记录，使用该版本的 `commit_id` 打包。

---

## 2. 总体流程

```mermaid
flowchart TD
    A["前端点击下载 ZIP"] --> B["GET /api/v1/skills/{skill_id}/download?version=v1.0.0"]
    B --> C{"version 参数是否存在"}
    C -->|否| C1["按 skill_id 查询 skills 表（最新版本）"]
    C -->|是| C2["按 skill_id + version 查询 skill_versions 表"]
    C1 --> D["预加载关联的 skill_repo"]
    C2 --> D
    D --> E["校验 skill_repo.local_path 对应的本地 Git 仓库"]
    E --> F["从 source_url 解析 Skill 相对路径"]
    F --> G["校验 commit_id"]
    G --> H["校验 commit 下存在 Skill/SKILL.md"]
    H --> I{"ZIP 缓存是否存在"}
    I -->|存在| J["复用缓存 ZIP"]
    I -->|不存在| K["git archive 生成 ZIP"]
    L --> M["写入缓存目录"]
    K --> N["记录 download_history"]
    M --> N
    N --> O["增加 download_count"]
    O --> P["FileResponse 返回 application/zip"]
    P --> Q["前端 Blob 触发浏览器保存"]
```

核心代码位置：

| 模块 | 职责 |
|------|------|
| `src/api/routes/skills.py` | 下载接口，返回 `FileResponse`，支持 `version` 查询参数 |
| `src/models/repository.py` | 查询 Skill/SkillVersion 并预加载所属 SkillRepo |
| `src/storage/downloader.py` | 解析路径、校验 Git 对象、生成或复用 ZIP（兼容 Skill 和 SkillVersion） |
| `web/src/api/client.ts` | 以 `responseType: 'blob'` 请求下载 |
| `web/src/pages/SkillDetail.vue` | 下载按钮、下载状态、浏览器保存文件 |

---

## 3. 如何根据 commit 打包

### 3.1 解析 Skill 相对路径

`skill_id` 的格式为 `{source}:{owner}/{repo}/{skill_name}`，其中 `skill_name` 是 SKILL.md 所在目录的名称。`skill_id` **不再编码完整仓库相对路径**，因此下载时需要从 `source_url`（Git 平台浏览 URL）反向解析出 Skill 在仓库中的相对目录。

示例：

```text
skill.skill_id:
atomgit:openeuler/opendesign-components/clean-code

skill.source_url:
https://atomgit.com/openeuler/opendesign-components/blob/master/packages/skills/clean-code/SKILL.md

skill_repo.source:
atomgit

skill_repo.url:
https://atomgit.com/openeuler/opendesign-components
```

解析步骤（[downloader.py `resolve_skill_relative_path`](file:///home/wjh/polymind/wittyhub/src/storage/downloader.py#L85-L118)）：

1. 从 `skill_repo.url` 提取 `owner/repo`（如 `openeuler/opendesign-components`）
2. 拼出前缀 `atomgit:openeuler/opendesign-components/`，校验 `skill_id` 以此前缀开头
3. 遍历 ref 候选列表 `[commit_id, version, branch, 'HEAD', 'master', 'main']`，在 `source_url` 中查找 `/blob/{ref}/` 标记
4. 截取标记之后的文件路径，去掉末尾 `SKILL.md`，得到相对目录

```text
source_url 中找到 /blob/master/ 标记
截取: packages/skills/clean-code/SKILL.md
去掉 SKILL.md: packages/skills/clean-code
```

这个结果就是 Skill 在仓库中的相对目录。

### 3.2 校验 commit 是否存在

后端先校验 `commit_id` 格式，只接受 7 到 64 位十六进制 Git object id。

然后执行等价命令：

```bash
git -C <skill_repo.local_path> cat-file -e <commit_id>^{commit}
```

作用：

| 结果 | 含义 |
|------|------|
| 返回码为 `0` | 本地仓库中存在该 commit |
| 返回码非 `0` | 本地仓库没有该 commit，接口返回 `409` |

### 3.3 校验该 commit 下存在 Skill

后端继续检查指定 commit 中是否存在目标 Skill 的 `SKILL.md`：

```bash
git -C <skill_repo.local_path> cat-file -e <commit_id>:<skill_relative_path>/SKILL.md
```

示例：

```bash
git -C /opt/wittyhub/skill-repositories/atomgit.com_openeuler_opendesign-components \
  cat-file -e \
  130b99b0145a4c357c9ac760b1834375716fdb01:packages/skills/clean-code/SKILL.md
```

这样可以确保下载的确实是一个 Skill 目录，而不是仓库中的普通目录。

### 3.4 使用 git archive 导出指定 commit 下的目录

真正打包时执行等价命令：

```bash
git -C <skill_repo.local_path> \
  archive \
  --format=zip \
  --prefix=<skill_name>/ \
  --output=<temporary_zip_path> \
  <commit_id>:<skill_relative_path>
```

本次示例等价为：

```bash
git -C /opt/wittyhub/skill-repositories/atomgit.com_openeuler_opendesign-components \
  archive \
  --format=zip \
  --prefix=clean-code/ \
  --output=/tmp/clean-code.zip \
  130b99b0145a4c357c9ac760b1834375716fdb01:packages/skills/clean-code
```

关键点：

| 设计 | 说明 |
|------|------|
| 使用 `<commit_id>:<path>` | 从指定 commit 的 Git tree 中导出目录 |
| 不 checkout 分支 | 不改变本地工作区，不影响爬虫更新仓库 |
| 使用 `--prefix=<skill_name>/` | ZIP 内部根目录是 `clean-code/`，不是 `packages/skills/clean-code/` |
| 使用 `--output=<temporary_zip_path>` | 先写临时文件，成功后再进入缓存 |

最终 ZIP 内容类似：

```text
clean-code/
├── SKILL.md
├── eslint.diagnose.ts
└── references/
    ├── config-object.md
    ├── guard-clause.md
    ├── reduce-complexity.md
    └── split-composable.md
```

---

## 4. ZIP 缓存设计

### 4.1 缓存目录

缓存 ZIP 存放在：

```text
<storage.local_path>/download-cache/
```

当前配置下是：

```text
/opt/wittyhub/download-cache/
```

### 4.2 缓存 Key

缓存文件名不直接使用 `skill_id`，而是使用哈希：

```python
sha256(f"{skill_id}:{commit_id}:{relative_path}")
```

生成结果：

```text
download-cache/<archive_key>.zip
```

缓存 Key 包含：

| 字段 | 作用 |
|------|------|
| `skill_id` | 区分不同 Skill |
| `commit_id` | 区分不同 Git 提交版本 |
| `relative_path` | 区分同仓库内不同目录 |

### 4.3 缓存有什么作用

缓存的作用是避免重复执行 `git archive`。

第一次下载：

```mermaid
flowchart LR
    A[请求下载] --> B[计算 archive_key]
    B --> C[缓存不存在]
    C --> D[执行 git archive]
    D --> E[生成临时 ZIP]
    E --> F[os.replace 写入缓存 ZIP]
    F --> G[返回 ZIP]
```

第二次下载同一个 Skill、同一个 commit、同一个相对路径：

```mermaid
flowchart LR
    A[请求下载] --> B[计算 archive_key]
    B --> C[缓存 ZIP 存在且非空]
    C --> D[直接返回缓存 ZIP]
```

也就是说：会重复利用。下次下载时会先查找缓存包，如果缓存包已经存在且大小大于 0，就不会再次打包。

当前判断逻辑：

```python
if archive_path.is_file() and archive_path.stat().st_size > 0:
    return archive_path
```

### 4.4 为什么缓存是安全的

缓存 Key 包含 `commit_id`，而 Git commit 的内容是不可变的。

因此：

| 情况 | 是否复用旧 ZIP |
|------|----------------|
| 同一个 Skill + 同一个 commit | 复用 |
| 同一个 Skill + 新 commit | 不复用，生成新 ZIP |
| 同仓库另一个 Skill | 不复用 |
| 相同 Skill ID 但相对路径变化 | 不复用 |

### 4.5 临时文件和原子替换

生成 ZIP 时先写临时文件：

```text
.<archive_key>.<uuid>.tmp
```

打包成功后再执行：

```python
os.replace(temporary_path, archive_path)
```

这样可以避免请求读到尚未写完的半成品 ZIP。

---

## 5. 接口流程

### 5.1 后端接口

请求：

```http
GET /api/v1/skills/{skill_id}/download                  # 最新版本
GET /api/v1/skills/{skill_id}/download?version=v1.0.0   # 指定版本
```

成功响应：

```http
HTTP/1.1 200 OK
Content-Type: application/zip
Content-Disposition: attachment; filename="clean-code-1.1.0.zip"
```

接口内部流程：

```mermaid
sequenceDiagram
    participant Web as Web 前端
    participant API as Skills Router
    participant DB as PostgreSQL
    participant DM as DownloadManager
    participant Git as 本地 Git 仓库
    participant Cache as ZIP 缓存

    Web->>API: GET /skills/{skill_id}/download?version=v1.0.0
    API->>API: 判断 version 参数是否存在
    alt version 存在
        API->>DB: 查询 SkillVersion + SkillRepo (skill_id + version)
        DB-->>API: skill_version, skill_repo
    else version 不存在
        API->>DB: 查询 Skill + SkillRepo (skill_id)
        DB-->>API: skill, skill_repo
    end
    API->>DM: create_skill_archive(skill_or_version, repository)
    DM->>DM: 从 source_url 解析 Skill 相对路径
    DM->>Git: cat-file -e commit
    Git-->>DM: commit 存在
    DM->>Git: cat-file -e commit:path/SKILL.md
    Git-->>DM: Skill 存在
    DM->>Cache: 查找 archive_key.zip
    alt 缓存存在
        Cache-->>DM: 返回缓存 ZIP
    else 缓存不存在
        DM->>Git: git archive commit:path
        Git-->>DM: 生成临时 ZIP
        DM->>Cache: 写入 archive_key.zip
    end
    DM-->>API: SkillArchive(path, filename)
    API->>DB: 写 download_history (resource_type=skill 或 skill_version)
    API->>DB: increment download_count (skills 或 skill_versions 表)
    API-->>Web: FileResponse application/zip
```

### 5.2 前端流程

前端调用：

```ts
const blob = await api.getSkillDownload(skill.value.skill_id)
// 下载指定版本
const blob = await api.getSkillDownload(skill.value.skill_id, 'v1.0.0')
```

API client 使用：

```ts
client.get(`/skills/${encodeURIComponent(skillId)}/download`, {
  params: version ? { version } : undefined,
  responseType: 'blob',
})
```

页面拿到 `Blob` 后：

```mermaid
flowchart TD
    A[点击下载 ZIP] --> B[按钮进入下载中状态]
    B --> C[请求后端下载接口]
    C --> D[收到 ZIP Blob]
    D --> E[URL.createObjectURL]
    E --> F[创建 a 标签并 click]
    F --> G[浏览器保存 ZIP]
    G --> H[URL.revokeObjectURL]
    H --> I[本地下载量 +1]
```

---

## 6. 异常处理

| 场景 | 后端状态码 | 说明 |
|------|------------|------|
| Skill 不存在 | `404` | `skill_id` 没有对应记录 |
| 指定版本不存在 | `404` | `skill_id` + `version` 在 `skill_versions` 表无记录 |
| SkillRepo 缺失 | `409` | Skill 没有关联仓库信息 |
| `local_path` 为空或不存在 | `409` | 本地仓库不可用 |
| `local_path` 不是 Git 仓库 | `409` | 本地仓库无效 |
| `skill_id` 不属于该仓库 | `409` | 前缀不匹配 |
| `source_url` 无法解析路径 | `404` | 浏览 URL 中找不到已知 ref 的 `/blob/{ref}/` 标记 |
| `commit_id` 缺失或格式非法 | `409` | 不能定位 Git 提交 |
| 本地仓库没有该 commit | `409` | 需要重新同步仓库 |
| 指定 commit 下没有 `SKILL.md` | `404` | 该提交下目标 Skill 不存在 |
| `git archive` 失败 | `500` | 打包失败 |

---

## 7. 设计收益

| 收益 | 说明 |
|------|------|
| 下载内容稳定 | 使用 `commit_id` 锁定索引时的 Git tree |
| 不依赖远端下载 URL | 不受 AtomGit/Gitee/GitHub 归档地址差异影响 |
| 只下载单个 Skill | 不把整个仓库传给前端 |
| 不修改本地工作区 | `git archive` 不需要 checkout |
| 可复用缓存 | 同一 Skill 同一 commit 后续下载直接返回缓存 ZIP |
| 前端体验直接 | 浏览器收到 `application/zip` 后保存文件 |
