<template>
  <section class="page" data-module="lifecycle">
    <header class="page-head">
      <div>
        <h2>表生命周期管理</h2>
        <p class="page-desc">维护生命周期策略定义、归档路径与清理方式；每次动作都会写入审计事件账本，列表显示来源事件，历史链可随时回看。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记生命周期策略</button>
        <button class="btn" type="button" @click="loadResults">生命周期结果清单</button>
        <button class="btn" type="button" @click="exportRows">导出表生命周期管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in statCards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>编号检索</span>
        <input v-model="filters.keyword" :placeholder="`按策略编号检索`" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters.status">
          <option value="">全部</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>来源事件</th>
          <th>版本链</th>
          <th>复核状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <button class="link" type="button" @click="openHistory(row)">{{ row['来源事件'] || '—' }}</button>
            <div class="sub-text">{{ row['入口标记'] || '—' }} · 事件序号 {{ row['来源事件序号'] ?? 0 }}</div>
          </td>
          <td class="sub-text">{{ chainText(row) }}</td>
          <td>{{ row['复核状态'] ?? '—' }}</td>
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
            <button v-if="hasDuration" class="link" type="button" @click="openDuration(row)">调整期限</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 4" class="empty-state">暂无表生命周期管理数据，可先登记生命周期策略</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条表生命周期管理记录 · 来源事件与版本链读自同一审计账本</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section v-if="history" class="panel">
      <header class="panel-head">
        <h3 class="panel-title">{{ history['业务对象编号'] }} 的事件版本链</h3>
        <button class="btn ghost" type="button" @click="history = null">收起</button>
      </header>
      <div class="history-grid">
        <div>
          <h4>版本链</h4>
          <ol class="chain-list">
            <li v-for="chain in history['版本链']" :key="chain['版本键']">
              <strong>{{ chain['变更对象'] }}</strong>
              <span v-for="version in chain['版本链']" :key="version['事件编号']" class="version-chip">
                v{{ version['版本序号'] }}/#{{ version['事件序号'] }} · {{ version['变更前值'] }} → {{ version['变更后值'] }}
                <em v-if="version['已归档']">（已归档）</em>
              </span>
            </li>
          </ol>
        </div>
        <div>
          <h4>事件流水</h4>
          <table class="data-table">
            <thead>
              <tr>
                <th>事件编号</th>
                <th>序号</th>
                <th>变更对象</th>
                <th>前值 → 后值</th>
                <th>操作人</th>
                <th>落库时间</th>
                <th>复核状态</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="event in history['事件流水']" :key="event['事件编号']">
                <td class="mono">{{ event['事件编号'] }}</td>
                <td>{{ event['事件序号'] }}</td>
                <td>{{ event['变更对象'] }}</td>
                <td>{{ event['变更前值'] }} → {{ event['变更后值'] }}</td>
                <td>{{ event['操作人'] }}</td>
                <td>{{ event['落库时间'] }}</td>
                <td>{{ event['复核状态'] }}</td>
              </tr>
            </tbody>
          </table>
          <p v-if="history['生命周期结果'] && history['生命周期结果'].length" class="sub-text">
            当前状态：{{ formatFields(history['生命周期结果'][0]['当前字段值']) }}
          </p>
        </div>
      </div>
    </section>

    <section v-if="lifecycleResults.length" class="panel">
      <h3 class="panel-title">生命周期结果清单（当前状态 · 只读投影）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>业务对象编号</th>
            <th>来源模块</th>
            <th>当前字段值</th>
            <th>复核状态</th>
            <th>事件数</th>
            <th>最后落库时间</th>
            <th>结果校验戳</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in lifecycleResults" :key="row['业务对象编号']">
            <td>{{ row['业务对象编号'] }}</td>
            <td>{{ row['来源模块'] }}</td>
            <td>{{ formatFields(row['当前字段值']) }}</td>
            <td>{{ row['复核状态'] }}</td>
            <td>{{ row['事件数'] }}</td>
            <td>{{ row['最后落库时间'] }}</td>
            <td class="mono">{{ row['结果校验戳'] }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="hasDuration && durationTarget" class="panel">
      <h3 class="panel-title">调整保留期限 · {{ durationTarget['规则编号'] }}</h3>
      <div class="filter-bar">
        <label class="filter-item">
          <span>当前期限</span>
          <input :value="durationTarget['保留期限']" disabled />
        </label>
        <label class="filter-item">
          <span>新期限</span>
          <input v-model="newDuration" placeholder="如 30" />
        </label>
        <button class="btn primary" type="button" @click="submitDuration">提交调整</button>
        <button class="btn ghost" type="button" @click="durationTarget = null">取消</button>
      </div>
      <p class="sub-text">调整会追加下一版本事件，不覆盖历史版本；上一版本的前后值仍在版本链中。</p>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/lifecycle'
const columns = ["策略编号", "表名称", "创建日期", "保留天数", "归档路径", "清理方式", "最后操作", "策略状态"]
const actions = ["启用策略", "归档表", "删除表"]
const statuses = ["草稿", "生效中", "已到期", "已归档"]
const hasDuration = false

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({ keyword: '', status: '' })
const history = ref<Record<string, any> | null>(null)
const durationTarget = ref<Row | null>(null)
const newDuration = ref('')
const lifecycleResults = ref<Row[]>([])

const statCards = ref([
  { label: '生命周期策略总数', value: 0 },
  { label: '生效中策略', value: 0 },
  { label: '带来源事件', value: 0 },
])

function resetFilters() {
  filters.value = { keyword: '', status: '' }
  history.value = null
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '生命周期策略登记请通过 POST /api/lifecycle 提交策略编号、表名称、创建日期'
}

function chainText(row: Row) {
  const chains = row['版本链'] ?? []
  if (!chains.length) return '—'
  return chains.map((chain: Row) => `${chain['变更对象']} ${chain['版本数']} 版`).join('；')
}

function formatFields(fields: Record<string, string> | undefined) {
  if (!fields) return '—'
  return Object.entries(fields).map(([key, value]) => `${key}=${value}`).join('；')
}

async function openHistory(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/history`)
    if (!response.ok) throw new Error('历史链读取失败')
    history.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '历史链读取失败'
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
    if (!payload.ok) throw new Error(payload.message || '表生命周期管理动作未生效')
    errorMessage.value = payload.message
    await reload()
    if (history.value) await openHistory(row)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '表生命周期管理操作失败'
  }
}

function openDuration(row: Row) {
  durationTarget.value = row
  newDuration.value = ''
}

async function submitDuration() {
  if (!durationTarget.value) return
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${durationTarget.value.id}/duration`, {
      method: 'POST',
      body: JSON.stringify({ values: { 保留期限: newDuration.value, 操作人: '值班管理员' } }),
    })
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.message || '期限调整未生效')
    errorMessage.value = payload.message
    durationTarget.value = null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '期限调整失败'
  }
}

