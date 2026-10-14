"""Input validation for user provision tool."""

import re

_NAME_RE = re.compile(r'^[a-zA-Z0-9_-]+$')
_LABEL_RE = re.compile(r'^\d+$')


class ValidationError(ValueError):
    pass


def validate_name(value: str, field: str = "name") -> str:
    """Validate that a name contains only [a-zA-Z0-9_-]."""
    if not value:
        raise ValidationError(f"{field} must not be empty.")
    if not _NAME_RE.match(value):
        raise ValidationError(
            f"{field} '{value}' is invalid: only English letters, digits, hyphens, and underscores are allowed."
        )
    # R1.5: `--` is RESERVED as the instance/sub-service separator. A name that
    # contains it would make the served name
    # `<instance host>--<sub_service>.<domain>` unparseable back into its two
    # halves — and that split is what the card, the gateway's hostname index and
    # a reader all rely on. Refused here, at validation, so no such name is ever
    # stored.
    if "--" in value:
        raise ValidationError(
            f"{field} '{value}' is invalid: '--' is reserved as the instance/sub-service "
            f"separator and must not appear inside a name."
        )
    return value


def validate_label(value: str) -> str:
    """Validate that a label contains only digits."""
    if not _LABEL_RE.match(value):
        raise ValidationError(
            f"label '{value}' is invalid: only digits are allowed."
        )
    return value
