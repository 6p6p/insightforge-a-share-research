# No company-specific hacks property test (P9)
# Audits production code for forbidden hardcoded-company-value branches

import os
import re
from pathlib import Path

import pytest

EXEMPT_DIRS = frozenset(["tests", "scripts", "eval", "alembic", "migrations"])
NL = chr(10)


def _is_exempt(path_parts):
    return any(p in EXEMPT_DIRS for p in path_parts)


def _has_violation(line):
    low = line.lower()
    if "security_code" not in low and "company_code" not in low:
        return None
    if not re.search(r"\d{6}", line):
        return None
    if "==" not in line:
        return None
    return "security_code/company_code hardcoded value branch"


class TestNoCompanySpecificHacks:
    """Production code must NOT contain company-specific branches."""

    def test_no_security_code_or_company_code_branches(self):
        root = Path(__file__).resolve().parents[3] / "backend"
        violations = []
        for py_file in root.rglob("*.py"):
            rel = str(py_file.relative_to(root))
            parts = frozenset(rel.replace(os.sep, "/").split("/"))
            if _is_exempt(parts):
                continue
            try:
                text = py_file.read_text(encoding="utf-8")
            except Exception:
                continue
            for line_num, line in enumerate(text.split(NL), 1):
                desc = _has_violation(line)
                if desc:
                    violations.append(
                        "  "
                        + rel
                        + ":"
                        + str(line_num)
                        + ": "
                        + desc
                        + NL
                        + "    "
                        + line.strip()[:120]
                    )
        if violations:
            pytest.fail(
                "Found company-specific patterns in production code:"
                + NL
                + NL.join(violations[:10])
            )
