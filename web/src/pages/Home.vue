<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSkillStore } from '@/stores/skill'
import { useAppStore } from '@/stores/app'
import { oaReport } from '@opendesign-plus/plugins/analytics'
import { OInput, OTab, OTabPane, OPagination, ODropdown, ODropdownItem, OLoading, OButton } from '@opensig/opendesign'
import FilterSidebar from '@/components/FilterSidebar.vue'
import SkillCard from '@/components/SkillCard.vue'
import SkillListItem from '@/components/SkillListItem.vue'
import heroBgLight from '@/assets/bg/hero-top-texture.png'
import heroBgDark from '@/assets/bg/hero-top-texture-dark.png'
import bannerBgLight from '@/assets/bg/card-bg.png'
import bannerBgDark from '@/assets/bg/card-bg-dark.png'
import viewGridSvg from '@/assets/icons/view-grid.svg?raw'
import viewListSvg from '@/assets/icons/view-list.svg?raw'
import closeSvg from '@/assets/icons/close.svg?raw'
import emptyStateSvg from '@/assets/icons/empty-state.svg?raw'

const skillStore = useSkillStore()
const appStore = useAppStore()
const route = useRoute()
const router = useRouter()
const searchInput = ref('')
const showSearchHistory = ref(false)
const searchHistory = ref<string[]>([])

const SEARCH_HISTORY_KEY = 'skillhub-search-history'
const SEARCH_HISTORY_LIMIT = 8

const sortOptions = [
  { label: '全部时间', value: 'all' },
  { label: '本周', value: 'week' },
  { label: '本月', value: 'month' }
]

const pageSizeOptions = [12, 24, 48, 96]

const currentSortLabel = computed(() => {
  const opt = sortOptions.find(o => o.value === skillStore.filter.sortPeriod)
  return opt ? opt.label : '全部时间'
})

const isSearchPage = computed(() => route.path === '/skills/search')
const searchQuery = computed(() => route.query.q as string || '')

// 是否有侧边栏筛选条件生效（分类/贡献者/安全等级）
const hasActiveFilters = computed(
  () =>
    skillStore.filter.category.length > 0 ||
    skillStore.filter.provider.length > 0 ||
    skillStore.filter.securityLevel.length > 0
)

// 热门浏览态显示结果计数的条件：切到本周/本月，或加了筛选；
// 默认态（热门 + 全部时间 + 无筛选）不显示
const showHotResultCount = computed(
  () =>
    skillStore.filter.sortPeriod !== 'all' || hasActiveFilters.value
)

onMounted(async () => {
  loadSearchHistory()
  document.addEventListener('mousedown', onDocumentMouseDown)
  // 无条件按当前路由同步搜索词：store 跨页面存活，无 q 时必须清掉残留
  // keyword，否则切走再切回 Skills 仍显示上次搜索结果
  searchInput.value = searchQuery.value
  skillStore.setFilter('keyword', searchQuery.value)

  if (route.params.category) {
    skillStore.setFilter('category', route.params.category as string)
  }

  await Promise.all([
    skillStore.fetchStats(),
    skillStore.fetchCategories(),
    skillStore.fetchSkills()
  ])
})

watch(() => route.query.q, (newQ) => {
  // / 与 /skills/search 复用同一 Home 实例，路由切换不触发重挂载；
  // 无 q 的导航（回到首页）同样要清空搜索词并刷新列表
  searchInput.value = (newQ as string) || ''
  skillStore.setFilter('keyword', (newQ as string) || '')
  skillStore.fetchSkills()
})

// 卡片网格引用，用于行内标题对齐
const cardGridRef = ref<HTMLElement | null>(null)

// 同一行内取最大标题高度并应用到整行卡片，保证简介对齐。
// 相比固定两行高度：全一行时无空余空间、超长标题（3行）也不会溢出破坏对齐。
function alignCardTitles() {
  const grid = cardGridRef.value
  if (!grid) return
  const cards = Array.from(grid.querySelectorAll<HTMLElement>('.card'))
  if (!cards.length) return

  // 按行分组：offsetTop 相近（容差 5px）视为同一行
  const rows: HTMLElement[][] = []
  for (const card of cards) {
    const top = card.offsetTop
    const row = rows.find(r => Math.abs(r[0].offsetTop - top) <= 5)
    if (row) row.push(card)
    else rows.push([card])
  }

  for (const row of rows) {
    let maxTitleHeight = 0
    for (const card of row) {
      const title = card.querySelector<HTMLElement>('.skill-card-title')
      if (title) maxTitleHeight = Math.max(maxTitleHeight, title.offsetHeight)
    }
    for (const card of row) {
      const title = card.querySelector<HTMLElement>('.skill-card-title')
      if (title) title.style.minHeight = `${maxTitleHeight}px`
    }
  }
}

