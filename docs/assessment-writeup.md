# Forward Deployed Engineer Assessment — Approach

## Approach

Migration Copilot is a generic customer-data migration agent. The consultant uploads multiple CSV/XLSX source files plus a user-defined JSON/YAML target schema. The system profiles each source, identifies the target entity, discovers source-to-target mappings, applies safe transformations, reconciles duplicate representations, validates canonical records, and pushes them to a mock target API.

The implementation is deterministic-first. Exact field/alias matches, strong semantic matches with compatible types, safe normalization, and strong identity matches are handled autonomously. Semantic similarity is used to rank candidate fields; the LLM is invoked only for ambiguous mappings, difficult value normalization, and other judgment calls. The LLM returns structured JSON, but Python validation/policy controls what can actually execute.

## Autonomy boundary

| Situation | Behavior |
|---|---|
| Exact/strong field alias and compatible type | Auto-resolve |
| High-confidence semantic mapping with clear margin | Auto-resolve |
| Ambiguous but plausible mapping | Ask LLM; if unavailable, use best safe candidate |
| Unmapped/ambiguous value with no safe fallback | Consultant review |
| Known transformation | Execute automatically |
| Unknown/new transformation | Consultant approval required |
| Conflicting non-null values | Apply configured source precedence; otherwise review |
| Strong duplicate identity evidence | Auto-reconcile |
| Ambiguous identity match | Consultant review |
| Target API failure | Per-record retry or rollback |

This boundary is deliberately conservative only where a wrong decision could alter customer data. The objective is safe autonomous progress rather than maximum model usage.

## What I would build next

For a production deployment I would add configurable customer-specific mapping memory, richer relationship-aware migration planning, PII masking/redaction, stronger observability/metrics, migration versioning, and provider failover for AI inference.
