<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/client'
import type { Skill, SkillVersion } from '@/api/types'
import { marked } from 'marked'
import { createHighlighter, type Highlighter } from 'shiki'
import { useAppStore } from '@/stores/app'
import heroBgLight from '@/assets/bg/hero-top-texture.png'
import heroBgDark from '@/assets/bg/hero-top-texture-dark.png'
import copySvg from '@/assets/icons/copy.svg?raw'
import checkSvg from '@/assets/icons/check.svg?raw'
import downloadSvg from '@/assets/icons/download.svg?raw'
import timeSvg from '@/assets/icons/skill-update-time.svg?raw'
import riskScoreSvg from '@/assets/icons/skill-risk-score.svg?raw'
import categorySvg from '@/assets/icons/skill-category.svg?raw'
import personSvg from '@/assets/icons/person.svg?raw'
import { OBreadcrumb, OBreadcrumbItem, OLoading, ODialog, OButton, OPopover, OIconInfoTip, useToast } from '@opensig/opendesign'
import { oaReport } from '@opendesign-plus/plugins/analytics'

const route = useRoute()
const router = useRouter()
const appStore = useAppStore()
const { show: showToast } = useToast()

const platformNames: Record<string, string> = {
  community: '社区SIG',
  enterprise: '企业组织',
  personal: '个人'
}

const skill = ref<Skill | null>(null)
const versions = ref<SkillVersion[]>([])
const loading = ref(true)
const error = ref('')
const activeTab = ref<'versions' | 'usage'>('usage')
const downloading = ref(false)
const toastVisible = ref(false)
// 第三方链接跳转提示
const externalDialogVisible = ref(false)
const externalUrl = ref('')

function confirmExternalLink() {
  if (!skill.value?.source_url) return
  externalUrl.value = skill.value.source_url
  externalDialogVisible.value = true
}

function openExternalUrl() {
  if (!externalUrl.value) return
  window.open(externalUrl.value, '_blank', 'noopener')
  externalDialogVisible.value = false
  oaReport('click_external_source', { module: 'skill_detail', skill_id: skill.value?.skill_id, host: (() => { try { return new URL(externalUrl.value).host } catch { return '' } })() })
}

/** 风险评估报告页链接：作为 OButton 的 href，使按钮渲染为 <a>，支持右击“在新标签页/新窗口打开” */
const riskReportHref = computed(() =>
  skill.value ? `/skills/${encodeURIComponent(skill.value.skill_id)}/report` : ''
)

/** 进入该 Skill 的风险评估报告页 */
function goRiskReport(e?: MouseEvent) {
  if (!skill.value) return
  oaReport('click_risk_report', { module: 'skill_detail', skill_id: skill.value.skill_id })
  // 修饰键 / 非左键点击交给浏览器原生 <a> 行为（新标签页、新窗口），不拦截
  if (e && (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button !== 0)) return
  e?.preventDefault()
  router.push(riskReportHref.value)
}

/** 「风险评估说明」浮层的查看详情：打开安全评估说明文档（docs/skillhub-security-audit.md） */
function goSecurityDoc() {
  oaReport('click_risk_guide', { module: 'skill_detail', skill_id: skill.value?.skill_id })
  router.push('/docs/skillhub-security-audit')
}

// ===== Shiki 代码高亮 =====
let highlighter: Highlighter | null = null
const highlighterReady = ref(false)

async function initHighlighter() {
  highlighter = await createHighlighter({
    themes: ['github-light', 'github-dark'],
    langs: ['bash', 'shell', 'javascript', 'typescript', 'python', 'json', 'yaml', 'markdown', 'html', 'css', 'vue', 'sql', 'go', 'java', 'rust', 'xml', 'diff']
  })
  highlighterReady.value = true
}

function showCopyToast() {
  toastVisible.value = true
  setTimeout(() => { toastVisible.value = false }, 2000)
}

function stripFrontmatter(content: string): string {
  const match = content.match(/^---\n[\s\S]*?\n---\n?/)
  if (match) {
    return content.slice(match[0].length).trim()
  }
  return content
}

// 使用描述展示 skills 表当前版本（latest）的内容；历史版本仅在版本信息表格中列出
const displayContent = computed(() => skill.value?.content ?? null)

const renderedContent = computed(() => {
  if (!displayContent.value) return ''
  // 触发响应式：highlighterReady 变化时重新计算
  const ready = highlighterReady.value

  const renderer = new marked.Renderer()
  renderer.code = (code: string, lang: string | undefined) => {
    let highlighted: string
    if (ready && highlighter && lang) {
      try {
        highlighted = highlighter.codeToHtml(code, {
          lang,
          themes: { light: 'github-light', dark: 'github-dark' }
        })
      } catch {
        highlighted = `<pre><code>${escapeHtml(code)}</code></pre>`
      }
    } else {
      highlighted = `<pre><code>${escapeHtml(code)}</code></pre>`
    }
    return `<div class="code-block-wrap">
  <button class="code-copy-btn" data-code="${code.replace(/"/g, '&quot;')}" aria-label="复制代码">${copySvg}</button>
  ${highlighted}
</div>\n`
  }
  renderer.heading = (text: string, level: number) => {
    const tag = `h${level}`
    return `<${tag}>${text}</${tag}>\n`
  }
  return marked(stripFrontmatter(displayContent.value), { renderer })
})

function escapeHtml(str: string): string {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

async function copyMarkdownCode(e: MouseEvent) {
  const btn = (e.target as HTMLElement).closest('.code-copy-btn') as HTMLElement
  if (!btn) return
  const code = btn.dataset.code
  if (!code) return
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(code)
    } else {
      const textarea = document.createElement('textarea')
      textarea.value = code
      textarea.style.position = 'fixed'
      textarea.style.opacity = '0'
      document.body.appendChild(textarea)
      textarea.select()
      document.execCommand('copy')
      document.body.removeChild(textarea)
    }
    showCopyToast()
  } catch (e) {
    console.error('复制代码失败:', e)
  }
}

// riskClass/arcColor 用于右侧风险评估卡片
function getSecurityLevel(score: number | null): { label: string; riskClass: string; arcColor: string; desc: string } {
  if (score === null) return { label: '未检测', riskClass: 'risk-gray', arcColor: '', desc: '暂无扫描结果' }
  if (score <= 20) return { label: '安全', riskClass: 'risk-green', arcColor: 'var(--o-color-success1)', desc: 'SkillSpector 得分 0–20，处于安全区间' }
  if (score <= 50) return { label: '低风险', riskClass: 'risk-blue', arcColor: '#497AF8', desc: 'SkillSpector 得分 21–50，处于低风险区间' }
  if (score <= 80) return { label: '中风险', riskClass: 'risk-orange', arcColor: 'var(--o-color-warning1)', desc: 'SkillSpector 得分 51–80，处于中风险区间' }
  return { label: '高风险', riskClass: 'risk-red', arcColor: 'var(--o-color-danger1)', desc: 'SkillSpector 得分 81–100，处于高风险区间' }
}

