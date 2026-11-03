"""命中解析、结构化判定与原生评分测试。"""

from src.security.audit_review.analysis import (
    _declared_capabilities,
    _parse_manifest_frontmatter,
    _structural_verdict,
)


class TestStructuralRules:
    def test_manifest_parses_space_separated_allowed_tools(self):
        manifest = _parse_manifest_frontmatter(
            "---\nname: demo\nallowed-tools: Bash(man:*) Bash(read:*)\n---\n# Demo\n"
        )
        assert manifest is not None
        assert manifest["allowed-tools"] == "Bash(man:*) Bash(read:*)"

    def test_manifest_without_frontmatter_is_empty(self):
        assert _parse_manifest_frontmatter("# Demo\n") == {}

    def test_scoped_space_separated_tools_map_to_capabilities(self):
        declared = _declared_capabilities(
            {"allowed-tools": "Bash(man:*) Bash(apropos:*) Read Write"}
        )
        assert declared == {"shell", "file_read", "file_write"}

    def test_lp3_with_declaration_is_false_positive(self):
        verdict = _structural_verdict(
            {"id": "LP3", "severity": "MEDIUM"},
            {"allowed-tools": "Bash(man:*)"},
        )
        assert verdict["is_vulnerability"] is False
        assert verdict["confidence"] == 1.0

    def test_lp3_without_declaration_is_confirmed(self):
        verdict = _structural_verdict({"id": "LP3", "severity": "MEDIUM"}, {})
        assert verdict["is_vulnerability"] is True

    def test_lp1_declared_capability_is_false_positive(self):
        issue = {
            "id": "LP1",
            "severity": "HIGH",
            "explanation": (
                "The skill uses 'shell' capability that is not listed in its permissions."
            ),
        }
        verdict = _structural_verdict(issue, {"allowed-tools": "Bash(man:*)"})
        assert verdict["is_vulnerability"] is False

    def test_lp1_undeclared_capability_is_confirmed(self):
        issue = {
            "id": "LP1",
            "severity": "HIGH",
            "explanation": (
                "The skill uses 'shell' capability that is not listed in its permissions."
            ),
        }
        verdict = _structural_verdict(issue, {"allowed-tools": "Read"})
        assert verdict["is_vulnerability"] is True

    def test_lp1_without_capability_text_falls_back(self):
        assert _structural_verdict({"id": "LP1", "explanation": "weird"}, {}) is None
