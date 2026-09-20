from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator


class TransformationStep(BaseModel):
    operation: str
    params: dict[str, Any] = Field(default_factory=dict)
    proposed_new_transformation: str | None = None

    @model_validator(mode="after")
    def normalize_parameter_aliases(self):
        """Normalize common LLM parameter aliases into the application's contract.

        The model must use ``input_format`` for parse_date.  Older/free model
        responses sometimes emit ``format`` or ``date_format`` instead.  We
        normalize those aliases here so one provider response cannot crash the
        transformation executor.
        """
        params = dict(self.params or {})
        if self.operation == "parse_date":
            if "input_format" not in params:
                for alias in ("format", "date_format", "inputFormat"):
                    if alias in params:
                        params["input_format"] = params.pop(alias)
                        break
            if "outputFormat" in params and "output_format" not in params:
                params["output_format"] = params.pop("outputFormat")
        self.params = params
        return self


class FieldMappingProposal(BaseModel):
    source_field: str
    target_field: str | None = None
    confidence: float = 0.0
    reason: str = ""
    transformations: list[TransformationStep] = Field(default_factory=list)
    needs_human: bool = False
    alternatives: list[tuple[str, float]] = Field(default_factory=list)


class AgentDecision(BaseModel):
    action: Literal[
        "profile",
        "identify_entity",
        "map_fields",
        "reconcile",
        "transform",
        "validate",
        "push",
        "escalate",
        "complete",
    ]
    reason: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
