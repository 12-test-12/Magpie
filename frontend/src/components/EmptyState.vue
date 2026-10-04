<script setup>
/**
 * 空态组件 —— 标准的「插画 + 标题 + 一句描述 + 行动按钮」。
 *
 * 为什么单独一个组件：仓库里原来有四种空态（引导卡、镜头页纯文本、
 * 表格 emptyText、抽屉长段落），全是长句堆在页面上，一眼望去密密麻麻。
 * 空状态是这个 App 的门面（计划书 §7.4），它该回答的是「这里为什么是空的、
 * 下一步做什么」—— 一句话标题、一句话描述、一个醒目的 CTA，留白交还给大屏。
 *
 * 插画是**内联 SVG**，不是图片文件也不是外链：R7-ASSETS 红线拦"静态资源引用
 * 外部域名"，一个 `<img src="https://…">` 或 CDN 图会让整轮验证变红。
 * 手写线性图形（望远镜 = 探测/看见，正好是这个工具的隐喻）只多几行字节。
 */
defineProps({
  title: { type: String, required: true },
  desc: { type: String, default: '' },
  /** 插画语气：scan（探测，带 CTA）/ clean（干净，无须行动）/ compact（轻量）。 */
  tone: { type: String, default: 'scan' },
})
</script>

<template>
  <div class="empty-state" :class="{ compact: tone === 'compact' }">
    <!-- 放大镜（compact）：小图标，没有大插画也没有大绿勾 —— "筛选没匹配"是
         中性状态，不是好消息（clean）也不是探索场景（scan），GPT ① 要的
         轻量化就落在这个 tone 上。 -->
    <svg v-if="tone === 'compact'" class="art" viewBox="0 0 96 96"
         fill="none" aria-hidden="true">
      <circle cx="42" cy="42" r="24" stroke="var(--dim)" stroke-width="5" />
      <path d="M60 60 L80 80" stroke="var(--accent)" stroke-width="7"
            stroke-linecap="round" />
    </svg>
    <!-- 望远镜：单色线性，accent 只点在镜片上 -->
    <svg v-else-if="tone === 'scan'" class="art" viewBox="0 0 96 96"
         fill="none" aria-hidden="true">
      <circle cx="48" cy="48" r="43" stroke="var(--line)" stroke-dasharray="3 5" />
      <path d="M20 66 L44 44 L56 54 L34 74 Z" stroke="var(--dim)"
            stroke-width="2" stroke-linejoin="round" />
      <path d="M44 44 L58 30 L70 40 L56 54 Z" stroke="var(--fg)"
            stroke-width="2" stroke-linejoin="round" />
      <circle cx="64" cy="35" r="5" stroke="var(--accent)" stroke-width="2" />
      <path d="M30 72 L24 82 M40 74 L36 84" stroke="var(--dim)"
            stroke-width="2" stroke-linecap="round" />
      <path d="M74 22 A14 14 0 0 1 80 34" stroke="var(--accent)"
            stroke-width="2" stroke-linecap="round" />
      <path d="M76 14 A22 22 0 0 1 86 32" stroke="var(--accent)" stroke-opacity=".45"
            stroke-width="2" stroke-linecap="round" />
    </svg>
    <!-- 对勾：「没有待办」是好消息，不是故障 -->
    <svg v-else class="art" viewBox="0 0 96 96" fill="none" aria-hidden="true">
      <circle cx="48" cy="48" r="30" stroke="var(--ok)" stroke-width="2.5" />
      <path d="M34 49 L44 59 L63 39" stroke="var(--ok)" stroke-width="3"
            stroke-linecap="round" stroke-linejoin="round" />
      <path d="M48 10 V18 M48 78 V86 M10 48 H18 M78 48 H86"
            stroke="var(--line)" stroke-width="2" stroke-linecap="round" />
    </svg>

    <h1 class="title">{{ title }}</h1>
    <p v-if="desc" class="desc">{{ desc }}</p>
    <div v-if="$slots.default" class="actions">
      <slot />
    </div>
  </div>
</template>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  max-width: 460px;
  margin: 0 auto;
  padding: 32px 24px;
  gap: 10px;
}
.art { width: 88px; height: 88px; opacity: .9; }
.title { font-size: 17px; font-weight: 600; margin: 4px 0 0; }
.desc { font-size: 12px; line-height: 1.7; color: var(--dim); margin: 0; }
.actions { display: flex; gap: 8px; margin-top: 8px; }

/* compact（GPT ①）：小图标、收窄留白、字号降一档 —— 筛选空态不该长成
   一屏的欢迎页。 */
.compact { padding: 16px 20px; gap: 6px; }
.compact .art { width: 34px; height: 34px; opacity: .85; }
.compact .title { font-size: 14px; }
.compact .desc { font-size: 11.5px; line-height: 1.6; }
</style>
