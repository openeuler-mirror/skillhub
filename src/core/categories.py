"""Canonical skill categories and their Chinese display labels.

The database stores English category keys (e.g. "Research and Design").
The API keeps those keys for filtering/lookup purposes and exposes a separate
Chinese ``label`` for UI display, mirroring the pattern used for security
levels elsewhere.
"""

# English canonical key -> Chinese display label
CATEGORY_LABELS: dict[str, str] = {
    "Research and Design": "研究设计",
    "Development and Build": "开发构建",
    "Engineering and Compilation": "工程编译",
    "Quality and Validation": "质量验证",
    "Release and Deployment": "发布部署",
    "Monitoring and Operations": "监控运维",
    "Performance Optimization": "性能优化",
    "Security Hardening": "安全加固",
    "others": "其他分类",
}

CANONICAL_CATEGORIES = list(CATEGORY_LABELS.keys())


def category_label(category_key: str | None) -> str:
    """Return the Chinese display label for a canonical category key.

    Falls back to the raw key when it is not a known canonical category.
    """
    if not category_key:
        return ""
    return CATEGORY_LABELS.get(category_key, category_key)