async function loadResults() {
  errorMessage.value = ''
  try {
    const response = await request('/api/lifecycle/results')
    if (!response.ok) throw new Error('生命周期结果读取失败')
    const payload = await response.json()
    lifecycleResults.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '生命周期结果读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (filters.value.keyword) query.set('keyword', filters.value.keyword)
  if (filters.value.status) query.set('status', filters.value.status)
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}&size=200`)
    if (!response.ok) throw new Error('表生命周期管理列表读取失败')
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    statCards.value = [
      { label: '生命周期策略总数', value: rows.value.length },
      { label: '生效中策略', value: rows.value.filter((row) => row['复核状态'] === '生效中').length },
      { label: '带来源事件', value: rows.value.filter((row) => row['来源事件'] && row['来源事件'] !== '—').length },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '表生命周期管理列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-top: 14px; }
.panel-head { display: flex; justify-content: space-between; align-items: center; }
.panel-title { font-size: 14px; margin: 0 0 10px; }
.sub-text { color: var(--muted); font-size: 12px; margin: 4px 0 0; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
.chain-list { font-size: 13px; padding-left: 18px; }
.chain-list li { margin-bottom: 8px; }
.version-chip { display: inline-block; margin-left: 8px; padding: 2px 8px; border-radius: 10px; background: #f1f5f9; }
.history-grid { display: grid; grid-template-columns: 1fr 1.3fr; gap: 16px; }
.filter-item select { padding: 4px 8px; border: 1px solid var(--border); border-radius: 6px; background: #fff; }
</style>
