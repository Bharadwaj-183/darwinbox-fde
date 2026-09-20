import pytest

from app.schemas.target_schema import EntitySchema, TargetField, TargetSchema
from app.services.entity_resolution import EntityResolutionEngine
from app.services.merge import MergeEngine
from app.tools.transformations import apply_transformation


def employee_entity() -> EntitySchema:
    return EntitySchema(
        entity_name="employee",
        source_precedence=["legacy.csv", "crm.csv"],
        fields=[
            TargetField(name="employee_id", data_type="string", required=True, unique=True),
            TargetField(name="first_name", data_type="string", required=True),
            TargetField(name="last_name", data_type="string", required=True),
            TargetField(name="email", data_type="string", required=True, unique=True),
            TargetField(name="joining_date", data_type="date", required=True),
            TargetField(name="department", data_type="string", required=True),
        ],
    )


def test_transformations():
    assert apply_transformation(" JOHN@ABC.COM ", "normalize_email") == "john@abc.com"
    assert apply_transformation("15/07/2024", "parse_date", {"input_format": "%d/%m/%Y"}) == "2024-07-15"
    assert apply_transformation("Aug 01, 2022", "parse_date_auto") == "2022-08-01"
    assert apply_transformation("₹1,25,000", "extract_number") == 125000


def test_resolution_matches_same_employee():
    entity = employee_entity()
    records = [
        {"employee_id": "E1", "first_name": "John", "last_name": "Smith", "email": "john@x.com"},
        {"employee_id": "E1", "first_name": "John", "last_name": "Smith", "email": "john@x.com"},
        {"employee_id": "E2", "first_name": "Ana", "last_name": "Patel", "email": "ana@x.com"},
    ]
    groups, escalations = EntityResolutionEngine().resolve(records, entity)
    assert any(g["method"] == "exact" and len(g["record_indexes"]) == 2 for g in groups)
    assert not escalations


def test_merge_conflict_resolved_by_precedence():
    entity = employee_entity()
    merged, escalations = MergeEngine().merge_group([
        ("legacy.csv", {"employee_id": "E1", "department": "Engineering"}),
        ("crm.csv", {"employee_id": "E1", "department": "Finance"}),
    ], entity)
    assert merged["department"] == "Engineering"
    assert not escalations


def test_target_schema_accepts_multiple_entities():
    schema = TargetSchema(entities=[
        EntitySchema(entity_name="employee", fields=[TargetField(name="id", data_type="string")]),
        EntitySchema(entity_name="department", fields=[TargetField(name="id", data_type="string")]),
    ])
    assert {e.entity_name for e in schema.entities} == {"employee", "department"}

import asyncio
from types import SimpleNamespace

from app.services.transformation_planner import TransformationPlanner


class FakeLLM:
    settings = SimpleNamespace(openrouter_api_key="")

    async def structured_json(self, **kwargs):
        raise AssertionError("LLM should not be called for this deterministic enum test")


def test_transformation_planner_repairs_common_date_and_enum_values():
    async def run():
        planner = TransformationPlanner(FakeLLM(), __import__("app.services.semantic", fromlist=["SemanticMatcher"]).SemanticMatcher())
        date_steps, date_issue = await planner.plan(
            source_field="DOJ",
            sample_values=["01/08/2022"],
            target_field=TargetField(name="joining_date", data_type="date"),
            proposed=[],
        )
        enum_steps, enum_issue = await planner.plan(
            source_field="Status",
            sample_values=["Active", "Currently Working"],
            target_field=TargetField(name="employment_status", data_type="string", enum_values=["ACTIVE", "INACTIVE"]),
            proposed=[],
        )
        assert any(step.operation == "parse_date_auto" for step in date_steps)
        mapping = next(step for step in enum_steps if step.operation == "map_value").params["mapping"]
        assert mapping["Active"] == "ACTIVE"
        assert mapping["Currently Working"] == "ACTIVE"
        assert date_issue is None
        assert enum_issue is None

    asyncio.run(run())


