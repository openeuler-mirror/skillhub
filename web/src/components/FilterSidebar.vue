<script setup lang="ts">
import { computed } from 'vue'
import { useSkillStore } from '@/stores/skill'
import { oaReport } from '@opendesign-plus/plugins/analytics'

const skillStore = useSkillStore()

const platformLabel: Record<string, string> = {
  community: '社区SIG',
  enterprise: '企业组织',
  personal: '个人'
}

const platformKeys = ['community', 'enterprise', 'personal']

const providers = computed(() => {
  return platformKeys.map(key => ({
    name: platformLabel[key],
    value: key
  }))
})

const allCategories = computed(() => skillStore.categories)

function selectProviderAll() {
  skillStore.setFilter('provider', [])
  skillStore.fetchSkills()
}

function selectCategoryAll() {
  skillStore.setFilter('category', [])
  skillStore.fetchSkills()
}

function toggleArrayItem(arr: string[], item: string): string[] {
  return arr.includes(item) ? arr.filter(i => i !== item) : [...arr, item]
}

function selectCategory(name: string) {
  const next = toggleArrayItem(skillStore.filter.category, name)
    // 上报本次点击的单个分类（而非整个选中数组），便于按分类维度聚合统计
  oaReport('filter_change', { module: 'filter', dim: 'category', value: name })
  skillStore.setFilter('category', next)
  skillStore.fetchSkills()
}

function selectProvider(name: string) {
  const current = skillStore.filter.provider
  // 单选语义：再次点击已选中的项保持选中不变
  if (current.length === 1 && current[0] === name) return
  oaReport('filter_change', { module: 'filter', dim: 'provider', value: name })
  skillStore.setFilter('provider', [name])
  skillStore.fetchSkills()
}

function clearFilters() {
  oaReport('filter_clear', { module: 'filter' })
  skillStore.resetFilter()
  skillStore.fetchSkills()
}

const hasActiveFilter = computed(() => {
  const f = skillStore.filter
  return f.keyword || f.category.length > 0 || f.provider.length > 0 || f.securityLevel.length > 0
})
</script>

<template>
  <aside class="w-56 flex-shrink-0">
    <div class="space-y-6">
      <div>
        <h3 class="filter-section-title">贡献者</h3>
        <div class="space-y-[1px]">
          <div
            class="filter-item"
            :class="{ 'filter-item-active': skillStore.filter.provider.length === 0 }"
            @click="selectProviderAll"
          >
            <span class="filter-item-label">
              <span class="filter-radio" :class="{ 'filter-radio-active': skillStore.filter.provider.length === 0 }">
              </span>
              全部
            </span>
          </div>
          <div
            v-for="p in providers"
            :key="p.value"
            class="filter-item"
            :class="{ 'filter-item-active': skillStore.filter.provider.includes(p.value) }"
            @click="selectProvider(p.value)"
          >
            <span class="filter-item-label">
              <span class="filter-radio" :class="{ 'filter-radio-active': skillStore.filter.provider.includes(p.value) }">
              </span>
              {{ p.name }}
            </span>
          </div>
        </div>
      </div>

      <div>
        <h3 class="filter-section-title">分类</h3>
        <div class="space-y-[1px]">
          <div
            class="filter-item"
            :class="{ 'filter-item-active': skillStore.filter.category.length === 0 }"
            @click="selectCategoryAll"
          >
            <span class="filter-item-label">
              <span class="filter-checkbox" :class="{ 'filter-checkbox-active': skillStore.filter.category.length === 0 }">
                <svg v-if="skillStore.filter.category.length === 0" class="filter-checkbox-icon" viewBox="0 0 12 12" fill="none">
                  <path d="M2.5 6L5 8.5L9.5 3.5" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
              </span>
              全部
            </span>
          </div>
          <div
            v-for="cat in allCategories"
            :key="cat.name"
            class="filter-item"
            :class="{ 'filter-item-active': skillStore.filter.category.includes(cat.name) }"
            @click="selectCategory(cat.name)"
          >
            <span class="filter-item-label">
              <span class="filter-checkbox" :class="{ 'filter-checkbox-active': skillStore.filter.category.includes(cat.name) }">
                <svg v-if="skillStore.filter.category.includes(cat.name)" class="filter-checkbox-icon" viewBox="0 0 12 12" fill="none">
                  <path d="M2.5 6L5 8.5L9.5 3.5" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
              </span>
              {{ cat.label || cat.name }}
            </span>
          </div>
        </div>
      </div>

      <button v-if="hasActiveFilter" class="filter-clear" @click="clearFilters">
        清空筛选
      </button>
    </div>
  </aside>
