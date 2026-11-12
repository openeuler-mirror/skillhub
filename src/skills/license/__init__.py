"""skill license 判定模块（与 openeuler-jenkins 门禁同源）。"""

from src.skills.license.access_list import (
    LicenseListUnavailableError,
    load_access_list,
)
from src.skills.license.gate_engine import (
    STATUS_ALLOWED,
    STATUS_BLACK,
    STATUS_CONFLICT,
    STATUS_ERROR,
    STATUS_MISSING,
    STATUS_RESTRICTED,
    STATUS_UNKNOWN,
    LicenseAccessList,
    LicenseScanner,
    LicenseVerdict,
)

__all__ = [
    "STATUS_ALLOWED",
    "STATUS_BLACK",
    "STATUS_CONFLICT",
    "STATUS_ERROR",
    "STATUS_MISSING",
    "STATUS_RESTRICTED",
    "STATUS_UNKNOWN",
    "LicenseAccessList",
    "LicenseListUnavailableError",
    "LicenseScanner",
    "LicenseVerdict",
    "load_access_list",
]