const securityLevel = computed(() => getSecurityLevel(skill.value?.risk_score ?? null))

// 得分圆弧：以 100 分为满分，圆弧从 12 点方向起顺时针填充「得分 ÷ 100 × 360°」的角度
// 圆心 (70, 70)、半径 63（140px 仪表盘）；得分变化时自动重算
const riskScore = computed(() => {
  const score = skill.value?.risk_score
  return typeof score === 'number' ? Math.min(Math.max(score, 0), 100) : null
})
// 满分绘制整圆（arc 起止点重合无法用 path 表达）
const scoreArcFull = computed(() => riskScore.value === 100)
const scoreArcPath = computed(() => {
  const score = riskScore.value
  // 未检测 / 0 分不绘制圆弧；100 分由整圆元素绘制
  if (score === null || score <= 0 || score >= 100) return ''
  const angle = score * 3.6
  const rad = (angle * Math.PI) / 180
  const x = (70 + 63 * Math.sin(rad)).toFixed(2)
  const y = (70 - 63 * Math.cos(rad)).toFixed(2)
  return `M 70 7 A 63 63 0 ${angle > 180 ? 1 : 0} 1 ${x} ${y}`
})
// 风险评分展示：有分值显示 x/100，未检测显示文案
const riskScoreText = computed(() => {
  const score = skill.value?.risk_score
  if (score === null || score === undefined) return '未检测'
  return `${score}/100`
})

// 统一 skill_id 到新格式 {source_type}:{owner}/{repo}/{skill_dir}。
// 历史数据可能仍为旧格式 {source_type}/{owner}/{repo}/<path...>，
// 优先用 source_url 的 SKILL.md 路径确定目录名，确保提示词恒为新格式。
function toNewSkillId(skillId: string, sourceUrl?: string | null): string {
  const sourceTypes = ['github', 'atomgit', 'gitlab', 'gitee']
  const id = (skillId || '').trim()
  const colon = id.indexOf(':')
  if (colon > 0 && sourceTypes.includes(id.slice(0, colon).toLowerCase())) {
    return id // 已是新格式
  }
  const parts = id.split('/').filter(Boolean)
  const sourceType = parts[0]?.toLowerCase() ?? ''
  if (!sourceTypes.includes(sourceType) || parts.length < 3) return id
  const owner = parts[1]!
  const repo = parts[2]!
  let skillDir: string | undefined
  const blobMatch = sourceUrl?.match(/\/blob\/[^/]+\/(.+?)\/?SKILL\.md$/i)
  if (blobMatch) {
    skillDir = blobMatch[1]!.split('/').filter(Boolean).pop()
  }
  if (!skillDir) {
    skillDir = parts.slice(3).filter((p) => p !== '.').pop() ?? repo
  }
  return `${sourceType}:${owner}/${repo}/${skillDir}`
}

// AI 提示词：复制后发送给任意 AI Agent，由其读取安装指南并执行安装
const installPrompt = computed(() => {
  if (!skill.value) return ''
  const skillId = toNewSkillId(skill.value.skill_id, skill.value.source_url)
  return `请根据 https://skillhub.openeuler.org/install/skillhub.md，安装 ${skillId}。`
})

async function copyInstallPrompt() {
  if (!skill.value) return
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(installPrompt.value)
    } else {
      const textarea = document.createElement('textarea')
      textarea.value = installPrompt.value
      textarea.style.position = 'fixed'
      textarea.style.opacity = '0'
      document.body.appendChild(textarea)
      textarea.select()
      document.execCommand('copy')
      document.body.removeChild(textarea)
    }
    showCopyToast()
    oaReport('copy_ai_prompt', { module: 'skill_detail', skill_id: skill.value.skill_id })
  } catch (e) {
    console.error('复制失败:', e)
  }
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return '-'
  const d = new Date(dateStr)
  const year = d.getFullYear()
  const month = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

// 从 Blob 错误响应中解析后端返回的 detail 文案（responseType: 'blob' 时 e.response.data 是 Blob 而非 JSON）
async function extractBlobErrorDetail(e: unknown): Promise<string> {
  const blob = (e as { response?: { data?: unknown } })?.response?.data
  if (blob instanceof Blob) {
    try {
      const parsed = JSON.parse(await blob.text())
      if (parsed?.detail) return String(parsed.detail)
    } catch { /* 非 JSON 响应，走兜底 */ }
  }
  return (e as { message?: string })?.message || '下载失败，请稍后重试'
}

// 触发浏览器下载 Blob；延迟释放 objectURL，避免 Safari 等浏览器中断下载
function triggerBlobDownload(blob: Blob, filename: string) {
  const url = window.URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => window.URL.revokeObjectURL(url), 1000)
}

async function downloadSkill() {
  if (!skill.value || downloading.value) return
  downloading.value = true
  try {
    const { blob, filename } = await api.getSkillDownload(skill.value.skill_id)
    triggerBlobDownload(blob, filename)
    oaReport('download_zip', { module: 'skill_detail', skill_id: skill.value.skill_id, success: true })
  } catch (e) {
    const detail = await extractBlobErrorDetail(e)
    console.error('下载失败:', e)
    showToast({ content: detail, long: true })
    oaReport('download_zip', { module: 'skill_detail', skill_id: skill.value.skill_id, success: false })
  } finally {
    downloading.value = false
  }
}

// 版本信息表格：按指定版本下载 ZIP
const downloadingVersion = ref<string | null>(null)

async function downloadVersionSkill(version: string) {
  if (!skill.value || downloadingVersion.value) return
  downloadingVersion.value = version
  try {
    const { blob, filename } = await api.getSkillDownload(skill.value.skill_id, version)
    triggerBlobDownload(blob, filename)
    oaReport('download_version_zip', { module: 'skill_detail', skill_id: skill.value.skill_id, version, success: true })
  } catch (e) {
    const detail = await extractBlobErrorDetail(e)
    console.error('版本下载失败:', e)
    showToast({ content: `${version} 下载失败：${detail}`, long: true })
    oaReport('download_version_zip', { module: 'skill_detail', skill_id: skill.value.skill_id, version, success: false })
  } finally {
    downloadingVersion.value = null
  }
}

