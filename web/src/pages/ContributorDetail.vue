<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '@/api/client'
import type { Skill, ContributorSkillsResponse } from '@/api/types'
import SkillCard from '@/components/SkillCard.vue'
import { OBreadcrumb, OBreadcrumbItem, OInput, OPagination, OLoading, OLink } from '@opensig/opendesign'
import { oaReport } from '@opendesign-plus/plugins/analytics'
import { useAppStore } from '@/stores/app'
import heroBgLight from '@/assets/bg/hero-top-texture.png'
import heroBgDark from '@/assets/bg/hero-top-texture-dark.png'
import emptyStateSvg from '@/assets/icons/empty-state.svg?raw'

const route = useRoute()
const appStore = useAppStore()

const platformNames: Record<string, string> = {
  community: '社区SIG',
  enterprise: '企业组织',
  personal: '个人'
}
const sourceNames: Record<string, string> = {
  github: 'GitHub',
  atomgit: 'AtomGit'
}

const contributor = ref<ContributorSkillsResponse | null>(null)
const skills = ref<Skill[]>([])
const loading = ref(true)
const error = ref('')

const page = ref(1)
const pageSize = ref(15)
const pageSizeOptions = [15, 30, 60]
const searchInput = ref('')

// 简介：优先 contributors 表的 description（catalog 注入），个人贡献者无简介时兜底默认文案
const contributorDesc = computed(() => {
  if (!contributor.value) return ''
  if (contributor.value.description?.trim()) return contributor.value.description
  if (contributor.value.platform === 'personal') return '社区个人贡献者'
  if (contributor.value.platform === 'enterprise') return '企业贡献者'
  if (contributor.value.platform === 'community') return '社区SIG贡献者'

  return ''
})

// 仓库地址：catalog 注入的 git 托管主页（enterprise 的 git_profile / personal 的 profile）
const contributorRepoLink = computed(() => contributor.value?.git_profile?.trim() || '')

// 官网：catalog 注入的官网地址（community 的 SIG 页面 / 企业官网 / 个人网站）
const contributorWebsite = computed(() => contributor.value?.website?.trim() || '')

// 下载量格式化：超过一万显示 xxx.xw，与首页一致
function formatDownloads(n?: number): string {
  if (n == null) return '0'
  if (n < 10000) return n.toLocaleString()
  return `${(n / 10000).toFixed(1)}w`
}

async function fetchContributorSkills() {
  const source = route.params.source as string
  const author = decodeURIComponent(route.params.author as string)
  if (!source || !author) {
    error.value = '参数缺失'
    loading.value = false
    return
  }
  loading.value = true
  error.value = ''
  try {
    const res = await api.getContributorSkills({
      source,
      author,
      skip: (page.value - 1) * pageSize.value,
      limit: pageSize.value,
      keyword: searchInput.value.trim() || undefined
    })
    contributor.value = res
    skills.value = res.skills || []
    oaReport('contributor_view', {
      module: 'contributor_detail',
      source,
      author: res.author,
      skill_count: res.skill_count
    })
  } catch (e: any) {
    console.error('加载贡献者详情失败:', e)
    error.value = e?.response?.data?.detail || e.message || '加载失败'
  } finally {
    loading.value = false
  }
}

function onPaginationChange() {
  fetchContributorSkills()
}

onMounted(fetchContributorSkills)

// 同组件实例复用（source/author 变化）时重新加载
watch(
  () => [route.params.source, route.params.author],
  () => {
    if (route.name === 'contributor-detail') {
      page.value = 1
      searchInput.value = ''
      fetchContributorSkills()
    }
  }
)

// 搜索：300ms 防抖后走服务端过滤（跨页匹配），分页 total 随搜索结果动态变化
let searchTimer: ReturnType<typeof setTimeout> | null = null
watch(searchInput, () => {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    page.value = 1
    fetchContributorSkills()
  }, 300)
})
</script>

