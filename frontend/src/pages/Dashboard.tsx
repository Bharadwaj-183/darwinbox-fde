import { useEffect, useMemo, useRef, useState } from 'react'
import {
  createMigration, getAudit, getEscalations, getMigration, getRecords, getResults,
  pushMigration, resolveEscalation, retryRecord, rollbackMigration,
  startMigration, stopMigration, stopMigrationOnUnload, streamMigrationEvents,
} from '../services/api'
import type { AgentEvent, AuditEvent, Escalation, MigrationRecord, MigrationResults, MigrationSummary } from '../types/api'

function pretty(value: unknown) {
  return JSON.stringify(value, null, 2)
}

function statusLabel(status: string) {
  if (status === 'WAITING_HUMAN') return 'Consultant Review'
  return status.replaceAll('_', ' ').toLowerCase().replace(/\b\w/g, (letter) => letter.toUpperCase())
}

function reasonLabel(reason: string) {
  const labels: Record<string, string> = {
    ambiguous_mapping: 'Ambiguous field mapping',
    conflicting_values: 'Conflicting source values',
    invalid_record: 'Record validation issue',
    ambiguous_match: 'Possible duplicate / identity match',
    new_transformation: 'New transformation proposed',
    llm_unavailable: 'AI reasoning unavailable',
    unsupported_operation: 'Unsupported transformation',
    unmapped_value: 'Source value needs mapping',
    target_api_failure: 'Target API failure',
  }
  return labels[reason] ?? reason.replaceAll('_', ' ')
}

function meaningfulResolution(value: unknown): boolean {
  if (value === null || value === undefined) return false
  if (typeof value === 'string') return value.trim().length > 0
  if (Array.isArray(value)) return value.length > 0 && value.every(meaningfulResolution)
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
    return entries.length > 0 && entries.every(([key, item]) => key.trim().length > 0 && meaningfulResolution(item))
  }
  return true
}

function progressInfo(migration: MigrationSummary | null) {
  const stats = migration?.stats ?? {}
  if (!migration) return { progress: 0, message: 'Ready to start', nextAction: 'Upload source files and a target schema.' }
  const fallback: Record<string, { progress: number; message: string; nextAction: string }> = {
    CREATED: { progress: 0, message: 'Ready to start migration', nextAction: 'Upload source files and target schema.' },
    QUEUED: { progress: 5, message: 'Starting migration', nextAction: 'The agent is starting automatically.' },
    PROFILING: { progress: 15, message: 'Reading source files', nextAction: 'The agent is profiling the uploaded data.' },
    MAPPING: { progress: 35, message: 'Resolving field mappings', nextAction: 'Clear matches are automatic; AI checks only ambiguous fields.' },
    RECONCILING: { progress: 58, message: 'Merging duplicate records', nextAction: 'The agent is reconciling records across files.' },
    VALIDATING: { progress: 72, message: 'Validating target data', nextAction: 'The agent is checking target-schema compatibility.' },
    CONSULTANT_REVIEW: { progress: 60, message: 'Review needed — check Consultant Review', nextAction: 'Resolve the open review item(s) to continue.' },
    WAITING_HUMAN: { progress: 60, message: 'Review needed — check Consultant Review', nextAction: 'Resolve the open review item(s) to continue.' },
    READY_TO_PUSH: { progress: 82, message: 'Ready to push changes', nextAction: 'Review the target preview, then push the changes.' },
    PUSHING: { progress: 90, message: 'Pushing changes to target', nextAction: 'Writing the transformed data to the target system.' },
    PARTIAL_FAILURE: { progress: 90, message: 'Retry needed — check failed records', nextAction: 'Retry failed records or roll back the migration.' },
    COMPLETED: { progress: 100, message: 'Migration completed successfully', nextAction: 'View the final source and target data.' },
    ROLLED_BACK: { progress: 100, message: 'Migration rolled back', nextAction: 'Start a new migration when ready.' },
    ROLLBACK_PARTIAL_FAILURE: { progress: 95, message: 'Rollback needs attention', nextAction: 'Review the Audit Trail and remaining target records.' },
    STOPPED: { progress: 0, message: 'Migration stopped', nextAction: 'Start a new migration when ready.' },
    FAILED: { progress: 100, message: 'Migration failed — review activity', nextAction: 'Review Agent Activity and Audit Trail.' },
  }
  return {
    progress: Math.max(0, Math.min(100, Number(stats.progress ?? fallback[migration.status]?.progress ?? 0))),
    message: String(stats.status_message ?? fallback[migration.status]?.message ?? statusLabel(migration.status)),
    nextAction: String(stats.next_action ?? fallback[migration.status]?.nextAction ?? ''),
  }
}