onMounted(async () => {
  const skillId = route.params.skillId as string
  if (!skillId) {
    error.value = '缺少 Skill ID'
    loading.value = false
    return
  }
  loading.value = true
  // 并行初始化 Shiki 高亮器和加载数据
  initHighlighter()
  try {
    const [skillRes, versionsRes] = await Promise.all([
      api.getSkill(decodeURIComponent(skillId)),
      api.getSkillVersions(decodeURIComponent(skillId))
    ])
    skill.value = skillRes
    versions.value = versionsRes.versions || []
    oaReport('detail_view', {
      module: 'skill_detail',
      skill_id: skill.value.skill_id,
      skill_name: skill.value.name
    })
  } catch (e: any) {
    console.error('加载 Skill 详情失败:', e)
    error.value = e?.response?.data?.detail || e.message || '加载失败'
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="detail-page">
    <!-- ========== Hero 区域 ========== -->
    <section class="hero-section">
      <div class="absolute inset-0 pointer-events-none">
        <img :src="appStore.isDark ? heroBgDark : heroBgLight" alt="" class="w-full h-full object-cover" />
      </div>
      <div class="container-wide relative flex flex-col">
        <!-- 复制成功提示 -->
        <transition name="toast">
          <div v-if="toastVisible" class="copy-toast">
            <span class="toast-icon" v-html="checkSvg"></span>
            <span class="toast-text">复制成功</span>
          </div>
        </transition>
        <!-- 面包屑 -->
        <div class="breadcrumb-wrap">
          <OBreadcrumb
            style="
              --breadcrumb-text-size: 14px;
              --breadcrumb-text-height: 22px;
              --breadcrumb-separator-size: 24px;
              --breadcrumb-gap: 4px;
            "
          >
            <OBreadcrumbItem to="/">SkillHub</OBreadcrumbItem>
            <OBreadcrumbItem v-if="skill">{{ skill.name }}</OBreadcrumbItem>
            <OBreadcrumbItem v-else>Skill 详情</OBreadcrumbItem>
          </OBreadcrumb>
        </div>

        <!-- 加载态 -->
        <div v-if="loading" class="info-card-hero loading-card">
          <OLoading :visible="true" size="medium" :mask="false" />
        </div>

        <!-- 错误态 -->
        <div v-else-if="error" class="error-section">
          <p class="error-text">{{ error }}</p>
        </div>

        <!-- 正常显示 -->
        <template v-else-if="skill">
          <!-- ========== 顶部信息卡片 ========== -->
          <div class="info-card-hero">
            <div class="skill-title-row">
              <h1 class="skill-name">{{ skill.name }}</h1>
            </div>
            <p class="skill-desc" v-if="skill.description">{{ skill.description }}</p>

            <!-- 元信息区：分类 / 平台类型 / 更新时间 / 风险评分 -->
            <div class="skill-meta">
              <div v-if="skill.category" class="skill-meta-item">
                <span class="skill-meta-icon" v-html="categorySvg"></span>
                <span class="skill-meta-text">{{ skill.category_label || skill.category }}</span>
              </div>
              <div v-if="skill.platform" class="skill-meta-item">
                <span class="skill-meta-icon" v-html="personSvg"></span>
                <span class="skill-meta-text">{{ platformNames[skill.platform] || skill.platform }}</span>
              </div>
              <div class="skill-meta-item">
                <span class="skill-meta-icon" v-html="timeSvg"></span>
                <span class="skill-meta-text">更新时间：{{ formatDate(skill.updated_at) }}</span>
              </div>
              <div class="skill-meta-item">
                <span class="skill-meta-icon" v-html="riskScoreSvg"></span>
                <span class="skill-meta-text">风险评分：{{ riskScoreText }}</span>
              </div>
            </div>

            <!-- 标签区 -->
            <div v-if="(skill.tags || []).length" class="skill-tags">
              <span
                v-for="tag in (skill.tags || [])"
                :key="tag"
                class="tag tag-gray"
              >{{ tag }}</span>
            </div>
          </div>
          <!-- end info-card-hero -->
        </template>
      </div>
    </section>

    <div v-if="skill" class="container-wide">
      <div class="detail-body">
        <div class="detail-body-main">
          <!-- ========== 使用文档 / 版本信息 ========== -->
          <div class="tab-content">
            <!-- Tab 页签：激活态 SemiBold + primary1 + 底部 pill 下划线，非激活 regular + 80% 黑 -->
            <div class="doc-tabs">
              <button
                :class="['doc-tab-btn', { 'doc-tab-btn--active': activeTab === 'usage' }]"
                @click="activeTab = 'usage'"
              >使用描述</button>
              <button
                :class="['doc-tab-btn', { 'doc-tab-btn--active': activeTab === 'versions' }]"
                @click="activeTab = 'versions'"
              >版本信息</button>
            </div>

            <!-- 使用描述 -->
            <template v-if="activeTab === 'usage'">
              <div v-if="displayContent" class="usage-content">
                <!-- eslint-disable-next-line vue/no-v-html -->
                <div class="markdown-body" v-html="renderedContent" @click="copyMarkdownCode"></div>
              </div>
              <div v-else class="empty-tab">
                <p>暂无使用描述</p>
                <p class="empty-hint">内容将在本地安装后显示</p>
              </div>
            </template>

            <!-- 版本信息 -->
            <template v-else>
              <div v-if="versions.length" class="version-table">
                <div class="version-table-header">
                  <span class="version-col-version">版本</span>
                  <span class="version-col-date">发布时间</span>
                  <span class="version-col-action">操作</span>
                </div>
                <div class="version-table-rows">
                  <div v-for="v in versions" :key="v.version" class="version-table-row">
                    <span class="version-col-version" :title="v.version">{{ v.version }}</span>
                    <span class="version-col-date">{{ formatDate(v.created_at) }}</span>
                    <span class="version-col-action">
                      <button
                        class="version-download-btn"
                        :disabled="downloadingVersion === v.version"
                        :aria-label="`下载 ${v.version}`"
                        @click="downloadVersionSkill(v.version)"
                      >
                        <span class="btn-icon-sm" v-html="downloadSvg"></span>
                      </button>
                    </span>
                  </div>
                </div>
              </div>
              <div v-else class="empty-tab">
                <p>暂无版本信息</p>
              </div>
            </template>
          </div>
        </div>

        <!-- ========== 右侧：安装卡片（设计稿-使用描述画板） ========== -->
        <aside class="detail-body-sidebar">
          <div class="sidebar-sticky">
            <div class="action-card">
              <h3 class="install-title">安装</h3>
              <div class="install-divider"></div>

              <!-- 方式一：复制提示词发送给 AI 直接安装 -->
              <p class="method-label">方式一：复制提示词发送给你的 AI 直接安装</p>
              <code class="prompt-box">{{ installPrompt }}</code>
              <OButton
                class="install-action"
                color="primary"
                variant="outline"
                size="large"
                round="pill"
                @click="copyInstallPrompt"
              >
                <template #icon>
                  <span class="action-icon" v-html="copySvg"></span>
                </template>
                复制提示词
              </OButton>

              <!-- 方式二：下载 ZIP 包安装 -->
              <p class="method-label method-label-second">方式二：下载ZIP包安装</p>
              <OButton
                class="install-action"
                color="primary"
                variant="outline"
                size="large"
                round="pill"
                :loading="downloading"
                @click="downloadSkill"
              >
                <template #icon>
                  <span class="action-icon" v-html="downloadSvg"></span>
                </template>
                {{ downloading ? '下载中...' : '立即下载' }}
              </OButton>
            </div>

            <!-- Skill 信息卡片 -->
            <div class="info-card">
              <h3 class="info-card-title">Skill 信息</h3>
              <div class="info-divider"></div>
              <div class="info-list">
                <div class="info-row">
                  <span class="info-label">贡献者</span>
                  <router-link
                    v-if="skill.author && skill.source"
                    :to="`/contributors/${encodeURIComponent(skill.source)}/${encodeURIComponent(skill.author)}`"
                    class="info-link"
                  >{{ skill.author }}</router-link>
                  <span v-else class="info-value">-</span>
                </div>
                <div class="info-row">
                  <span class="info-label">仓库地址</span>
                  <a
                    v-if="skill.source_url"
                    href="javascript:void(0)"
                    class="info-link info-repo-link"
                    @click.prevent="confirmExternalLink"
                  >{{ skill.source_url }}</a>
                  <span v-else class="info-value">-</span>
                </div>
                <div class="info-row">
                  <span class="info-label">下载量</span>
                  <span class="info-value">{{ skill.download_count.toLocaleString() }}</span>
                </div>
                <div class="info-row">
                  <span class="info-label">版本</span>
                  <span class="info-value">{{ skill.version || '-' }}</span>
                </div>
              </div>
            </div>

            <!-- ========== 风险评估卡片（设计稿-使用描述画板） ========== -->
            <div class="risk-card">
              <div class="risk-card-header">
                <h3 class="risk-card-title">风险评估</h3>
                <!-- 箭头尖距 ⓘ 图标框顶边 4px：offset = 4 + 5.66（官方 8×8 锚点斜向伸出量） -->
                <OPopover
                  position="top"
                  trigger="hover"
                  wrap-class="risk-popover"
                  :offset="9.66"
                  :adjust-width="false"
                  :adjust-min-width="false"
                >
                  <div class="risk-popover-body">
                    <p class="risk-popover-title">风险评估说明</p>
                    <p class="risk-popover-desc">得分基于 NVIDIA SkillSpector 多维扫描加权统计，单项等级不等同总体等级。</p>
                    <div class="risk-popover-row">
                      <span class="risk-popover-label">0-20 分：</span>
                      <span class="risk-tag risk-tag-fixed risk-green">安全</span>
                    </div>
                    <div class="risk-popover-row">
                      <span class="risk-popover-label">21-50 分：</span>
                      <span class="risk-tag risk-tag-fixed risk-blue">低风险</span>
                    </div>
                    <div class="risk-popover-row">
                      <span class="risk-popover-label">51-80 分：</span>
                      <span class="risk-tag risk-tag-fixed risk-orange">中风险</span>
                    </div>
                    <div class="risk-popover-row">
                      <span class="risk-popover-label">81-100 分：</span>
                      <span class="risk-tag risk-tag-fixed risk-red">高风险</span>
                    </div>
                    <a class="risk-popover-link" href="javascript:void(0)" @click="goSecurityDoc">查看详情</a>
                  </div>
                  <template #target>
                    <span class="risk-info-trigger"><OIconInfoTip /></span>
                  </template>
                </OPopover>
              </div>

              <div class="risk-gauge-wrap">
                <div class="risk-gauge">
                  <svg class="risk-gauge-svg" viewBox="0 0 140 140">
                    <circle class="risk-gauge-track" cx="70" cy="70" r="63" />
                    <!-- 满分：完整圆环，circle 默认从 3 点起，旋转 -90° 使其从 12 点起 -->
                    <circle
                      v-if="scoreArcFull"
                      class="risk-gauge-arc risk-gauge-arc-full"
                      cx="70"
                      cy="70"
                      r="63"
                      transform="rotate(-90 70 70)"
                      :style="{ stroke: securityLevel.arcColor }"
                    />
                    <!-- 其余得分：按得分百分比绘制弧（path 从 12 点起顺时针，直角端头） -->
                    <path
                      v-else-if="scoreArcPath"
                      class="risk-gauge-arc"
                      :d="scoreArcPath"
                      :style="{ stroke: securityLevel.arcColor }"
                    />
                  </svg>
                  <div class="risk-gauge-center">
                    <span class="risk-score">{{ skill.risk_score ?? '-' }}</span>
                    <span class="risk-score-label">风险得分</span>
                  </div>
                </div>
                <span :class="['risk-tag', securityLevel.riskClass]">{{ securityLevel.label }}</span>
                <p class="risk-desc">{{ securityLevel.desc }}</p>
                <OButton
                  class="risk-report-btn"
                  color="primary"
                  variant="text"
                  size="large"
                  round="pill"
                  :href="riskReportHref"
                  @click="goRiskReport"
                >
                  查看风险评估报告
                </OButton>
              </div>
            </div>
          </div>
          <!-- end sidebar-sticky -->
        </aside>
      </div>
    </div>

    <!-- ========== 第三方链接跳转提示 ========== -->
    <ODialog
      v-model:visible="externalDialogVisible"
      size="auto"
      main-class="external-jump-dialog"
      :mask-close="false"
    >
      <template #header>
        <p class="external-title">即将跳转到外部网站</p>
      </template>
      <div class="external-content">
        <p class="text">您将要访问的链接不属于openEuler社区，请关注您的账号安全。</p>
        <div class="content-link">
          <div class="external-link-href" :title="externalUrl">{{ externalUrl }}</div>
        </div>
      </div>
      <template #footer>
        <div class="dlg-action">
          <OButton color="primary" variant="solid" size="large" round="pill" @click="openExternalUrl">继续前往</OButton>
          <OButton color="primary" variant="outline" size="large" round="pill" @click="externalDialogVisible = false">取消</OButton>
        </div>
      </template>
    </ODialog>
  </div>
</template>

<style lang="scss" scoped>
.container-wide {
  max-width: 1488px;
  margin: 0 auto;
  padding: 0 24px;
}



/* ===== Hero 背景区 ===== */
.hero-section {
  position: relative;
  overflow: hidden;
  padding-bottom: 32px;
}

/* ===== 面包屑 ===== */
.breadcrumb-wrap {
  margin-top: 40px;
  margin-bottom: 40px;

  --breadcrumb-color: #00000099;

  :deep(.o-breadcrumb-item-label) {
    font-family: HarmonyHeiTi;
    font-weight: var(--o-font_weight-regular);
    letter-spacing: 0px;
    text-align: left;
    color: var(--breadcrumb-color);
    transition: color 0.2s;
    cursor: pointer;

    @include hover {
      color: var(--o-color-primary1);
    }
  }

  :deep(.o-icon-chevron-right) {
    color: var(--breadcrumb-color);
  }
}

.dark .breadcrumb-wrap,
[data-o-theme="e.dark"] .breadcrumb-wrap {
  --breadcrumb-color: rgba(255, 255, 255, 0.6);
}

/* ===== 加载 & 错误 ===== */
.loading-card {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 200px;
}

.error-section {
  text-align: center;
  padding: 80px 0;
  .error-text {
    color: var(--o-color-danger1);
    font-size: var(--o-r-font_size-text1);
  }
}

/* ===== 顶部信息卡片 ===== */
.info-card-hero {
  background: var(--o-color-fill2);
  border-radius: 8px;
  padding: 24px;

  .skill-title-row {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 8px;
  }

  .skill-name {
    font-family: HarmonyHeiTi;
    font-weight: var(--o-font_weight-medium);
    font-size: var(--o-r-font_size-h2);
    line-height: var(--o-r-line_height-h2);
    letter-spacing: 0px;
    text-align: left;
    color: var(--o-color-info1);
    margin-bottom: 0;
  }

  .skill-desc {
    font-family: HarmonyHeiTi;
    font-weight: var(--o-font_weight-regular);
    font-size: 16px;
    line-height: var(--o-r-line_height-text1);
    letter-spacing: 0px;
    text-align: left;
    color: var(--o-color-info3);
    margin-bottom: 24px;
  }

  .tag-category {
    font-family: HarmonyHeiTi;
    font-weight: var(--o-font_weight-regular);
    font-size: 12px;
    line-height: 18px;
    letter-spacing: 0px;
    text-align: left;
    color: var(--o-color-info1);
  }

  .tag-gray {
    border-radius: 4px;
    border: 1px solid var(--o-color-control4);
    background: var(--o-color-fill2);
    font-family: HarmonyHeiTi;
    font-weight: var(--o-font_weight-regular);
    font-size: 12px;
    line-height: 18px;
    letter-spacing: 0px;
    text-align: left;
    color: var(--o-color-info1);
  }

  .skill-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 12px;
    margin-bottom: 0;
  }

  .skill-meta {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 4px 24px;
  }

  .skill-meta-item {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .skill-meta-icon {
    display: inline-flex;
    align-items: center;
    color: var(--o-color-info3);

    :deep(svg) {
      width: 16px;
      height: 16px;
    }
  }

  .skill-meta-text {
    font-family: HarmonyHeiTi;
    font-weight: var(--o-font_weight-regular);
    font-size: 14px;
    line-height: 22px;
    letter-spacing: 0px;
    text-align: left;
    color: var(--o-color-info3);
  }
}

/* ===== 主体区域（Tab + 右侧卡片） ===== */
.detail-body {
  display: flex;
  gap: 32px;
  align-items: stretch;

  .detail-body-main {
    flex: 1;
    min-width: 0;
  }

  .detail-body-sidebar {
    width: 440px;
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
  }
}

.sidebar-sticky {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.tab-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 32px;
  min-height: 36px;
}

.version-toolbar {
  display: flex;
  align-items: center;
  min-height: 30px;
}

.version-card {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.version-card-label {
  font-size: var(--o-font_size-text1);
  font-weight: var(--o-font_weight-regular);
  line-height: var(--o-line_height-text1);
  color: var(--o-color-info1);
  white-space: nowrap;
}

/* 版本按钮 */
.version-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  height: 32px;
  min-width: 92px;
  padding: 0 8px;
  border: 1px solid #0000003F;
  background: #FFFFFF;
  color: #000000;
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: 16px;
  line-height: 24px;
  letter-spacing: 0px;
  text-align: left;
  cursor: pointer;
  border-radius: 4px;
  white-space: nowrap;

  &:hover {
    border-color: #002FA7;
  }
}

.dark .version-btn:hover,
[data-o-theme="e.dark"] .version-btn:hover {
  border-color: var(--o-color-primary1);
}

.dark .version-btn,
[data-o-theme="e.dark"] .version-btn {
  background: var(--o-color-fill2);
  border-color: var(--o-color-control4);
  color: var(--o-color-info1);
}

.version-btn-icon {
  display: inline-flex;
  align-items: center;
  width: 24px;
  height: 24px;

  :deep(svg) {
    width: 24px;
    height: 24px;
    display: block;
  }
}

.action-card {
  background: var(--o-color-fill2);
  border-radius: 4px;
  padding: 24px;
  display: flex;
  flex-direction: column;
}

.info-card {
  background: var(--o-color-fill2);
  border-radius: 8px;
  padding: 20px;
}

.info-card-title {
  font-size: var(--o-font_size-h3);
  font-weight: var(--o-font_weight-medium);
  line-height: var(--o-line_height-h3);
  color: var(--o-color-info1);
  margin: 0;
}

.info-divider {
  height: 1px;
  background: var(--o-color-control4);
  margin: 24px 0;
}

.info-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.info-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 12px;
}

.info-label {
  font-size: var(--o-font_size-text1);
  font-weight: var(--o-font_weight-regular);
  line-height: var(--o-line_height-text2);
  color: var(--o-color-info3);
  white-space: nowrap;
  flex-shrink: 0;
}

.info-value {
  font-size: var(--o-font_size-text1);
  font-weight: var(--o-font_weight-regular);
  line-height: var(--o-line_height-text1);
  color: var(--o-color-info1);
  text-align: left;
  word-break: break-all;
  min-width: 0;
}

.info-link {
  color: var(--o-color-link1);
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular, 400);
  font-size: var(--o-font_size-text1);
  line-height: var(--o-line_height-text1);
  letter-spacing: 0px;
  text-align: right;
  word-break: break-all;
  text-decoration: none;
  transition: color 0.2s;

  @include hover {
    color: var(--o-color-link2);
    text-decoration: none;
  }

  &:focus-visible {
    color: var(--o-color-link2);
    text-decoration: none;
  }
}

.info-repo-link:hover,
.info-repo-link:focus-visible {
  text-decoration: underline;
}

/* ===== 安装卡片（设计稿-使用描述画板） ===== */
.install-title {
  margin: 0 0 24px;
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-medium);
  font-size: 22px;
  line-height: 30px;
  letter-spacing: 0px;
  text-align: left;
  color: var(--o-color-info1);
}

