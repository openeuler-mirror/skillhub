<script setup lang="ts">
import { ODivider } from '@opensig/opendesign'
import ContentWrapper from './ContentWrapper.vue'

import LogoAtom from '@/assets/footer/atom-logo.png'
import LogoFooter from '@/assets/footer/footer-logo2.png'
import LogoFooterMo from '@/assets/footer/footer-logo1.png'
import policeIcon from '@/assets/footer/police.png'
import CodeQrGzh from '@/assets/footer/code-zgz.jpg'
import CodeQrXzs from '@/assets/footer/code-xzs.png'

const footerCodeList = [
  { code: CodeQrGzh, label: 'openEuler公众号' },
  { code: CodeQrXzs, label: 'openEuler小助手' },
]

// 页脚链接应指向 openEuler 官网，而非当前 SkillHub 域名下的相对路径
const OPEN_EULER_SITE_BASE = 'https://www.openeuler.org'

const quickNav = [
  {
    title: '关于 openEuler',
    list: [
      { title: '成员单位', link: `${OPEN_EULER_SITE_BASE}/zh/community/member/` },
      { title: '组织架构', link: `${OPEN_EULER_SITE_BASE}/zh/community/organization/` },
      { title: '社区章程', link: `${OPEN_EULER_SITE_BASE}/zh/community/charter/` },
      { title: '贡献看板', link: 'https://datastat.openeuler.org/zh/overview' },
      { title: '社区介绍', link: `${OPEN_EULER_SITE_BASE}/whitepaper/openEuler%20%E5%BC%80%E6%BA%90%E7%A4%BE%E5%8C%BA%E4%BB%8B%E7%BB%8D.pdf` },
    ],
  },
  {
    title: '新闻与资讯',
    list: [
      { title: '新闻', link: `${OPEN_EULER_SITE_BASE}/zh/interaction/news-list/` },
      { title: '博客', link: `${OPEN_EULER_SITE_BASE}/zh/interaction/blog-list/` },
      { title: '白皮书', link: `${OPEN_EULER_SITE_BASE}/zh/showcase/technical-white-paper/` },
    ],
  },
  {
    title: '获取与下载',
    list: [
      { title: '获取 openEuler 操作系统', link: `${OPEN_EULER_SITE_BASE}/zh/download/#get-openeuler` },
      { title: '最新社区发行版', link: `${OPEN_EULER_SITE_BASE}/zh/download/` },
      { title: '商业发行版', link: `${OPEN_EULER_SITE_BASE}/zh/download/commercial-release/` },
      { title: '软件中心', link: 'https://easysoftware.openeuler.org/zh' },
    ],
  },
  {
    title: '支持与服务',
    list: [
      { title: '文档', link: 'https://docs.openeuler.org/zh/' },
      { title: 'FAQ', link: `${OPEN_EULER_SITE_BASE}/zh/faq/` },
      { title: '联系我们', link: `${OPEN_EULER_SITE_BASE}/zh/contact-us/` },
    ],
  },
  {
    title: '互动与交流',
    list: [
      { title: '邮件列表', link: `${OPEN_EULER_SITE_BASE}/zh/community/mailing-list/` },
      { title: '活动', link: `${OPEN_EULER_SITE_BASE}/zh/interaction/event-list/` },
      { title: '论坛', link: 'https://forum.openeuler.org/' },
    ],
  },
  {
    title: '贡献与成长',
    list: [
      { title: 'SIG 中心', link: `${OPEN_EULER_SITE_BASE}/zh/sig/sig-list/` },
      { title: '贡献攻略', link: `${OPEN_EULER_SITE_BASE}/zh/community/contribution/` },
      { title: '课程中心', link: `${OPEN_EULER_SITE_BASE}/zh/learn/mooc/` },
    ],
  },
]

const friendshipLinks = [
  { link: 'https://portal.mulanos.cn/', title: '木兰开源社区' },
  { link: 'https://www.hikunpeng.com/zh/', title: '鲲鹏社区' },
  { link: 'https://pcl.ac.cn/', title: '鹏城实验室' },
  { link: 'https://www.infoq.cn/?utm_source=openeuler&utm_medium=youlian', title: 'InfoQ' },
  { link: 'https://kaiyuanshe.cn/', title: '开源社' },
  { link: 'http://www.vulab.com.cn/', title: '中科微澜' },
  { link: 'https://www.authing.cn/', title: 'Authing' },
  { link: 'https://www.opengauss.org/zh/', title: 'openGauss' },
  { link: 'https://www.mindspore.cn/', title: '昇思MindSpore' },
  { link: 'https://www.openubmc.cn/', title: 'openUBMC' },
  { link: 'https://www.openfuyao.cn/', title: 'openFuyao' },
  { link: 'http://www.ebaina.com/', title: 'Ebaina' },
]

