"""Tests for lib/var_scan.py (design §Env story L156-163, L177-178; GAP-13)."""

from lib.var_scan import (
    env_completeness,
    needs_env,
    scan_compose_vars,
    skeleton_env,
)


class TestScanComposeVars:
    def test_plain_var(self):
        refs = scan_compose_vars("image: nginx:${NGINX_TAG}")
        assert [(r.name, r.has_default, r.required) for r in refs] == [
            ("NGINX_TAG", False, False)
        ]

    def test_default_colon_form(self):
        refs = scan_compose_vars("${PORT:-8080}")
        assert refs[0].name == "PORT"
        assert refs[0].has_default is True

    def test_default_colonless_form(self):
        refs = scan_compose_vars("${PORT-8080}")
        assert refs[0].name == "PORT"
        assert refs[0].has_default is True

    def test_required_form(self):
        refs = scan_compose_vars('${DB_PASSWORD:?db password is required}')
        assert refs[0].name == "DB_PASSWORD"
        assert refs[0].has_default is False
        assert refs[0].required is True

    def test_nested_default(self):
        """${A:-${B:-x}} → A (default present) AND B (default present)."""
        refs = scan_compose_vars("${A:-${B:-x}}")
        by_name = {r.name: r for r in refs}
        assert "A" in by_name and "B" in by_name
        assert by_name["A"].has_default is True
        assert by_name["B"].has_default is True

    def test_nested_required(self):
        refs = scan_compose_vars("${A:-${B:?err}}")
        by_name = {r.name: r for r in refs}
        assert by_name["A"].has_default is True
        assert by_name["B"].required is True

    def test_dollar_escape_not_scanned(self):
        """$${VAR} is a literal — never counts as interpolation."""
        refs = scan_compose_vars("run: echo '$${LITERAL}' ${REAL}")
        names = [r.name for r in refs]
        assert "LITERAL" not in names
        assert "REAL" in names

    def test_triple_dollar(self):
        """$$${VAR} → literal $ + real interpolation."""
        names = [r.name for r in scan_compose_vars("$$${VAR}")]
        assert names == ["VAR"]

    def test_unclosed_ignored(self):
        assert scan_compose_vars("${UNCLOSED") == []

    def test_non_var_names_ignored(self):
        refs = scan_compose_vars("${9BAD} ${good_name}")
        assert [r.name for r in refs] == ["good_name"]


class TestNeedsEnv:
    def test_true_when_any_var(self):
        assert needs_env("services:\n  web:\n    image: x:${TAG}")

    def test_false_when_none(self):
        assert needs_env("services:\n  web:\n    image: nginx:alpine") is False

    def test_false_on_escaped_only(self):
        assert needs_env("echo '$${NOT_A_VAR}'") is False


class TestCompletenessAndSkeleton:
    def test_completeness_missing(self):
        refs = scan_compose_vars("${A} ${B:-x} ${C}")
        missing = env_completeness(refs, "A=1\n")
        assert missing == ["C"]  # A present, B has default

    def test_completeness_required_included(self):
        refs = scan_compose_vars("${A:?err}")
        assert env_completeness(refs, "") == ["A"]

    def test_skeleton_deterministic(self):
        refs = scan_compose_vars("${B} ${A} ${A}")
        assert skeleton_env(refs) == "B=\nA=\n"

    def test_skeleton_keeps_existing_values(self):
        refs = scan_compose_vars("${A}")
        assert skeleton_env(refs, "A=secret\n") == "A=\n"

    def test_skeleton_skips_defaulted(self):
        refs = scan_compose_vars("${A:-8080} ${B}")
        assert skeleton_env(refs) == "B=\n"

    def test_skeleton_empty_when_nothing_needed(self):
        assert skeleton_env(scan_compose_vars("${A:-x}")) == ""