.install-divider {
  height: 1px;
  background: var(--o-color-control4);
  margin-bottom: 24px;
}

.method-label {
  margin: 0 0 12px;
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-medium);
  font-size: 14px;
  line-height: 30px;
  letter-spacing: 0px;
  text-align: left;
  color: var(--o-color-info1);
}

.method-label-second {
  margin-top: 24px;
}

.prompt-box {
  display: block;
  min-height: 60px;
  margin: 0 0 12px;
  padding: 8px 12px;
  box-sizing: border-box;
  background: var(--o-color-fill1);
  border-radius: 4px;
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: 14px;
  line-height: 22px;
  color: var(--o-color-info3);
  word-break: break-all;
  white-space: normal;
}

.install-action {
  width: 100%;
  justify-content: center;
  font-family: HarmonyHeiTi;

  /* 图标与文字同色（primary1），与组件库描边按钮规范一致 */
  .action-icon {
    display: inline-flex;
    align-items: center;
    width: 24px;
    height: 24px;

    :deep(svg) {
      width: 24px;
      height: 24px;
      display: block;
      fill: currentColor;
    }
  }
}

/* ===== 风险评估卡片（设计稿-使用描述画板） ===== */
.risk-card {
  background: var(--o-color-fill2);
  border-radius: 4px;
  padding: 32px;
}