const legalLinks = [
  { name: '品牌', url: `${OPEN_EULER_SITE_BASE}/zh/other/brand/` },
  { name: '隐私声明', url: `${OPEN_EULER_SITE_BASE}/zh/other/privacy/` },
  { name: '法律声明', url: `${OPEN_EULER_SITE_BASE}/zh/other/legal/` },
  { name: '关于 cookies', url: `${OPEN_EULER_SITE_BASE}/zh/other/cookies/` },
]
</script>

<template>
  <div class="footer">
    <div class="footer-content">
      <ContentWrapper>
        <div class="info">
          <p class="info-text">
            openEuler 是由开放原子开源基金会孵化的全场景开源操作系统项目，面向数字基础设施四大核心场景（服务器、云计算、边缘计算、嵌入式），全面支持 ARM、x86、RISC-V、LoongArch、PowerPC、SW-64 等多样性计算架构
          </p>
          <a href="https://openatom.cn" target="_blank" rel="noopener noreferrer">
            <img :src="LogoAtom" class="info-logo" alt="开放原子开源基金会" />
          </a>
        </div>

        <div class="content">
          <div class="quick-nav">
            <div v-for="nav in quickNav" :key="nav.title" class="nav">
              <p class="quick-title">{{ nav.title }}</p>
              <p v-for="item in nav.list" :key="item.title" class="nav-item">
                <a :href="item.link" target="_blank" rel="noopener noreferrer">{{ item.title }}</a>
              </p>
            </div>
          </div>

          <div class="friendship-link">
            <span class="friendship-link-title">友情链接</span>
            <div class="friendship-link-box">
              <a
                v-for="item in friendshipLinks"
                :key="item.link"
                :href="item.link"
                target="_blank"
                rel="noopener noreferrer"
              >{{ item.title }}</a>
            </div>
          </div>
        </div>

        <div class="footer-bottom">
          <div class="footer-logo">
            <img class="show-pc" :src="LogoFooter" alt="openEuler" />
            <img class="show-mo" :src="LogoFooterMo" alt="openEuler" />
            <p>
              <a class="email" href="mailto:contact@openeuler.io" target="_blank" rel="noopener noreferrer">
                contact@openeuler.io
              </a>
            </p>
          </div>
          <div class="footer-option">
            <div class="footer-option-item">
              <template v-for="(link, index) in legalLinks" :key="link.name">
                <a :href="link.url" target="_blank" rel="noopener noreferrer">{{ link.name }}</a>
                <ODivider v-if="index !== legalLinks.length - 1" direction="v" />
              </template>
            </div>
            <p class="license">
              <span class="license-text">遵循</span>
              <span class="license-info">木兰宽松许可证第2版（MulanPSL2）</span>
            </p>
            <div class="copyright">
              <div class="copyright-left">
                <p class="copyright-text">版权所有 © {{ new Date().getFullYear() }} openEuler 保留一切权利</p>
                <a href="https://beian.miit.gov.cn/#/Integrated/index" target="_blank" rel="noopener noreferrer" class="beian-info">
                  京ICP备2020036654号-1
                </a>
              </div>
              <div class="filing">
                <img :src="policeIcon" class="filing-img" alt="" />
                <p class="beian-info">京公网安备 11030102011597 号</p>
              </div>
            </div>
          </div>
          <div class="footer-right">
            <div v-for="(item, index) in footerCodeList" :key="index" class="code-pop">
              <img :src="item.code" alt="" />
              <p class="txt">{{ item.label }}</p>
            </div>
          </div>
        </div>
      </ContentWrapper>
    </div>
  </div>
</template>

<style lang="scss" scoped>
$color: #fff;