function resolutionExample(item: Escalation): string {
  const context = item.context ?? {}
  if (item.reason_code === 'ambiguous_mapping') {
    const candidates = Array.isArray(context.candidates) ? context.candidates : []
    const first = candidates[0]?.target_field ?? 'target_field_from_candidates'
    return JSON.stringify({
      target_field: first,
      transformations: context.suggested_transformations ?? [],
      reason: `Approve the target field that best represents source column ${context.source_field ?? 'source_field'}.`,
    }, null, 2)
  }
  if (item.reason_code === 'llm_unavailable') {
    const fields = Array.isArray(context.fields) ? context.fields : []
    return JSON.stringify({
      mappings: fields.map((field: any) => ({
        source_field: field.source_field ?? 'source_column',
        target_field: field.candidates?.[0]?.target_field ?? 'target_field',
        transformations: field.transformations ?? [],
      })),
      note: 'Use only when AI reasoning is unavailable. Confirm every mapping before submitting.',
    }, null, 2)
  }
  if (item.reason_code === 'unmapped_value') {
    const values = Array.isArray(context.unresolved_values) ? context.unresolved_values : ['Currently Working']
    const allowed = Array.isArray(context.allowed_values) && context.allowed_values.length ? context.allowed_values : ['ACTIVE', 'INACTIVE']
    return JSON.stringify({
      transformations: [{
        operation: 'map_value',
        params: { mapping: Object.fromEntries(values.map((value: string) => [value, String(allowed[0])])) },
      }],
      note: `Every unresolved value must map to one of: ${allowed.join(', ')}.`,
    }, null, 2)
  }
  if (item.reason_code === 'invalid_record') {
    const firstError = Array.isArray(context.errors) ? context.errors[0] : null
    const field = firstError?.field ?? 'target_field'
    return JSON.stringify({
      corrections: {
        [field]: 'replace_with_a_real_target_compatible_value',
      },
      note: 'Replace the example placeholder with the corrected value for this migration.',
    }, null, 2)
  }
  if (item.reason_code === 'new_transformation') {
    return JSON.stringify({
      proposed_new_transformation: {
        description: 'Describe the new transformation and why it is required.',
        applies_to: context.source_field ?? 'source_field',
        example_input: context.record_value ?? 'source example',
        expected_output: 'target-compatible value',
        requires_approval: true,
      },
      approved_by_consultant: true,
    }, null, 2)
  }
  return JSON.stringify({ action: 'retry' }, null, 2)
}

function reviewInstruction(item: Escalation): string {
  const context = item.context ?? {}
  if (item.reason_code === 'ambiguous_mapping') {
    if (Array.isArray(context.available_source_fields) && context.available_source_fields.length > 0 && (!Array.isArray(context.candidates) || context.candidates.length === 0)) {
      return `Choose which uploaded source column should populate ${String(context.target_field ?? 'this target field')}.`;
    }
    return 'Choose the source-to-target mapping that best matches the client data.';
  }
  if (item.reason_code === 'conflicting_values') return 'Choose which source value should be kept for the target field.';
  if (item.reason_code === 'unmapped_value') return 'Choose the target value for each unresolved source value.';
  if (item.reason_code === 'new_transformation') return 'Review the proposed transformation and approve it if it matches the intended business rule.';
  if (item.reason_code === 'llm_unavailable') return 'Choose the best available mapping below. The agent could not obtain AI reasoning.';
  return 'Review the details below and provide the decision needed to continue.';
}

function EscalationContext({ item }: { item: Escalation }) {
  const context = item.context ?? {}
  const candidates = Array.isArray(context.candidates)
    ? context.candidates as Array<{ target_field?: string; score?: number; reason?: string }>
    : []
  const errors = Array.isArray(context.errors)
    ? context.errors as Array<{ field?: string; code?: string; message?: string }>
    : []
  const recordData = context.record && typeof context.record === 'object' ? context.record as Record<string, unknown> : {}
  const values = Array.isArray(context.values)
    ? context.values as Array<{ source?: string; value?: unknown }>
    : []
  const fields = Array.isArray(context.fields) ? context.fields as Array<any> : []
  const recordSummary = context.record_summary && typeof context.record_summary === 'object' ? context.record_summary as Record<string, unknown> : null
  const sourceFiles = Array.isArray(context.source_files) ? context.source_files as string[] : (context.source_file ? [String(context.source_file)] : [])

  return (
    <div className="escalation-context">
      {(context.record_key || recordSummary || sourceFiles.length > 0 || context.source_field || context.target_field) && (
        <div className="review-details-block">
          <span className="detail-label">What this review is about</span>
          <div className="detail-grid">
            {context.record_key && <div><span className="detail-label">Record</span><strong>{String(context.record_key)}</strong></div>}
            {sourceFiles.length > 0 && <div><span className="detail-label">Source file(s)</span><strong>{sourceFiles.join(', ')}</strong></div>}
            {context.source_field && <div><span className="detail-label">Source field</span><strong>{String(context.source_field)}</strong></div>}
            {context.target_field && <div><span className="detail-label">Target field</span><strong>{String(context.target_field)}</strong></div>}
          </div>
          {recordSummary && <div className="record-summary">
            {Object.entries(recordSummary).map(([key, value]) => <div key={key}><span>{key.replaceAll('_', ' ')}</span><strong>{String(value)}</strong></div>)}
          </div>}
        </div>
      )}
      {context.error && <div className="issue-box"><span className="detail-label">What happened</span><div>{String(context.error)}</div></div>}
      {context.reason && <div className="issue-box"><span className="detail-label">Why review is needed</span><div>{String(context.reason)}</div></div>}
      {context.resolution_note && <div className="notice-box"><span className="detail-label">What you need to do</span><div>{String(context.resolution_note)}</div></div>}
      {context.record_value !== undefined && <div className="value-box"><span className="detail-label">Source value</span><code>{String(context.record_value)}</code></div>}

      {fields.length > 0 && (
        <div className="field-review-list">
          <span className="detail-label">Mappings needing a decision</span>
          {fields.map((field, index) => {
            const source = String(field.source_field ?? `Field ${index + 1}`)
            const fieldCandidates = Array.isArray(field.candidates) ? field.candidates : []
            return (
              <div className="field-review" key={`${source}-${index}`}>
                <strong>{source}</strong>
                <span className="muted">Top candidate: {String(fieldCandidates[0]?.target_field ?? 'No safe candidate')}</span>
                <div className="candidate-mini-list">
                  {fieldCandidates.slice(0, 3).map((candidate: any) => <span key={String(candidate.target_field)}>{String(candidate.target_field)} · {Math.round(Number(candidate.score ?? 0) * 100)}%</span>)}
                </div>
              </div>
            )
          })}
        </div>
      )}

      {candidates.length > 0 && (
        <div className="candidate-list">
          <span className="detail-label">Agent candidates</span>
          {candidates.slice(0, 4).map((candidate) => {
            const score = Math.max(0, Math.min(1, Number(candidate.score ?? 0)))
            return (
              <div className="candidate-row" key={candidate.target_field}>
                <div><div className="candidate-name">{candidate.target_field ?? 'Unknown'}</div>{candidate.reason && <small className="muted">{candidate.reason}</small>}</div>
                <div className="confidence-bar"><span style={{ width: `${score * 100}%` }} /></div>
                <div className="confidence-value">{Math.round(score * 100)}%</div>
              </div>
            )
          })}
        </div>
      )}

      {context.allowed_values && <div className="value-list"><span className="detail-label">Allowed target values</span><div><code>{(context.allowed_values as string[]).join(', ')}</code></div></div>}
      {values.length > 0 && <div className="value-list"><span className="detail-label">Conflicting source values</span>{values.map((entry, index) => <div key={`${entry.source}-${index}`}><strong>{entry.source ?? 'Source'}</strong><code>{String(entry.value)}</code></div>)}</div>}
      {context.unresolved_values && <div className="value-list"><span className="detail-label">Values that need mapping</span>{(context.unresolved_values as string[]).map((value) => <div key={value}><code>{value}</code></div>)}</div>}
      {errors.length > 0 && <div className="validation-list"><span className="detail-label">Validation checks</span>{errors.map((error, index) => {
        const field = String(error.field ?? '')
        const currentValue = field ? recordData[field] : undefined
        return <div key={`${field}-${index}`}><strong>{field || 'Field'}</strong><span>{currentValue === undefined || currentValue === null || currentValue === '' ? 'Current value: missing' : `Current value: ${String(currentValue)}`} — {error.message ?? error.code}</span></div>
      })}</div>}
    </div>
  )
}

