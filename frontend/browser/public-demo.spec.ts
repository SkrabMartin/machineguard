import { test, expect } from '@playwright/test'

declare const process: { env: Record<string, string | undefined> }

// Run with a real public-demo API and an explicitly configured Vite proxy.
test.skip(process.env.MACHINEGUARD_LIVE_DEMO !== '1', 'requires a live synthetic public-demo API')

for (const size of [{ name: 'desktop', width: 1440, height: 900 }, { name: 'mobile', width: 390, height: 844 }]) {
  test(`real public demo dashboard workflow on ${size.name}`, async ({ page }) => {
    test.setTimeout(120000)
    await page.setViewportSize({ width: size.width, height: size.height })
    const failedApi: string[] = []
    page.on('response', response => {
      if (response.url().includes('/api/v1/') && response.status() >= 400) failedApi.push(`${response.status()} ${response.url()}`)
    })
    await page.goto('/')
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible()
    await expect(page.getByText('SYNTHETIC DEMO', { exact: true })).toBeVisible()
    await expect(page.getByRole('combobox', { name: 'Equipment' }).locator('option')).toHaveCount(2)
    await expect(page.locator('.availability span')).toHaveCount(2)
    await page.getByRole('combobox', { name: 'Equipment' }).selectOption('synthetic-compressor-b')
    await expect(page.getByRole('combobox', { name: 'Equipment' })).toHaveValue('synthetic-compressor-b')

    await page.getByRole('button', { name: 'Telemetry' }).click()
    await expect(page.getByText(/entirely generated for this demo/i)).toBeVisible()
    await page.getByRole('checkbox', { name: 'motor current' }).check()
    await page.getByRole('button', { name: 'Load telemetry' }).click()
    await expect(page.getByText('500 rows shown')).toBeVisible()
    await expect(page.getByRole('img', { name: /Sensor trend chart/ })).toBeVisible({ timeout: 30000 })
    await page.getByRole('button', { name: 'Next page' }).click()
    await expect(page.getByText('500 rows shown')).toBeVisible()
    await expect(page.getByText(/Offset 500/)).toBeVisible()
    await page.getByRole('button', { name: 'Previous page' }).click()
    await expect(page.getByText(/Offset 0/)).toBeVisible()
    await page.getByLabel('End (synthetic time)').fill('2020-02-01T00:05')
    await page.getByRole('button', { name: 'Load telemetry' }).click()
    await expect(page.getByText('30 rows shown')).toBeVisible()

    await page.getByRole('button', { name: 'Alert evidence' }).click()
    await expect(page.getByText(/unavailable for these assets/i)).toBeVisible()
    await page.getByRole('button', { name: 'RUL research' }).click()
    await expect(page.getByText(/separate from the selected telemetry/i)).toBeVisible()
    await page.getByRole('button', { name: 'Scenarios' }).click()
    await page.getByRole('button', { name: 'Evaluate hypothetical scenario' }).click()
    await expect(page.getByText('Hypothetical', { exact: true })).toBeVisible()
    await expect(page.getByText(/not a confidence interval/i).first()).toBeVisible()
    await page.getByRole('button', { name: 'Provenance' }).click()
    await expect(page.getByText(/Synthetic demo artifacts are generated in isolated storage/i)).toBeVisible()
    await page.getByText('Artifact summary').first().click()
    await expect(page.getByText('synthetic_demo', { exact: true }).first()).toBeVisible()
    expect(failedApi).toEqual([])
  })
}