</template>

<style scoped>
.filter-section-title {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-semibold);
  font-size: var(--o-r-font_size-text1);
  line-height: var(--o-r-line_height-text1);
  letter-spacing: 0px;
  text-align: left;
  color: var(--o-color-info4);
  margin-bottom: 12px;
}

[data-o-theme="e.dark"] .filter-section-title,
.dark .filter-section-title {
  color: #C9CDD4;
}

[data-o-theme="e.dark"] .filter-radio,
.dark .filter-radio {
  background-color: var(--o-color-fill2);
}

[data-o-theme="e.dark"] .filter-radio-active::after,
.dark .filter-radio-active::after {
  background: var(--o-color-fill2);
}

[data-o-theme="e.dark"] .filter-checkbox,
.dark .filter-checkbox {
  background-color: var(--o-color-fill2);
}

.filter-item-label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: var(--o-r-font_size-text1);
  line-height: var(--o-r-line_height-text1);
  letter-spacing: 0px;
  text-align: left;
  color: var(--o-color-info1);
}

.filter-item-active .filter-item-label {
  font-weight: var(--o-font_weight-semibold);
  color: var(--o-color-primary1);
}

.filter-item {
  display: flex;
  align-items: center;
  padding: 8px 12px;
  border-radius: var(--o-radius-s);
  cursor: pointer;
  transition: all var(--o-duration-s) var(--o-easing-standard);
  user-select: none;
}

.filter-item:hover {
  background: var(--o-color-control2-light);
  border-radius: 8px;
}

.filter-item-active:hover {
  background-color: #CEDBF5;
  border-radius: var(--o-radius-s);
}

[data-o-theme="e.dark"] .filter-item:hover,
.dark .filter-item:hover {
  background: var(--o-color-fill3);
}

.filter-item-active {
  font-weight: var(--o-font_weight-medium);
  background-color: #CEDBF5;
  border-radius: var(--o-radius-s);
}

[data-o-theme="e.dark"] .filter-item-active:hover,
.dark .filter-item-active:hover {
  background-color: #353539;
}

[data-o-theme="e.dark"] .filter-item-active,
.dark .filter-item-active {
  background-color: #353539;
}

.filter-radio {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  border: 1px solid var(--o-color-control1);
  background-color: var(--o-color-white);
  flex-shrink: 0;
  transition: all var(--o-duration-s) var(--o-easing-standard);
}

.filter-radio-active {
  border: 4px solid var(--o-color-primary1);
  background-color: var(--o-color-primary1);
}

.filter-radio-active::after {
  content: '';
  display: block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--o-color-white);
  border: 1px solid var(--o-color-control4);
}

.filter-checkbox {
  width: 16px;
  height: 16px;
  border-radius: 4px;
  border: 1px solid var(--o-color-control1);
  background-color: var(--o-color-white);
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all var(--o-duration-s) var(--o-easing-standard);
}

.filter-checkbox-active {
  background-color: var(--o-color-primary1);
  border-color: var(--o-color-primary1);
}

.filter-checkbox-icon {
  width: 10px;
  height: 10px;
}

.filter-clear {
  width: 100%;
  text-align: left;
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: 16px;
  line-height: 24px;
  letter-spacing: 0px;
  color: var(--o-color-info1);
  cursor: pointer;
  transition: color var(--o-duration-s) var(--o-easing-standard);
  padding: 4px 0;
  border: none;
  background: none;
}

.filter-clear:hover {
  color: var(--o-color-primary2);
}
</style>