// 数据/视图模式变化后重算标题对齐
watch(
  () => [skillStore.skills, skillStore.filter.viewMode],
  async () => {
    await nextTick()
    alignCardTitles()
  }
)

function onResizeAlign() {
  alignCardTitles()
}

onMounted(() => {
  // 字体加载完成（字体影响行高）后重算
  document.fonts?.ready.then(() => alignCardTitles())
  window.addEventListener('resize', onResizeAlign)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResizeAlign)
  document.removeEventListener('mousedown', onDocumentMouseDown)
})

function loadSearchHistory() {
  try {
    const stored = JSON.parse(localStorage.getItem(SEARCH_HISTORY_KEY) || '[]')
    if (Array.isArray(stored)) {
      searchHistory.value = stored.filter(
        (item): item is string => typeof item === 'string' && item.trim().length > 0
      ).slice(0, SEARCH_HISTORY_LIMIT)
    }
  } catch {
    searchHistory.value = []
  }
}

function persistSearchHistory() {
  try {
    localStorage.setItem(SEARCH_HISTORY_KEY, JSON.stringify(searchHistory.value))
  } catch {
    // Storage may be unavailable in privacy-restricted browser contexts.
  }
}

function addSearchHistory(query: string) {
  searchHistory.value = [
    query,
    ...searchHistory.value.filter(item => item !== query)
  ].slice(0, SEARCH_HISTORY_LIMIT)
  persistSearchHistory()
}

function removeSearchHistory(query: string) {
  searchHistory.value = searchHistory.value.filter(item => item !== query)
  persistSearchHistory()
}

function clearSearchHistory() {
  searchHistory.value = []
  persistSearchHistory()
}

function onDocumentMouseDown(event: MouseEvent) {
  const target = event.target as HTMLElement
  if (!target.closest('.search-box-wrapper')) {
    showSearchHistory.value = false
  }
}

// 下载量格式化：超过一万显示 xxx.xw（w=万），否则原样
function formatDownloads(n?: number): string {
  if (n == null) return '0'
  if (n < 10000) return n.toLocaleString()
  return `${(n / 10000).toFixed(1)}w`
}

function handleSearch() {
  const q = searchInput.value.trim()
  if (q) {
    addSearchHistory(q)
    showSearchHistory.value = false
    oaReport('search_submit', { module: 'home', keyword: q })
    router.push({
      path: '/skills/search',
      query: { q }
    })
  } else {
    router.push('/')
    skillStore.setFilter('keyword', '')
    skillStore.fetchSkills()
  }
}

function selectSearchHistory(query: string) {
  searchInput.value = query
  handleSearch()
}

function handleClear() {
  searchInput.value = ''
  oaReport('search_clear', { module: 'home' })
  router.push('/')
  skillStore.setFilter('keyword', '')
  skillStore.fetchSkills()
}

function goToContributors() {
  oaReport('click_contribute_guide', { module: 'home' })
  router.push('/docs/skillhub-publish-and-manage')
}

function setSortBy(sort: 'hot' | 'latest' | 'downloads') {
  skillStore.setFilter('sortBy', sort)
  skillStore.fetchSkills()
}

// 切换排序（热门/最新）时回到第一页
function onSortByChange() {
  oaReport('sort_change', { module: 'home', by: skillStore.filter.sortBy })
  skillStore.setFilter('page', 1)
  skillStore.fetchSkills()
}

function setSortPeriod(period: string) {
  oaReport('sort_period_change', { module: 'home', period })
  skillStore.setFilter('sortPeriod', period)
  skillStore.setFilter('page', 1)
  skillStore.fetchSkills()
}

function setViewMode(mode: 'card' | 'list') {
  oaReport('view_mode_change', { module: 'home', view: mode })
  skillStore.setFilter('viewMode', mode)
}

function onPaginationChange(
  newVal: { page: number; pageSize: number },
  oldVal: { page: number; pageSize: number }
) {
  if (newVal.pageSize !== oldVal.pageSize) {
    skillStore.filter.page = 1
  }
  skillStore.fetchSkills()
  if (newVal.page !== oldVal.page) {
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }
}
</script>

