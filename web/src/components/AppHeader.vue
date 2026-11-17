<script setup lang="ts">
import { RouterLink } from 'vue-router'
import { useAppStore } from '@/stores/app'
import ContentWrapper from './ContentWrapper.vue'
import HeaderNav from './HeaderNav.vue'
import logoSrc from '@/assets/header/logo.svg'
import logoDarkSrc from '@/assets/header/logo_dark.svg'

const appStore = useAppStore()
</script>

<template>
  <header
    class="app-header"
    :class="{ dark: appStore.isDark }"
  >
    <ContentWrapper class="app-header-wrap">
      <!-- Logo -->
      <a href="https://www.openeuler.org/zh/" class="logo-link">
        <img
          class="logo"
          alt="openEuler logo"
          :src="appStore.isDark ? logoDarkSrc : logoSrc"
        />
      </a>
      <div class="logo-divider"></div>
      <RouterLink to="/" class="skillhub-title">SkillHub</RouterLink>

      <!-- 导航 -->
      <HeaderNav />
    </ContentWrapper>
  </header>
</template>

<style lang="scss" scoped>
.app-header {
  background-color: var(--o-color-fill2);
  /* sticky 使 header 与内容同宽（页面固定 1488px），随页面横向滚动，纵向吸附顶部 */
  position: sticky;
  top: 0;
  left: 0;
  right: 0;
  z-index: 98;
  box-shadow: var(--o-shadow-1);
  backdrop-filter: blur(5px);

  &.dark {
    &:after {
      content: '';
      position: absolute;
      left: 0;
      right: 0;
      bottom: 0;
      height: 1px;
      background-color: var(--o-color-control4);
    }
  }

  &:before {
    bottom: 0;
    box-shadow: var(--o-shadow-1);
    content: '';
    left: 0;
    pointer-events: none;
    position: absolute;
    right: 0;
    top: 0;
    z-index: 100;
  }

  .app-header-wrap {
    display: flex;
    align-items: center;
    height: 72px;
  }
}

.logo {
  cursor: pointer;
  flex-shrink: 0;
  height: 32px;
  width: 136px;

  @include respond-to('laptop') {
    margin-right: 0;
  }
  @include respond-to('pad_h') {
    margin-right: 0;
  }
}

.logo-divider {
  width: 1px;
  height: 24px;
  background-color: var(--o-color-control4);
  margin: 0 16px;
  flex-shrink: 0;
}

.skillhub-title {
  font-family: HarmonyHeiTi;
  font-weight: var(--o-font_weight-semibold);
  font-size: 20px;
  line-height: 26px;
  color: var(--o-color-info1);
  text-decoration: none;
  flex-shrink: 0;
  margin-right: var(--o-gap-7);

  @include hover {
    color: var(--o-color-primary1);
  }
}
</style>
