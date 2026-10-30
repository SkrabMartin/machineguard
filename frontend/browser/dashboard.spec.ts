import { test, expect } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'

const artifact = (kind: string, id: string) => ({ kind, artifact_id: id, manifest_sha256: 'a'.repeat(64), summary: { source_sha256: 'b'.repeat(64) } })

test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    let body: unknown
    if (path.endsWith('/assets')) body = { items: [{ asset_id: 'MetroPT compressor', dataset_artifact_id: 'dataset-1', dataset_id: 'metropt-3-uci', time_basis: 'source_naive_unresolved', source_sha256: 'b'.repeat(64) }], total: 1 }
    else if (path.endsWith('/artifacts')) body = { items: [artifact('dataset', 'dataset-1'), artifact('baseline', 'baseline-1'), artifact('rul', 'rul-1')], total: 3 }
    else if (path.endsWith('/quality')) body = { quality: { rows_written: 1500000, missing_samples: 4 }, provenance: artifact('dataset', 'dataset-1'), time_basis: 'source_naive_unresolved' }
    else if (path.endsWith('/risk') || path.endsWith('/rul') && path.includes('/assets/')) body = { status: 'unavailable', reason: 'Evidence does not support transfer or calibration' }
    else if (path.endsWith('/alerts')) body = { metrics: { false_alarms_per_hour: 0.1 }, reported_windows: { physical: [] }, metric_caveat: 'Descriptive research only; no calibrated probability.', evidence_status: 'descriptive_anomaly_only', provenance: artifact('baseline', 'baseline-1') }
    else if (path === '/api/v1/rul/rul-1') body = { dataset_id: 'nasa-cmapss-classic', target: 'cycles', domain: 'simulated aircraft engines', subsets: { FD001: { validation: { mae: 18 }, publisher_test: { mae: 19 } } }, provenance: artifact('rul', 'rul-1'), transfer_status: 'not_validated_for_metropt_or_industrial_compressors' }
    else if (path.endsWith('/series')) {
      const offset = Number(url.searchParams.get('offset') ?? '0')
      body = { items: Array.from({ length: offset ? 1 : 500 }, (_, index) => ({ asset_id: 'MetroPT compressor', source_row_index: offset + index, event_time_source: `2020-02-01T00:${String(index % 60).padStart(2, '0')}:00`, measurements: { tp3: index } })), offset, limit: 500, next_offset: offset ? null : 500, time_basis: 'source_naive_unresolved', sensor_units: { tp3: 'bar' }, provenance: artifact('dataset', 'dataset-1') }
    } else if (path.endsWith('/scenarios/evaluate')) body = { evidence_status: 'hypothetical_only', model_probability_status: 'unavailable', decision_status: 'human_review_required', currency: 'USD', horizon_hours: 24, assumptions: {}, asset_economics: [{ asset_id: 'synthetic-pump-A', slot_options: [{ slot_id: 'day-1', net_benefit_of_maintenance: { low: '-10.00', base: '20.00', high: '40.00' }, sensitivity: 'changes_sign_with_assumptions', uncertainty_meaning: 'not a confidence interval' }] }], total_expected_cost_if_all_deferred: '100.00', schedule: { status: 'feasible' }, limitations: ['Hypothetical only'] }
    else throw new Error(`Unexpected API path: ${path}`)
    await route.fulfill({ json: body })
  })
})

test('overview and navigation keep scientific limits visible and meet automated accessibility checks', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible()
  await expect(page.getByText('UNAVAILABLE')).toHaveCount(2)
  await page.screenshot({ path: '/tmp/machineguard-phase12-overview.png', fullPage: true })
  const accessibility = await new AxeBuilder({ page }).analyze()
  expect(accessibility.violations).toEqual([])
  await page.keyboard.press('Tab')
  await expect(page.getByRole('link', { name: 'Skip to content' })).toBeFocused()
  await page.getByRole('button', { name: 'RUL research' }).click()
  await expect(page.getByText(/not validated for MetroPT compressors/i)).toBeVisible()
  await page.getByRole('button', { name: 'Alert evidence' }).click()
  await expect(page.getByText(/not a timestamped alert feed/i)).toBeVisible()
})

test('500-row telemetry page is bounded, paged, and retains alert caveat', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: 'Telemetry' }).click()
  await page.getByRole('button', { name: 'Load telemetry' }).click()
  await expect(page.getByText('500 rows shown')).toBeVisible()
  await expect(page.getByRole('img', { name: /Sensor trend chart/ })).toBeVisible({ timeout: 30000 })
  await expect(page.getByText('Descriptive research only; no calibrated probability.')).toBeVisible()
  await page.screenshot({ path: '/tmp/machineguard-phase12-telemetry.png', fullPage: true })
  await page.getByRole('button', { name: 'Next page' }).click()
  await expect(page.getByText('1 rows shown')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Previous page' })).toBeEnabled()
})

test('scenario labels assumptions and reveals bounded sensitivity', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: 'Scenarios' }).click()
  await expect(page.getByText('SYNTHETIC EXAMPLE')).toBeVisible()
  await page.getByRole('button', { name: 'Evaluate hypothetical scenario' }).click()
  await expect(page.getByText('USD 100.00')).toBeVisible()
  await expect(page.getByText(/not a confidence interval/i).first()).toBeVisible()
  await page.screenshot({ path: '/tmp/machineguard-phase12-scenarios.png', fullPage: true })
})
