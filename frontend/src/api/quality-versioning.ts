import { request } from './client'

const BASE = '/api/quality-versioning'

async function getJson<T>(path: string): Promise<T> {
  const response = await request(`${BASE}${path}`)
  if (!response.ok) throw new Error(`接口返回 ${response.status}，数据未更新`)
  return (await response.json()) as T
}

async function postJson<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  const response = await request(`${BASE}${path}`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    let detail = `接口返回 ${response.status}`
    try {
      const payload = await response.json()
      if (payload?.detail) detail = payload.detail
    } catch {
      // 忽略非 JSON 错误体
    }
    throw new Error(detail)
  }
  return (await response.json()) as T
}

export type SchemaColumn = {
  name: string
  type: string
  nullable: boolean
  unit: string
  factor: number
}

export type SchemaVersion = {
  version_id: string
  lake_table: string
  parent_version_id: string | null
  status: string
  reason: string
  columns: SchemaColumn[]
  conversions: Record<string, Record<string, unknown>>
}

export type RuleVersion = {
  rule_version_id: string
  lake_table: string
  schema_version_id: string
  status: string
  rules: Record<string, unknown>[]
}

export type EvaluationRow = {
  row_index: number
  row_key: string
  verdict: 'passed' | 'violated'
  hit_rules: string[]
  raw_input: Record<string, unknown>
  converted_input: Record<string, unknown>
  evaluated_values: Record<string, unknown>
}

export type Evaluation = {
  evaluation_id: string
  batch_id: string
  lake_table: string
  schema_version_id: string
  rule_version_id: string
  origin: string
  source_evaluation_id: string | null
  status: string
  total_rows: number
  passed_rows: number
  violated_rows: number
  rows: EvaluationRow[]
}

export type QuarantineItem = {
  quarantine_id: string
  batch_id: string
  lake_table: string
  row_key: string
  rule_id: string
  status: string
  schema_version_id: string
  rule_version_id: string
  initial_evaluation_id: string
  original_input: Record<string, unknown>
  corrected_input: Record<string, unknown> | null
  evaluation_count: number
}

export type QuarantineDetail = QuarantineItem & {
  evaluation_history: {
    evaluation_id: string
    origin: string
    schema_version_id: string
    rule_version_id: string
    verdict: string
    hit_rules: string[]
    raw_input: Record<string, unknown>
    converted_input: Record<string, unknown>
    evaluated_values: Record<string, unknown>
    corrected?: boolean
  }[]
}

export type RecheckResult = {
  quarantine_id: string
  evaluation_id: string
  initial_evaluation_id: string
  schema_version_id: string
  rule_version_id: string
  input_used: string
  verdict: 'passed' | 'violated'
  hit_rules: string[]
  raw_input: Record<string, unknown>
  converted_input: Record<string, unknown>
  evaluated_values: Record<string, unknown>
}

export type RetryResult = RecheckResult & {
  released: boolean
  batch_id: string
  row_key?: string
  ledger_duplicated?: boolean
  ledger_entry?: {
    row_key: string
    batch_id: string
    evaluation_id: string
    schema_version_id: string
  }
  message?: string
  original_input: Record<string, unknown>
  corrected_input: Record<string, unknown>
}

export type LedgerEntry = {
  row_key: string
  lake_table: string
  batch_id: string
  evaluation_id: string
  schema_version_id: string
  payload: Record<string, unknown>
}

export const qualityVersioningApi = {
  listSchemas: (lakeTable?: string) =>
    getJson<{ items: SchemaVersion[] }>(
      `/schemas${lakeTable ? `?lake_table=${encodeURIComponent(lakeTable)}` : ''}`,
    ),
  listRuleVersions: (lakeTable?: string) =>
    getJson<{ items: RuleVersion[] }>(
      `/rule-versions${lakeTable ? `?lake_table=${encodeURIComponent(lakeTable)}` : ''}`,
    ),
  listEvaluations: (lakeTable?: string, batchId?: string) => {
    const params = new URLSearchParams()
    if (lakeTable) params.set('lake_table', lakeTable)
    if (batchId) params.set('batch_id', batchId)
    const query = params.toString()
    return getJson<{ items: Evaluation[] }>(`/evaluations${query ? `?${query}` : ''}`)
  },
  listQuarantine: (lakeTable?: string, batchId?: string, status?: string) => {
    const params = new URLSearchParams()
    if (lakeTable) params.set('lake_table', lakeTable)
    if (batchId) params.set('batch_id', batchId)
    if (status) params.set('status', status)
    const query = params.toString()
    return getJson<{ items: QuarantineItem[] }>(`/quarantine${query ? `?${query}` : ''}`)
  },
  quarantineDetail: (quarantineId: string) =>
    getJson<QuarantineDetail>(`/quarantine/${encodeURIComponent(quarantineId)}`),
  recheck: (quarantineId: string, payload: Record<string, unknown> = {}) =>
    postJson<RecheckResult>(
      `/quarantine/${encodeURIComponent(quarantineId)}/recheck`,
      payload,
    ),
  retry: (quarantineId: string, correctedInput: Record<string, unknown>) =>
    postJson<RetryResult>(`/quarantine/${encodeURIComponent(quarantineId)}/retry`, {
      corrected_input: correctedInput,
    }),
  ledger: (lakeTable?: string) =>
    getJson<{ total_valid_rows: number; items: LedgerEntry[] }>(
      `/ledger${lakeTable ? `?lake_table=${encodeURIComponent(lakeTable)}` : ''}`,
    ),
}
