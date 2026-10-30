import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { App } from './App'

vi.mock('./Plot', () => ({ Plot: () => <div role="img" aria-label="Mock sensor chart" /> }))
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

function mockFetch(handler: (path: string) => unknown) {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => ({ ok: true, json: async () => handler(path) })))
}

it('shows a truthful empty catalog state and navigates by keyboard-accessible buttons', async () => {
  mockFetch(path => path.includes('/assets') ? { items: [], total: 0 } : { items: [], total: 0 })
  render(<App />)
  expect(await screen.findByText(/No equipment is indexed/)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Telemetry' }))
  expect(screen.getByRole('heading', { name: 'Sensor trends' })).toBeTruthy()
  expect(screen.getByText(/no row-level alert stream/i)).toBeTruthy()
})

it('shows catalog errors as alerts', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
  render(<App />)
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('offline'))
})

it('keeps risk unavailable and limits sensor selection to four', async () => {
  mockFetch(path => {
    if (path.endsWith('/assets')) return { items: [{ asset_id: 'asset-1', dataset_artifact_id: 'data-1', dataset_id: 'metropt-3-uci', time_basis: 'source_naive_unresolved', source_sha256: 'abc' }], total: 1 }
    if (path.includes('/artifacts')) return { items: [{ artifact_id: 'data-1', kind: 'dataset', manifest_sha256: 'abc', summary: {} }], total: 1 }
    if (path.includes('/quality')) return { quality: { rows_written: 10 }, provenance: {}, time_basis: 'source_naive_unresolved' }
    return { status: 'unavailable', reason: 'Insufficient evidence' }
  })
  render(<App />)
  expect((await screen.findAllByText('Insufficient evidence')).length).toBe(2)
  fireEvent.click(screen.getByRole('button', { name: 'Telemetry' }))
  for (const name of ['tp2', 'h1', 'dv pressure']) fireEvent.click(screen.getByRole('checkbox', { name }))
  expect((screen.getByRole('checkbox', { name: 'reservoirs' }) as HTMLInputElement).disabled).toBe(true)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Load telemetry' })).toBeTruthy())
})