<template>
  <div class="contributor-page">
    <!-- ========== Hero 区域 ========== -->
    <section class="hero-section">
      <div class="absolute inset-0 pointer-events-none">
        <img :src="appStore.isDark ? heroBgDark : heroBgLight" alt="" class="w-full h-full object-cover" />
      </div>
      <div class="container-wide relative">
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
            <OBreadcrumbItem to="/contributors">贡献</OBreadcrumbItem>
            <OBreadcrumbItem v-if="contributor">{{ contributor.author }}</OBreadcrumbItem>
            <OBreadcrumbItem v-else>贡献者详情</OBreadcrumbItem>
          </OBreadcrumb>
        </div>

        <!-- 加载态 -->
        <div v-if="loading" class="contributor-card loading-card">
          <OLoading :visible="true" size="medium" :mask="false" />
        </div>

        <!-- 错误态 -->
        <div v-else-if="error" class="error-section">
          <p class="error-text">{{ error }}</p>
        </div>

        <!-- 贡献者信息卡 -->
        <div v-else-if="contributor" class="contributor-card">
          <div class="contributor-main">
            <div class="contributor-title-row">
              <h1 class="contributor-name">{{ contributor.author }}</h1>
              <span v-if="contributor.platform" class="tag tag-platform">{{ platformNames[contributor.platform] || contributor.platform }}</span>
              <span class="tag tag-source">{{ sourceNames[contributor.source] || contributor.source }}</span>
            </div>
            <p v-if="contributorDesc" class="contributor-desc">{{ contributorDesc }}</p>
            <div v-if="contributorRepoLink || contributorWebsite" class="contributor-links">
              <div class="contributor-link-row">
                <template v-if="contributorRepoLink">
                  <span class="contributor-link-label">git主页：</span>
                  <OLink
                    :href="contributorRepoLink"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="contributor-repo-link"
                    color="normal"
                    @click="oaReport('click_contributor_repo', { module: 'contributor_detail', author: contributor.author })"
                  >{{ contributorRepoLink }}</OLink>
                </template>
                <template v-if="contributorRepoLink && contributorWebsite">
                  <span class="contributor-link-sep"></span>
                </template>
                <template v-if="contributorWebsite">
                  <span class="contributor-link-label">官网：</span>
                  <OLink
                    :href="contributorWebsite"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="contributor-repo-link"
                    color="normal"
                    @click="oaReport('click_contributor_homepage', { module: 'contributor_detail', author: contributor.author })"
                  >{{ contributorWebsite }}</OLink>
                </template>
              </div>
            </div>
          </div>

          <div class="contributor-stats">
            <div class="stat-item">
              <div class="stat-value">{{ contributor.skill_count }}</div>
              <div class="stat-label">Skills</div>
            </div>
            <div class="stat-item">
              <div class="stat-value">{{ formatDownloads(contributor.total_downloads) }}</div>
              <div class="stat-label">下载量</div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- ========== 技能列表区域 ========== -->
    <section v-if="contributor" class="container-wide list-section">
      <div class="list-header-row">
        <h2 class="section-title">贡献的Skill</h2>
        <OInput
          v-model="searchInput"
          placeholder="搜索Skill"
          size="large"
          round="4px"
          clearable
          style="width: 320px"
        >
          <template #prefix>
            <svg class="search-icon" width="24" height="24" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
              <path d="M17.549 16.523l0.087 0.074 2.76 2.754c0.274 0.273 0.274 0.716 0.001 0.99-0.246 0.246-0.629 0.271-0.903 0.075l-0.087-0.074-2.76-2.754c-0.274-0.273-0.274-0.716-0.001-0.99 0.246-0.246 0.629-0.271 0.903-0.075zM10.821 3.454c4.099 0 7.423 3.323 7.423 7.423s-3.323 7.423-7.423 7.423c-4.099 0-7.423-3.323-7.423-7.423s3.323-7.423 7.423-7.423zM10.821 4.854c-3.326 0-6.023 2.696-6.023 6.023s2.696 6.023 6.023 6.023c3.326 0 6.023-2.696 6.023-6.023s-2.696-6.023-6.023-6.023z" fill="currentColor"></path>
            </svg>
          </template>
        </OInput>
      </div>

      <!-- 加载态 -->
      <div v-if="loading" class="loading-container">
        <OLoading :visible="true" size="medium" :mask="false" />
      </div>

      <!-- 空态 -->
      <div v-else-if="skills.length === 0" class="empty-state">
        <div class="empty-state-svg" v-html="emptyStateSvg"></div>
        <p class="empty-state-text">没有匹配的结果</p>
      </div>

      <!-- 卡片网格 -->
      <div v-else class="skills-grid">
        <SkillCard
          v-for="skill in skills"
          :key="skill.id"
          :skill="skill"
        />
      </div>

      <!-- 分页 -->
      <div v-if="contributor.total > 0 && !loading" class="pagination-row">
        <OPagination
          v-model:page="page"
          v-model:page-size="pageSize"
          :total="contributor.total"
          :page-sizes="pageSizeOptions"
          :layout="['total', 'pagesize', 'pager', 'jumper']"
          @change="onPaginationChange"
        />
      </div>
    </section>
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

  /* 设计稿：非当前项 color=#000000 op=0.60 → --o-color-info3（light/dark 自动适配） */
  --breadcrumb-color: var(--o-color-info3);

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

  /* 设计稿：当前项（最后一项）color=#002FA7 SemiBold → --o-color-primary1 + semibold */
  :deep(.o-breadcrumb-item:last-child .o-breadcrumb-item-label) {
    color: var(--o-color-primary1);
    font-weight: var(--o-font_weight-semibold);
    cursor: default;
  }

  :deep(.o-icon-chevron-right) {
    color: var(--breadcrumb-color);
  }
}

/* ===== 加载 & 错误 ===== */
.loading-card {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 176px;
}

.loading-container {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 300px;
}

.error-section {
  text-align: center;
  padding: 80px 0;

  .error-text {
    color: var(--o-color-danger1);
    font-size: var(--o-r-font_size-text1);
  }
}

/* ===== 贡献者信息卡 ===== */
/* 设计稿：容器 174 1488x176, fills=#FFFFFF, cornerRadius=4；左 padding 24，右 padding 64（统计区向内推） */
.contributor-card {
  background: var(--o-color-fill2);
  border-radius: var(--o-radius-xs);
  padding: 24px 64px 24px 24px;
  display: flex;
  justify-content: space-between;
  gap: 24px;
}