function DataTable({ rows }: { rows: Array<Record<string, any>> }) {
  const columns = useMemo(() => {
    const keys = new Set<string>()
    rows.forEach((row) => Object.keys(row).forEach((key) => keys.add(key)))
    return Array.from(keys)
  }, [rows])
  if (!rows.length) return <div className="empty-table">No rows available.</div>
  return (
    <div className="table-scroll">
      <table>
        <thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead>
        <tbody>
          {rows.map((row, index) => <tr key={index}>{columns.map((column) => <td key={column}>{formatCell(row[column])}</td>)}</tr>)}
        </tbody>
      </table>
    </div>
  )
}

function formatCell(value: unknown) {
  if (value === null || value === undefined || value === '') return <span className="empty-cell">—</span>
  if (typeof value === 'object') return <code>{JSON.stringify(value)}</code>
  return String(value)
}

function ResultsPage({ results, onBack }: { results: MigrationResults; onBack: () => void }) {
  const sourceEntries = Object.entries(results.source_tables)
  const targetEntries = Object.entries(results.target_tables)
  return (
    <main className="page-shell">
      <header className="page-topbar">
        <div className="brand-mark"><span className="brand-dot" /> Migration Copilot</div>
        <div className="nav-actions"><button className="secondary-button" onClick={onBack}>Back to migration</button></div>
      </header>
      <section className="results-hero card">
        <div>
          <p className="eyebrow">FINAL RESULTS</p>
          <h1>Migration completed</h1>
          <p className="muted">Review the original source data, canonical target data, live target API, and the audit trail.</p>
        </div>
        <span className="success-badge">Completed</span>
      </section>

      <section className="card">
        <div className="section-heading"><div><p className="eyebrow small">SOURCE DATA</p><h2>What the client supplied</h2></div><span className="counter">{sourceEntries.length} file(s)</span></div>
        <div className="results-grid">
          {sourceEntries.map(([filename, rows]) => <article className="data-panel" key={filename}><div className="data-panel-heading"><strong>{filename}</strong><span>{rows.length} rows</span></div><DataTable rows={rows} /></article>)}
        </div>
      </section>

      <section className="card">
        <div className="section-heading"><div><p className="eyebrow small">TARGET DATA</p><h2>Canonical data written to the target</h2></div><span className="counter">{targetEntries.length} entity type(s)</span></div>
        <div className="results-grid">
          {targetEntries.map(([entity, rows]) => <article className="data-panel" key={entity}><div className="data-panel-heading"><strong>{entity}</strong><span>{rows.length} records</span></div><DataTable rows={rows} /></article>)}
        </div>
        {targetEntries.length === 0 && <div className="notice-box">No target records are currently present. This is expected after a complete rollback.</div>}
      </section>

      <section className="card">
        <div className="section-heading"><div><p className="eyebrow small">TARGET API</p><h2>Open the actual target data</h2></div></div>
        <div className="api-link-list">
          {Object.entries(results.target_urls).map(([entity, url]) => <a href={url} target="_blank" rel="noreferrer" key={entity}><span>{entity}</span><span>{url} ↗</span></a>)}
          {!Object.keys(results.target_urls).length && <span className="muted">No target entities are currently available.</span>}
        </div>
      </section>
    </main>
  )
}