.risk-card-header {
  display: flex;
  align-items: center;
  gap: 4px;
}

.risk-card-title {
  margin: 0;
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-medium);
  font-size: 22px;
  line-height: 30px;
  letter-spacing: 0px;
  text-align: left;
  color: var(--o-color-info1);
}

/* 标题旁 ⓘ 说明图标（设计稿 提示/形状结合）：默认一级文字色，悬停品牌蓝 */
.risk-info-trigger {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  color: var(--o-color-info1);
  cursor: pointer;
  transition: color 0.2s;

  @include hover {
    color: var(--o-color-primary1);
  }

  svg {
    width: 24px;
    height: 24px;
    display: block;
  }
}

.risk-gauge-wrap {
  display: flex;
  flex-direction: column;
  align-items: center;
  margin-top: 24px;
}

.risk-gauge-wrap .risk-tag {
  margin-top: 12px;
}

/* 风险得分仪表盘：140px 圆环，环厚 14px，12 点起顺时针 90° 弧（与设计稿一致） */
.risk-gauge {
  position: relative;
  width: 140px;
  height: 140px;
}

.risk-gauge-svg {
  width: 140px;
  height: 140px;
  display: block;
}

.risk-gauge-track {
  fill: none;
  /* 圆环底色：light 主题等同 brand-1（浅紫 246,238,253）保持设计稿效果；
     dark 主题自动切换为 control2-light（深灰 43,43,47），避免深紫底环 */
  stroke: var(--o-color-control2-light);
  stroke-width: 14;
}

