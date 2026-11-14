<template>
  <section class="page" data-module="audit">
    <header class="page-head">
      <div>
        <h2>变更审计追踪管理</h2>
        <p class="page-desc">
          多个业务模块把变更写入同一本审计账本：按对象、变更类型和版本键合并重复事件，
          乱序到达按事件序号归位，列表、版本链与生命周期结果读的是同一条事件链。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记变更记录</button>
        <button class="btn" type="button" @click="exportRows">导出事件流水</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">事件总数</span>
        <strong class="stat-value">{{ ledgerStats['事件总数'] ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">版本链数</span>
        <strong class="stat-value">{{ ledgerStats['版本链数'] ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">重放忽略次数</span>
        <strong class="stat-value">{{ ledgerStats['重放忽略次数'] ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">归并合并条数</span>
        <strong class="stat-value">{{ ledgerStats['合并归并条数'] ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">归档事件</span>
        <strong class="stat-value">{{ ledgerStats['归档事件数'] ?? 0 }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>检索</span>
        <input v-model="filters.keyword" placeholder="对象编号 / 事件编号 / 操作人" />
      </label>
      <label class="filter-item">
        <span>变更对象</span>
        <input v-model="filters.field" placeholder="如 参数阈值、负责人" />
      </label>
      <label class="filter-item">
        <span>变更类型</span>
        <input v-model="filters.change_type" placeholder="如 参数调整" />
      </label>
      <label class="filter-item">
        <span>来源模块</span>
        <select v-model="filters.source_module">
          <option value="">全部</option>
          <option v-for="item in sourceModules" :key="item.value" :value="item.value">{{ item.label }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>复核状态</span>
        <select v-model="filters.status">
          <option value="">全部</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>归档期</span>
        <input v-model="filters.archive_period" placeholder="YYYY-MM" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <section class="panel">
      <h3 class="panel-title">事件流水（按对象与事件序号稳定排序）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>对象编号</th>
            <th>事件序号</th>
            <th>版本序号</th>
            <th>父序号</th>
            <th>变更对象</th>
            <th>变更类型</th>
            <th>变更前值 → 变更后值</th>
            <th>操作人</th>
            <th>操作时间</th>
            <th>落库时间</th>
            <th>复核状态</th>
            <th>来源模块</th>
            <th>入口标记</th>
            <th>稳定排序键</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="String(row.id)" :class="{ 'row-archived': row['已归档'], 'row-broken': row['断点标记'] }">
            <td>
              <button class="link" type="button" @click="openHistory(String(row['记录编号']))">
                {{ row['记录编号'] }}
              </button>
            </td>
            <td>{{ row['事件序号'] }}</td>
            <td>{{ row['版本序号'] }}</td>
            <td>{{ row['父事件序号'] }}</td>
            <td>{{ row['变更对象'] }}</td>
            <td>{{ row['变更类型'] }}</td>
            <td>{{ row['变更前值'] }} → {{ row['变更后值'] }}</td>
            <td>{{ row['操作人'] }}</td>
            <td>{{ row['操作时间'] }}</td>
            <td>
              {{ row['落库时间'] }}
              <span v-if="row['已归档']" class="badge">归档 {{ row['归档期'] }}</span>
            </td>
            <td>{{ row['复核状态'] }}</td>
            <td>{{ row['来源模块'] }}</td>
            <td>{{ row['入口标记'] }}</td>
            <td class="mono">{{ row['稳定排序键'] }}</td>
            <td class="row-actions">
              <button
                v-for="action in actions"
                :key="action"
                class="link"
                type="button"
                @click="runAction(action, row)"
              >
                {{ action }}
              </button>
            </td>
          </tr>
          <tr v-if="!rows.length">
            <td :colspan="15" class="empty-state">暂无事件数据，可先登记变更记录</td>
          </tr>
        </tbody>
      </table>
      <footer class="page-foot">
        <span>共 {{ total }} 条事件 · 按对象编号、事件序号、本地序列号稳定排序</span>
        <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      </footer>
    </section>

    <section class="panel">
      <h3 class="panel-title">版本链（按事件序号还原，归档事件同样参与）</h3>
      <div class="chain-grid">
        <article v-for="chain in chains" :key="chain['版本键']" class="chain-card">
          <header class="chain-head">
            <strong>{{ chain['业务对象编号'] }} · {{ chain['变更对象'] }}</strong>
            <span class="badge">{{ chain['复核状态'] }}</span>
          </header>
          <p class="chain-meta">
            版本键 {{ chain['版本键'] }} · {{ chain['来源模块'] }} · 链指纹 {{ chain['链指纹'] }}
          </p>
          <ol class="chain-list">
            <li v-for="version in chain['版本链']" :key="version['事件编号']">
              <span class="mono">v{{ version['版本序号'] }}/事件 {{ version['事件序号'] }}</span>
              <span>{{ version['变更前值'] }} → {{ version['变更后值'] }}</span>
              <span class="muted">{{ version['操作时间'] }}</span>
              <span class="badge" :class="version['已归档'] ? 'badge-archived' : ''">
                {{ version['已归档'] ? '已归档' : version['复核状态'] }}
              </span>
            </li>
          </ol>
        </article>
        <p v-if="!chains.length" class="empty-state">暂无版本链</p>
      </div>
    </section>

    <section class="panel">
      <h3 class="panel-title">生命周期结果清单（当前状态）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>业务对象编号</th>
            <th>来源模块</th>
            <th>当前字段值</th>
            <th>复核状态</th>
            <th>复核人</th>
            <th>最后操作时间</th>
            <th>最后落库时间</th>
            <th>事件数</th>
            <th>归档期</th>
            <th>当前来源事件</th>
            <th>结果校验戳</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in results" :key="row['业务对象编号']">
            <td>{{ row['业务对象编号'] }}</td>
            <td>{{ row['来源模块'] }}</td>
            <td>{{ formatFields(row['当前字段值']) }}</td>
            <td>{{ row['复核状态'] }}</td>
            <td>{{ row['复核人'] }}</td>
            <td>{{ row['最后操作时间'] }}</td>
            <td>{{ row['最后落库时间'] }}</td>
            <td>{{ row['事件数'] }}</td>
            <td>{{ (row['归档期'] as string[]).join('、') || '—' }}</td>
            <td>{{ row['当前来源事件'] }}</td>
            <td class="mono">{{ row['结果校验戳'] }}</td>
          </tr>
          <tr v-if="!results.length">
            <td colspan="11" class="empty-state">暂无生命周期结果</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <h3 class="panel-title">归档（按月归档，跨月后仍按原序列读取）</h3>
      <div class="replay-bar">
        <span>归档期</span>
        <input v-model="archivePeriod" class="period-input" placeholder="YYYY-MM，如 2026-08" />
        <button class="btn" type="button" :disabled="!archivePeriod" @click="archivePeriodEvents">归档该月事件</button>
        <span v-if="archiveMessage" class="muted">{{ archiveMessage }}</span>
      </div>
      <p class="muted">归档只改变事件的存储分区，不改变版本链顺序；刷新前后列表、当前状态和历史链保持一致。</p>
    </section>

    <section v-if="history" class="panel">
      <header class="panel-head">
        <h3 class="panel-title">对象 {{ history['业务对象编号'] }} 的完整历史链</h3>
        <button class="btn ghost" type="button" @click="history = null">收起</button>
      </header>
      <div class="history-grid">
        <div>
          <h4>事件流水（落库顺序不影响展示顺序）</h4>
          <table class="data-table">
            <thead>
              <tr>
                <th>序号</th>
                <th>本地序列号</th>
                <th>变更对象</th>
                <th>变更前值 → 变更后值</th>
                <th>操作人</th>
                <th>操作时间</th>
                <th>落库时间</th>
                <th>复核状态</th>
                <th>断点诊断</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="event in history['事件流水']" :key="event['事件编号']">
                <td>{{ event['事件序号'] }}</td>
                <td>{{ event['本地序列号'] }}</td>
                <td>{{ event['变更对象'] }}</td>
                <td>{{ event['变更前值'] }} → {{ event['变更后值'] }}</td>
                <td>{{ event['操作人'] }}</td>
                <td>{{ event['操作时间'] }}</td>
                <td>{{ event['落库时间'] }}</td>
                <td>{{ event['复核状态'] }}</td>
                <td>{{ event['断点标记'] || '接续正常' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div>
          <h4>调用链环节</h4>
          <ol class="step-list">
            <li v-for="step in flowSteps" :key="step['序号']">
              <strong>{{ step['环节'] }}</strong>：{{ step['说明'] }}（{{ step['输入'] }}）
            </li>
          </ol>
          <h4>对象事件链（父序号与稳定排序键）</h4>
          <table class="data-table">
            <thead>
              <tr>
                <th>序号</th>
                <th>本地序列号</th>
                <th>变更对象</th>
                <th>父序号</th>
                <th>校验码</th>
                <th>链指纹</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="link in history['对象事件链']" :key="`${link['事件编号']}-link`">
                <td>{{ link['事件序号'] }}</td>
                <td>{{ link['本地序列号'] }}</td>
                <td>{{ link['变更对象'] }}</td>
                <td>{{ link['父事件序号'] }}</td>
                <td class="mono">{{ link['校验码'] }}</td>
                <td class="mono">{{ link['链指纹'] }}</td>
              </tr>
            </tbody>
          </table>
          <h4>派生只读摘要</h4>
          <table class="data-table">
            <thead>
              <tr>
                <th>版本</th>
                <th>摘要</th>
                <th>只读校验戳</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="event in history['事件流水']" :key="`${event['事件编号']}-summary`">
                <td>v{{ event['版本序号'] }}</td>
                <td>{{ event['摘要文本'] }}</td>
                <td class="mono">{{ event['摘要']['只读校验戳'] }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
      <div class="replay-bar">
        <span>重放同一事件验证幂等：</span>
        <select v-model.number="replayTarget">
          <option :value="0">选择事件</option>
          <option v-for="event in history['事件流水']" :key="event['事件编号']" :value="event['id']">
            {{ event['事件编号'] }} · {{ event['变更对象'] }} v{{ event['版本序号'] }}
          </option>
        </select>
        <button class="btn" type="button" :disabled="!replayTarget" @click="replayEvent(false)">原样重放</button>
        <button class="btn" type="button" :disabled="!replayTarget" @click="replayEvent(true)">重放并改结论</button>
        <span v-if="replayMessage" class="muted">{{ replayMessage }}</span>
      </div>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

type HistoryPayload = {
  业务对象编号: string
  事件流水: Row[]
  版本链: Row[]
  对象事件链: Row[]
  生命周期结果: Row[]
}

const ENDPOINT = '/api/audit'
const actions = ['标记复核', '归档记录']
const statuses = ['已记录', '已复核', '已归档']
const sourceModules = [
  { label: '变更审计追踪', value: 'audit' },
  { label: '数据资产盘点', value: 'catalog' },
  { label: '数据保留策略', value: 'retention' },
  { label: '表生命周期管理', value: 'lifecycle' },
]

const rows = ref<Row[]>([])
const chains = ref<Row[]>([])
const results = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const ledgerStats = ref<Record<string, any>>({})
const history = ref<HistoryPayload | null>(null)
const flowSteps = ref<Row[]>([])
const replayTarget = ref(0)
const replayMessage = ref('')
const archivePeriod = ref('')
const archiveMessage = ref('')
const filters = ref<Record<string, string>>({
  keyword: '',
  field: '',
  change_type: '',
  source_module: '',
  status: '',
  archive_period: '',
})

function formatFields(fields: Record<string, string> | undefined) {
  if (!fields) return '—'
  return Object.entries(fields)
    .map(([key, value]) => `${key}=${value}`)
    .join('；')
}

function resetFilters() {
  filters.value = {
    keyword: '',
    field: '',
    change_type: '',
    source_module: '',
    status: '',
    archive_period: '',
  }
  history.value = null
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '变更记录登记请通过业务模块动作入口，或直接调用 POST /api/audit/events'
}

async function openHistory(objectNo: string) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${objectNo}/history`)
    if (!response.ok) throw new Error('历史链读取失败')
    history.value = await response.json()
    replayTarget.value = 0
    replayMessage.value = ''
    const flow = await request(`${ENDPOINT}/flow?object_no=${encodeURIComponent(objectNo)}`)
    flowSteps.value = flow.ok ? (await flow.json())['环节'] ?? [] : []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '历史链读取失败'
  }
}

async function replayEvent(changeConclusion: boolean) {
  if (!replayTarget.value) return
  replayMessage.value = ''
  try {
    const body = changeConclusion
      ? { values: { event_id: replayTarget.value, changes: { 变更后值: '被篡改的值', 复核状态: '已归档' } } }
      : { values: { event_id: replayTarget.value } }
    const response = await request(`${ENDPOINT}/replay`, {
      method: 'POST',
      body: JSON.stringify(body),
    })
    const payload = await response.json()
    replayMessage.value = payload.message
    if (history.value) await openHistory(history.value['业务对象编号'])
    await reload()
  } catch (error) {
    replayMessage.value = error instanceof Error ? error.message : '重放失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '变更审计追踪动作未生效')
    }
    errorMessage.value = payload.message
    await reload()
    if (history.value) await openHistory(history.value['业务对象编号'])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '变更审计追踪操作失败'
  }
}

async function archivePeriodEvents() {
  archiveMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/archive`, {
      method: 'POST',
      body: JSON.stringify({ values: { archive_period: archivePeriod.value } }),
    })
    const payload = await response.json()
    archiveMessage.value = payload.message
    await reload()
    if (history.value) await openHistory(history.value['业务对象编号'])
  } catch (error) {
    archiveMessage.value = error instanceof Error ? error.message : '归档失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  Object.entries(filters.value).forEach(([key, value]) => {
    if (value) query.set(key, value)
  })
  try {
    const [listResponse, resultResponse, statsResponse] = await Promise.all([
      request(`${ENDPOINT}?${query.toString()}&size=200`),
      request(`${ENDPOINT}/results`),
      request(`${ENDPOINT}/stats`),
    ])
    if (!listResponse.ok) throw new Error('事件流水读取失败')
    const payload = await listResponse.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    // 一次拉全量版本链，再按当前筛选结果里的对象裁剪，避免 N+1 请求
    const objects = new Set(rows.value.map((row) => String(row['记录编号'])))
    const chainResponse = await request(`${ENDPOINT}/chains`)
    const chainPayload = chainResponse.ok ? await chainResponse.json() : { items: [] }
    chains.value = (chainPayload.items ?? []).filter((chain: Row) => objects.has(String(chain['业务对象编号'])))
    const resultPayload = resultResponse.ok ? await resultResponse.json() : { items: [] }
    results.value = resultPayload.items ?? []
    ledgerStats.value = statsResponse.ok ? await statsResponse.json() : {}
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '变更审计追踪列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-top: 14px; }
.panel-head { display: flex; justify-content: space-between; align-items: center; }
.panel-title { font-size: 14px; margin: 0 0 10px; }
.chain-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 10px; }
.chain-card { border: 1px solid var(--border); border-radius: 6px; padding: 10px; }
.chain-head { display: flex; justify-content: space-between; align-items: center; }
.chain-meta { font-size: 12px; color: var(--muted); margin: 6px 0; word-break: break-all; }
.chain-list { margin: 0; padding-left: 18px; font-size: 13px; }
.chain-list li { margin-bottom: 6px; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.badge { background: #eef2ff; color: #3730a3; border-radius: 10px; padding: 1px 8px; font-size: 12px; }
.badge-archived { background: #f1f5f9; color: #475569; }
.muted { color: var(--muted); font-size: 12px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; word-break: break-all; }
.row-archived { background: #fafafa; }
.row-broken { background: #fff7ed; }
.history-grid { display: grid; grid-template-columns: 1.4fr 1fr; gap: 16px; }
.step-list { font-size: 13px; padding-left: 18px; }
.replay-bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 10px; font-size: 13px; }
.filter-item select { padding: 4px 8px; border: 1px solid var(--border); border-radius: 6px; background: #fff; }
.period-input { padding: 5px 8px; border: 1px solid var(--border); border-radius: 6px; }
</style>
