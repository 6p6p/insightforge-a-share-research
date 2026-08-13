"""EvaluationBundleLoader 测试（stage 7B.1.1A）：label leakage boundary + 路径防 traversal。"""

import json
from dataclasses import fields

import pytest

from app.eval.bundle import layout
from app.eval.bundle.loader import EvaluationBundleLoader
from app.eval.errors import EvalContractError


def test_execution_loader_does_not_expose_label(built_bundle) -> None:
    root, spec = built_bundle
    loader = EvaluationBundleLoader(root)
    exec_case = loader.load_execution_case(spec.case.case_id, spec.case.case_version)

    field_names = {f.name for f in fields(exec_case)}
    assert "human_label" not in field_names
    assert "human_label_fingerprint" not in field_names
    assert "label" not in field_names

    # execution 字段与 snapshot 齐备
    assert exec_case.case_fingerprint == spec.case_fingerprint
    assert exec_case.snapshot == spec.snapshot
    assert exec_case.company_id == spec.case.company_id
    assert exec_case.research_question == spec.case.research_question


def test_traversal_input_rejected(tmp_path) -> None:
    loader = EvaluationBundleLoader(tmp_path / "bundle")
    with pytest.raises(EvalContractError):
        loader.load_case("../evil", 1)
    with pytest.raises(EvalContractError):
        loader.load_case("a/b", 1)
    with pytest.raises(EvalContractError):
        loader.load_snapshot("../../etc/passwd")
    with pytest.raises(EvalContractError):
        loader.read_document_blob("../../etc/passwd")


def test_unknown_field_in_case_json_rejected(built_bundle) -> None:
    """bundle JSON 额外未知字段 → strict loader 包装成稳定 EvalContractError。"""
    root, spec = built_bundle
    case_path = layout.case_path(root, spec.case.case_id, spec.case.case_version)
    data = json.loads(case_path.read_text(encoding="utf-8"))
    data["legacy_security_code"] = "600519"
    case_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    loader = EvaluationBundleLoader(root)
    with pytest.raises(EvalContractError):
        loader.load_case(spec.case.case_id, spec.case.case_version)