.contributor-title-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.contributor-name {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-medium);
  font-size: var(--o-r-font_size-h2);
  line-height: var(--o-r-line_height-h2);
  letter-spacing: 0px;
  text-align: left;
  color: var(--o-color-info1);
  margin-bottom: 0;
}

/* 设计稿：标签 Tag/信息标签 size=72x24, strokes=#000000 op=0.25, cornerRadius=4, 文本 12px/18px regular */
.tag {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: 12px;
  line-height: 18px;
  letter-spacing: 0px;
  text-align: left;
  color: var(--o-color-info1);
  border-radius: var(--o-radius-xs);
  height: 24px;
  padding: 3px 12px;
  display: inline-flex;
  align-items: center;
  flex-shrink: 0;
  background: transparent;
  border: 1px solid var(--o-color-control1);
}

.tag-platform {
  /* 与 tag-source 一致：信息标签样式（透明底 + 25% 边框） */
}

.tag-source {
  /* 同上 */
}

/* 设计稿：简介 16/24 regular, color=#000000 op=0.60 → info3；简介→链接行间距 32px */
.contributor-desc {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: var(--o-r-font_size-text1);
  line-height: var(--o-r-line_height-text1);
  letter-spacing: 0px;
  text-align: left;
  color: var(--o-color-info3);
  margin-bottom: 32px;
}

.contributor-links {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}

.contributor-link-row {
  display: flex;
  align-items: baseline;
}

/* 设计稿：仓库地址/官网行 16/24 regular, color=#000000 op=0.60 → info3（label 与 URL 同色） */
.contributor-link-label {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: var(--o-r-font_size-text1);
  line-height: var(--o-r-line_height-text1);
  color: var(--o-color-info3);
  flex-shrink: 0;
}

/* 设计稿：仓库地址与官网两段文字间以空格分隔（无"|"竖线），间隙 72px */
.contributor-link-sep {
  width: 72px;
  flex-shrink: 0;
}

.contributor-repo-link {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: var(--o-r-font_size-text1);
  line-height: var(--o-r-line_height-text1);
  letter-spacing: 0px;
  text-align: left;
  color: var(--o-color-link1);
  text-decoration: none;
  word-break: break-all;
}

/* ===== 右侧统计 ===== */
/* 设计稿：容器 170 200x72（两个 stat 各 64，gap 72）；值 36/48 SemiBold info1；label 16/24 regular info3 */
.contributor-stats {
  display: flex;
  align-items: center;
  gap: 72px;
  flex-shrink: 0;
  align-self: center;
}

.stat-item {
  text-align: center;
}

.stat-value {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-semibold);
  /* 36px/48px 无对应响应式 token，保持固定 */
  font-size: 36px;
  line-height: 48px;
  letter-spacing: 0px;
  text-align: center;
  color: var(--o-color-info1);
}

.stat-label {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: var(--o-r-font_size-text1);
  line-height: var(--o-r-line_height-text1);
  letter-spacing: 0px;
  text-align: center;
  color: var(--o-color-info3);
}

/* ===== 列表区域 ===== */
.list-section {
  padding-top: 24px;
  padding-bottom: 64px;
}

.list-header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 24px;
}

/* 设计稿：贡献的技能 40/56 SemiBold #000000 → --o-r-font_size-display3 / line_height-display3 + info1 */
.section-title {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-semibold);
  font-size: var(--o-r-font_size-display3);
  line-height: var(--o-r-line_height-display3);
  letter-spacing: 0px;
  text-align: left;
  color: var(--o-color-info1);
  margin-bottom: 0;
}

/* ===== 卡片网格（3 列，间距 32 对齐设计稿） ===== */
.skills-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 32px;
}

/* ===== 空态 ===== */
.empty-state {
  text-align: center;
  padding: 64px 0;

  .empty-state-svg {
    width: 256px;
    margin: 0 auto 24px;

    :deep(svg) {
      width: 100%;
      height: auto;
    }
  }

  .empty-state-text {
    font-family: HarmonyHeiTi;
    font-weight: var(--o-font_weight-regular);
    font-size: var(--o-r-font_size-text1);
    line-height: var(--o-r-line_height-text1);
    letter-spacing: 0px;
    color: var(--o-color-info3);
  }
}

/* ===== 分页 ===== */
.pagination-row {
  display: flex;
  justify-content: flex-end;
  margin-top: 32px;
}

/* ===== 响应式 ===== */
@media (max-width: 1200px) {
  .skills-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 840px) {
  .contributor-card {
    flex-direction: column;
    gap: 16px;
    /* 移动端重置 PC 端非对称 padding（右侧 64 → 24） */
    padding: 24px;
  }

  .contributor-stats {
    justify-content: flex-start;
    gap: 40px;
  }

  .list-header-row {
    flex-direction: column;
    align-items: stretch;
  }

  .skills-grid {
    grid-template-columns: 1fr;
  }
}
</style>