.risk-gauge-arc {
  fill: none;
  stroke-width: 14;
  stroke-linecap: butt;
}

.risk-gauge-arc-full {
  fill: none;
  stroke-width: 14;
}

.risk-gauge-center {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
}

.risk-score {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-semibold);
  font-size: 24px;
  line-height: 32px;
  letter-spacing: 0px;
  color: var(--o-color-info1);
}

.risk-score-label {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: 12px;
  line-height: 18px;
  letter-spacing: 0px;
  color: var(--o-color-info3);
}

/* 风险等级标签：24px 高、12px 文字（设计稿 标签 Tag/状态标签） */
.risk-tag {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 24px;
  padding: 0 12px;
  border-radius: 4px;
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: 12px;
  line-height: 18px;
  letter-spacing: 0px;
  white-space: nowrap;
  box-sizing: border-box;
}

.risk-green {
  background: var(--o-color-success1);
  color: #ffffff;
}

.risk-blue {
  background: #497af8;
  color: #ffffff;
}

.risk-orange {
  background: var(--o-color-warning1);
  color: #ffffff;
}

.risk-red {
  background: var(--o-color-danger1);
  color: #ffffff;
}

.risk-gray {
  border: 1px solid var(--o-color-control4);
  background: transparent;
  color: var(--o-color-info1);
}

.risk-desc {
  margin: 12px 0 0;
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: 16px;
  line-height: 24px;
  letter-spacing: 0px;
  text-align: center;
  color: var(--o-color-info3);
}

.risk-card .risk-report-btn {
  margin-top: 12px;
  font-family: HarmonyHeiTi;

  /* 设计稿链接按钮为品牌色文字（组件 text 变体默认 info1，需提高优先级覆盖） */
  --btn-color: var(--o-color-primary1) !important;
}

/* ===== Tab 导航 (OTab button variant) ===== */
.detail-tab {
  :deep(.o-tab-head) {
    background: var(--o-color-fill1);
    border-radius: 4px;
    padding: 4px;
    height: 48px;
    border: none;
    box-sizing: border-box;
  }

  :deep(.o-tab-navs) {
    gap: 0;
  }

  :deep(.o-tab-nav) {
    height: 40px;
    padding: 0 16px;
    border: none !important;
    font-family: HarmonyHeiTi;
    font-weight: var(--o-font_weight-regular);
    font-size: var(--o-r-font_size-text2);
    line-height: var(--o-r-line_height-text2);
    letter-spacing: 0;
    color: var(--o-color-info2) !important;
    border-radius: 4px !important;
    background: transparent !important;
    justify-content: center;
    align-items: center;

    &:hover:not(.is-active) {
      background: color-mix(in srgb, var(--o-color-primary1) 8%, transparent);
    }

    &.is-active {
      font-weight: var(--o-font_weight-semibold);
      color: var(--o-color-primary1) !important;
      background: var(--o-color-fill2) !important;
      box-shadow: 0 1px 4px rgba(0, 0, 0, 0.15);
    }
  }
}

/* ===== Tab 内容区（白卡 1008 宽，圆角 4px，padding 24） ===== */
.tab-content {
  background: var(--o-color-fill2);
  border-radius: 4px;
  padding: 24px;
  min-height: 200px;
}

/* ===== Tab 页签（tab 高 48，文字顶部对齐，激活 tab 底部 80×2 pill 下划线） ===== */
.doc-tabs {
  display: flex;
  align-items: flex-start;
  gap: 40px;
  margin-bottom: 32px;
}

