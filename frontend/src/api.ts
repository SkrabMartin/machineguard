export type Artifact = { artifact_id: string; kind: string; manifest_sha256: string; summary: Record<string, unknown> }
export type Page<T> = { items: T[]; total: number; offset: number; limit: number }
export type Asset = { asset_id: string; dataset_artifact_id: string; dataset_id: string; time_basis: string; source_sha256: string }
export type Quality = { quality: Record<string, unknown>; provenance: Artifact; time_basis: string }
export type Series = { items: { asset_id: string; source_row_index: number; event_time_source: string; measurements: Record<string, number> }[]; offset: number; limit: number; next_offset: number | null; time_basis: string; sensor_units: Record<string, string | null>; provenance: Artifact }
export type Alerts = { metrics: Record<string, unknown>; reported_windows: Record<string, unknown>; metric_caveat: string; evidence_status: string; provenance: Artifact }
export type Rul = { dataset_id: string; target: string; domain: string; subsets: Record<string, unknown>; provenance: Artifact; transfer_status: string }
export type Unavailable = { status: 'unavailable'; reason: string }
export type ScenarioResult = { evidence_status: string; model_probability_status: string; decision_status: string; currency: string; horizon_hours: number; assumptions: Record<string, unknown>; asset_economics: { asset_id: string; slot_options: { slot_id: string; net_benefit_of_maintenance: Record<'low' | 'base' | 'high', string>; sensitivity: string; uncertainty_meaning: string }[] }[]; total_expected_cost_if_all_deferred: string; schedule: Record<string, unknown>; limitations: string[] }

export async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, options)
  if (!response.ok) {
    let message = `HTTP ${response.status}`
    try {
      const body = await response.json() as { error?: { message?: string; code?: string } }
      message = body.error?.message ?? body.error?.code ?? message
    } catch { /* Preserve status if the server returned non-JSON. */ }
    throw new Error(message)
  }
  return response.json() as Promise<T>
}

export function seriesPath(id: string, start: string, end: string, sensors: string[], offset: number): string {
  const query = new URLSearchParams({ start: `${start}:00`, end: `${end}:00`, sensors: sensors.join(','), offset: String(offset), limit: '500' })
  return `/datasets/${encodeURIComponent(id)}/series?${query}`
}

export function entries(value: Record<string, unknown>): [string, string][] {
  return Object.entries(value).map(([key, item]) => [key, typeof item === 'object' && item !== null ? JSON.stringify(item) : String(item)])
}
