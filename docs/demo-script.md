# Suggested 5-minute Demo

1. Upload the three sample employee source files and the target schema.
2. Point out the live progress bar and the consultant-facing status message.
3. Show autonomous field mapping and cleaning happening without individual approvals.
4. Show the deliberately ambiguous `Org` field if it is surfaced; approve it once and let the agent resume.
5. Show the reconciled canonical records and explain that duplicates across files were merged by stable identity evidence.
6. Push to target. Demonstrate the intentional one-time failure for `E-FAIL`.
7. Click retry and show the status change to completed.
8. Open the final Results page: source tables, canonical target tables, target API links, and audit trail.
9. Open the target API URL and show the actual target JSON.
10. Briefly open the backend console to show an `llm_call_start` / `llm_response_received` line only if the run actually required AI reasoning.