.doc-tab-btn {
  position: relative;
  height: 48px;
  padding: 2px 0 0;
  border: none;
  background: transparent;
  cursor: pointer;
  font-family: 'HarmonyHeiTi', var(--o-font_family);
  font-weight: 400;
  font-size: 20px;
  line-height: 28px;
  letter-spacing: 0px;
  text-align: left;
  color: hsla(0, 0%, 0%, 0.8);
  transition: color 0.2s;

  @include hover {
    color: hsl(223.1, 100%, 32.7%);
  }

  &--active {
    font-weight: 600;
    color: hsl(223.1, 100%, 32.7%);

    @include hover {
      color: hsl(223.1, 100%, 32.7%);
    }

    /* 激活下划线：文字下方 16px，80×2 primary1，两端全圆角 */
    &::after {
      content: '';
      position: absolute;
      bottom: 0;
      left: 0;
      width: 80px;
      height: 2px;
      border-radius: 100px;
      background: hsl(223.1, 100%, 32.7%);
    }
  }
}

/* Dark 模式：未激活白色 80% 透明度，激活/下划线用 primary1 token */
[data-o-theme='e.dark'] .doc-tab-btn {
  color: hsla(0, 0%, 100%, 0.8);

  @include hover {
    color: var(--o-color-primary1);
  }

  &.doc-tab-btn--active {
    color: var(--o-color-primary1);

    &::after {
      background: var(--o-color-primary1);
    }
  }
}

/* ===== 版本信息表格（表头 38px、数据行 56px、下载图标 24×24） ===== */
.version-table {
  display: flex;
  flex-direction: column;
}

.version-col-version {
  width: 238px;
  flex-shrink: 0;
  /* 长版本号（如 commit hash）单行截断显示，hover 时通过 title 查看完整值 */
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.version-col-date {
  flex: 1;
  min-width: 0;
}

/* 操作列：内容右对齐，距右缘 12px（设计稿图标距内容右缘 ~13px） */
.version-col-action {
  width: 72px;
  flex-shrink: 0;
  display: flex;
  justify-content: flex-end;
  align-items: center;
  padding-right: 12px;
}

.version-table-header {
  display: flex;
  align-items: center;
  height: 38px;
  padding: 0 24px;
  border-bottom: 1px solid var(--o-color-primary1);

  > span {
    font-family: var(--o-font_family);
    font-weight: var(--o-font_weight-semibold);
    font-size: var(--o-font_size-tip1);
    line-height: var(--o-line_height-tip1);
    color: var(--o-color-info2);
  }
}

.version-table-rows {
  display: flex;
  flex-direction: column;
}

.version-table-row {
  position: relative;
  display: flex;
  align-items: center;
  height: 56px;
  padding: 0 24px;
  border-bottom: 1px solid var(--o-color-control4);

  &:last-child {
    border-bottom: none;
  }

  > span {
    font-family: var(--o-font_family);
    font-weight: var(--o-font_weight-regular);
    font-size: var(--o-font_size-tip1);
    line-height: var(--o-line_height-tip1);
    color: var(--o-color-info1);
  }
}

.version-download-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: none;
  background: transparent;
  color: var(--o-color-info1);
  cursor: pointer;
  transition: color 0.2s;
  @include hover {
    color: var(--o-color-primary1);
  }

  &:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }

  .btn-icon-sm {
    width: 18px;
    height: 18px;
    display: inline-flex;
  }
}

/* ===== 使用描述 ===== */
.usage-content {
  line-height: 1.8;
}

.markdown-body {
  color: var(--o-color-info1);
  font-size: 15px;
  line-height: 1.8;

  :deep(h1), :deep(h2), :deep(h3), :deep(h4), :deep(h5), :deep(h6) {
    margin-top: 24px;
    margin-bottom: 24px;
    color: var(--o-color-info1);
    font-weight: 600;
  }

  :deep(h1) {
    margin-top: 0;
    margin-bottom: 12px;
    color: var(--o-color-info1);
    font-family: var(--o-font_family);
    font-weight: var(--o-font_weight-semibold);
    font-size: var(--o-font_size-h1);
    line-height: var(--o-line_height-h1);
    letter-spacing: 0px;
    text-align: left;
  }

  :deep(.heading-divider) {
    height: 2px;
    background: #002FA7;
  }

  :deep(.heading-divider--h1) {
    margin-bottom: 16px;
  }

  :deep(.heading-divider--h2) {
    height: 1px;
    background: var(--o-color-control4);
    margin-bottom: 12px;
  }
  :deep(h2) {
    margin-bottom: 8px;
    font-size: 24px;
  }
  :deep(h3) { font-size: 20px; }
  :deep(h4) { font-size: 15px; }
  :deep(h5) { font-size: 14px; }
  :deep(h6) { font-size: 13px; }

  :deep(p) {
      margin-bottom: 8px;
      color: var(--o-color-info2);
      font-size: 16px;
    }

  :deep(ul) {
    margin-bottom: 14px;
    padding-left: 24px;
    color: var(--o-color-info2);
    list-style: disc;
  }

  :deep(ol) {
    margin-bottom: 14px;
    padding-left: 24px;
    color: var(--o-color-info2);
    list-style: decimal;
  }

  :deep(li) {
    margin-bottom: 8px;
    font-size: 16px;
  }

  :deep(code) {
    font-family: var(--o-font_family-code);
    font-size: 14px;
    background: var(--o-color-fill1);
    padding: 2px 8px;
    border-radius: 4px;
    color: var(--o-color-info2);
  }

  :deep(pre) {
    background: var(--o-color-control2-light);
    border-radius: var(--o-radius-m);
    padding: var(--o-r-gap-4);
    overflow-x: auto;
    margin-bottom: 16px;
    color: var(--o-color-info1);
    font-family: var(--o-font_family-code);
    font-weight: var(--o-font_weight-regular);
    font-size: var(--o-r-font_size-tip1);
    line-height: var(--o-r-line_height-tip1);
    letter-spacing: 0px;
    text-align: left;

    &::-webkit-scrollbar {
      height: 4px;
    }

    &::-webkit-scrollbar-track {
      background: transparent;
    }

    &::-webkit-scrollbar-thumb {
      background: var(--o-color-control4);
      border-radius: var(--o-radius-s);
    }

    code {
      background: none;
      padding: 0;
      color: inherit;
      font-size: inherit;
    }
  }

  /* ===== Shiki 双主题：浅色默认，深色由 --shiki-dark ===== */
  :deep(.shiki) {
    background-color: #F3F3F5 !important;
    border-radius: 4px;
    color: var(--shiki-light) !important;
  }

  :deep(.shiki span) {
    color: var(--shiki-light);
  }

  :deep(blockquote) {
    border-left: 3px solid var(--o-color-primary1);
    padding-left: 16px;
    margin-left: 0;
    margin-bottom: 14px;
    color: var(--o-color-info3);
  }

  :deep(a) {
    color: var(--o-color-primary1);
    text-decoration: none;
    @include hover { text-decoration: underline; }
  }

  :deep(table) {
    width: 100%;
    border-collapse: collapse;
    margin-bottom: 16px;

    th, td {
      padding: 10px 14px;
      border: none;
      text-align: left;
      font-size: 14px;
    }

    th {
      border-radius: 4px 4px 0px 0px;
      background: #FFFFFF;
      border: none;
      border-bottom: 1px solid #002FA7;
      font-weight: 600;
      color: var(--o-color-info2);
    }

    tbody tr {
      background: #FFFFFF02;

      &:nth-child(even) {
        background: #EBF1FA66;
      }

      td {
        border: none;
        color: var(--o-color-info2);
      }
    }
  }

  :deep(hr) {
    display: none;
  }

  :deep(img) {
    max-width: 100%;
    border-radius: 8px;
  }

  /* ===== 代码块复制按钮 ===== */
  :deep(.code-block-wrap) {
    position: relative;
  }

  :deep(.code-copy-btn) {
    position: absolute;
    top: 12px;
    right: 12px;
    z-index: 1;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    background: transparent;
    border: none;
    color: var(--o-color-info3);
    cursor: pointer;
    opacity: 1;
    transition: color 0.2s;

    @include hover {
      color: var(--o-color-primary1);
    }

    &:active {
      opacity: 1;
    }

    &.is-copied {
      color: var(--o-color-success1);
      cursor: default;
      opacity: 1;
      border-color: var(--o-color-success1);
    }

    :deep(svg) {
      width: 12px;
      height: 12px;
    }
  }
}

