from typing import TypeVar
import json

from lark_oapi.api.drive.v1 import P2DriveFileBitableRecordChangedV1
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

T = TypeVar("T")


def required(value: T | None) -> T:
    """Assert a fixture result exists and narrow its type for the caller."""
    assert value is not None, "expected a non-empty fixture result"
    return value


def as_message_event(value: object) -> P2ImMessageReceiveV1:
    return P2ImMessageReceiveV1(json.loads(json.dumps(value, default=vars)))


def as_record_event(value: object) -> P2DriveFileBitableRecordChangedV1:
    return P2DriveFileBitableRecordChangedV1(json.loads(json.dumps(value, default=vars)))