<template>
  <div>
    <!-- Hero 区域 -->
    <section class="hero-section relative" style="height: 319.2px">
      <!-- 设计稿背景图 -->
      <div class="absolute inset-0 pointer-events-none">
        <img :src="appStore.isDark ? heroBgDark : heroBgLight" alt="" class="w-full h-full object-cover" />
      </div>

      <div class="container-wide relative h-full flex flex-col items-center justify-center text-center">
        <h1 class="hero-title">探索、评估和获取可复用的Skills</h1>
        <p class="hero-subtitle">为openEuler社区AI Agent与开发者工作流提供安全、可信的能力集市</p>
        <p class="hero-stats">
          <span class="hero-stats-item">
            <span class="hero-stats-number">{{ skillStore.stats?.total_skills?.toLocaleString() || '200' }}</span>
            <span class="hero-stats-label">Skills</span>
          </span>
          <span class="hero-stats-separator"></span>
          <span class="hero-stats-item">
            <span class="hero-stats-number">{{ skillStore.stats?.total_categories || '15' }}</span>
            <span class="hero-stats-label">领域分类</span>
          </span>
          <span class="hero-stats-separator"></span>
          <span class="hero-stats-item">
            <span class="hero-stats-number">{{ formatDownloads(skillStore.stats?.total_downloads) }}</span>
            <span class="hero-stats-label">下载量</span>
          </span>
        </p>

        <!-- 搜索框：660x40 圆角 4px -->
        <div class="search-box-wrapper" style="max-width:660px; width:100%; margin:0 auto">
          <OInput
            v-model="searchInput"
            placeholder="搜索Skill：使用关键词匹配或自然语言描述"
            size="large"
            round="4px"
            clearable
            @focus="showSearchHistory = true"
            @press-enter="handleSearch"
            @clear="handleClear"
            style="width: 100%"
          >
            <template #prefix>
              <svg class="search-icon" width="24" height="24" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
                <path d="M17.549 16.523l0.087 0.074 2.76 2.754c0.274 0.273 0.274 0.716 0.001 0.99-0.246 0.246-0.629 0.271-0.903 0.075l-0.087-0.074-2.76-2.754c-0.274-0.273-0.274-0.716-0.001-0.99 0.246-0.246 0.629-0.271 0.903-0.075zM10.821 3.454c4.099 0 7.423 3.323 7.423 7.423s-3.323 7.423-7.423 7.423c-4.099 0-7.423-3.323-7.423-7.423s3.323-7.423 7.423-7.423zM10.821 4.854c-3.326 0-6.023 2.696-6.023 6.023s2.696 6.023 6.023 6.023c3.326 0 6.023-2.696 6.023-6.023s-2.696-6.023-6.023-6.023z" fill="currentColor"></path>
              </svg>
            </template>
          </OInput>
          <div
            v-if="showSearchHistory && searchHistory.length"
            class="search-history-panel"
            role="listbox"
            aria-label="搜索历史"
          >
            <div class="search-history-header">
              <span>搜索历史</span>
              <button type="button" class="search-history-clear" @mousedown.prevent="clearSearchHistory">
                清空
              </button>
            </div>
            <div
              v-for="item in searchHistory"
              :key="item"
              class="search-history-item"
              role="option"
              tabindex="0"
              @mousedown.prevent="selectSearchHistory(item)"
              @keydown.enter.prevent="selectSearchHistory(item)"
            >
              <svg class="search-history-clock" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M12 4a8 8 0 1 0 8 8 8.01 8.01 0 0 0-8-8Zm0 14a6 6 0 1 1 6-6 6.01 6.01 0 0 1-6 6Zm.5-9h-1v3.4l2.7 1.6.5-.86-2.2-1.3Z" fill="currentColor" />
              </svg>
              <span class="search-history-text">{{ item }}</span>
              <button
                type="button"
                class="search-history-remove"
                aria-label="删除搜索历史"
                @mousedown.stop.prevent="removeSearchHistory(item)"
              >
                <span class="search-history-remove-icon" v-html="closeSvg" aria-hidden="true"></span>
              </button>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- 主要内容区 -->
    <section class="container-wide py-6">
      <div class="flex gap-8">
        <!-- 左侧筛选侧栏 -->
        <div class="w-60 flex-shrink-0 sticky bottom-0 self-start">
          <FilterSidebar />
        </div>

        <!-- 右侧内容区 -->
        <div class="flex-1 min-w-0">
          <!-- 搜索结果提示 + 工具栏 -->
          <div class="flex items-center justify-between mb-4">
            <div class="flex items-center gap-2">
              <OTab
                v-model="skillStore.filter.sortBy"
                variant="button"
                round="4px"
                size="large"
                header-class="sort-tab"
                @change="onSortByChange"
              >
                <OTabPane value="hot" label="热门" />
                <OTabPane value="latest" label="最新" />
              </OTab>

              <div class="sort-dropdown-wrapper" v-if="skillStore.filter.sortBy !== 'latest'">
              <ODropdown
                trigger="click"
                option-width-mode="min-width"
                option-wrap-class="sort-period-dropdown"
                style="--dropdown-item-bg-color-hover: var(--o-color-control2-light)"
              >
                <button class="sort-period-btn">
                  {{ currentSortLabel }}
                  <svg class="w-4 h-4" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M5.759 8.873a.7.7 0 0 1 .918-.063l.072.063 5.016 5.016a.3.3 0 0 0 .37.043l.054-.043 5.062-5.062a.7.7 0 0 1 1.053.918l-.063.072-5.062 5.062a1.7 1.7 0 0 1-2.296.099l-.108-.099-5.016-5.016a.7.7 0 0 1 0-.99"/>
                  </svg>
                </button>
                <template #dropdown>
                  <ODropdownItem
                    v-for="opt in sortOptions"
                    :key="opt.value"
                    :label="opt.label"
                    :value="opt.value"
                    @click="setSortPeriod(opt.value)"
                  />
                </template>
              </ODropdown>
              </div>

              <!-- 结果计数提示：数据加载完成后再显示 -->
              <!-- 搜索态：展示与关键词匹配的结果数 -->
              <div v-if="isSearchPage && searchQuery && !skillStore.loading" class="text-sm text-[var(--o-color-info3)]">
                为您找到 <span class="text-[var(--o-color-info1)] font-semibold">{{ skillStore.total }}</span> 个与 "{{ searchQuery }}" 匹配的搜索结果
              </div>
              <!-- 热门浏览态：仅统计下载量大于 0 的 Skill；
                   默认态（热门+全部时间+无筛选）不显示，切周期或加筛选后才显示 -->
              <div v-else-if="!isSearchPage && skillStore.filter.sortBy === 'hot' && showHotResultCount && !skillStore.loading" class="text-sm text-[var(--o-color-info3)]">
                为您找到 <span class="text-[var(--o-color-info1)] font-semibold">{{ skillStore.total }}</span> 个相关Skill
              </div>
            </div>

            <OTab
                v-model="skillStore.filter.viewMode"
                variant="button"
                round="4px"
                size="large"
                header-class="view-tab"
              >
                <OTabPane value="card">
                  <template #nav>
                    <span class="w-5 h-5 flex items-center justify-center" v-html="viewGridSvg" title="卡片视图"></span>
                  </template>
                </OTabPane>
                <OTabPane value="list">
                  <template #nav>
                    <span class="w-5 h-5 flex items-center justify-center" v-html="viewListSvg" title="列表视图"></span>
                  </template>
                </OTabPane>
              </OTab>
          </div>

          <!-- 加载态：内联 spinner，不使用全屏遮罩，避免整页变黑 -->
          <div v-if="skillStore.loading" class="loading-container">
            <OLoading :visible="true" size="medium" :mask="false" />
          </div>

          <!-- 空态 -->
          <div v-else-if="!skillStore.loading && skillStore.skills.length === 0" class="text-center py-16">
            <div class="empty-state-svg w-64 mx-auto mb-6" v-html="emptyStateSvg"></div>
            <p class="text-[var(--o-color-info3)]">暂无相关 Skill</p>
          </div>

          <!-- Skill 列表 -->
          <template v-else-if="!skillStore.loading">
            <div v-if="skillStore.filter.viewMode === 'card'" ref="cardGridRef" class="grid grid-cols-3 gap-4">
              <SkillCard v-for="(skill, index) in skillStore.skills" :key="skill.id" :skill="skill" :rank="index" />
            </div>
            <div v-else class="list-view-container">
              <!-- 列表视图表头 -->
              <div class="flex items-center gap-8 px-6 py-4 list-header" style="font-family: HarmonyHeiTi; font-size: 14px; line-height: 22px; font-weight: 600; letter-spacing: 0px; border-bottom: 1px solid var(--o-color-primary1);">
                <div class="flex-1 min-w-0 max-w-[560px]">名称</div>
                <div class="w-[100px]">分类</div>
                <div class="w-[100px]">风险等级</div>
                <div class="w-[100px]">下载量</div>
                <div class="w-36">贡献者</div>
              </div>
              <SkillListItem v-for="(skill, index) in skillStore.skills" :key="skill.id" :skill="skill" :rank="index" />
            </div>
          </template>

          <!-- 分页 -->
          <div v-if="skillStore.total > 0 && !skillStore.loading" class="flex items-center justify-between mt-8">
              <div></div>
              <OPagination
                v-model:page="skillStore.filter.page"
                v-model:page-size="skillStore.filter.pageSize"
                :total="skillStore.total"
                :page-sizes="pageSizeOptions"
                :layout="['total', 'pagesize', 'pager', 'jumper']"
                @change="onPaginationChange"
              />
            </div>
        </div>
      </div>

      <!-- 贡献你的Skill 横幅 -->
      <div class="mt-8 submit-banner">
        <img :src="appStore.isDark ? bannerBgDark : bannerBgLight" alt="" class="submit-banner-bg" />
        <h3 class="submit-banner-title">贡献你的Skill</h3>
        <p class="submit-banner-desc">SkillHub 欢迎大家贡献可复用的Skills，一起丰富openEuler AI技能生态</p>
        <OButton
          color="primary"
          variant="solid"
          round="pill"
          class="submit-banner-btn"
          @click="goToContributors"
        >查看 Skill 贡献指南</OButton>
      </div>
    </section>
  </div>