def test_mapping_fallback_prefers_exact_aliases_and_limits_review_scope():
    import asyncio
    from types import SimpleNamespace
    from app.services.mapping import MappingEngine
    from app.services.semantic import SemanticMatcher

    class NoKeyLLM:
        settings = SimpleNamespace(openrouter_api_key="")

    entity = EntitySchema(
        entity_name="employee",
        fields=[
            TargetField(name="employee_id", data_type="string", required=True, aliases=["emp id", "staff id"]),
            TargetField(name="first_name", data_type="string", required=True, aliases=["given name"]),
            TargetField(name="department", data_type="string", required=True, aliases=["dept", "org"]),
            TargetField(name="business_unit", data_type="string", required=True, aliases=["org"]),
        ],
    )

    async def run():
        engine = MappingEngine(NoKeyLLM(), SemanticMatcher())
        proposals, escalations = await engine.map_fields(
            columns=["Emp ID", "Given Name", "Org", "Unused Column"],
            sample_values={"Emp ID": ["E1"], "Given Name": ["John"], "Org": ["Engineering"], "Unused Column": ["x"]},
            inferred_types={"Emp ID": "object", "Given Name": "object", "Org": "object", "Unused Column": "object"},
            entity=entity,
        )
        # Org is ambiguous between department/business_unit, but both are plausible.
        # With no LLM key, the policy should still auto-select the best candidate.
        assert {p.source_field for p in proposals} == {"Emp ID", "Given Name", "Org"}
        assert any(p.source_field == "Org" and p.target_field == "department" for p in proposals)
        assert not any(e["context"].get("source_field") == "Unused Column" for e in escalations)

    asyncio.run(run())


def test_llm_date_parameter_alias_is_normalized():
    assert apply_transformation("15/07/2024", "parse_date", {"format": "%d/%m/%Y"}) == "2024-07-15"
    assert apply_transformation("15/07/2024", "parse_date", {"date_format": "%d/%m/%Y"}) == "2024-07-15"


def test_invalid_transformation_parameters_become_transformation_error():
    import pytest
    from app.tools.transformations import TransformationError

    with pytest.raises(TransformationError, match="Invalid parameters for transformation"):
        apply_transformation("15/07/2024", "parse_date", {"unexpected": "%d/%m/%Y"})


def test_extended_transformations():
    assert apply_transformation('  Jane   Doe  ', 'collapse_whitespace') == 'Jane Doe'
    assert apply_transformation('₹1,25,000', 'normalize_integer') == 125000
    assert apply_transformation('12.3456 USD', 'normalize_decimal', {'decimals': 2}) == 12.35
    assert apply_transformation('Engineering, Finance; HR', 'split', {'separator': ','}) == ['Engineering', 'Finance; HR']
    assert apply_transformation(['Engineering', 'Finance'], 'join', {'separator': ' | '}) == 'Engineering | Finance'
    assert apply_transformation('Working', 'normalize_boolean') is True


def test_policy_prefers_autonomy_with_plausible_fallback():
    from app.services.policy import DecisionPolicy
    from app.core.constants import Decision
    policy = DecisionPolicy()
    assert policy.mapping_decision(0.92, 0.02) == Decision.AUTO
    assert policy.mapping_decision(0.63, 0.01) == Decision.AUTO
    assert policy.mapping_decision(0.40, 0.01) == Decision.ESCALATE


def test_entity_resolution_does_not_merge_weak_name_only_matches():
    entity = employee_entity()
    records = [
        {'employee_id': 'E1', 'first_name': 'John', 'last_name': 'Smith', 'email': 'john1@x.com'},
        {'employee_id': 'E2', 'first_name': 'John', 'last_name': 'Smith', 'email': 'john2@x.com'},
    ]
    groups, escalations = EntityResolutionEngine().resolve(records, entity)
    assert len(groups) == 2
    assert escalations == []


def test_target_schema_supports_relationships_and_multiple_entities():
    from app.schemas.target_schema import EntityRelationship
    schema = TargetSchema(entities=[
        EntitySchema(
            entity_name='employee',
            fields=[TargetField(name='department_id', data_type='string')],
            relationships=[EntityRelationship(field='department_id', references_entity='department', references_field='department_id')],
        ),
        EntitySchema(
            entity_name='department',
            fields=[TargetField(name='department_id', data_type='string', required=True, unique=True)],
        ),
    ])
    assert schema.entities[0].relationships[0].references_entity == 'department'


def test_llm_client_cache_key_is_stable():
    from app.services.llm import LLMClient
    key1 = LLMClient._cache_key('openrouter', 'openrouter/free', 'system', {'x': 1})
    key2 = LLMClient._cache_key('openrouter', 'openrouter/free', 'system', {'x': 1})
    key3 = LLMClient._cache_key('openrouter', 'openrouter/free', 'system', {'x': 2})
    assert key1 == key2
    assert key1 != key3


