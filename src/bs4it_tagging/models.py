from dataclasses import dataclass, field
from enum import Enum


class TagStatus(str, Enum):
    COMPLIANT = "COMPLIANT"
    MISSING = "MISSING"
    CONFLICT = "CONFLICT"
    ERROR = "ERROR"


class Action(str, Enum):
    NONE = "NONE"
    TAG_APPLIED = "TAG_APPLIED"
    SKIPPED = "SKIPPED"
    DRY_RUN = "DRY_RUN"


@dataclass
class ResourceRecord:
    account_id: str
    account_name: str
    region: str
    service: str
    resource_type: str
    resource_arn: str
    existing_tags: dict = field(default_factory=dict)
    status: TagStatus = TagStatus.MISSING
    action: Action = Action.NONE
    error: str | None = None
    native_id: str | None = None

    @property
    def aws_apn_id(self) -> str:
        return self.existing_tags.get("aws-apn-id", "")

    def get_tag(self, key: str) -> str:
        """Return the current value of any tag key, or empty string."""
        return self.existing_tags.get(key, "")