</template>

<style scoped>
.container-wide {
  max-width: 1488px;
  margin: 0 auto;
  padding: 0 24px;
}

.hero-section {
  z-index: 10;
}

/* 加载容器 */
.loading-container {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 300px;
}

/* 搜索图标： #000000 op=0.80 = --o-color-info2 */
.search-icon {
  width: 24px;
  height: 24px;
  color: var(--o-color-info2);
}

.search-box-wrapper {
  position: relative;
  z-index: 2;
}

.search-history-panel {
  position: absolute;
  top: calc(100% + var(--o-gap-1));
  left: 0;
  width: 100%;
  max-height: 320px;
  overflow-y: auto;
  padding: var(--o-gap-2) 0;
  border: 1px solid var(--o-color-control4);
  border-radius: var(--o-radius-m);
  background-color: var(--o-color-fill2);
  box-shadow: var(--o-shadow-l1);
  text-align: left;
}

.search-history-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 32px;
  padding: 0 var(--o-gap-4);
  color: var(--o-color-info3);
  font-size: var(--o-font_size-tip1);
  line-height: var(--o-line_height-tip1);
}

.search-history-clear,
.search-history-remove {
  border: 0;
  padding: 0;
  background: transparent;
  color: var(--o-color-info3);
  cursor: pointer;
}

