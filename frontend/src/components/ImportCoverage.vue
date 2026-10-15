<script setup>
defineProps({ coverage: { type: Object, required: true } })
const labels = { mapped_complete: '完整映射', mapped_partial: '部分映射',
  unrecognized: '未识别', ignored: '人工忽略', record_error: '记录错误' }
</script>

<template>
  <section class="coverage" aria-label="导入覆盖报告">
    <div class="coverage-head"><strong>数据覆盖 · {{ coverage.format || '格式未知' }}</strong>
      <span>{{ coverage.records_total }} 条输入记录</span></div>
    <p v-if="!coverage.complete" class="warn">处理未完成，以下计数仅为已处理部分，不能视为全文件覆盖。</p>
    <p v-if="coverage.error" class="bad">{{ coverage.error }}</p>
    <div class="coverage-counts">
      <div v-for="(label, key) in labels" :key="key"><b>{{ coverage[key] || 0 }}</b><span>{{ label }}</span></div>
    </div>
    <p v-if="coverage.fields_total !== undefined" class="dim">字段出现次数：{{ coverage.fields_total }} ·
      已映射 {{ coverage.fields_mapped }} · 人工忽略 {{ coverage.fields_ignored }} · 待处理 {{ coverage.fields_unresolved }}</p>
    <p v-if="coverage.field_catalog_truncated" class="warn">字段目录超过 2000 项，仅展示前 2000 项；请拆分后继续映射。</p>
    <p class="dim">完整映射仅表示字段均有映射或人工忽略决策，不代表漏洞已确认。输入记录数与生成实体、观测数不同。</p>
    <details><summary>查看来源、未映射字段与错误明细</summary>
      <p v-if="coverage.details_truncated" class="warn">仅显示前 200 条记录；完整原文保留在导入归档中。</p>
      <article v-for="(row, index) in coverage.details || []" :key="index" class="coverage-row">
        <strong>{{ row.record_path }} · 行 {{ row.line }} · {{ labels[row.state] }}</strong>
        <p v-if="row.error" class="bad">{{ row.error }}</p>
        <div v-for="(field, i) in row.fields || []" :key="i" class="field-row">
          <code>{{ field.field }}</code><span>{{ field.value }}</span>
          <span :class="{ warn: !field.mapped }">{{ field.kind }} · {{ field.reason }}</span>
        </div>
        <p v-if="row.fields_truncated" class="warn">此记录字段明细只显示前 50 项，原文仍保留。</p>
      </article>
    </details>
  </section>
</template>

<style scoped>
.coverage { margin: 12px 0; padding: 14px; border: 1px solid var(--line); border-radius: 8px; font-size: 12px; container-type: inline-size; }
.coverage-head { display: flex; justify-content: space-between; gap: 12px; }
.coverage-counts { display: grid; grid-template-columns: repeat(auto-fit, minmax(80px, 1fr)); gap: 8px; margin: 12px 0; }
.coverage-counts div { padding: 8px; background: var(--bg); display: flex; flex-direction: column; gap: 5px; }
.coverage-counts b { font-size: 20px; }
.coverage-row { padding: 10px 0; border-bottom: 1px solid var(--line); }
.field-row { display: grid; grid-template-columns: 1fr 1fr 1.5fr; gap: 8px; padding: 4px 0; overflow-wrap: anywhere; }
details { max-height: 300px; overflow: auto; }
summary { cursor: pointer; }
@container (max-width: 350px) { .field-row { grid-template-columns: 1fr; gap: 3px; padding: 8px 0; } }
</style>
