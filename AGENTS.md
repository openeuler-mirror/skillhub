# Wittyhub — Agent 长期记忆

> 本文件是所有 AI 编码工具在此仓库中工作的**唯一入口**和**长期记忆**，遵循 [agents.md](https://agents.md) 业界标准。

---

## 1. 项目速览

**Wittyhub** 是一个 AI Agent Skills 检索与分发平台，基于 **FastAPI + Vue 3** 构建。

- 后端：Python FastAPI + PostgreSQL，Docker Compose 部署
- 前端：**Vue 3 + Vite + Tailwind CSS**
- UI 组件库：**@opensig/opendesign**（OpenDesign Vue 3 组件库）
- 状态管理：Pinia
- HTTP 客户端：axios
- Skills 引擎：基于 agent-spec 标准的 Skills 管理

### 常用命令

| 命令 | 用途 |
|------|------|
| `cd web && npm run dev` | 启动前端开发服务器 |
| `cd web && npm run build` | 前端生产构建 |
| `docker compose up` | 启动全栈服务 |
| `python scripts/populate_skills.py` | 填充 Skills 数据 |

---

## 2. 目录结构

```
skillhub/
├── .agent/
│   └── skills/
│       ├── opendesign-components/   # OpenDesign 46+ 组件使用指南 Skill
│       └── opendesign-tokens/       # OpenDesign 设计令牌 Skill（6 套主题）
├── src/                             # Python 后端 (FastAPI)
│   ├── api/
│   │   ├── routes/                  # API 路由
│   │   ├── schemas/                 # 数据模型
│   │   └── services/                # 业务逻辑
│   └── ...
├── web/                             # Vue 3 前端
│   ├── src/
│   │   ├── pages/                   # 页面组件
│   │   ├── components/              # 通用组件
│   │   ├── api/                     # API 客户端
│   │   └── styles/                  # 样式
│   └── ...
├── deploy/                          # Docker 部署配置
├── scripts/                         # 工具脚本
└── AGENTS.md                        # 本文件（长期记忆）
```

### 关键参考路径

| 路径 | 用途 |
|------|------|
| `.agent/skills/opendesign-components/` | OpenDesign 组件库使用指南（46+ 组件详细文档） |
| `.agent/skills/opendesign-tokens/` | OpenDesign 设计令牌指南（6 套主题 Token） |

---

## 3. 前端开发核心记忆

### 3.1 风格参考源

| 维度 | 参考方式 |
|------|---------|
| 组件写法 | 参考 `components/` 下的 `.vue` 文件，使用 `<script setup lang="ts">` + scoped SCSS |
| 页面布局 | 参考 `views/` 下的页面组件，使用 `AppSection` / `ContentWrapper` 容器 |
| 状态管理 | 参考 `stores/` 下的 Pinia store（options API 写法） |
| 组合式函数 | 参考 `composables/` 下的 `useXxx` 模式 |
| 样式体系 | 参考 `assets/style/` 下的 SCSS mixin 和 token 变量 |
| 图标使用 | 参考 SVG 图标导入和使用方式 |
| API 封装 | 参考 `shared/axios/` 的 axios 实例封装 |

### 3.2 组件编写规范

组件编写规则：

```
- 使用 <script setup lang="ts"> + TypeScript interface 定义 Props
- 样式使用 <style lang="scss" scoped>
- 组件名使用 PascalCase，文件夹名使用 kebab-case
- 通用组件放在 components/ 目录，页面级组件放在 views/ 目录
```

### 3.3 响应式断点体系

断点定义：

| 断点 | 范围 | CSS 写法 |
|------|------|---------|
| phone | ≤600px | `@include respond-to('<=phone')` |
| pad_v | 601-840px | `@include respond-to('<=pad_v')` |
| pad_h | 841-1200px | `@include respond-to('<=pad_h')` |
| laptop | 1201-1440px | `@include respond-to('>laptop')` |

JS 端通过 `useScreen()` composable 获取 `isPhone`、`isPad`、`isLaptop` 等响应式状态。

### 3.4 字体排版系统

字体排版 mixin：

| Mixin | 用途 | PC 字号/行高 | Pad 字号/行高 |
|-------|------|-------------|---------------|
| `@include display1` | 一级数据展示 | 56px/80 | 40px/56 |
| `@include h1` | 一级标题 | 32px/44 | 20px/28 |
| `@include h2` | 二级标题 | 24px/32 | 18px/26 |
| `@include text1` | 常规正文 | 16px/24 | 14px/22 |
| `@include tip1` | 提示文本 | 14px/22 | 12px/18 |

### 3.5 常用 SCSS Mixin

| Mixin | 说明 |
|-------|------|
| `@include respond-to(...)` | 响应式断点（支持 `<=`, `>`, 直接区间匹配） |
| `@include in-dark` | 暗色模式样式容器 |
| `@include text-truncate(n)` | 多行文本截断 |
| `@include scrollbar` | 自定义滚动条 |
| `@include hoverable` / `@include hover` | 悬停样式（区分触控设备） |

### 3.6 暗色模式

通过 `data-o-theme` 属性控制：
- `data-o-theme="e.light"` — 浅色模式
- `data-o-theme="e.dark"` — 深色模式

所有组件需同时提供 light/dark 样式，暗色模式样式放在 `@include in-dark { ... }` 中。

---

## 4. OpenDesign 组件使用约定

### 4.1 核心依赖

```json
{
  "@opensig/opendesign": "≥1.2.5",
  "@opensig/opendesign-token": "≥0.1.1"
}
```

### 4.2 引入方式

```typescript
// main.ts
import '@opensig/opendesign/es/index.css'
import '@opensig/opendesign-token/themes/e.token.css'

document.documentElement.setAttribute('data-o-theme', 'e.light')
```

### 4.3 组件导入

```typescript
import { OButton, OInput, OIcon, OLink, OCard, OTag, OSelect, ODataTable, ODialog, OPagination, ORow, OCol } from '@opensig/opendesign'
```

### 4.4 设计令牌使用

全部使用 CSS 变量，禁止硬编码：

| 类别 | 示例变量 |
|------|---------|
| 颜色 | `var(--o-color-info1)`、`var(--o-color-fill2)`、`var(--o-color-link1)` |
| 间距 | `var(--o-spacing-h4)`、`var(--o-gap-section)`、`var(--o-gap-t2c)` |
| 圆角 | `var(--o-radius-m)` |
| 字号 | `var(--o-font_size-h1)`、`var(--o-font_size-text1)` |
| 阴影 | `var(--o-shadow-l1)` |

### 4.5 红线

1. **必须用 OpenDesign 组件**：`OButton`/`OSelect`/`ODataTable`/`ODialog` 等存在的场景，禁止使用原生 HTML 或 Element Plus 替代
2. **样式必须用 CSS 变量**：禁止硬编码颜色、间距、字号值
3. **Vue API 必须显式 import**：`import { ref, computed, onMounted } from 'vue'`
4. **样式覆盖用 `:deep()`**：当需要覆盖 OpenDesign 组件内部样式时

---

## 5. Skills 使用约定

### 5.1 可用 Skills

| Skill | 位置 | 用途 |
|-------|------|------|
| `opendesign-components` | `.agent/skills/opendesign-components/SKILL.md` | 46+ 个 OpenDesign 组件使用指南 |
| `opendesign-tokens` | `.agent/skills/opendesign-tokens/SKILL.md` | 6 套主题的设计令牌参考 |

### 5.2 触发场景

| 场景 | 使用的 Skill |
|------|-------------|
| 写/改 Vue 组件 | `opendesign-components`（查询组件 API 和示例） |
| 查颜色/间距/圆角 token | `opendesign-tokens`（查找 CSS 变量名） |
| 确认组件 Props/Events | `opendesign-components/references/{组件名}.md` |

### 5.3 Skills 文件结构

```
.agent/skills/
├── opendesign-components/
│   ├── SKILL.md                    # 组件库索引与使用指南
│   └── references/                 # 各组件详细文档
│       ├── button.md
│       ├── dialog.md
│       ├── data-table.md
│       └── ... (46+ 组件)
└── opendesign-tokens/
    ├── SKILL.md                    # 设计令牌使用指南
    └── references/                 # 各主题 token 文档
        ├── tokens-openeuler.md
        ├── tokens-ascend.md
        └── ... (6 套主题)
```

---

## 6. Workflow

1. **理解需求**：检查 `AGENTS.md` 获取项目记忆上下文
2. **查阅 Skill**：根据场景选择对应的 Skill（opendesign-components / opendesign-tokens）
3. **编写代码**：遵循 OpenDesign 组件规范和前端风格约定
4. **自检**：检查是否使用 CSS 变量、是否正确导入组件、是否覆盖了 light/dark 模式

---

## 7. 红线

- **HIGH**: 有 OpenDesign 组件时禁止使用原生 HTML 替代
- **HIGH**: 禁止硬编码颜色/间距/字号值，必须使用 CSS 变量
- **HIGH**: 新增 Vue 组件时必须同时提供 light/dark 模式样式
- **MEDIUM**: 参考 `opendesign-components` Skill 获取组件正确 API 用法

---

> **最后更新**：2026-07-16