.search-history-clear {
  min-height: 32px;
  font-size: var(--o-font_size-tip1);
  line-height: var(--o-line_height-tip1);
}

.search-history-clear:hover,
.search-history-remove:hover {
  color: var(--o-color-primary1);
}

.search-history-item {
  display: flex;
  align-items: center;
  width: 100%;
  min-height: 40px;
  padding: 0 var(--o-gap-4);
  color: var(--o-color-info1);
  cursor: pointer;
  outline: none;
}

.search-history-item:hover,
.search-history-item:focus-visible {
  background-color: var(--o-color-control2-light);
}

.search-history-clock {
  width: 18px;
  height: 18px;
  flex-shrink: 0;
}

.search-history-remove-icon {
  display: flex;
}

/* v-html 注入的 svg 不带 scoped 属性，需 :deep() 才能命中 */
.search-history-remove :deep(svg) {
  width: 18px;
  height: 18px;
  flex-shrink: 0;
}

.search-history-clock {
  margin-right: var(--o-gap-3);
  color: var(--o-color-info3);
}

.search-history-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  color: var(--o-color-info1);
  font-size: var(--o-font_size-text1);
  line-height: var(--o-line_height-text1);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.search-history-remove {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  margin-left: var(--o-gap-2);
  flex-shrink: 0;
  /* 默认隐藏：悬停历史项时淡入，减少视觉噪音与误触；
     pointer-events:none 保证隐藏时不可点，但仍可 Tab 聚焦 */
  opacity: 0;
  pointer-events: none;
  transition: opacity var(--o-duration-m1) var(--o-easing-standard);
}

.search-history-item:hover .search-history-remove,
.search-history-remove:focus-visible {
  opacity: 1;
  pointer-events: auto;
}

/* 触屏设备没有 hover，删除按钮保持常显 */
@media (hover: none) {
  .search-history-remove {
    opacity: 1;
    pointer-events: auto;
  }
}

/* Hero 标题： size=40 weight=SemiBold color=#000000 */
.hero-title {
  color: var(--o-color-info1);
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-semibold);
  font-size: var(--o-font_size-display3);
  line-height: var(--o-line_height-display3);
  letter-spacing: 0px;
  text-align: left;
  margin-bottom: 8px
}

