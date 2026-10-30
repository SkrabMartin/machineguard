import { lazy, Suspense, useEffect, useState } from 'react'
import { type Alerts, type Artifact, type Asset, type Page, type Quality, type Rul, type ScenarioResult, type Series, type Unavailable, entries, request, seriesPath } from './api'
import scenarioExample from '../../configs/maintenance_scenario.synthetic.json'

type View = 'Overview' | 'Telemetry' | 'Alert evidence' | 'RUL research' | 'Scenarios' | 'Provenance'
const views: View[] = ['Overview', 'Telemetry', 'Alert evidence', 'RUL research', 'Scenarios', 'Provenance']
const Plot = lazy(() => import('./Plot').then(module => ({ default: module.Plot })))
const sensors = ['tp2', 'tp3', 'h1', 'dv_pressure', 'reservoirs', 'oil_temperature', 'motor_current', 'comp', 'dv_electric', 'towers', 'mpg', 'lps', 'pressure_switch', 'oil_level', 'caudal_impulses']

function FieldList({ values }: { values: Record<string, unknown> }) {
  return <dl className="field-list">{entries(values).map(([key, value]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd title={value}>{value}</dd></div>)}</dl>
}
function Notice({ children, tone = 'amber' }: { children: React.ReactNode; tone?: 'amber' | 'teal' }) {
  return <div className={`notice ${tone}`} role="note">{children}</div>
}
function ErrorMessage({ error }: { error: string | null }) { return error && <div className="error" role="alert">{error}</div> }
function ArtifactLabel({ item }: { item: Artifact }) {
  return <div className="artifact"><span className="badge">{item.kind}</span><code>{item.artifact_id}</code><small>Manifest SHA-256: {item.manifest_sha256}</small></div>
}

export function App() {
  const [view, setView] = useState<View>('Overview')
  const [assets, setAssets] = useState<Asset[]>([])
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [selected, setSelected] = useState('')
  const [quality, setQuality] = useState<Quality | null>(null)
  const [alerts, setAlerts] = useState<Alerts | null>(null)
  const [rul, setRul] = useState<Rul | null>(null)
  const [risk, setRisk] = useState<Unavailable | null>(null)
  const [assetRul, setAssetRul] = useState<Unavailable | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [start, setStart] = useState('2020-02-01T00:00')
  const [end, setEnd] = useState('2020-02-01T04:00')
  const [chosenSensors, setChosenSensors] = useState<string[]>(['tp3'])
  const [page, setPage] = useState<Series | null>(null)
  const [pageHistory, setPageHistory] = useState<number[]>([])
  const [seriesError, setSeriesError] = useState<string | null>(null)
  const [seriesLoading, setSeriesLoading] = useState(false)
  const [scenarioText, setScenarioText] = useState(JSON.stringify(scenarioExample, null, 2))
  const [scenarioResult, setScenarioResult] = useState<ScenarioResult | null>(null)
  const [scenarioError, setScenarioError] = useState<string | null>(null)
  const [scenarioLoading, setScenarioLoading] = useState(false)

  useEffect(() => {
    let active = true
    Promise.all([request<Page<Asset>>('/assets'), request<Page<Artifact>>('/artifacts?limit=100')])
      .then(([assetPage, artifactPage]) => {
        if (!active) return
        setAssets(assetPage.items); setArtifacts(artifactPage.items)
        setSelected(assetPage.items[0]?.asset_id ?? '')
      })
      .catch((reason: unknown) => { if (active) setError(`Could not load catalog: ${String(reason)}`) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])

  const asset = assets.find(item => item.asset_id === selected)
  const dataset = artifacts.find(item => item.artifact_id === asset?.dataset_artifact_id)
  const isSynthetic = dataset?.summary.data_origin === 'synthetic_demo'
  const dataLabel = isSynthetic ? 'SYNTHETIC DEMO' : dataset ? 'REGISTERED DATA' : 'NO DATA'
  const timeLabel = isSynthetic ? 'synthetic time' : 'source time'
  const modeLabel = isSynthetic ? 'PUBLIC SYNTHETIC DEMO' : 'LOCAL RESEARCH MODE'
  const baseline = artifacts.find(item => item.kind === 'baseline')
  const rulArtifact = artifacts.find(item => item.kind === 'rul')

  useEffect(() => {
    if (!asset) return
    let active = true
    setQuality(null); setRisk(null); setAssetRul(null)
    Promise.all([
      request<Quality>(`/datasets/${asset.dataset_artifact_id}/quality`),
      request<Unavailable>(`/assets/${encodeURIComponent(asset.asset_id)}/risk`),
      request<Unavailable>(`/assets/${encodeURIComponent(asset.asset_id)}/rul`),
    ]).then(([q, r, u]) => { if (active) { setQuality(q); setRisk(r); setAssetRul(u) } })
      .catch((reason: unknown) => { if (active) setError(`Could not load asset evidence: ${String(reason)}`) })
    return () => { active = false }
  }, [asset?.asset_id, asset?.dataset_artifact_id])

  useEffect(() => {
    if (!baseline) return
    let active = true
    request<Alerts>(`/baselines/${baseline.artifact_id}/alerts`).then(value => { if (active) setAlerts(value) })
      .catch((reason: unknown) => { if (active) setError(`Could not load alert evidence: ${String(reason)}`) })
    return () => { active = false }
  }, [baseline?.artifact_id])

  useEffect(() => {
    if (!rulArtifact) return
    let active = true
    request<Rul>(`/rul/${rulArtifact.artifact_id}`).then(value => { if (active) setRul(value) })
      .catch((reason: unknown) => { if (active) setError(`Could not load RUL report: ${String(reason)}`) })
    return () => { active = false }
  }, [rulArtifact?.artifact_id])

  async function loadSeries(offset = 0, history: number[] = []) {
    if (!dataset) { setSeriesError('Select a registered dataset artifact first.'); return }
    if (!start || !end || start >= end) { setSeriesError('Choose an end after the start.'); return }
    if ((Date.parse(end) - Date.parse(start)) > 7 * 86400000) { setSeriesError('The API accepts at most seven days per query.'); return }
    if (chosenSensors.length < 1 || chosenSensors.length > 4) { setSeriesError('Select one to four sensors.'); return }
    setSeriesLoading(true); setSeriesError(null)
    try {
      const result = await request<Series>(seriesPath(dataset.artifact_id, start, end, chosenSensors, offset))
      setPage(result); setPageHistory(history)
    } catch (reason) { setSeriesError(`Could not load telemetry: ${String(reason)}`) }
    finally { setSeriesLoading(false) }
  }

  function toggleSensor(sensor: string) {
    setChosenSensors(previous => previous.includes(sensor) ? previous.filter(item => item !== sensor) : previous.length < 4 ? [...previous, sensor] : previous)
    setPage(null); setPageHistory([])
  }

  async function evaluateScenario() {
    setScenarioLoading(true); setScenarioError(null); setScenarioResult(null)
    try {
      const parsed = JSON.parse(scenarioText) as unknown
      if (typeof parsed !== 'object' || parsed === null || (parsed as { scenario_kind?: string }).scenario_kind !== 'hypothetical_assumptions') {
        throw new Error('Only explicitly hypothetical assumptions are accepted.')
      }
      const result = await request<ScenarioResult>('/scenarios/evaluate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(parsed) })
      setScenarioResult(result)
    } catch (reason) { setScenarioError(`Scenario rejected: ${String(reason)}`) }
    finally { setScenarioLoading(false) }
  }

  return <div className="shell">
    <a className="skip" href="#main">Skip to content</a>
    <aside className="sidebar" aria-label="Primary navigation">
      <div className="brand"><div className="brand-mark">M<span>G</span></div><div><strong>MachineGuard</strong><small>RESEARCH CONSOLE</small></div></div>
      <div className="nav-label">WORKSPACE</div>
      <nav aria-label="Dashboard sections">{views.map((item, index) => <button key={item} type="button" className={`nav-item ${view === item ? 'active' : ''}`} aria-current={view === item ? 'page' : undefined} onClick={() => setView(item)}><span className="nav-icon" aria-hidden="true">{['◫','⌁','◇','◷','▥','▤'][index]}</span>{item}</button>)}</nav>
      <div className="sidebar-foot"><span className="status-dot" /> {modeLabel} <small>Advisory analysis · human review required</small></div>
    </aside>
    <main id="main" className="main">
      <header className="topbar"><div className="crumb">MACHINEGUARD <span>/</span> {view.toUpperCase()}</div><div className="top-state"><span className="status-dot" /> {isSynthetic ? 'PUBLIC DEMO · SYNTHETIC DATA' : 'LOCAL API · SOURCE EVIDENCE'}</div></header>
      <div className="content">
        <div className="page-heading"><div><span className="eyebrow">OPERATIONS INTELLIGENCE / {view.toUpperCase()}</span><h1>{view}</h1><p>{view === 'Overview' ? 'A traceable view of available machine evidence.' : 'Research outputs and assumptions, presented with their limits.'}</p></div><span className="phase-pill">EXPERIMENTAL · PHASE 12</span></div>
        <ErrorMessage error={error} />
        {loading && <p role="status">Loading artifact catalog…</p>}
        {!loading && assets.length === 0 && <Notice>No equipment is indexed. Use the CLI to register a verified MetroPT dataset; the dashboard does not contain demo machine data.</Notice>}
        {assets.length > 0 && <div className="toolbar"><label htmlFor="asset">Equipment</label><select id="asset" value={selected} onChange={event => setSelected(event.target.value)}>{assets.map(item => <option key={item.asset_id} value={item.asset_id}>{item.asset_id}</option>)}</select><span className="toolbar-note">{asset?.dataset_id} · {isSynthetic ? 'artificial timestamps' : 'source time, timezone unresolved'}</span></div>}
        {view === 'Overview' && <>
          <div className="metric-grid"><div className="metric"><span>Indexed equipment</span><strong>{assets.length}</strong><small>Verified artifact catalog</small></div><div className="metric"><span>Dataset artifacts</span><strong>{artifacts.filter(item => item.kind === 'dataset').length}</strong><small>Metadata references</small></div><div className="metric"><span>Alert evidence</span><strong>{alerts ? 'Descriptive' : 'Unavailable'}</strong><small>No calibrated failure risk</small></div><div className="metric"><span>Decision mode</span><strong>Human review</strong><small>Hypothetical scenarios only</small></div></div>
          <div className="two-col"><section className="card"><div className="card-head"><h2>Asset evidence</h2><span className="badge teal">{dataLabel}</span></div>{asset && <FieldList values={{ asset_id: asset.asset_id, dataset: asset.dataset_id, time_basis: asset.time_basis, source_sha256: asset.source_sha256 }} />}{quality ? <><h3>Data quality summary</h3><FieldList values={quality.quality} /></> : <p className="muted">No quality summary loaded.</p>}</section><section className="card"><div className="card-head"><h2>Model availability</h2><span className="badge amber">EVIDENCE GATED</span></div><div className="availability"><strong>Failure probability</strong><span>UNAVAILABLE</span><p>{risk?.reason ?? 'Checking evidence status…'}</p></div><div className="availability"><strong>MetroPT remaining useful life</strong><span>UNAVAILABLE</span><p>{assetRul?.reason ?? 'Checking transfer status…'}</p></div><Notice>C-MAPSS RUL results belong to simulated aircraft engines and are shown separately under RUL research.</Notice></section></div>
        </>}
        {view === 'Telemetry' && <>
          <Notice>{isSynthetic ? 'These signals and timestamps are entirely generated for this demo; they contain no observed faults or trained-model outputs. ' : 'These are source-naive timestamps with unresolved timezone. '}The chart shows one bounded page at a time. Alert aggregates remain separate because the API has no row-level alert stream.</Notice>
          <section className="card"><div className="card-head"><h2>Sensor trends</h2><span className="badge teal">{dataLabel}</span></div><div className="filters"><label>Start ({timeLabel})<input type="datetime-local" value={start} onChange={event => { setStart(event.target.value); setPage(null) }} /></label><label>End ({timeLabel})<input type="datetime-local" value={end} onChange={event => { setEnd(event.target.value); setPage(null) }} /></label></div><fieldset className="sensor-list"><legend>Sensors · select up to 4</legend>{sensors.map(sensor => <label key={sensor}><input type="checkbox" checked={chosenSensors.includes(sensor)} onChange={() => toggleSensor(sensor)} disabled={!chosenSensors.includes(sensor) && chosenSensors.length >= 4} /> {sensor.replaceAll('_', ' ')}</label>)}</fieldset><button className="primary" type="button" onClick={() => void loadSeries()} disabled={seriesLoading || !dataset}>{seriesLoading ? 'Loading…' : 'Load telemetry'}</button><ErrorMessage error={seriesError} />{page && <><div className="page-meta"><strong>{page.items.length} rows shown</strong><span>Offset {page.offset} · page limit {page.limit} · {page.next_offset === null ? 'last page' : 'more rows available'}</span></div>{page.items.length ? <><Suspense fallback={<p role="status">Loading chart…</p>}><Plot page={page} sensors={chosenSensors} /></Suspense><details><summary>Accessible sensor values for this page</summary><div className="table-wrap"><table><caption>Current telemetry page, {timeLabel} and selected sensor values</caption><thead><tr><th>{timeLabel}</th>{chosenSensors.map(sensor => <th key={sensor}>{sensor} ({page.sensor_units[sensor] ?? 'unit unknown'})</th>)}</tr></thead><tbody>{page.items.map(row => <tr key={row.source_row_index}><td>{row.event_time_source}</td>{chosenSensors.map(sensor => <td key={sensor}>{row.measurements[sensor]}</td>)}</tr>)}</tbody></table></div></details></> : <p role="status" className="empty">No telemetry rows in this range. Choose another time interval.</p>}<div className="pager"><button type="button" disabled={pageHistory.length === 0 || seriesLoading} onClick={() => void loadSeries(pageHistory.at(-1), pageHistory.slice(0, -1))}>Previous page</button><button type="button" disabled={page.next_offset === null || seriesLoading} onClick={() => void loadSeries(page.next_offset!, [...pageHistory, page.offset])}>Next page</button></div></>}</section><section className="card"><h2>Alert context</h2>{alerts ? <><p>{alerts.metric_caveat}</p><span className="badge amber">DESCRIPTIVE ANOMALY ONLY</span></> : <p>No baseline artifact is indexed.</p>}</section>
        </>}
        {view === 'Alert evidence' && <><Notice>Reported windows and validation metrics are descriptive research evidence. They are not a timestamped alert feed, calibrated failure probability, or operational alarm guarantee.</Notice>{alerts ? <div className="two-col"><section className="card"><h2>Evaluation metrics</h2><FieldList values={alerts.metrics} /></section><section className="card"><h2>Reported fault windows</h2><FieldList values={alerts.reported_windows} /></section><section className="card full"><h2>Interpretation</h2><p>{alerts.metric_caveat}</p><ArtifactLabel item={alerts.provenance} /></section></div> : <section className="card"><p>No statistical baseline artifact is included in this catalog. Descriptive alert evidence is unavailable for these assets.</p></section>}</>}
        {view === 'RUL research' && <><Notice>C-MAPSS uses simulated aircraft engines and cycle-based targets. These results are not validated for MetroPT compressors or industrial equipment.</Notice>{rul ? <><div className="metric-grid"><div className="metric"><span>Dataset</span><strong>{rul.dataset_id}</strong><small>Separate benchmark</small></div><div className="metric"><span>Target</span><strong>{rul.target}</strong><small>Operational cycles</small></div></div><section className="card"><h2>Subset evaluation</h2>{Object.entries(rul.subsets).map(([name, value]) => <details key={name} className="subset"><summary>{name}</summary><FieldList values={value as Record<string, unknown>} /></details>)}<p className="muted">Domain: {rul.domain}. Transfer status: {rul.transfer_status.replaceAll('_', ' ')}.</p><ArtifactLabel item={rul.provenance} /></section></> : <section className="card"><p>No verified C-MAPSS RUL report is included in this catalog. This benchmark is separate from the selected telemetry.</p></section>}</>}
        {view === 'Scenarios' && <><Notice>All probabilities, costs, and effectiveness values below are user-supplied hypothetical assumptions. Sensitivity bounds are not confidence intervals. No observed savings or work order is produced.</Notice><section className="card"><div className="card-head"><h2>Scenario workbench</h2><span className="badge amber">SYNTHETIC EXAMPLE</span></div><p>Edit the explicit synthetic JSON example. The API validates the assumptions and computes a bounded crew schedule for human review.</p><label className="editor-label" htmlFor="scenario">Hypothetical assumptions JSON</label><textarea id="scenario" spellCheck={false} value={scenarioText} onChange={event => setScenarioText(event.target.value)} /><button className="primary" type="button" disabled={scenarioLoading} onClick={() => void evaluateScenario()}>{scenarioLoading ? 'Evaluating…' : 'Evaluate hypothetical scenario'}</button><ErrorMessage error={scenarioError} /></section>{scenarioResult && <><div className="metric-grid"><div className="metric"><span>Evidence status</span><strong>Hypothetical</strong><small>{scenarioResult.decision_status.replaceAll('_', ' ')}</small></div><div className="metric"><span>Assumed defer cost</span><strong>{scenarioResult.currency} {scenarioResult.total_expected_cost_if_all_deferred}</strong><small>Base assumptions · not observed</small></div><div className="metric"><span>Schedule</span><strong>{String(scenarioResult.schedule.status)}</strong><small>Capacity-constrained</small></div></div><section className="card"><h2>Assumption sensitivity</h2><div className="table-wrap"><table><thead><tr><th>Asset</th><th>Slot</th><th>Low</th><th>Base</th><th>High</th><th>Interpretation</th></tr></thead><tbody>{scenarioResult.asset_economics.flatMap(item => item.slot_options.map(option => <tr key={`${item.asset_id}-${option.slot_id}`}><td>{item.asset_id}</td><td>{option.slot_id}</td><td>{option.net_benefit_of_maintenance.low}</td><td>{option.net_benefit_of_maintenance.base}</td><td>{option.net_benefit_of_maintenance.high}</td><td>{option.sensitivity.replaceAll('_', ' ')}</td></tr>))}</tbody></table></div><p className="muted">Amounts in {scenarioResult.currency}. Box bounds across stated assumptions; not a confidence interval.</p><details><summary>Schedule and limitations</summary><pre>{JSON.stringify(scenarioResult.schedule, null, 2)}</pre><ul>{scenarioResult.limitations.map(item => <li key={item}>{item}</li>)}</ul></details></section></>}</>}
        {view === 'Provenance' && <><Notice>Artifact IDs and SHA-256 fingerprints identify verified inputs. Synthetic demo artifacts are generated in isolated storage; research datasets and model files are not included in the public repository.</Notice><section className="card"><h2>Registered artifacts</h2>{artifacts.length ? artifacts.map(item => <div className="provenance-item" key={item.artifact_id}><ArtifactLabel item={item} /><details><summary>Artifact summary</summary><FieldList values={item.summary} /></details></div>) : <p>No artifacts indexed.</p>}</section></>}
      </div>
    </main>
  </div>
}
