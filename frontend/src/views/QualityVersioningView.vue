<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>结构版本化质量校验</h2>
        <p class="page-desc">
          按结构版本校验与转换批次：规则阈值随版本冻结，在途校验期间发布新版本会冲突；
          隔离行保留初次判定版本与原输入，可复检并通过原回炉接口重试。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" :disabled="loading" @click="reload">刷新</button>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">结构版本</span>
        <strong class="stat-value">{{ schemas.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">规则版本</span>
        <strong class="stat-value">{{ ruleVersions.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">评估会话</span>
        <strong class="stat-value">{{ evaluations.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">隔离记录</span>
        <strong class="stat-value">{{ quarantine.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">台账有效行（唯一）</span>
        <strong class="stat-value">{{ ledgerTotal }}</strong>
      </article>
    </div>

    <article class="panel">
      <h3>结构版本（含量纲与跨版本转换）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>版本编号</th><th>湖表</th><th>父版本</th><th>状态</th>
            <th>列（量纲）</th><th>转换映射</th><th>变更原因</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="schema in schemas" :key="`${schema.lake_table}:${schema.version_id}`">
            <td>{{ schema.version_id }}</td>
            <td>{{ schema.lake_table }}</td>
            <td>{{ schema.parent_version_id ?? '—' }}</td>
            <td>{{ schema.status }}</td>
            <td>
              <span v-for="col in schema.columns" :key="col.name" class="col-chip">
                {{ col.name }}<template v-if="col.unit">（{{ col.unit }}）</template>
              </span>
            </td>
            <td><pre class="mini-json">{{ formatJson(schema.conversions) }}</pre></td>
            <td>{{ schema.reason }}</td>
          </tr>
        </tbody>
      </table>
    </article>

    <article class="panel">
      <h3>规则版本（不可变阈值快照）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>规则版本</th><th>湖表</th><th>绑定结构版本</th><th>状态</th><th>规则与阈值</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="rv in ruleVersions" :key="rv.rule_version_id">
            <td>{{ rv.rule_version_id }}</td>
            <td>{{ rv.lake_table }}</td>
            <td>{{ rv.schema_version_id }}</td>
            <td>{{ rv.status }}</td>
            <td><pre class="mini-json">{{ formatJson(rv.rules) }}</pre></td>
          </tr>
        </tbody>
      </table>
    </article>

    <article class="panel">
      <h3>评估会话（按结构版本的逐行复算轨迹）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>评估编号</th><th>批次</th><th>环节</th><th>结构版本</th>
            <th>规则版本</th><th>状态</th><th>通过/违规</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="evaluation in evaluations" :key="evaluation.evaluation_id">
            <td>{{ evaluation.evaluation_id }}</td>
            <td>{{ evaluation.batch_id }}</td>
            <td>{{ originLabel(evaluation.origin) }}</td>
            <td>{{ evaluation.schema_version_id }}</td>
            <td>{{ evaluation.rule_version_id }}</td>
            <td>{{ evaluation.status }}</td>
            <td>{{ evaluation.passed_rows }} / {{ evaluation.violated_rows }}</td>
          </tr>
        </tbody>
      </table>
    </article>

    <article class="panel">
      <h3>隔离区（点击编号打开详情页核对规则版本并复检/回炉）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>隔离编号</th><th>湖表</th><th>来源批次</th><th>行主键</th>
            <th>结构版本</th><th>规则版本</th><th>状态</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in quarantine" :key="item.quarantine_id">
            <td>
              <RouterLink class="link" :to="`/quality-versioning/quarantine/${item.quarantine_id}`">
                {{ item.quarantine_id }}
              </RouterLink>
            </td>
            <td>{{ item.lake_table }}</td>
            <td>{{ item.batch_id }}</td>
            <td>{{ item.row_key }}</td>
            <td>{{ item.schema_version_id }}</td>
            <td>{{ item.rule_version_id }}</td>
            <td>{{ item.status }}</td>
            <td>
              <RouterLink class="link" :to="`/quality-versioning/quarantine/${item.quarantine_id}`">
                隔离详情
              </RouterLink>
            </td>
          </tr>
        </tbody>
      </table>
    </article>

    <article class="panel">
      <h3>入湖台账（幂等：同一行主键在同一湖表只出现一次）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>行主键</th><th>湖表</th><th>来源批次</th><th>落湖评估</th><th>结构版本</th><th>落湖内容</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="entry in ledger" :key="`${entry.lake_table}:${entry.row_key}`">
            <td>{{ entry.row_key }}</td>
            <td>{{ entry.lake_table }}</td>
            <td>{{ entry.batch_id }}</td>
            <td>{{ entry.evaluation_id }}</td>
            <td>{{ entry.schema_version_id }}</td>
            <td><pre class="mini-json">{{ formatJson(entry.payload) }}</pre></td>
          </tr>
        </tbody>
      </table>
    </article>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  qualityVersioningApi,
  type Evaluation,
  type LedgerEntry,
  type QuarantineItem,
  type RuleVersion,
  type SchemaVersion,
} from '@/api/quality-versioning'

const loading = ref(true)
const errorMessage = ref('')
const schemas = ref<SchemaVersion[]>([])
const ruleVersions = ref<RuleVersion[]>([])
const evaluations = ref<Evaluation[]>([])
const quarantine = ref<QuarantineItem[]>([])
const ledger = ref<LedgerEntry[]>([])
const ledgerTotal = ref(0)

function formatJson(value: unknown): string {
  return JSON.stringify(value, null, 2)
}

function originLabel(origin: string): string {
  if (origin === 'initial') return '初次判定'
  if (origin === 'recheck') return '复检'
  if (origin === 'retry') return '回炉'
  return origin
}

async function reload() {
  loading.value = true
  errorMessage.value = ''
  try {
    const [schemaResp, ruleResp, evalResp, quarResp, ledgerResp] = await Promise.all([
      qualityVersioningApi.listSchemas(),
      qualityVersioningApi.listRuleVersions(),
      qualityVersioningApi.listEvaluations(),
      qualityVersioningApi.listQuarantine(),
      qualityVersioningApi.ledger(),
    ])
    schemas.value = schemaResp.items
    ruleVersions.value = ruleResp.items
    evaluations.value = evalResp.items
    quarantine.value = quarResp.items
    ledger.value = ledgerResp.items
    ledgerTotal.value = ledgerResp.total_valid_rows
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版本化质量数据读取失败'
  } finally {
    loading.value = false
  }
}

onMounted(reload)
</script>

<style scoped>
.panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
  margin-bottom: 14px;
}
.panel h3 {
  margin: 0 0 10px;
  font-size: 15px;
}
.col-chip {
  display: inline-block;
  background: #eef2ff;
  border: 1px solid #c7d2fe;
  border-radius: 999px;
  padding: 2px 8px;
  margin: 2px 4px 2px 0;
  font-size: 12px;
}
.mini-json {
  margin: 0;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
