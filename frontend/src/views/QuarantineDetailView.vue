<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>隔离详情 · 版本化复检与回炉</h2>
        <p class="page-desc">
          固定初次判定的结构版本与规则版本，保留原输入；复检与回炉都用同一套完整结构和规则复算。
        </p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn" to="/quarantine_zone">返回隔离区</RouterLink>
      </div>
    </header>

    <div v-if="loading" class="empty-state">正在加载隔离记录…</div>
    <div v-else-if="loadError" class="error-text">{{ loadError }}</div>

    <template v-else-if="detail">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">隔离编号</span>
          <strong class="stat-value">{{ detail.quarantine_id }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">处置状态</span>
          <strong class="stat-value">{{ detail.status }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">结构版本（初次判定）</span>
          <strong class="stat-value">{{ detail.schema_version_id }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">规则版本（初次判定）</span>
          <strong class="stat-value">{{ detail.rule_version_id }}</strong>
        </article>
      </div>

      <div class="detail-grid">
        <article class="detail-card">
          <h3>来源追溯</h3>
          <dl class="kv">
            <dt>所属湖表</dt><dd>{{ detail.lake_table }}</dd>
            <dt>来源批次</dt><dd>{{ detail.batch_id }}</dd>
            <dt>行主键</dt><dd>{{ detail.row_key }}</dd>
            <dt>命中规则</dt><dd>{{ detail.rule_id }}</dd>
            <dt>初次评估</dt><dd>{{ detail.initial_evaluation_id }}</dd>
          </dl>
        </article>

        <article class="detail-card">
          <h3>原输入（采集快照，不可变）</h3>
          <pre class="json-box">{{ formatJson(detail.original_input) }}</pre>
          <h3>更正输入</h3>
          <pre class="json-box">{{ detail.corrected_input ? formatJson(detail.corrected_input) : '尚未更正' }}</pre>
        </article>
      </div>

      <article class="detail-card">
        <h3>复检与回炉操作</h3>
        <div class="action-row">
          <button class="btn" type="button" :disabled="busy" @click="runRecheck({})">
            按原输入复检
          </button>
          <button class="btn primary" type="button" :disabled="busy" @click="openCorrected">
            填写更正输入后复检
          </button>
          <button
            class="btn primary"
            type="button"
            :disabled="busy || detail.status === 'released'"
            @click="openRetry"
          >
            通过原回炉接口重试
          </button>
        </div>
        <p v-if="detail.status === 'released'" class="page-desc">
          该记录已回炉落湖，重复重投会被拒绝。
        </p>
        <p v-if="actionMessage" class="page-desc">{{ actionMessage }}</p>
        <p v-if="actionError" class="error-text">{{ actionError }}</p>

        <div v-if="lastRecheck" class="result-box">
          <h4>最近一次复检结论</h4>
          <dl class="kv">
            <dt>评估编号</dt><dd>{{ lastRecheck.evaluation_id }}</dd>
            <dt>使用输入</dt>
            <dd>{{ lastRecheck.input_used === 'corrected_input' ? '更正输入' : '原输入' }}</dd>
            <dt>结构版本</dt><dd>{{ lastRecheck.schema_version_id }}</dd>
            <dt>规则版本</dt><dd>{{ lastRecheck.rule_version_id }}</dd>
            <dt>判定</dt>
            <dd :class="lastRecheck.verdict === 'passed' ? 'verdict-pass' : 'verdict-fail'">
              {{ lastRecheck.verdict === 'passed' ? '通过' : '违规' }}
            </dd>
            <dt>命中规则</dt><dd>{{ lastRecheck.hit_rules.join('、') || '—' }}</dd>
            <dt>转换后输入</dt><dd><pre class="json-box inline">{{ formatJson(lastRecheck.converted_input) }}</pre></dd>
            <dt>实际读取值</dt><dd><pre class="json-box inline">{{ formatJson(lastRecheck.evaluated_values) }}</pre></dd>
          </dl>
        </div>

        <div v-if="lastRetry" class="result-box">
          <h4>回炉回执</h4>
          <dl class="kv">
            <dt>原批次</dt><dd>{{ lastRetry.batch_id }}</dd>
            <dt>原输入参与的评估</dt><dd>{{ lastRetry.initial_evaluation_id }}</dd>
            <dt>更正输入参与的评估</dt><dd>{{ lastRetry.evaluation_id }}</dd>
            <dt>是否释放隔离</dt><dd>{{ lastRetry.released ? '是，已幂等落湖' : '否，仍违规' }}</dd>
            <dt v-if="lastRetry.row_key">行主键</dt><dd v-if="lastRetry.row_key">{{ lastRetry.row_key }}</dd>
          </dl>
        </div>
      </article>

      <article class="detail-card">
        <h3>计算轨迹（initial / recheck / retry）</h3>
        <table class="data-table">
          <thead>
            <tr>
              <th>环节</th>
              <th>评估编号</th>
              <th>结构版本</th>
              <th>规则版本</th>
              <th>判定</th>
              <th>输入来源</th>
              <th>转换后输入</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in detail.evaluation_history" :key="item.evaluation_id">
              <td>{{ originLabel(item.origin) }}{{ item.corrected ? '（更正）' : '' }}</td>
              <td>{{ item.evaluation_id }}</td>
              <td>{{ item.schema_version_id }}</td>
              <td>{{ item.rule_version_id }}</td>
              <td :class="item.verdict === 'passed' ? 'verdict-pass' : 'verdict-fail'">
                {{ item.verdict === 'passed' ? '通过' : '违规' }}
              </td>
              <td><pre class="json-box inline">{{ formatJson(item.raw_input) }}</pre></td>
              <td><pre class="json-box inline">{{ formatJson(item.converted_input) }}</pre></td>
            </tr>
          </tbody>
        </table>
      </article>
    </template>

    <div v-if="showCorrectedModal" class="modal-backdrop" @click.self="showCorrectedModal = false">
      <form class="modal" @submit.prevent="submitCorrectedRecheck">
        <h3>更正输入（JSON）</h3>
        <textarea v-model="correctedText" class="json-input" rows="8"></textarea>
        <p class="page-desc">例如：{"order_id": "ORD-130", "amount": 80}</p>
        <p v-if="modalError" class="error-text">{{ modalError }}</p>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCorrectedModal = false">取消</button>
          <button class="btn primary" type="submit" :disabled="busy">提交复检</button>
        </div>
      </form>
    </div>

    <div v-if="showRetryModal" class="modal-backdrop" @click.self="showRetryModal = false">
      <form class="modal" @submit.prevent="submitRetry">
        <h3>回炉更正输入（JSON）</h3>
        <textarea v-model="retryText" class="json-input" rows="8"></textarea>
        <p class="page-desc">通过原回炉接口重试；通过后幂等落湖，同一行只落一次。</p>
        <p v-if="modalError" class="error-text">{{ modalError }}</p>
        <div class="page-actions">
          <button class="btn" type="button" @click="showRetryModal = false">取消</button>
          <button class="btn primary" type="submit" :disabled="busy">确认回炉重投</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import {
  qualityVersioningApi,
  type QuarantineDetail,
  type RecheckResult,
  type RetryResult,
} from '@/api/quality-versioning'

const route = useRoute()
const quarantineId = String(route.params.quarantineId ?? '')

const loading = ref(true)
const loadError = ref('')
const detail = ref<QuarantineDetail | null>(null)
const busy = ref(false)
const actionMessage = ref('')
const actionError = ref('')
const lastRecheck = ref<RecheckResult | null>(null)
const lastRetry = ref<RetryResult | null>(null)

const showCorrectedModal = ref(false)
const showRetryModal = ref(false)
const correctedText = ref('')
const retryText = ref('')
const modalError = ref('')

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
  loadError.value = ''
  try {
    detail.value = await qualityVersioningApi.quarantineDetail(quarantineId)
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '隔离详情读取失败'
  } finally {
    loading.value = false
  }
}

async function runRecheck(payload: Record<string, unknown>) {
  busy.value = true
  actionError.value = ''
  actionMessage.value = ''
  try {
    lastRecheck.value = await qualityVersioningApi.recheck(quarantineId, payload)
    actionMessage.value = `复检完成：${lastRecheck.value.verdict === 'passed' ? '通过' : '违规'}`
    await reload()
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : '复检失败'
  } finally {
    busy.value = false
  }
}

function openCorrected() {
  modalError.value = ''
  correctedText.value = detail.value
    ? JSON.stringify(detail.value.original_input, null, 2)
    : '{}'
  showCorrectedModal.value = true
}

function parseJson(text: string): Record<string, unknown> {
  const parsed = JSON.parse(text)
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new Error('更正输入必须是 JSON 对象')
  }
  return parsed as Record<string, unknown>
}

async function submitCorrectedRecheck() {
  modalError.value = ''
  try {
    const corrected = parseJson(correctedText.value)
    showCorrectedModal.value = false
    await runRecheck({ corrected_input: corrected })
  } catch (error) {
    modalError.value = error instanceof Error ? error.message : 'JSON 格式不正确'
  }
}

function openRetry() {
  modalError.value = ''
  retryText.value = detail.value
    ? JSON.stringify(detail.value.original_input, null, 2)
    : '{}'
  showRetryModal.value = true
}

async function submitRetry() {
  modalError.value = ''
  try {
    const corrected = parseJson(retryText.value)
    showRetryModal.value = false
    busy.value = true
    actionError.value = ''
    lastRetry.value = await qualityVersioningApi.retry(quarantineId, corrected)
    actionMessage.value = lastRetry.value.message ?? '回炉处理完成'
    await reload()
  } catch (error) {
    modalError.value = error instanceof Error ? error.message : '回炉失败'
  } finally {
    busy.value = false
  }
}

onMounted(reload)
</script>

<style scoped>
.detail-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-bottom: 12px;
}
.detail-card {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
  margin-bottom: 12px;
}
.detail-card h3 {
  margin: 0 0 10px;
  font-size: 15px;
}
.kv {
  display: grid;
  grid-template-columns: 160px 1fr;
  gap: 6px 12px;
  margin: 0;
}
.kv dt {
  color: var(--muted);
  font-size: 12px;
}
.kv dd {
  margin: 0;
  font-size: 13px;
}
.json-box {
  background: #f8fafc;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px;
  font-size: 12px;
  margin: 0 0 10px;
  white-space: pre-wrap;
  word-break: break-all;
}
.json-box.inline {
  display: inline-block;
  margin: 0;
  padding: 4px 6px;
}
.action-row {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.result-box {
  border-top: 1px solid var(--border);
  margin-top: 12px;
  padding-top: 12px;
}
.verdict-pass {
  color: #15803d;
  font-weight: 600;
}
.verdict-fail {
  color: #b42318;
  font-weight: 600;
}
.json-input {
  width: 100%;
  font-family: monospace;
  font-size: 13px;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px;
  resize: vertical;
}
</style>