export default function Dashboard() {
  const [files, setFiles] = useState<File[]>([])
  const [targetSchema, setTargetSchema] = useState<File | null>(null)
  const [migrationId, setMigrationId] = useState('')
  const [migration, setMigration] = useState<MigrationSummary | null>(null)
  const [escalations, setEscalations] = useState<Escalation[]>([])
  const [records, setRecords] = useState<MigrationRecord[]>([])
  const [audit, setAudit] = useState<AuditEvent[]>([])
  const [events, setEvents] = useState<AgentEvent[]>([])
  const [results, setResults] = useState<MigrationResults | null>(null)
  const [resolutionText, setResolutionText] = useState<Record<string, string>>({})
  const [mappingSourceSelection, setMappingSourceSelection] = useState<Record<string, string>>({})
  const [valueSelections, setValueSelections] = useState<Record<string, Record<string, string>>>({})
  const [recordCorrections, setRecordCorrections] = useState<Record<string, Record<string, string>>>({})
  const [resolvingId, setResolvingId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [pushing, setPushing] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [rollingBack, setRollingBack] = useState(false)
  const [retryingId, setRetryingId] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [view, setView] = useState<'migration' | 'results'>(() => window.location.hash === '#results' ? 'results' : 'migration')
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const previousStatus = useRef<string | null>(null)

  const openEscalations = useMemo(() => escalations.filter((item) => item.status === 'OPEN'), [escalations])
  const retryable = useMemo(() => records.filter((record) => record.status === 'PUSH_FAILED'), [records])
  const primaryTargetRecords = useMemo(() => {
    const current = records.filter((record) => ['READY_TO_PUSH', 'PUSHED', 'PUSH_FAILED', 'ROLLED_BACK'].includes(record.status))
    return current.length ? current : records
  }, [records])
  const lifecycle = progressInfo(migration)
  const migrationIsActive = Boolean(migration && !['COMPLETED', 'ROLLED_BACK', 'STOPPED', 'FAILED'].includes(migration.status))
  const showProgressLoader = Boolean(migration && lifecycle.progress < 100 && !['STOPPED', 'FAILED', 'ROLLED_BACK'].includes(migration.status))

  const openResults = async () => {
    if (!migrationId) return
    try {
      const result = await getResults(migrationId)
      setResults(result)
      window.location.hash = 'results'
      setView('results')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to load final results')
    }
  }

  useEffect(() => {
    const handleHash = () => setView(window.location.hash === '#results' ? 'results' : 'migration')
    window.addEventListener('hashchange', handleHash)
    return () => window.removeEventListener('hashchange', handleHash)
  }, [])

  function auditMessage(item: AuditEvent): string {
    if (item.action === 'MIGRATION_CREATED') return 'Source files and target schema validated.'
    if (item.action === 'MIGRATION_STARTED') return 'Migration started.'
    if (item.action === 'AGENT_STATUS') {
      const stageMap: Record<string, string> = {
        PROFILING: 'Reading source files', MAPPING: 'Resolving field mappings', RECONCILING: 'Merging duplicate records', VALIDATING: 'Validating transformed data', PUSHING: 'Pushing the changes',
      }
      return stageMap[item.reason] ?? statusLabel(item.reason)
    }
    if (item.action === 'AGENT_CONSULTANT_REVIEW') return 'Review needed — check Consultant Review.'
    if (item.action === 'AGENT_RUN_COMPLETED') return item.reason === 'READY_TO_PUSH' ? 'Target data is ready to push.' : `Agent run completed: ${statusLabel(item.reason)}.`
    if (item.action === 'ESCALATION_RESOLVED') return 'Consultant decision accepted.'
    if (item.action === 'TARGET_PUSH_FAILED') return `Target push failed: ${item.reason}`
    if (item.action === 'TARGET_RETRY_SUCCEEDED') return 'Target record retry succeeded.'
    if (item.action === 'ROLLBACK_COMPLETE') return item.reason
    if (item.action === 'MIGRATION_PUSH_COMPLETE') return item.reason === 'COMPLETED' ? 'All target records were accepted.' : 'Target push finished with failed record(s).'
    if (item.action === 'MIGRATION_STOPPED') return item.reason.includes('restart') ? 'Migration stopped because the backend restarted.' : 'Migration stopped by consultant.'
    return item.reason || item.action.replaceAll('_', ' ').toLowerCase()
  }

  const mergeHistoricalActivity = (auditData: AuditEvent[], current: AgentEvent[]): AgentEvent[] => {
    const history = auditData.map((item) => ({ type: 'history', migration_id: migrationId, message: auditMessage(item), source: 'history' as const, timestamp: item.created_at }))
    const combined = [...history, ...current.filter((item) => item.source === 'live')]
    const seen = new Set<string>()
    return combined.filter((item) => {
      const key = `${item.timestamp ?? ''}|${item.type}|${item.message ?? ''}`
      if (seen.has(key)) return false
      seen.add(key)
      return true
    }).slice(-160)
  }

  const refresh = async (id: string) => {
    const [summary, escalationData, recordData, auditData] = await Promise.all([getMigration(id), getEscalations(id), getRecords(id), getAudit(id)])
    setMigration(summary)
    setEscalations(escalationData)
    setRecords(recordData)
    setAudit(auditData)
    setEvents((current) => mergeHistoricalActivity(auditData, current))
    if (summary.status === 'COMPLETED' && previousStatus.current !== 'COMPLETED') {
      void openResults()
    }
    previousStatus.current = summary.status
  }

  const queueRefresh = (id: string) => {
    if (refreshTimer.current) clearTimeout(refreshTimer.current)
    refreshTimer.current = setTimeout(() => { void refresh(id) }, 250)
  }

  useEffect(() => {
    if (!migrationId) return
    const source = streamMigrationEvents(migrationId, (event) => {
      try {
        const parsed = JSON.parse(event.data) as AgentEvent
        if (parsed.type === 'keepalive') return
        parsed.source = 'live'
        parsed.timestamp = new Date().toISOString()
        setEvents((prev) => [...prev, parsed].slice(-160))
        queueRefresh(migrationId)
      } catch {
        // Ignore malformed events.
      }
    })
    void (async () => {
      try {
        await refresh(migrationId)
        const latest = await getMigration(migrationId)
        if (latest.status === 'CREATED') {
          await startMigration(migrationId)
          await refresh(migrationId)
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unable to restore migration')
      }
    })()
    return () => {
      source.close()
      if (refreshTimer.current) clearTimeout(refreshTimer.current)
    }
  }, [migrationId])

  useEffect(() => {
    if (!migrationId || !migrationIsActive) return
    const warnOnExit = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    const stopOnPageHide = () => stopMigrationOnUnload(migrationId)
    window.addEventListener('beforeunload', warnOnExit)
    window.addEventListener('pagehide', stopOnPageHide)
    return () => {
      window.removeEventListener('beforeunload', warnOnExit)
      window.removeEventListener('pagehide', stopOnPageHide)
    }
  }, [migrationId, migrationIsActive])

  function clearMigrationState() {
    window.location.hash = ''
    setMigrationId('')
    setMigration(null)
    setEscalations([])
    setRecords([])
    setAudit([])
    setEvents([])
    setResults(null)
    setResolutionText({})
    setFiles([])
    setTargetSchema(null)
    setError('')
    setNotice('')
    previousStatus.current = null
  }

  async function handleNewMigration() {
    if (migrationId && migrationIsActive) {
      const confirmed = window.confirm('A migration is still in progress. Stop it and start a new migration?')
      if (!confirmed) return
      try { await stopMigration(migrationId) } catch (err) { setError(err instanceof Error ? err.message : 'Unable to stop the current migration'); return }
    }
    clearMigrationState()
  }

  async function handleCreate() {
    setError('')
    setNotice('')
    setEvents([])
    if (!files.length) return setError('Select at least one CSV/XLSX source file.')
    if (!targetSchema) return setError('Select the target JSON/YAML schema.')
    setBusy(true)
    try {
      const result = await createMigration(files, targetSchema)
      setMigrationId(result.migration_id)
      setNotice('Migration created. The agent will start automatically.')
    } catch (err) { setError(err instanceof Error ? err.message : 'Unable to create migration') }
    finally { setBusy(false) }
  }

  async function handleStart() {
    if (!migrationId) return
    setError('')
    setNotice('')
    try {
      await startMigration(migrationId)
      setNotice('Agent resumed. Watching the live activity panel for progress.')
      await refresh(migrationId)
    } catch (err) { setError(err instanceof Error ? err.message : 'Unable to start migration') }
  }

  async function handleStop() {
    if (!migrationId || stopping) return
    const confirmed = window.confirm('Force-stop this migration? Any agent work still in progress will be cancelled.')
    if (!confirmed) return
    setError('')
    setNotice('')
    setStopping(true)
    try {
      await stopMigration(migrationId)
      await refresh(migrationId)
      setNotice('Migration stopped. No further agent actions will run for this migration.')
    } catch (err) { setError(err instanceof Error ? err.message : 'Unable to stop migration') }
    finally { setStopping(false) }
  }

  async function handleResolve(item: Escalation, resolution: Record<string, unknown>) {
    if (resolvingId) return
    if (!meaningfulResolution(resolution)) return setError('Resolution cannot be empty. Choose a concrete target field, mapping, or transformation.')
    setError('')
    setNotice('')
    const previous = escalations
    setResolvingId(item.id)
    setEscalations((prev) => prev.map((existing) => existing.id === item.id ? { ...existing, status: 'RESOLVED', resolution } : existing))
    try {
      await resolveEscalation(item.id, resolution)
      setResolutionText((prev) => ({ ...prev, [item.id]: '' }))
      setNotice('Consultant decision accepted. The agent is resuming automatically.')
      queueRefresh(migrationId)
    } catch (err) {
      setEscalations(previous)
      setError(err instanceof Error ? err.message : 'Unable to resolve consultant review')
    } finally { setResolvingId(null) }
  }

  async function handleSourceMappingResolve(item: Escalation) {
    const sourceField = mappingSourceSelection[item.id]?.trim()
    const targetField = String(item.context?.target_field ?? '').trim()
    if (!sourceField || !targetField) return setError('Choose a source field before continuing.')
    await handleResolve(item, { source_field: sourceField, target_field: targetField, reason: 'Consultant selected the source column for the required target field.' })
  }

  async function handleValueMappingResolve(item: Escalation) {
    const selections = valueSelections[item.id] ?? {}
    const unresolved = Array.isArray(item.context?.unresolved_values) ? item.context.unresolved_values as string[] : []
    const missing = unresolved.find((value) => !selections[String(value)])
    if (missing) return setError(`Choose a target value for \"${missing}\" before continuing.`)
    const transformations = [{ operation: 'map_value', params: { mapping: selections } }]
    await handleResolve(item, { transformations, reason: 'Consultant approved the source-to-target value mapping.' })
  }

  async function handleSuggestedTransformationResolve(item: Escalation) {
    const transformations = Array.isArray(item.suggested_resolution?.transformations) ? item.suggested_resolution?.transformations : []
    if (!transformations.length) return setError('No suggested transformation is available to approve.')
    await handleResolve(item, { transformations, reason: 'Consultant approved the suggested transformation.' })
  }

  async function handleInvalidRecordResolve(item: Escalation) {
    const errors = Array.isArray(item.context?.errors) ? item.context.errors as Array<{ field?: string; code?: string; message?: string }> : []
    const missingFields = Array.isArray(item.context?.missing_fields) ? item.context.missing_fields as string[] : []
    const fields = Array.from(new Set([
      ...missingFields,
      ...errors.map((error) => String(error.field ?? '')).filter(Boolean),
    ]))
    const corrections = recordCorrections[item.id] ?? {}
    const missing = fields.find((field) => !String(corrections[field] ?? '').trim())
    if (missing) return setError(`Provide a value for ${missing.replaceAll('_', ' ')} before continuing.`)
    await handleResolve(item, {
      corrections,
      reason: `Consultant corrected ${fields.map((field) => field.replaceAll('_', ' ')).join(', ')} for record ${String(item.context?.record_key ?? 'the selected record')}.`,
    })
  }

  async function handleCustomResolve(item: Escalation) {
    const text = resolutionText[item.id]?.trim() ?? ''
    if (!text) return setError('Enter a non-empty JSON resolution before applying it.')
    try {
      const parsed = JSON.parse(text) as Record<string, unknown>
      if (!meaningfulResolution(parsed)) throw new Error('Resolution contains only empty values.')
      await handleResolve(item, parsed)
    } catch (err) {
      setError(err instanceof Error && err.message !== 'Unexpected end of JSON input' ? err.message : 'Resolution must be valid, non-empty JSON.')
    }
  }

  async function handlePush() {
    if (pushing || !migrationId) return
    setError('')
    setNotice('')
    setPushing(true)
    try {
      const result = await pushMigration(migrationId) as { success?: number; failed?: number }
      await refresh(migrationId)
      const failed = result.failed ?? 0
      setNotice(failed ? `${failed} record(s) failed. Please check Retry below.` : 'All target records were accepted. Opening final results.')
      if (!failed) await openResults()
    } catch (err) { setError(err instanceof Error ? err.message : 'Unable to push migration') }
    finally { setPushing(false) }
  }

  async function handleRetry(recordId: string) {
    if (retryingId) return
    setError('')
    setNotice('')
    setRetryingId(recordId)
    try {
      await retryRecord(recordId)
      await refresh(migrationId)
      const latest = await getMigration(migrationId)
      if (latest.status === 'COMPLETED') await openResults()
      else setNotice('Retry completed. Check the target status for any remaining failures.')
    } catch (err) { setError(err instanceof Error ? err.message : 'Retry failed') }
    finally { setRetryingId(null) }
  }

  async function handleRetryAll() {
    if (retryable.length === 0 || retryingId) return
    for (const record of retryable) {
      await handleRetry(record.id)
    }
  }

  async function handleRollback() {
    if (rollingBack) return
    setError('')
    setNotice('')
    setRollingBack(true)
    try {
      const result = await rollbackMigration(migrationId) as { deleted?: number; failed?: number }
      await refresh(migrationId)
      setNotice(`Rollback finished: ${result.deleted ?? 0} target record(s) removed, ${result.failed ?? 0} failed.`)
      if (!result.failed) await openResults()
    } catch (err) { setError(err instanceof Error ? err.message : 'Unable to rollback migration') }
    finally { setRollingBack(false) }
  }

  const statusLabelText = statusLabel(migration?.status ?? 'Ready')
  const canManuallyStart = migration ? ['PAUSED', 'CONSULTANT_REVIEW'].includes(migration.status) : false
  const canRollback = migration ? ['COMPLETED', 'PARTIAL_FAILURE', 'ROLLBACK_PARTIAL_FAILURE'].includes(migration.status) : false
  const showStop = migrationIsActive

  if (view === 'results' && results) return <ResultsPage results={results} onBack={() => { window.location.hash = ''; setView('migration') }} />

  return (
    <main className="page-shell">
      <header className="page-topbar">
        <div className="brand-mark"><span className="brand-dot" /> Migration Copilot</div>
        <div className="topbar-actions">
          {migration?.status === 'COMPLETED' && <button className="secondary-button" onClick={() => void openResults()}>View final results</button>}
          {migration && <button className="secondary-button" onClick={() => void handleNewMigration()}>Start new migration</button>}
        </div>
      </header>

      <section className="hero">
        <div>
          <p className="eyebrow">DARWINBOX FDE ASSESSMENT</p>
          <h1>Migration Copilot</h1>
          <p>AI-assisted client data migration with controlled autonomy and human supervision.</p>
        </div>
        <div className="status-pill">{statusLabelText}</div>
      </section>

      {migration && (
        <section className={`progress-card ${lifecycle.progress >= 100 ? 'progress-complete' : ''}`}>
          <div className="progress-topline"><span className="progress-status"><span className={showProgressLoader ? 'progress-spinner' : 'progress-check'}>{showProgressLoader ? '' : '✓'}</span>{lifecycle.message}</span><strong>{lifecycle.progress}%</strong></div>
          <div className="progress-track"><span style={{ width: `${lifecycle.progress}%` }} /></div>
          <div className="progress-bottom"><span>{lifecycle.nextAction}</span>{openEscalations.length > 0 && <strong className="attention-link">{openEscalations.length} review item(s) need attention</strong>}{retryable.length > 0 && <strong className="attention-link danger">{retryable.length} record(s) need retry</strong>}</div>
        </section>
      )}

      {(error || notice) && <div className={error ? 'error-banner' : 'notice-banner'}>{error || notice}</div>}

      <section className="grid">
        <div className="card">
          <p className="eyebrow small">START HERE</p>
          <h2>Migration inputs</h2>
          <p className="muted">Upload the client's source files and their target schema. The agent discovers the mappings and transformations.</p>
          <input type="file" multiple accept=".csv,.xlsx" onChange={(event) => setFiles(Array.from(event.target.files ?? []))} />
          <div className="file-list">{files.map((file) => <span key={file.name}>{file.name}</span>)}</div>
          <label className="schema-upload">Target JSON/YAML schema
            <input type="file" accept=".json,.yaml,.yml" onChange={(event) => setTargetSchema(event.target.files?.[0] ?? null)} />
          </label>
          {targetSchema && <div className="selected-schema">Using: {targetSchema.name}</div>}
          {!migration && <button onClick={handleCreate} disabled={busy}>{busy ? 'Creating…' : 'Create migration'}</button>}
          {migration && <div className="input-note">Migration state is persisted on the server. Closing this page while work is active asks for confirmation and stops the migration if you leave.</div>}
        </div>

        <div className="card">
          <p className="eyebrow small">CONTROL</p>
          <h2>Migration status</h2>
          {!migration ? <p className="muted">No migration created yet.</p> : <>
            <div className="stats">
              <div><strong>{statusLabelText}</strong><span>Status</span></div>
              <div><strong>{migration.stats.files ?? 0}</strong><span>Files</span></div>
              <div><strong>{migration.stats.records ?? 0}</strong><span>Source rows</span></div>
              <div><strong>{migration.stats.ready_to_push ?? 0}</strong><span>Ready to push</span></div>
            </div>
            <p className="mono">Migration {migration.migration_id}</p>
            <div className="button-row">
              {canManuallyStart && <button onClick={handleStart}>Resume agent</button>}
              {showStop && <button className="danger-button" onClick={() => void handleStop()} disabled={stopping}>{stopping ? 'Stopping…' : 'Stop migration'}</button>}
              <button onClick={handlePush} disabled={migration.status !== 'READY_TO_PUSH' || pushing}>{pushing ? 'Pushing changes…' : 'Push to target'}</button>
              {retryable.length > 0 && <button className="warning-button" onClick={() => void handleRetryAll()} disabled={Boolean(retryingId)}>Retry {retryable.length} failed</button>}
              <button className="secondary-button" onClick={handleRollback} disabled={!canRollback || rollingBack}>{rollingBack ? 'Rolling back…' : 'Rollback migration'}</button>
            </div>
          </>}
        </div>
      </section>

      {migration && <section className="review-grid">
        <section className="card review-card">
          <div className="section-heading"><div><p className="eyebrow small">HUMAN-IN-THE-LOOP</p><h2>Consultant review</h2></div><span className="counter">{openEscalations.length} open</span></div>
          <p className="muted review-intro">Only decisions the agent cannot safely make on its own appear here.</p>
          {openEscalations.length === 0 ? <div className="empty-state success-state"><strong>No consultant action needed</strong><span>The agent is operating autonomously.</span></div> : <div className="escalation-list">
            {openEscalations.map((item) => {
              const candidates = Array.isArray(item.context?.candidates) ? item.context.candidates as Array<{ target_field?: string }> : []
              const options = candidates.map((candidate) => candidate.target_field).filter(Boolean) as string[]
              const availableSources = Array.isArray(item.context?.available_source_fields) ? item.context.available_source_fields as string[] : []
              const unresolvedValues = Array.isArray(item.context?.unresolved_values) ? item.context.unresolved_values as string[] : []
              const allowedValues = Array.isArray(item.context?.allowed_values) ? item.context.allowed_values as string[] : []
              const selectedValues = valueSelections[item.id] ?? {}
              const resolving = resolvingId === item.id
              const invalidErrors = Array.isArray(item.context?.errors) ? item.context.errors as Array<{ field?: string; code?: string; message?: string }> : []
              const invalidFields = Array.from(new Set([
                ...(Array.isArray(item.context?.missing_fields) ? item.context.missing_fields as string[] : []),
                ...invalidErrors.map((entry) => String(entry.field ?? '')).filter(Boolean),
              ]))
              const currentCorrections = recordCorrections[item.id] ?? {}
              const showAdvanced = !['ambiguous_mapping', 'conflicting_values', 'unmapped_value', 'new_transformation', 'target_api_failure', 'invalid_record'].includes(item.reason_code)
              return <div className="escalation" key={item.id}>
                <div className="escalation-title"><div><strong>{item.title}</strong><span className="reason-label">{reasonLabel(item.reason_code)}</span></div>{resolving && <span className="working-badge">Submitting…</span>}</div>
                <EscalationContext item={item} />
                <div className="notice-box"><span className="detail-label">What you need to do</span><div>{reviewInstruction(item)}</div></div>
                {item.reason_code === 'target_api_failure' && <div className="review-action-note">The target system rejected this record. Use Retry after checking the error details.</div>}

                {item.reason_code === 'ambiguous_mapping' && options.length > 0 && (
                  <div className="button-row">
                    {options.slice(0, 4).map((option) => <button key={option} disabled={resolving} onClick={() => void handleResolve(item, { target_field: option })}>Use {option}</button>)}
                  </div>
                )}

                {item.reason_code === 'ambiguous_mapping' && options.length === 0 && availableSources.length > 0 && (
                  <div className="button-row">
                    <select value={mappingSourceSelection[item.id] ?? ''} onChange={(event) => setMappingSourceSelection((prev) => ({ ...prev, [item.id]: event.target.value }))} disabled={resolving}>
                      <option value="">Choose a source column</option>
                      {availableSources.map((source) => <option key={source} value={source}>{source}</option>)}
                    </select>
                    <button disabled={resolving || !mappingSourceSelection[item.id]} onClick={() => void handleSourceMappingResolve(item)}>Use selected source</button>
                  </div>
                )}

                {item.reason_code === 'conflicting_values' && Array.isArray(item.context?.values) && (
                  <div className="button-row">
                    {(item.context.values as Array<{ source?: string; value?: unknown }>).map((entry, index) => <button key={`${entry.source}-${index}`} disabled={resolving} onClick={() => void handleResolve(item, { corrections: { [String(item.context?.field ?? '')]: entry.value }, reason: `Use value from ${entry.source ?? 'selected source'}.` })}>Use {String(entry.source ?? 'source')} · {String(entry.value)}</button>)}
                  </div>
                )}

                {item.reason_code === 'unmapped_value' && unresolvedValues.length > 0 && allowedValues.length > 0 && (
                  <div className="field-review-list">
                    {unresolvedValues.map((value) => <div className="field-review" key={String(value)}>
                      <strong>{String(value)}</strong>
                      <select value={selectedValues[String(value)] ?? ''} onChange={(event) => setValueSelections((prev) => ({ ...prev, [item.id]: { ...(prev[item.id] ?? {}), [String(value)]: event.target.value } }))} disabled={resolving}>
                        <option value="">Choose target value</option>
                        {allowedValues.map((allowed) => <option key={allowed} value={allowed}>{allowed}</option>)}
                      </select>
                    </div>)}
                    <button disabled={resolving || unresolvedValues.some((value) => !selectedValues[String(value)])} onClick={() => void handleValueMappingResolve(item)}>Apply value mapping</button>
                  </div>
                )}

                {item.reason_code === 'invalid_record' && invalidFields.length > 0 && (
                  <div className="correction-panel">
                    <span className="detail-label">Provide the correction for this record</span>
                    {invalidFields.map((field) => {
                      const validation = invalidErrors.find((entry) => String(entry.field ?? '') === field)
                      return <div className="correction-row" key={field}>
                        <div>
                          <strong>{field.replaceAll('_', ' ')}</strong>
                          <span className="muted">{validation?.message ?? 'Value is required or invalid.'}</span>
                        </div>
                        <input
                          className="resolution-input"
                          value={currentCorrections[field] ?? ''}
                          placeholder={field === 'email' ? 'Enter the employee email address' : `Enter ${field.replaceAll('_', ' ')}`}
                          onChange={(event) => setRecordCorrections((prev) => ({ ...prev, [item.id]: { ...(prev[item.id] ?? {}), [field]: event.target.value } }))}
                          disabled={resolving}
                        />
                      </div>
                    })}
                    <button disabled={resolving || invalidFields.some((field) => !String(currentCorrections[field] ?? '').trim())} onClick={() => void handleInvalidRecordResolve(item)}>
                      {resolving ? 'Submitting…' : 'Apply correction for this record'}
                    </button>
                  </div>
                )}

                {item.reason_code === 'new_transformation' && <div className="button-row"><button disabled={resolving} onClick={() => void handleSuggestedTransformationResolve(item)}>Approve suggested transformation</button></div>}

                {showAdvanced && (
                  <details className="json-resolution">
                    <summary>Advanced technical resolution</summary>
                    <p className="muted">Use this only when the standard review actions do not cover the decision. This is intended for technical users.</p>
                    <pre>{resolutionExample(item)}</pre>
                    <button className="secondary-button" onClick={() => setResolutionText((prev) => ({ ...prev, [item.id]: resolutionExample(item) }))} disabled={resolving}>Load example</button>
                    <textarea className="resolution-box" aria-label={`Technical resolution for ${item.title}`} placeholder="Enter a concrete, non-empty JSON resolution…" value={resolutionText[item.id] ?? ''} onChange={(event) => setResolutionText((prev) => ({ ...prev, [item.id]: event.target.value }))} disabled={resolving} />
                    <button onClick={() => void handleCustomResolve(item)} disabled={resolving}>{resolving ? 'Submitting…' : 'Apply technical resolution'}</button>
                  </details>
                )}
              </div>
            })}
          </div>}
        </section>

        <aside className="card activity-card">
          <div className="section-heading"><div><p className="eyebrow small">OBSERVABILITY</p><h2>Agent activity</h2></div><span className="live-badge">Live</span></div>
          <p className="muted review-intro">A concise record of what the agent is doing, waiting for, and completing.</p>
          <div className="activity-log">
            {events.length === 0 && <span className="muted">Waiting for migration activity…</span>}
            {events.map((event, index) => <div key={`${event.timestamp ?? ''}-${event.type}-${index}`}><span className="dot" /><span>{event.message ?? event.type}</span></div>)}
          </div>
        </aside>
      </section>}

      {migration && <>
        <section className="card">
          <div className="section-heading"><div><p className="eyebrow small">CURRENT TARGET STATE</p><h2>Target preview</h2></div><span className="counter">{primaryTargetRecords.length} canonical record(s)</span></div>
          <div className="record-list">
            {primaryTargetRecords.map((record) => <div className="record" key={record.id}>
              <div><strong>{record.entity_name} · {record.target_record_key ?? record.canonical_data?.employee_id ?? record.id}</strong><span className={`record-status ${record.status.toLowerCase()}`}>{statusLabel(record.status)}</span></div>
              <div className="mono">{pretty(record.canonical_data)}</div>
              {record.status === 'PUSH_FAILED' && <div className="record-actions"><span className="danger-text">Target push failed — retry is available.</span><button className="warning-button" onClick={() => void handleRetry(record.id)} disabled={retryingId === record.id}>{retryingId === record.id ? 'Retrying…' : 'Retry target push'}</button></div>}
            </div>)}
          </div>
        </section>

        <section className="card">
          <div className="section-heading"><div><p className="eyebrow small">AUDITABILITY</p><h2>Audit trail</h2></div><span className="counter">{audit.length} events</span></div>
          <div className="audit-list">
            {audit.length === 0 && <span className="muted">No events yet.</span>}
            {audit.slice(-50).map((event) => <div key={event.id}><strong>{event.action.replaceAll('_', ' ')}</strong><span>{auditMessage(event)}</span><small>{new Date(event.created_at).toLocaleTimeString()}</small></div>)}
          </div>
        </section>
      </>}
    </main>
  )
}