def test_entity_detector_handles_multiple_entities_without_llm_for_clear_files():
    import asyncio
    from types import SimpleNamespace
    from app.services.entity_detector import EntityDetector
    from app.services.semantic import SemanticMatcher

    class NoKeyLLM:
        settings = SimpleNamespace(openrouter_api_key="")

    schema = TargetSchema(entities=[
        EntitySchema(entity_name='employee', fields=[
            TargetField(name='employee_id', data_type='string', aliases=['emp id', 'staff id']),
            TargetField(name='first_name', data_type='string', aliases=['given name']),
            TargetField(name='email', data_type='string', aliases=['email address']),
        ]),
        EntitySchema(entity_name='department', fields=[
            TargetField(name='department_id', data_type='string', aliases=['dept id', 'department code']),
            TargetField(name='department_name', data_type='string', aliases=['dept name', 'department']),
        ]),
    ])

    async def run():
        detector = EntityDetector(NoKeyLLM(), SemanticMatcher())
        employee, employee_errors = await detector.detect(
            filename='employees.csv',
            columns=['Employee ID', 'First Name', 'Email'],
            sample_values={'Employee ID': ['E1'], 'First Name': ['John'], 'Email': ['john@example.com']},
            target_schema=schema,
        )
        department, department_errors = await detector.detect(
            filename='departments.csv',
            columns=['Dept ID', 'Dept Name'],
            sample_values={'Dept ID': ['D1'], 'Dept Name': ['Engineering']},
            target_schema=schema,
        )
        assert employee == 'employee'
        assert department == 'department'
        assert not employee_errors
        assert not department_errors

    asyncio.run(run())



def test_ambiguous_mapping_invokes_llm_and_accepts_supported_choice():
    import asyncio
    from types import SimpleNamespace
    from app.services.mapping import MappingEngine

    class FakeLLM:
        settings = SimpleNamespace(openrouter_api_key="configured")
        called = False

        async def structured_json(self, **kwargs):
            self.called = True
            return {
                "field_mappings": [{
                    "source_field": "org_area",
                    "target_field": "department",
                    "confidence": 0.91,
                    "reason": "The value looks like an organizational department name.",
                    "transformations": [],
                    "needs_human": False,
                    "alternatives": [["business_unit", 0.50]],
                }],
                "escalations": [],
            }

    class FakeSemantic:
        def best_match(self, query, candidates, *, aliases=None):
            if query == "org_area":
                return [("department", 0.52), ("business_unit", 0.50), ("employee_id", 0.10)]
            return [(query, 1.0)] + [(candidate, 0.1) for candidate in candidates if candidate != query]

    entity = EntitySchema(
        entity_name="employee",
        fields=[
            TargetField(name="employee_id", data_type="string", required=True),
            TargetField(name="department", data_type="string", required=True),
            TargetField(name="business_unit", data_type="string", required=False),
        ],
    )

    async def run():
        llm = FakeLLM()
        engine = MappingEngine(llm, FakeSemantic())
        proposals, escalations = await engine.map_fields(
            columns=["employee_id", "org_area"],
            sample_values={"employee_id": ["E1001"], "org_area": ["Corporate Services"]},
            inferred_types={"employee_id": "string", "org_area": "string"},
            entity=entity,
        )
        assert llm.called is True
        assert any(p.source_field == "org_area" and p.target_field == "department" for p in proposals)
        assert not escalations

    asyncio.run(run())



def test_resolved_merge_conflict_is_consumed_before_re_escalation():
    from app.agent.orchestrator import MigrationAgent

    merged, unresolved = MigrationAgent._apply_merge_corrections(
        {"employee_id": "E1024", "department": "Engineering"},
        [
            {
                "reason_code": "conflicting_values",
                "context": {
                    "field": "employee_id",
                    "values": [
                        {"source": "legacy.csv", "value": "E1024"},
                        {"source": "payroll.xlsx", "value": "1024"},
                    ],
                },
            },
            {
                "reason_code": "conflicting_values",
                "context": {
                    "field": "department",
                    "values": [
                        {"source": "legacy.csv", "value": "Engineering"},
                        {"source": "payroll.xlsx", "value": "Finance"},
                    ],
                },
            },
        ],
        {"employee_id": "E1024"},
    )
    assert merged["employee_id"] == "E1024"
    assert len(unresolved) == 1
    assert unresolved[0]["context"]["field"] == "department"
