import { describe, expect, it, vi } from 'vitest'
import { request, seriesPath } from './api'

describe('API contract helpers', () => {
  it('bounds a telemetry request to 500 rows and preserves source-naive text', () => {
    expect(seriesPath('artifact id', '2020-02-01T00:00', '2020-02-01T01:00', ['tp3', 'motor_current'], 500))
      .toContain('start=2020-02-01T00%3A00%3A00')
    expect(seriesPath('id', '2020-02-01T00:00', '2020-02-01T01:00', ['tp3'], 0)).toContain('limit=500')
  })
  it('returns a useful API error without exposing a body', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 422, json: async () => ({ error: { message: 'invalid range' } }) }))
    await expect(request('/datasets/id/series')).rejects.toThrow('invalid range')
    vi.unstubAllGlobals()
  })
})
