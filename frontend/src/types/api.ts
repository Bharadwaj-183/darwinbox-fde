export interface MigrationCreated {
  migration_id: string
  status: string
  files: string[]
  entities: string[]
}

export interface MigrationSummary {
  migration_id: string
  status: string
  stats: Record<string, any>
  files: Array<{ filename: string; row_count: number | null; columns: string[] }>
}

export interface Escalation {
  id: string
  reason_code: string
  status: string
  title: string
  entity_name: string | null
  context: Record<string, any>
  suggested_resolution: Record<string, any> | null
  resolution: Record<string, any> | null
}

export interface AuditEvent {
  id: string
  record_id: string | null
  actor: string
  action: string
  reason: string
  before_data: Record<string, any> | null
  after_data: Record<string, any> | null
  created_at: string
}

export interface MigrationRecord {
  id: string
  entity_name: string
  source_file: string
  source_row_number: number
  raw_data: Record<string, any>
  canonical_data: Record<string, any> | null
  status: string
  target_record_key: string | null
  target_response: Record<string, any> | null
}

export interface MigrationResults {
  migration_id: string
  status: string
  source_tables: Record<string, Array<Record<string, any>>>
  target_tables: Record<string, Array<Record<string, any>>>
  target_urls: Record<string, string>
}

export interface AgentEvent {
  type: string
  message?: string
  migration_id?: string
  status?: string
  progress?: number
  action_required?: boolean
  source?: 'live' | 'history'
  timestamp?: string
  [key: string]: unknown
}
