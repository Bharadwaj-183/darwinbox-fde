from enum import StrEnum


class Decision(StrEnum):
    AUTO = "AUTO"
    ESCALATE = "ESCALATE"
    REJECT = "REJECT"


class EscalationReason(StrEnum):
    AMBIGUOUS_MAPPING = "ambiguous_mapping"
    CONFLICTING_VALUES = "conflicting_values"
    INVALID_RECORD = "invalid_record"
    AMBIGUOUS_MATCH = "ambiguous_match"
    NEW_TRANSFORMATION = "new_transformation"
    LLM_UNAVAILABLE = "llm_unavailable"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    UNMAPPED_VALUE = "unmapped_value"
    TARGET_API_FAILURE = "target_api_failure"
    UNSUPPORTED_FILE = "unsupported_file"
