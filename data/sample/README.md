# Sample demo data

The sample inputs represent the same employee entity across CSV/XLSX files with intentionally inconsistent field names and formats.

- `legacy_hr.csv` — canonical HR export plus one `E-FAIL` record used to demonstrate target API retry.
- `payroll.xlsx` — alternate employee representation with different field names and date formats.
- `crm.csv` — another representation with different identifiers/contact formatting.
- `employee_target_schema.json` — the target schema the user uploads to drive the migration.

The application itself is **not hardcoded to employees**. These files are only the demonstration dataset.