.hero-subtitle {
  color: var(--o-color-info3);
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: var(--o-font_size-text1);
  line-height: var(--o-line_height-text1);
  letter-spacing: 0px;
  text-align: left;
  margin-bottom: 8px
}

.hero-stats {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 496px;
  height: 40px;
  gap: 32px;
  font-family: HarmonyHeiTi;
  text-align: center;
  margin-bottom: 32px;
}

.hero-stats-item {
  display: inline-flex;
  flex-direction: row;
  align-items: flex-start;
  width: 142px;
  height: 40px;
  flex-shrink: 0;
  /*容器高 40，数字 y=0 行盒40，文字 y=13 行盒24，水平间距 8 */
  column-gap: 8px;
}

.hero-stats-number {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-semibold);
  font-size: 28px;
  line-height: 40px;
  letter-spacing: 0px;
  text-align: center;
  color: var(--o-color-primary1);
}

.hero-stats-label {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: 16px;
  /* 文字相对容器顶部偏移 13px，行盒 24px（盒底 37，距数字盒底 3px） */
  line-height: 24px;
  margin-top: 13px;
  letter-spacing: 0px;
  text-align: center;
  color: var(--o-color-info3);
}

/* 1x17px 竖线，颜色 #000000 op=0.25 = --o-color-control1 */
.hero-stats-separator {
  display: inline-block;
  width: 1px;
  height: 17px;
  background-color: var(--o-color-control1);
  flex-shrink: 0;
}

/* 列表视图容器 */
.list-view-container {
  border: 1px solid var(--o-color-control4);
  border-radius: var(--o-radius-m);
  overflow: hidden;
}

[data-o-theme="e.dark"] .list-view-container,
.dark .list-view-container {
  border-color: var(--o-color-control4);
}

/* 列表视图表头 */
.list-header {
  background-color: var(--o-color-fill2);
  color: var(--o-color-info2);
}

[data-o-theme="e.dark"] .list-header,
.dark .list-header {
  background-color: var(--o-color-fill2);
  color: var(--o-color-info2);
}

/* 贡献你的Skill 横幅 */
/* 容器 1488x200 cornerRadius=4；内层蒙版 1488x265.71 渐变 cornerRadius=8.18 */
.submit-banner {
  position: relative;
  overflow: hidden;
  border-radius: var(--o-radius-xs);
  padding: 32px 24px;
  text-align: center;
  background: var(--o-color-fill2);
}

.submit-banner-bg {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  pointer-events: none;
}

/* 背景图为绝对定位，需把标题/描述/按钮等子元素提到图片之上 */
.submit-banner > *:not(.submit-banner-bg) {
  position: relative;
  z-index: 1;
}

.submit-banner-title {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-semibold);
  font-size: var(--o-font_size-h1);
  line-height: var(--o-line_height-h1);
  letter-spacing: 0px;
  color: var(--o-color-info1);
  margin-bottom: 16px;
}

.submit-banner-desc {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: var(--o-font_size-text1);
  line-height: var(--o-line_height-text1);
  letter-spacing: 0px;
  color: var(--o-color-info1);
  margin-bottom: 24px;
}

/* 横幅按钮：深蓝实心胶囊 */
.submit-banner-btn {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: var(--o-font_size-tip1);
  line-height: var(--o-line_height-tip1);
  letter-spacing: 0px;
}

[data-o-theme="e.dark"] .submit-banner,
.dark .submit-banner {
  background: var(--o-color-fill2);
}

/* OTab button variant: 排序切换 (热门/最新) */
/* 设计稿：选中态 #002FA7 SemiBold，未选中态 #000000 op=0.80 = --o-color-info2 */
.sort-tab {
  width: 148px;

  :deep(.o-tab-head) {
    display: flex;
    align-items: center;
    background: var(--o-color-fill1);
    border-radius: 4px;
    padding: 4px;
    height: 48px !important;
    border: none;
    box-sizing: border-box;
  }

  :deep(.o-tab-navs) {
    gap: 0;
    height: 40px;
    align-items: center;
  }

  :deep(.o-tab-nav-list) {
    height: 40px;
    align-items: center;
  }

  :deep(.o-tab-nav) {
    width: 68px !important;
    min-width: 68px !important;
    height: 40px;
    padding: 0;
    border: none !important;
    font-family: HarmonyHeiTi;
    font-weight: var(--o-font_weight-regular);
    font-size: var(--o-r-font_size-text2);
    line-height: var(--o-r-line_height-text2);
    color: var(--o-color-info2) !important;
    border-radius: 4px !important;
    background: transparent !important;
    justify-content: center;
    align-items: center;

    &:hover:not(.o-tab-nav-active) {
      color: var(--o-color-primary1) !important;
      background: color-mix(in srgb, var(--o-color-primary1) 8%, transparent);
    }

    &.o-tab-nav-active {
      font-weight: var(--o-font_weight-semibold);
      color: var(--o-color-primary1) !important;
      background: var(--o-color-fill2) !important;
      box-shadow: var(--o-shadow-1);
    }
  }
}

