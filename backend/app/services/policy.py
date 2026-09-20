from app.core.config import get_settings
from app.core.constants import Decision, EscalationReason


class DecisionPolicy:
    """Safety boundary: prefer deterministic progress, use AI only for ambiguity, and review only when no safe fallback exists."""

    def __init__(self) -> None:
        self.settings = get_settings()

    def mapping_decision(self, confidence: float, margin: float) -> Decision:
        if confidence >= self.settings.auto_mapping_threshold and margin >= self.settings.auto_mapping_margin:
            return Decision.AUTO
        if confidence >= self.settings.auto_fallback_threshold:
            # This value is still plausible enough for autonomous fallback if AI
            # reasoning times out or is unavailable.
            return Decision.AUTO
        return Decision.ESCALATE

    def transformation_decision(self, *, known_operation: bool, has_safe_fallback: bool) -> tuple[Decision, str | None]:
        if known_operation or has_safe_fallback:
            return Decision.AUTO, None
        return Decision.ESCALATE, EscalationReason.NEW_TRANSFORMATION.value

    @staticmethod
    def merge_value_decision(left: object, right: object) -> tuple[Decision, str | None]:
        if left in (None, "") or right in (None, ""):
            return Decision.AUTO, None
        if str(left).strip().casefold() == str(right).strip().casefold():
            return Decision.AUTO, None
        return Decision.ESCALATE, EscalationReason.CONFLICTING_VALUES.value
