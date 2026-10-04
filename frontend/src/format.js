/**
 * 数字出口的**唯一**格式化（GPT 评审清单：千分位）。
 *
 * 界面上每个"给人看的数"都过这里 —— 10290 与 10,290 的差别在数字超过四位
 * 之后不是审美问题：三个口径（库中 / 符合筛选 / 已加载）并排出现时，没有千
 * 分位的长数字串会让人把两个数读成一个。
 *
 * `Intl.NumberFormat` 构造有成本，**模块级缓存一次**；locale 钉死 `en-US`
 * （千分位逗号）—— 跟着系统 locale 走会在逗号/点号之间漂，本地工具不需要
 * 那种"贴心"。
 */

const INT_FMT = new Intl.NumberFormat('en-US')

/** 整数千分位。输入可能来自 JSON（字符串），先 Number 化再格式化；
 *  不是有限数就原样返回 `—`，绝不把 `undefined` 渲染成 "NaN"。 */
export function fmtInt(value) {
  const n = Number(value)
  return Number.isFinite(n) ? INT_FMT.format(Math.round(n)) : '—'
}

/** 百分比（0–1 的比例 → "73%"），一位小数以内直接抹掉（73.0% → 73%）。 */
export function fmtPct(ratio) {
  const n = Number(ratio)
  if (!Number.isFinite(n)) return '—'
  const pct = Math.max(0, Math.min(100, n * 100))
  const rounded = Math.round(pct * 10) / 10
  return `${rounded % 1 === 0 ? rounded : rounded.toFixed(1)}%`
}
