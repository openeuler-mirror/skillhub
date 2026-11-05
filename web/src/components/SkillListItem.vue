<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import type { Skill } from '@/api/types'
import { useSkillStore } from '@/stores/skill'
import { oaReport } from '@opendesign-plus/plugins/analytics'

const props = defineProps<{
  skill: Skill
  rank?: number
}>()

const skillStore = useSkillStore()

// 近7天/近30天排序时显示周期下载量，否则显示累计下载量
// 搜索场景后端不返回周期下载量（搜索忽略 sort_period），始终显示累计下载量
// 贡献者详情页等非首页场景后端也不会返回 period_downloads，此时回退到累计下载量
const displayDownloadCount = computed(() => {
  const period = skillStore.filter.sortPeriod
  const isSearching = !!skillStore.filter.keyword
  if (!isSearching && (period === 'week' || period === 'month') && props.skill.period_downloads != null) {
    return props.skill.period_downloads
  }
  return props.skill.download_count
})

function onClickListItem() {
  oaReport('list_click_card', { module: 'skill_list', skill_id: props.skill.skill_id, rank: props.rank })
}

function getSkillRoutePath(skillId: string): string {
  return `/skills/${encodeURIComponent(skillId)}`
}

function truncate(text: string | null, length: number): string {
  const description = text?.trim()
  if (!description) return '暂无说明'
  return description.length > length ? description.slice(0, length) + '...' : description
}
</script>

<template>
  <RouterLink
    :to="getSkillRoutePath(skill.skill_id)"
    class="flex items-center gap-8 px-6 py-4 border-b border-gray-200 bg-white hover:bg-gray-50 transition-colors last:border-b-0 dark:border-gray-700 dark:bg-gray-800 dark:hover:bg-gray-700 group"
    @click="onClickListItem"
  >
    <div class="flex-1 min-w-0 max-w-[560px]">
      <div class="flex items-center mb-1">
        <h3 class="skill-list-name">{{ skill.name }}</h3>
      </div>
      <p class="skill-list-desc">
        {{ truncate(skill.description, 100) }}
      </p>
    </div>

    <div class="w-[100px] text-left">
      <span v-if="skill.category" class="tag tag-category">{{ skill.category_label || skill.category }}</span>
      <span v-else class="text-gray-400 dark:text-gray-500">-</span>
    </div>

    <div class="flex w-[100px] text-sm text-gray-500 dark:text-gray-400 items-center justify-start gap-1">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" class="flex-shrink-0">
        <path d="M12 3.29492C12.3544 3.29492 12.6473 3.55827 12.6936 3.89994L12.7 3.99492L12.7 15.5335L17.2249 10.9581C17.4968 10.6833 17.94 10.6808 18.2149 10.9526C18.4623 11.1973 18.489 11.5808 18.2937 11.8554L18.2204 11.9426L12.5008 17.726C12.3635 17.8647 12.1825 17.9341 12.0016 17.9337L12 17.9337C11.6456 17.9337 11.3527 17.6704 11.3064 17.3287L11.3 17.2337L11.3 3.99492C11.3 3.60832 11.6134 3.29492 12 3.29492ZM6.68698 10.8826C6.41393 10.6851 6.03026 10.7087 5.78362 10.9541C5.50957 11.2268 5.50845 11.67 5.78113 11.9441L8.73786 14.9157L8.82444 14.9897C9.09749 15.1872 9.48116 15.1636 9.72781 14.9182C10.0019 14.6455 10.003 14.2023 9.73029 13.9282L6.77356 10.9566L6.68698 10.8826ZM19.0079 19.3594C19.3945 19.3594 19.7079 19.6728 19.7079 20.0594C19.7079 20.4138 19.4445 20.7066 19.1028 20.753L19.0079 20.7594L5.01445 20.7594C4.62785 20.7594 4.31445 20.446 4.31445 20.0594C4.31445 19.705 4.5778 19.4121 4.91947 19.3658L5.01445 19.3594L19.0079 19.3594Z" fill="currentColor" fill-rule="evenodd"/>
      </svg>
      {{ displayDownloadCount.toLocaleString() }}
    </div>

    <div class="w-36 text-left text-sm text-gray-500 truncate dark:text-gray-400" :title="skill.author || '-'">
      {{ skill.author || '-' }}
    </div>
  </RouterLink>
</template>

<style scoped>
.tag-category {
  border-radius: 4px;
  background: var(--o-color-control2-light);
  color: var(--o-color-info1);
}

.skill-list-name {
  font-family: HarmonyHeiTi;
  font-weight: 600;
  font-size: 16px;
  line-height: 24px;
  letter-spacing: 0px;
  text-align: left;
  color: var(--o-color-info1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin: 0;
}

.group:hover .skill-list-name {
  color: var(--o-color-primary1);
}

.skill-list-desc {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-regular);
  font-size: 14px;
  line-height: var(--o-r-line_height-text1);
  letter-spacing: 0px;
  text-align: left;
  color: var(--o-color-info3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin: 0;
}
</style>
