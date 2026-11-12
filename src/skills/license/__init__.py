"""skill license 判定模块（与 openeuler-jenkins 门禁同源）。"""

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
from src.skills.license.runtime import (
    action_for_status,
    build_checker,
    get_license_checker,
    is_compliant,
    reset_license_checker,
)
from src.skills.license.scanner import (
    LicenseChecker,
    LicenseCheckResult,
    derive_skill_dir,
    is_protected_repo,
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
    "LicenseCheckResult",
    "LicenseChecker",
    "LicenseScanner",
    "LicenseVerdict",
    "action_for_status",
    "build_checker",
    "derive_skill_dir",
    "get_license_checker",
    "is_compliant",
    "is_protected_repo",
    "reset_license_checker",
]
