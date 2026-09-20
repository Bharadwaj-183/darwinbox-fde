import type { AuditEvent, Escalation, MigrationCreated, MigrationRecord, MigrationResults, MigrationSummary } from '../types/api'

const API_BASE = import.meta.env.VITE_API_BASE ?? `${window.location.protocol}//${window.location.hostname}:8000/api`

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  const raw = await response.text()
  if (!response.ok) {
    let message = raw || `Request failed (${response.status})`
    try {
      const parsed = JSON.parse(raw) as { detail?: unknown; message?: unknown }
      const detail = parsed.detail ?? parsed.message
      if (typeof detail === 'string') message = detail
      else if (detail && typeof detail === 'object') {
        const detailObject = detail as Record<string, unknown>
        message = String(detailObject.message ?? detailObject.error ?? detailObject.error_code ?? message)
      }
    } catch {
      // Keep the raw response only for non-JSON failures.
    }

    // Never surface raw framework/validation payloads to consultants.
    if (response.status === 400 && /target schema/i.test(message)) {
      if (/email fields must use/i.test(message)) {
        message = "Target schema issue: use data_type 'string' for email fields."
      } else if (/enum fields must use/i.test(message)) {
        message = "Target schema issue: use data_type 'string' and enum_values for allowed choices."
      } else {
        message = "Target schema is invalid. Please check the schema structure and field definitions."
      }
    }
    throw new Error(message)
  }
  return raw ? JSON.parse(raw) as T : (undefined as T)
}

export async function createMigration(files: File[], targetSchemaFile: File): Promise<MigrationCreated> {
  const form = new FormData()
  files.forEach((file) => form.append('files', file))
  form.append('target_schema', await targetSchemaFile.text())
  form.append('target_schema_filename', targetSchemaFile.name)
  return request<MigrationCreated>(`${API_BASE}/migrations`, { method: 'POST', body: form })
}

export async function startMigration(id: string): Promise<void> {
  await request(`${API_BASE}/migrations/${id}/start`, { method: 'POST' })
}

export async function stopMigration(id: string): Promise<Record<string, unknown>> {
  return request(`${API_BASE}/migrations/${id}/stop`, { method: 'POST', keepalive: true })
}

export function stopMigrationOnUnload(id: string): void {
  const url = `${API_BASE}/migrations/${id}/stop`
  if (navigator.sendBeacon) {
    const sent = navigator.sendBeacon(url, new Blob([], { type: 'application/json' }))
    if (sent) return
  }
  void fetch(url, { method: 'POST', keepalive: true, credentials: 'include', headers: { 'Content-Type': 'application/json' } }).catch(() => undefined)
}

export async function pushMigration(id: string): Promise<Record<string, unknown>> {
  return request(`${API_BASE}/migrations/${id}/push`, { method: 'POST' })
}

export async function rollbackMigration(id: string): Promise<Record<string, unknown>> {
  return request(`${API_BASE}/migrations/${id}/rollback`, { method: 'POST' })
}

export async function retryRecord(id: string): Promise<Record<string, unknown>> {
  return request(`${API_BASE}/records/${id}/retry`, { method: 'POST' })
}

export async function getMigration(id: string): Promise<MigrationSummary> {
  return request(`${API_BASE}/migrations/${id}`)
}

export async function getEscalations(id: string): Promise<Escalation[]> {
  return request(`${API_BASE}/migrations/${id}/escalations`)
}

export async function getRecords(id: string): Promise<MigrationRecord[]> {
  return request(`${API_BASE}/migrations/${id}/records`)
}

export async function resolveEscalation(id: string, resolution: Record<string, unknown>): Promise<Escalation> {
  return request(`${API_BASE}/escalations/${id}/resolve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(resolution),
  })
}

export async function getAudit(id: string): Promise<AuditEvent[]> {
  return request(`${API_BASE}/migrations/${id}/audit`)
}

export async function getResults(id: string): Promise<MigrationResults> {
  return request(`${API_BASE}/migrations/${id}/results`)
}

export function streamMigrationEvents(
  id: string,
  onEvent: (event: MessageEvent) => void,
  onError?: () => void,
): EventSource {
  const source = new EventSource(`${API_BASE}/migrations/${id}/events`)
  ;['migration_created', 'agent_status', 'escalation_created', 'record_pushed'].forEach((name) => {
    source.addEventListener(name, onEvent)
  })
  source.onerror = () => onError?.()
  return source
}
