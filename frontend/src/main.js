/**
 * 入口。挂载 App，并把 Vue Flow 的样式装载进来。
 *
 * Vue Flow 的 `theme-default.css` 带一套浅色主题（白底、灰边），本项目的画布
 * 是**深色**的（计划书 §7.3）。所以顺序很重要：先装它的样式，再装本项目的
 * `tokens.css` —— 后者覆盖前者。反过来会让页面在第一次渲染时闪一下白底，
 * 而那是"这个工具打开就发光刺眼"的那一类问题，不是纯粹的观感。
 */
import { createApp } from 'vue'

import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'
import './styles/tokens.css'
import './styles/workspace.css'

import App from './App.vue'

createApp(App).mount('#app')
