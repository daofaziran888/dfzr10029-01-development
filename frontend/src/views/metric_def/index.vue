<template>
  <section class="page" data-module="metric_def">
    <header class="page-head">
      <div>
        <h2>指标定义与口径发布</h2>
        <p class="page-desc">
          提交指标公式、发起版本校验、逐项核对差异明细；发布版本包内同时固化公式快照与基准快照，
          图表只读取一份完整的已发布版本包。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="refresh-all" type="button" @click="reloadAll">刷新数据</button>
      </div>
    </header>

    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span style="font-size:12px;color:var(--muted)">{{ m.hint }}</span>
      </article>
    </div>

    <section class="panel">
      <div class="panel-head">
        <h3>发起版本校验</h3>
        <span class="panel-hint">校验结果落在同一个发布边界内，差异明细可逐项核对</span>
      </div>
      <form class="analysis-form" @submit.prevent="runAnalysis">
        <label>
          <span>指标</span>
          <select v-model="params['指标编号']" data-testid="param-metric">
            <option v-for="metric in metrics" :key="metric['指标编号']" :value="metric['指标编号']">
              {{ metric['指标编号'] }} · {{ metric['指标名称'] }}
            </option>
          </select>
        </label>
        <label>
          <span>统计粒度</span>
          <select v-model="params['统计粒度']" data-testid="param-granularity">
            <option v-for="option in granularities" :key="option" :value="option">{{ option }}</option>
          </select>
        </label>
        <label>
          <span>时间窗口</span>
          <input v-model="params['时间窗口']" data-testid="param-window" placeholder="如 2026-09-01~2026-09-07" />
        </label>
        <label>
          <span>重算方式</span>
          <select v-model="params['重算方式']" data-testid="param-mode">
            <option v-for="option in modes" :key="option" :value="option">{{ option }}</option>
          </select>
        </label>
        <button class="btn primary" type="submit" data-testid="run-analysis" :disabled="validating">
          {{ validating ? '校验中…' : '运行版本校验' }}
        </button>
      </form>
      <p v-if="validationMessage" class="notice" data-testid="validation-message">{{ validationMessage }}</p>
    </section>

    <div v-if="diffRows.length" class="analysis-chart" data-testid="diff-chart">
      <div
        v-for="(bar, i) in chartData"
        :key="i"
        class="analysis-bar"
        :class="{ 'bar-exceeded': bar.exceeded }"
        :style="{ height: barHeight(bar.value) + 'px' }"
        :title="bar.label"
      ><span>{{ bar.label }}</span></div>
    </div>

    <section v-if="activeDiff" class="panel" data-testid="diff-panel">
      <div class="panel-head">
        <h3>差异明细 · {{ activeDiff.包编号 }}（{{ activeDiff.口径版本 }} vs {{ activeDiff.基准版本 }}）</h3>
        <span class="panel-hint">
          共 {{ activeDiff.明细条数 }} 条，超阈值 {{ activeDiff.超阈值条数 }} 条 · 发布边界 {{ activeDiff.发布边界 }}
        </span>
      </div>
      <p class="notice" data-testid="unified-explanation">{{ activeDiff.统一口径说明 }}</p>
      <div class="field-checks">
        <span
          v-for="field in activeDiff.字段核对"
          :key="field.核对项"
          class="field-check"
          :class="field.是否一致 ? 'same' : 'changed'"
        >{{ field.核对项 }}：{{ field.是否一致 ? '一致' : `${field.基准值 ?? '空'} → ${field.发布值 ?? '空'}` }}</span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>范围键</th><th>起始日期</th><th>结束日期</th><th>基准值</th><th>发布值</th>
            <th>差异</th><th>差异率</th><th>是否超阈值</th><th>统一口径说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in diffRows" :key="row.范围键" :data-testid="`diff-${row.范围键}`" :data-exceeded="row.是否超阈值">
            <td>{{ row.范围键 }}</td>
            <td>{{ row.起始日期 ?? '—' }}</td>
            <td>{{ row.结束日期 ?? '—' }}</td>
            <td>{{ row.基准值 }}</td>
            <td>{{ row.发布值 }}</td>
            <td>{{ row.差异 }}</td>
            <td>{{ (row.差异率 * 100).toFixed(2) }}%</td>
            <td>{{ row.是否超阈值 ? '是' : '否' }}</td>
            <td>{{ row.统一口径说明 }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h3>指标列表</h3>
        <span class="panel-hint">提交公式后，草稿与已发布记录会同时存在</span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>指标编号</th><th>指标名称</th><th>计算公式</th><th>统计粒度</th>
            <th>时间窗口</th><th>精度位数</th><th>口径版本</th><th>状态</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="metric in metrics" :key="String(metric.id)" :data-testid="`metric-${metric.id}`" :data-status="metric.status">
            <td>{{ metric['指标编号'] }}</td>
            <td>{{ metric['指标名称'] }}</td>
            <td class="mono">{{ metric['计算公式'] }}</td>
            <td>{{ metric['统计粒度'] }}</td>
            <td>{{ metric['时间窗口'] }}</td>
            <td>{{ metric['精度位数'] }}</td>
            <td>{{ metric['口径版本'] }}</td>
            <td>{{ metric.status }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`edit-formula-${metric.id}`" @click="openFormula(metric)">
                提交公式
              </button>
              <button class="link" type="button" :data-testid="`validate-${metric.id}`" @click="validateMetric(metric)">
                版本校验
              </button>
            </td>
          </tr>
          <tr v-if="!metrics.length">
            <td colspan="9" class="empty-state">暂无指标，请先到指标定义数据中登记</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h3>指标发布版本包</h3>
        <span class="panel-hint">同一指标同一时间只会有一个有效发布版本</span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>包编号</th><th>指标编号</th><th>口径版本</th><th>基准版本</th><th>统计粒度</th>
            <th>时间窗口</th><th>重算方式</th><th>差异条数</th><th>超阈值</th><th>状态</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="pkg in releases" :key="String(pkg.id)" :data-testid="`release-${pkg.id}`" :data-status="pkg.status">
            <td>{{ pkg['包编号'] }}</td>
            <td>{{ pkg['指标编号'] }}</td>
            <td>{{ pkg['口径版本'] }}</td>
            <td>{{ pkg['基准版本'] }}</td>
            <td>{{ pkg['统计粒度'] }}</td>
            <td>{{ pkg['时间窗口'] }}</td>
            <td>{{ pkg['重算方式'] }}</td>
            <td>{{ pkg['差异条数'] }}</td>
            <td>{{ pkg['超阈值条数'] }}</td>
            <td>{{ pkg.status }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`view-diff-${pkg.id}`" @click="viewDiff(pkg.id)">差异明细</button>
              <button class="link" type="button" :data-testid="`view-sources-${pkg.id}`" @click="viewSources(pkg.id)">来源记录</button>
              <button
                v-if="pkg.status === '待发布'"
                class="link"
                type="button"
                :data-testid="`publish-${pkg.id}`"
                @click="runReleaseAction('发布版本', pkg.id)"
              >发布版本</button>
              <button
                v-if="pkg.status === '待发布'"
                class="link"
                type="button"
                :data-testid="`reject-${pkg.id}`"
                @click="runReleaseAction('驳回版本', pkg.id)"
              >驳回版本</button>
            </td>
          </tr>
          <tr v-if="!releases.length">
            <td colspan="11" class="empty-state">暂无指标发布版本包，请先发起版本校验</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="sources" class="panel" data-testid="sources-panel">
      <div class="panel-head">
        <h3>版本包来源输入 · {{ sources.包编号 }}</h3>
        <span class="panel-hint">可以从版本包回到指标草稿与历史发布记录</span>
      </div>
      <table class="data-table">
        <thead><tr><th>来源模块</th><th>来源类型</th><th>记录 id</th><th>来源路径</th><th>当前是否仍存在</th><th>字段快照</th></tr></thead>
        <tbody>
          <tr v-for="(source, index) in sources.关联来源记录" :key="index">
            <td>{{ source.来源模块 }}</td>
            <td>{{ source.来源类型 }}</td>
            <td>{{ source.来源记录id }}</td>
            <td class="mono">{{ source.来源路径 }}</td>
            <td>{{ source.是否仍存在 ? '是' : '否' }}</td>
            <td class="mono">{{ summarize(source.字段快照) }}</td>
          </tr>
        </tbody>
      </table>
      <p class="notice">
        主版本包：{{ sources.关联主版本包 ? `${sources.关联主版本包['包编号']}（${sources.关联主版本包['口径版本']}）` : '无（本次为首次发布）' }}
      </p>
    </section>

    <section class="panel" data-testid="chart-panel">
      <div class="panel-head">
        <h3>图表取值一致性</h3>
        <span class="panel-hint">图表刷新只读取一份完整已发布版本包</span>
      </div>
      <form class="analysis-form" @submit.prevent="refreshChart">
        <label>
          <span>图表</span>
          <select v-model.number="chartId" data-testid="chart-select">
            <option v-for="chart in charts" :key="String(chart.id)" :value="chart.id">
              {{ chart['图表编号'] }} · {{ chart['图表名称'] }}
            </option>
          </select>
        </label>
        <button class="btn primary" type="submit" data-testid="refresh-chart" :disabled="chartLoading">刷新图表</button>
      </form>
      <p v-if="chartMessage" class="notice" data-testid="chart-message">{{ chartMessage }}</p>
      <div v-if="chartSnapshot" class="snapshot-grid" data-testid="chart-snapshot">
        <div><span class="stat-label">口径版本</span><strong>{{ chartSnapshot.口径版本 ?? '—' }}</strong></div>
        <div><span class="stat-label">指标值</span><strong>{{ chartSnapshot.指标值 ?? '—' }}</strong></div>
        <div><span class="stat-label">版本包</span><strong>{{ chartSnapshot.包编号 ?? '—' }}</strong></div>
        <div><span class="stat-label">基准版本</span><strong>{{ chartSnapshot.基准版本 ?? '—' }}</strong></div>
        <div><span class="stat-label">超阈值条数</span><strong>{{ chartSnapshot.超阈值条数 ?? 0 }}</strong></div>
        <div><span class="stat-label">一致性</span><strong>{{ chartSnapshot.指标值口径版本一致 ? '一致' : '未取到已发布版本' }}</strong></div>
      </div>
    </section>

    <div v-if="showFormula" class="modal-backdrop" data-testid="formula-modal">
      <form class="modal" @submit.prevent="submitFormula">
        <h3>提交公式 · {{ formulaTarget?.['指标编号'] }}</h3>
        <p class="panel-hint">
          当前状态：{{ formulaTarget?.status }}。已发布记录会保留原公式作为基准，新公式落到同指标的新草稿。
        </p>
        <label class="create-field">
          <span>计算公式</span>
          <input v-model="formulaValue" data-testid="formula-input" required placeholder="如 SUM(成交金额)/COUNT(订单编号)" />
        </label>
        <label class="create-field">
          <span>统计粒度</span>
          <select v-model="formulaGranularity" data-testid="formula-granularity">
            <option v-for="option in granularities" :key="option" :value="option">{{ option }}</option>
          </select>
        </label>
        <label class="create-field">
          <span>时间窗口</span>
          <input v-model="formulaWindow" data-testid="formula-window" placeholder="如 2026-09-01~2026-09-07" />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showFormula = false">取消</button>
          <button class="btn primary" data-testid="submit-formula" type="submit" :disabled="submitting">
            {{ submitting ? '提交中…' : '保存公式' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ metrics.length }} 个指标 · {{ releases.length }} 个发布版本包</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/metric_def'
const granularities = ['按日', '按周', '按月', '按季', '按年']
const modes = ['按业务日期重算', '按统计粒度整组重算']

const metrics = ref<Row[]>([])
const releases = ref<Row[]>([])
const charts = ref<Row[]>([])
const summaryMetrics = ref<{ label: string; value: number; hint?: string }[]>([])
const params = ref<Record<string, string>>({
  '指标编号': '',
  '统计粒度': '按日',
  '时间窗口': '2026-09-01~2026-09-07',
  '重算方式': '按业务日期重算',
})
const validating = ref(false)
const validationMessage = ref('')
const activeDiff = ref<Row | null>(null)
const sources = ref<Row | null>(null)
const errorMessage = ref('')
const showFormula = ref(false)
const submitting = ref(false)
const formulaTarget = ref<Row | null>(null)
const formulaValue = ref('')
const formulaGranularity = ref('按日')
const formulaWindow = ref('')
const chartId = ref<number | null>(null)
const chartLoading = ref(false)
const chartSnapshot = ref<Row | null>(null)
const chartMessage = ref('')

const diffRows = computed<Row[]>(() => (activeDiff.value?.差异明细 as Row[]) ?? [])
const chartData = computed(() =>
  diffRows.value.slice(0, 12).map((row) => ({
    label: String(row.范围键),
    value: Number(row.发布值) || 0,
    exceeded: Boolean(row.是否超阈值),
  })),
)

function barHeight(value: number) {
  const max = Math.max(...chartData.value.map((item) => item.value), 1)
  return Math.max(10, (value / max) * 140)
}

function summarize(value: unknown) {
  if (!value || typeof value !== 'object') return '—'
  return Object.entries(value as Record<string, unknown>)
    .filter(([, v]) => v !== null && v !== '')
    .map(([k, v]) => `${k}=${v}`)
    .join('；')
}

async function fetchJson(path: string) {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return response.json()
}

async function postJson(path: string, body: unknown) {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload?.detail ?? `接口返回 ${response.status}`)
  return payload
}

async function fetchSummary() {
  try {
    const payload = await fetchJson(`${ENDPOINT}/summary`)
    summaryMetrics.value = payload.metrics || []
  } catch {
    summaryMetrics.value = []
  }
}

async function reloadMetrics() {
  const payload = await fetchJson(ENDPOINT)
  metrics.value = payload.items ?? []
  if (!params.value['指标编号'] && metrics.value.length) {
    params.value['指标编号'] = String(metrics.value[0]['指标编号'])
  }
}

async function reloadReleases() {
  const payload = await fetchJson(`${ENDPOINT}/releases?size=200`)
  releases.value = payload.items ?? []
}

async function reloadCharts() {
  const payload = await fetchJson('/api/chart?size=200')
  charts.value = (payload.items ?? []).filter((row: Row) => String(row['数据来源'] ?? '').trim())
  if (chartId.value === null && charts.value.length) chartId.value = Number(charts.value[0].id)
}

async function reloadChartsSafe() {
  try {
    await reloadCharts()
  } catch {
    charts.value = []
  }
}

async function reloadAll() {
  errorMessage.value = ''
  try {
    await Promise.all([reloadMetrics(), reloadReleases(), reloadChartsSafe(), fetchSummary()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据读取失败'
  }
}

function currentMetric() {
  return metrics.value.find((metric) => String(metric['指标编号']) === params.value['指标编号']) ?? metrics.value[0]
}

async function runAnalysis() {
  if (!metrics.value.length) {
    try {
      await reloadMetrics()
    } catch (error) {
      validationMessage.value = error instanceof Error ? error.message : '指标列表读取失败'
      return
    }
  }
  const metric = currentMetric()
  if (!metric) {
    validationMessage.value = '请先选择要校验的指标'
    return
  }
  validating.value = true
  validationMessage.value = ''
  errorMessage.value = ''
  try {
    const payload = await postJson(`${ENDPOINT}/${metric.id}/validate`, {
      values: {
        '统计粒度': params.value['统计粒度'],
        '时间窗口': params.value['时间窗口'],
        '重算方式': params.value['重算方式'],
      },
    })
    validationMessage.value = payload.message
    if (payload.entry?.id) {
      // 成功生成或命中冲突/去重时，页面都锚定到同一个版本包，避免看到两份口径。
      await viewDiff(payload.entry.id)
      await reloadReleases()
    }
  } catch (error) {
    validationMessage.value = error instanceof Error ? error.message : '版本校验失败'
    activeDiff.value = null
  } finally {
    validating.value = false
  }
}

async function validateMetric(metric: Row) {
  params.value['指标编号'] = String(metric['指标编号'])
  params.value['统计粒度'] = granularities.includes(metric['统计粒度']) ? metric['统计粒度'] : '按日'
  if (metric['时间窗口']) params.value['时间窗口'] = String(metric['时间窗口'])
  await runAnalysis()
}

async function viewDiff(releaseId: number) {
  errorMessage.value = ''
  try {
    const payload = await fetchJson(`${ENDPOINT}/releases/${releaseId}/diff`)
    activeDiff.value = payload
    sources.value = null
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '差异明细读取失败'
  }
}

async function viewSources(releaseId: number) {
  errorMessage.value = ''
  try {
    sources.value = await fetchJson(`${ENDPOINT}/releases/${releaseId}/sources`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '来源记录读取失败'
  }
}

async function runReleaseAction(action: string, releaseId: number) {
  errorMessage.value = ''
  try {
    const payload = await postJson(`${ENDPOINT}/releases/${releaseId}/actions`, { action })
    if (!payload.ok) throw new Error(payload.message || '版本动作未生效')
    validationMessage.value = payload.message
    await reloadReleases()
    await reloadMetrics()
    await viewDiff(releaseId)
    if (chartId.value !== null) await refreshChart()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版本动作失败'
  }
}

function openFormula(metric: Row) {
  formulaTarget.value = metric
  formulaValue.value = String(metric['计算公式'] ?? '')
  formulaGranularity.value = granularities.includes(metric['统计粒度']) ? metric['统计粒度'] : '按日'
  formulaWindow.value = String(metric['时间窗口'] ?? '')
  errorMessage.value = ''
  showFormula.value = true
}

async function submitFormula() {
  if (!formulaTarget.value) return
  submitting.value = true
  errorMessage.value = ''
  try {
    const payload = await postJson(`${ENDPOINT}/${formulaTarget.value.id}/formula`, {
      values: {
        '计算公式': formulaValue.value,
        '统计粒度': formulaGranularity.value,
        '时间窗口': formulaWindow.value,
      },
    })
    if (!payload.ok) throw new Error(payload.message || '公式提交失败')
    validationMessage.value = payload.message
    showFormula.value = false
    await reloadMetrics()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '公式提交失败'
  } finally {
    submitting.value = false
  }
}

async function refreshChart() {
  if (chartId.value === null) {
    chartMessage.value = '请先登记带指标数据来源的图表'
    chartSnapshot.value = null
    return
  }
  chartLoading.value = true
  errorMessage.value = ''
  try {
    const payload = await fetchJson(`/api/chart/${chartId.value}/metric`)
    chartSnapshot.value = payload.指标取值
    chartMessage.value = payload.刷新说明
  } catch (error) {
    chartSnapshot.value = null
    chartMessage.value = error instanceof Error ? error.message : '图表刷新失败'
  } finally {
    chartLoading.value = false
  }
}

onMounted(async () => {
  await reloadAll()
  if (releases.value.length) {
    const latest = releases.value[releases.value.length - 1]
    await viewDiff(Number(latest.id))
    await refreshChart()
  }
})
</script>

<style scoped>
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin: 12px 0; }
.panel-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
.panel-head h3 { margin: 0 0 4px; font-size: 15px; }
.panel-hint { color: var(--muted); font-size: 12px; }
.notice { background: #f8fafc; border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; font-size: 12px; color: #334155; }
.field-checks { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0; }
.field-check { font-size: 12px; border-radius: 10px; padding: 2px 10px; border: 1px solid var(--border); }
.field-check.same { background: #ecfdf3; color: #067647; }
.field-check.changed { background: #fff7ed; color: #b45309; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.analysis-bar.bar-exceeded { background: #f97316; }
.snapshot-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-top: 8px; }
.snapshot-grid > div { background: #f8fafc; border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; }
.snapshot-grid strong { display: block; font-size: 16px; margin-top: 4px; }
</style>