/* ===== Shiki 深色模式覆盖 ===== */
[data-o-theme='e.dark'] .markdown-body {
  :deep(.shiki),
  :deep(.shiki span) {
    background-color: var(--o-color-control2-light) !important;
    color: var(--shiki-dark) !important;
    font-style: var(--shiki-dark-font-style) !important;
    font-weight: var(--shiki-dark-font-weight) !important;
    text-decoration: var(--shiki-dark-text-decoration) !important;
  }

  :deep(table th) {
    background: var(--o-color-control2-light);
    border-bottom-color: var(--o-color-primary1);
  }

  :deep(table tbody tr) {
    background: transparent;
  }

  :deep(table tbody tr:nth-child(even)) {
    background: rgba(255, 255, 255, 0.04);
  }
}

/* ===== 空态 ===== */
.empty-tab {
  text-align: center;
  padding: 48px 0;
  color: var(--o-color-info3);

  .empty-hint {
    margin-top: 8px;
    font-size: 13px;
    color: var(--o-color-info4);
  }
}

/* ===== 第三方链接跳转提示对话框 ===== */
:global(.external-jump-dialog) {
  --dlg-width: min(680px, calc(100vw - var(--o-gap-8)));
}

.dlg-action {
  display: flex;
  justify-content: center;
  gap: var(--o-gap-4);
}

.external-title,
.external-content .text {
  margin: 0;
}

.external-title {
  font-size: var(--o-r-font_size-h2);
  line-height: var(--o-r-line_height-h2);
  font-weight: var(--o-font_weight-regular);
  letter-spacing: 0;
  text-align: center;
  color: var(--o-color-info1);
}

.external-content .text {
  font-size: var(--o-r-font_size-text2);
  line-height: var(--o-r-line_height-text2);
}

.external-content {
  display: flex;
  flex-direction: column;
  gap: var(--o-gap-5);
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  letter-spacing: 0;
  text-align: left;
  color: var(--o-color-info1);
}

.content-link {
  min-width: 0;
}

.external-link-href {
  font-size: var(--o-r-font_size-text2);
  line-height: var(--o-r-line_height-text2);
  letter-spacing: 0;
  color: var(--o-color-info2);
  word-break: break-all;
}
</style>

<style scoped>
/* ===== 复制提示 Toast ===== */
.copy-toast {
  position: fixed;
  top: 96px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 9999;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 12px 24px;
  border-radius: var(--o-radius-s);
  background: var(--o-color-fill2);
  box-shadow: var(--o-shadow-2);
}

.toast-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border-radius: 50%;

  :deep(svg) {
    width: 24px;
    height: 24px;
  }

  :deep(rect) {
    fill: #0BB151;
  }
}

.toast-text {
  color: var(--o-color-info1);
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: var(--o-r-font_size-text1);
  line-height: var(--o-r-line_height-text1);
  letter-spacing: 0;
  text-align: left;
}

.toast-enter-active,
.toast-leave-active {
  transition: opacity 0.3s ease, transform 0.3s ease;
}

.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateX(-50%) translateY(-12px);
}
</style>

<style lang="scss">
/* ===== 风险评估说明浮层（设计稿 气泡提示 → OpenDesign OPopover） ===== */
/* OPopover teleport 到 body，样式需全局；宽度 375 含内边距 */
/* 其余视觉（圆角 4px、边框 --o-color-control4、阴影 --o-shadow-1、8×8 锚点箭头）
   均沿用 OPopover 组件规范默认值，light/dark 由 token 自动适配 */
.risk-popover {
  width: 375px;
  box-sizing: border-box;
  --popup-padding: 16px 16px 8px;
}

.risk-popover-title {
  margin: 0 0 4px;
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-semibold);
  font-size: 16px;
  line-height: 24px;
  letter-spacing: 0px;
  text-align: left;
  color: var(--o-color-info1);
}

.risk-popover-desc {
  margin: 0 0 10px;
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: 14px;
  line-height: 22px;
  letter-spacing: 0px;
  text-align: left;
  color: var(--o-color-info2);
}

.risk-popover-row {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 24px;
  margin-bottom: 10px;
}

.risk-popover-row:last-of-type {
  margin-bottom: 8px;
}

.risk-popover-label {
  display: inline-flex;
  align-items: center;
  width: 100px;
  flex-shrink: 0;
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: 14px;
  line-height: 22px;
  letter-spacing: 0px;
  text-align: left;
  color: var(--o-color-info2);
  /* 设计稿为单行文本（高22px），防止回退字体过宽导致换行 */
  white-space: nowrap;

  /* 分数前缀圆点（设计稿列表项目符号）：3px 实心圆，左缩进 9px 时圆点占 +9~+12、文字墨迹起点 +22，与设计稿逐像素对齐 */
  &::before {
    content: '';
    display: inline-block;
    flex-shrink: 0;
    width: 3px;
    height: 3px;
    border-radius: 50%;
    background: currentColor;
    margin: 0 9px;
  }
}

.risk-tag-fixed {
  width: 60px;
  padding: 0;
}

.risk-popover-link {
  align-self: flex-start;
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: 14px;
  line-height: 22px;
  letter-spacing: 0px;
  text-align: left;
  color: var(--o-color-link1);
  text-decoration: none;
  cursor: pointer;
}

.risk-popover-link:hover {
  text-decoration: underline;
}
</style>