/* OTab button variant: 视图切换 (卡片/列表) */
.view-tab {
  --tab-nav-padding: 8px 16px;

  :deep(.o-tab-head) {
    background: var(--o-color-fill1);
    border-radius: 4px;
    padding: 3px;
    min-height: auto;
    border: none;
    box-sizing: border-box;
  }

  :deep(.o-tab-navs) {
    gap: 0;
  }

  :deep(.o-tab-nav) {
    width: 44px !important;
    min-width: 44px !important;
    padding: 0;
    border: none !important;
    color: var(--o-color-primary1) !important;
    border-radius: 4px !important;
    background: transparent !important;
    justify-content: center;
    align-items: center;

    &:hover:not(.is-active) {
      background: color-mix(in srgb, var(--o-color-primary1) 8%, transparent);
    }

    &.is-active {
      background: var(--o-color-fill2) !important;
      box-shadow: var(--o-shadow-1);
    }
  }
}

/* 空态 SVG */
.empty-state-svg {
  display: flex;
  align-items: center;
  justify-content: center;
  aspect-ratio: 8 / 7;
}

.empty-state-svg svg {
  width: 100%;
  height: 100%;
}

/* OSelect 覆盖：与筛选栏统一 */
:deep(.o-select) {
  --select-height: 32px;
  --select-radius: 4px;
  --select-text-size: 16px;
  width: 120px;
  border: none !important;
  background: transparent !important;
}

:deep(.o-select-input) {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: var(--o-r-font_size-text1);
  line-height: var(--o-r-line_height-text1);
  color: var(--o-color-info1);
  text-align: center;
}

:deep(.o-option-item) {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: var(--o-r-font_size-text1);
  line-height: var(--o-r-line_height-text1);
  color: var(--o-color-info1);
  text-align: right;
}

/* 分页下拉框 */
.page-size-select {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: var(--o-font_size-tip1);
  line-height: var(--o-line_height-tip1);
  letter-spacing: 0px;
  text-align: left;
  color: var(--o-color-info1);
}

[data-o-theme="e.dark"] .page-size-select,
.dark .page-size-select {
  background-color: var(--o-color-fill2);
}

/* 分页信息文本 */
.pagination-info {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: var(--o-font_size-tip1);
  line-height: var(--o-line_height-tip1);
  letter-spacing: 0px;
  text-align: left;
  color: var(--o-color-info2);
}

/* OPagination 统一高度 32px */
:deep(.o-pagination-wrap) {
  --pagination-item-size: 32px;
  --pagination-arrow-size: 32px;
}

:deep(.o-pagination-select.o-select) {
  height: 32px !important;
  border-radius: 4px !important;
  background: var(--o-color-white) !important;
  border: 1px solid var(--o-color-control1) !important;
  transition: border-color var(--o-duration-s) var(--o-easing-standard);

  &:hover {
    border-color: var(--o-color-primary1) !important;
  }
}

:deep(.o-pagination-input.o-input-number) {
  height: 32px !important;
  --input-height: 32px;
}

:deep(.o-pagination-input.o-input-number .o_input-input) {
  height: 32px !important;
}

/* 下拉按钮 92x40，文字 16px regular，颜色 #000000，hover #002FA7 */
.sort-dropdown-wrapper {
  margin-left: 16px;
}

.sort-period-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  height: 40px;
  width: 92px;
  min-width: 92px;
  max-width: 92px;
  box-sizing: border-box;
  padding: 0 4px;
  border: none;
  background: none;
  color: var(--o-color-info1);
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: var(--o-r-font_size-text1);
  line-height: var(--o-r-line_height-text1);
  letter-spacing: 0px;
  text-align: right;
  cursor: pointer;
  border-radius: var(--o-radius_control-xs);
  white-space: nowrap;

  &:hover {
    color: var(--o-color-primary1);
  }
}

[data-o-theme="e.dark"] .sort-period-btn:hover,
.dark .sort-period-btn:hover {
  color: var(--o-color-primary1);
}