.footer {
  overflow: hidden;
  background: #121214;
  margin-top: 72px;

  :deep(.content-wrapper) {
    padding-bottom: 0;
  }

  .footer-content {
    background: url('@/assets/footer/footer-bg.png') no-repeat center bottom;
  }

  .info {
    text-align: center;
    padding: 24px 0 16px;

    .info-text {
      color: var(--o-color-white);
      @include h4;
    }
    a {
      display: inline-block;
    }
    .info-logo {
      height: 32px;
      margin-top: 12px;
    }
  }

  .content {
    padding: 16px 0 12px;
    border-top: 1px solid rgba(229, 229, 229, 0.12);
    border-bottom: 1px solid rgba(229, 229, 229, 0.12);

    .quick-nav {
      display: flex;
      justify-content: center;
      flex-wrap: wrap;

      .nav + .nav {
        margin-left: 88px;
      }

      .quick-title {
        color: var(--o-color-white);
        @include h4;
      }
      .nav-item {
        margin-top: 8px;
        @include tip1;
        a {
          color: rgba(255, 255, 255, 0.7);
          @include hover {
            color: rgba(255, 255, 255, 1);
          }
        }
      }
    }

    .friendship-link {
      margin-top: 32px;
      display: flex;
      justify-content: center;
      @include tip2;

      .friendship-link-title {
        color: var(--o-color-white);
        flex-shrink: 0;
      }
      .friendship-link-box {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        margin-left: 32px;

        a {
          margin-right: 24px;
          white-space: nowrap;
          color: rgba(255, 255, 255, 0.7);
          @include hover {
            color: rgba(255, 255, 255, 1);
          }
        }
      }
    }
  }

  .footer-bottom {
    padding: 16px 0 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .footer-logo {
    img {
      height: 46px;
    }
    .show-pc {
      display: block;
    }
    .show-mo {
      display: none;
    }
    .email {
      color: var(--o-color-white);
      margin-top: 4px;
      @include tip1;
    }
  }

  .footer-option {
    text-align: center;
    @include tip1;

    .footer-option-item {
      display: flex;
      align-items: center;
      justify-content: center;
      flex-wrap: wrap;

      a {
        color: rgba(255, 255, 255, 0.8);
        display: inline-block;
      }
      :deep(.o-divider) {
        height: 12px;
        --o-divider-label-gap: 0 8px;
        --o-divider-bd-color: rgba(255, 255, 255, 0.6);
      }
    }

    .license {
      display: flex;
      align-items: center;
      justify-content: center;
      margin-top: 8px;
      color: var(--o-color-white);

      .license-text {
        color: rgba(255, 255, 255, 0.6);
        margin-right: 4px;
      }
    }

    .copyright {
      display: flex;
      align-items: center;
      justify-content: center;
      flex-wrap: wrap;
      margin-top: 4px;
      color: rgba(255, 255, 255, 0.6);

      .copyright-left {
        display: flex;
        a {
          color: rgba(255, 255, 255, 0.6);
          margin-left: 4px;
          @include hover {
            color: rgba(255, 255, 255, 1);
          }
        }
      }
      .filing {
        display: flex;
        align-items: center;
        margin-left: 16px;

        .filing-img {
          height: 18px;
        }
        .beian-info {
          margin-left: 4px;
        }
      }
    }
  }

  .footer-right {
    display: flex;
    flex-shrink: 0;

    .code-pop {
      display: flex;
      flex-direction: column;
      align-items: center;

      img {
        width: 64px;
      }
      .txt {
        margin-top: 4px;
        color: var(--o-color-white);
        @include tip1;
      }
    }
    .code-pop + .code-pop {
      margin-left: 16px;
    }
  }

  @include respond-to('<=pad') {
    .info {
      padding: 12px 0 8px;

      .info-logo {
        margin-top: 8px;
      }
    }
    .content {
      padding: 8px 0;

      .quick-nav {
        .nav + .nav {
          margin-left: auto;
        }
      }
      .friendship-link {
        margin-top: 16px;
      }
    }
    .footer-bottom {
      padding: 12px 0 16px;
    }
  }

  @include respond-to('<=pad_v') {
    .info {
      padding: 24px 0 16px;

      .info-logo {
        height: 30px;
        margin-top: 12px;
      }
    }
    .content {
      padding: 16px 0;

      .friendship-link {
        margin-top: 0;

        .friendship-link-box {
          margin-left: 24px;
        }
      }
    }
    .footer-bottom {
      padding: 16px 0 24px;
      flex-direction: column;
    }
    .copyright {
      flex-direction: column;

      .copyright-left {
        justify-content: center;
        flex-wrap: wrap;
      }
      .filing {
        margin: 4px 0 0;
      }
    }
    .footer-right {
      margin-top: 24px;

      .code-pop img {
        width: 78px;
      }
      .code-pop + .code-pop {
        margin-left: 80px;
      }
    }
  }

  @include respond-to('<=phone') {
    .content .friendship-link {
      flex-direction: column;

      .friendship-link-title {
        margin-bottom: 8px;
      }
      .friendship-link-box {
        margin-left: 0;

        a {
          margin: 4px 24px 0 0;
        }
      }
    }
    .footer-right .code-pop + .code-pop {
      margin-left: 60px;
    }
  }
}
</style>