[data-o-theme="e.dark"] .sort-period-btn,
.dark .sort-period-btn {
  color: var(--o-color-info1);
}

/* 分页跳转输入框 */
:deep(.o_box-main) {
  border-radius: 4px;
  background: var(--o-color-white);
  border: 1px solid var(--o-color-control4);
}

/* 分页箭头图标替换 */
:deep(.o-icon-chevron-left),
:deep(.o-icon-chevron-right) {
  width: 24px;
  height: 24px;
}

:deep(.o-icon-chevron-left path),
:deep(.o-icon-chevron-right path) {
  display: none;
}

:deep(.o-icon-chevron-left) {
  mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24'%3E%3Cpath d='M14.754 5.764c0.251 0.251 0.271 0.644 0.063 0.918l-0.063 0.072-5.016 5.016c-0.1 0.1-0.115 0.254-0.043 0.37l0.043 0.054 5.062 5.062c0.273 0.273 0.273 0.717 0 0.99-0.251 0.251-0.644 0.271-0.918 0.063l-0.072-0.063-5.062-5.062c-0.629-0.629-0.662-1.628-0.099-2.296l0.099-0.108 5.016-5.016c0.273-0.273 0.717-0.273 0.99 0z'/%3E%3C/svg%3E") no-repeat center;
  -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24'%3E%3Cpath d='M14.754 5.764c0.251 0.251 0.271 0.644 0.063 0.918l-0.063 0.072-5.016 5.016c-0.1 0.1-0.115 0.254-0.043 0.37l0.043 0.054 5.062 5.062c0.273 0.273 0.273 0.717 0 0.99-0.251 0.251-0.644 0.271-0.918 0.063l-0.072-0.063-5.062-5.062c-0.629-0.629-0.662-1.628-0.099-2.296l0.099-0.108 5.016-5.016c0.273-0.273 0.717-0.273 0.99 0z'/%3E%3C/svg%3E") no-repeat center;
  mask-size: contain;
  -webkit-mask-size: contain;
  background: currentColor;
}

:deep(.o-icon-chevron-right) {
  mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24'%3E%3Cpath d='M9.246 5.764c-0.251 0.251-0.271 0.644-0.063 0.918l0.063 0.072 5.016 5.016c0.1 0.1 0.115 0.254 0.043 0.37l-0.043 0.054-5.062 5.062c-0.273 0.273-0.273 0.717 0 0.99 0.251 0.251 0.644 0.271 0.918 0.063l0.072-0.063 5.062-5.062c0.629-0.629 0.662-1.628 0.099-2.296l-0.099-0.108-5.016-5.016c-0.273-0.273-0.717-0.273-0.99 0z'/%3E%3C/svg%3E") no-repeat center;
  -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24'%3E%3Cpath d='M9.246 5.764c-0.251 0.251-0.271 0.644-0.063 0.918l0.063 0.072 5.016 5.016c0.1 0.1 0.115 0.254 0.043 0.37l-0.043 0.054-5.062 5.062c-0.273 0.273-0.273 0.717 0 0.99 0.251 0.251 0.644 0.271 0.918 0.063l0.072-0.063 5.062-5.062c0.629-0.629 0.662-1.628 0.099-2.296l-0.099-0.108-5.016-5.016c-0.273-0.273-0.717-0.273-0.99 0z'/%3E%3C/svg%3E") no-repeat center;
  mask-size: contain;
  -webkit-mask-size: contain;
  background: currentColor;
}
</style>

<style lang="scss">
/* Teleport 到 body 的下拉面板全局样式 */
.sort-period-dropdown {
  width: 116px !important;
  min-width: 116px;
  transform: translateX(-12px);
}

[data-o-theme="e.dark"] .o-pagination-select.o-select,
.dark .o-pagination-select.o-select {
  background: var(--o-color-fill2) !important;
  border-color: var(--o-color-control4) !important;

  &:hover {
    border-color: var(--o-color-primary1) !important;
  }
}

[data-o-theme="e.dark"] .o-pagination-select .o-select-input,
.dark .o-pagination-select .o-select-input {
  color: var(--o-color-info1) !important;
}

[data-o-theme="e.dark"] .o_box-main,
.dark .o_box-main {
  background: var(--o-color-fill2);
  border-color: var(--o-color-control4);
}

[data-o-theme="e.dark"] .o_box-main .o_input-input,
.dark .o_box-main .o_input-input {
  color: var(--o-color-info1) !important;
}

.o-dropdown-item:hover {
  background: var(--o-color-control2-light);
}
</style>
