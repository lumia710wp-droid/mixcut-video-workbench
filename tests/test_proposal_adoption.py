from pathlib import Path

from mixcut.contracts import content_hash
import pytest

from mixcut.core import Blocked, atomic_write, digest, load
from mixcut.proposal_adoption import apply_prefill, prepare_prefill, record_confirmations


def test_prefill_only_populates_empty_ai_review_fields(tmp_path: Path):
    atomic_write(
        tmp_path / "config/taxonomy.json",
        {
            "axes": {
                "content": [{"id": "detail"}],
                "action": [{"id": "show"}],
                "shot_size": [{"id": "closeup"}],
                "risk": [{"id": "uncertain_product"}],
            }
        },
    )
    field_ids = {
        key: key
        for key in [
            "source_type",
            "content_tags",
            "action_tags",
            "shot_size_tags",
            "risk_tags",
            "usable_start_sec",
            "usable_end_sec",
            "description",
        ]
    }
    atomic_write(
        tmp_path / "config/feishu_mapping.local.json",
        {
            "base_token": "BASE",
            "tables": {"clips": {"table_id": "TABLE", "field_ids": field_ids}},
        },
    )
    atomic_write(
        tmp_path / "catalog/clips.json",
        [{"clip_id": "C1", "revision": 1, "content_sha256": "clip-hash"}],
    )
    atomic_write(
        tmp_path / "catalog/feishu_snapshot.json",
        {
            "tables": {
                "clips": [
                    {
                        "record_id": "R1",
                        "fields": {"clip_id": "C1", "rights_confirmed": None},
                    }
                ]
            }
        },
    )
    atomic_write(
        tmp_path / "proposals.json",
        {
            "records": [
                {
                    "clip_id": "C1",
                    "source_duration_sec": 10,
                    "catalog_revision_at_analysis": 1,
                    "catalog_content_sha256_at_analysis": "clip-hash",
                    "analysis_sha256": "analysis-hash",
                    "proposal": {
                        "description": "双手制作近景",
                        "tags": {
                            "content": ["detail"],
                            "action": [],
                            "shot_size": ["closeup"],
                            "risk": ["uncertain_product"],
                        },
                        "start_sec": 2,
                        "end_sec": 7,
                    },
                }
            ]
        },
    )
    result = prepare_prefill(tmp_path, tmp_path / "proposals.json")
    assert result["status"] == "ready" and result["updates"] == 1
    plan = load(Path(result["path"]))
    assert plan["update_records"]["R1"]["usable_start_sec"] == 2
    assert "rights_confirmed" not in plan["update_records"]["R1"]
    assert "goods_id" in plan["protected_fields"]


def test_prefill_preserves_conflicting_human_value(tmp_path: Path):
    test_prefill_only_populates_empty_ai_review_fields(tmp_path)
    snapshot = load(tmp_path / "catalog/feishu_snapshot.json")
    snapshot["tables"]["clips"][0]["fields"]["description"] = "人工已有描述"
    atomic_write(tmp_path / "catalog/feishu_snapshot.json", snapshot)
    result = prepare_prefill(
        tmp_path,
        tmp_path / "proposals.json",
        report_path=tmp_path / "reports/conflict_recheck.json",
    )
    assert result["status"] == "conflict"
    assert result["conflicts"] == [{"clip_id": "C1", "fields": ["description"]}]


def test_prefill_never_refills_cleared_risk_after_human_review(tmp_path: Path):
    test_prefill_only_populates_empty_ai_review_fields(tmp_path)
    snapshot = load(tmp_path / "catalog/feishu_snapshot.json")
    fields = snapshot["tables"]["clips"][0]["fields"]
    fields.update(
        review_revision=1,
        review_action=["approve"],
        review_status=["approved"],
        risk_tags=None,
    )
    atomic_write(tmp_path / "catalog/feishu_snapshot.json", snapshot)
    result = prepare_prefill(
        tmp_path,
        tmp_path / "proposals.json",
        report_path=tmp_path / "reports/locked_recheck.json",
    )
    plan = load(Path(result["path"]))
    assert result["status"] == "no_change" and result["updates"] == 0
    assert plan["records"][0]["human_locked"] is True
    assert plan["records"][0]["changed_fields"] == []


def test_prefill_refuses_to_overwrite_different_report_evidence(tmp_path: Path):
    test_prefill_only_populates_empty_ai_review_fields(tmp_path)
    snapshot = load(tmp_path / "catalog/feishu_snapshot.json")
    snapshot["tables"]["clips"][0]["fields"]["description"] = "人工已有描述"
    atomic_write(tmp_path / "catalog/feishu_snapshot.json", snapshot)

    with pytest.raises(Blocked, match="Report already exists") as error:
        prepare_prefill(tmp_path, tmp_path / "proposals.json")

    assert error.value.code == "REPORT_CONFLICT"


def test_apply_prefill_reads_remote_then_updates_only_missing_fields(tmp_path: Path):
    test_prefill_only_populates_empty_ai_review_fields(tmp_path)
    plan_path = tmp_path / "reports/material_prefill_plan_20261015.json"
    plan = load(plan_path)
    field_ids = load(tmp_path / "config/feishu_mapping.local.json")["tables"]["clips"]["field_ids"]
    calls = []

    def api(args):
        calls.append(args)
        if args[0] == "+record-list":
            return {
                "data": {
                    "field_id_list": list(field_ids.values()),
                    "record_id_list": ["R1"],
                    "data": [[None for _ in field_ids]],
                    "has_more": False,
                }
            }
        body = load_json_arg(args)
        assert set(body["update_records"]["R1"]) == set(field_ids.values()) - {"action_tags"}
        return {"data": {"record_id_list": ["R1"]}}

    result = apply_prefill(tmp_path, plan_path, call=api)
    assert result["status"] == "acknowledged_pending_readback"
    assert [call[0] for call in calls] == ["+record-list", "+record-batch-update"]
    assert "rights_confirmed" not in plan["protected_fields"]


def load_json_arg(args):
    import json

    return json.loads(args[args.index("--json") + 1])


def test_record_confirmation_binds_approved_current_revision(tmp_path: Path):
    actor = "用户确认"
    fields = {
        "source_type": "门店实拍",
        "content_tags": ["detail"],
        "action_tags": [],
        "shot_size_tags": ["closeup"],
        "risk_tags": ["uncertain_product"],
        "usable_start_sec": 2,
        "usable_end_sec": 7,
        "description": "双手制作近景",
    }
    prefill = {
        "plan_sha256": "prefill-hash",
        "records": [{"clip_id": "C1", "analysis_sha256": "analysis-hash", "fields": fields}],
    }
    atomic_write(tmp_path / "reports/material_prefill_plan_20261015.json", prefill)
    confirmation = {
        "schema_version": "1.0.0",
        "entity": "material_confirmation_plan",
        "actor": actor,
        "records": [
            {
                "clip_id": "C1",
                "brand_id": "示例品牌",
                "goods_id": "芒果",
                "rights_confirmed": True,
                "source_path": "/素材/芒果/C1.mov",
                "analysis_sha256": "analysis-hash",
            }
        ],
    }
    confirmation["plan_sha256"] = digest(confirmation)
    atomic_write(tmp_path / "reports/material_confirmation_plan_20261015.json", confirmation)
    clip = {
        "clip_id": "C1",
        "revision": 3,
        "brand_id": "示例品牌",
        "goods_id": "芒果",
        "rights_confirmed": True,
        "source_type": "门店实拍",
        "tags": {"content": ["detail"], "action": [], "shot_size": ["closeup"], "risk": []},
        "usable_start_sec": 2,
        "usable_end_sec": 7,
        "description": "双手制作近景",
        "review_status": "approved",
        "approval": None,
    }
    clip["content_sha256"] = content_hash(clip)
    clip["approval"] = {
        "actor_id": actor,
        "channel": "feishu",
        "approved_at": "2026-10-15T00:00:00+00:00",
        "approved_content_sha256": clip["content_sha256"],
    }
    atomic_write(tmp_path / "catalog/clips.json", [clip])
    atomic_write(
        tmp_path / "catalog/feishu_snapshot.json",
        {
            "tables": {
                "clips": [
                    {
                        "record_id": "R1",
                        "fields": {
                            "clip_id": "C1",
                            "brand_id": "示例品牌",
                            "goods_id": "芒果",
                            "rights_confirmed": True,
                            "risk_tags": [],
                            "review_revision": 3,
                            "review_action": ["approve"],
                            "review_actor": actor,
                            "review_status": ["approved"],
                            "content_sha256": clip["content_sha256"],
                            "approved_content_sha256": clip["content_sha256"],
                        },
                    }
                ]
            }
        },
    )
    result = record_confirmations(tmp_path, actor)
    assert result["approval_count"] == 1
    receipt = load(tmp_path / "catalog/material_confirmations.json")
    assert receipt["records"][0]["confirmed_catalog_revision"] == 3
    assert receipt["records"][0]["goods_id"] == "芒果"


def test_mixed_source_prefill_does_not_label_unknown_origin_as_store_shot(tmp_path: Path):
    test_prefill_only_populates_empty_ai_review_fields(tmp_path)
    proposal = load(tmp_path / "proposals.json")
    proposal["records"][0]["source_type_suggestion"] = "other"
    atomic_write(tmp_path / "mixed.json", proposal)
    result = prepare_prefill(
        tmp_path, tmp_path / "mixed.json", report_path=tmp_path / "reports/mixed.json"
    )
    assert load(Path(result["path"]))["update_records"]["R1"]["source_type"] == "other"
    assert "rights_confirmed" not in load(Path(result["path"]))["update_records"]["R1"]


def test_free_text_cannot_invent_a_material_type_or_approval(tmp_path: Path):
    test_prefill_only_populates_empty_ai_review_fields(tmp_path)
    proposal = load(tmp_path / "proposals.json")
    proposal["records"][0]["source_type_suggestion"] = "owned-and-approved"
    atomic_write(tmp_path / "invalid-type.json", proposal)
    with pytest.raises(Blocked) as caught:
        prepare_prefill(
            tmp_path, tmp_path / "invalid-type.json", report_path=tmp_path / "reports/invalid.json"
        )
    assert caught.value.code == "PROPOSAL_SOURCE_TYPE"


def test_prefill_ack_without_record_ids_still_requires_readback(tmp_path: Path):
    test_prefill_only_populates_empty_ai_review_fields(tmp_path)
    mapping = load(tmp_path / "config/feishu_mapping.local.json")
    fields = list(mapping["tables"]["clips"]["field_ids"])

    def api(args):
        if args[0] == "+record-list":
            return {
                "data": {
                    "field_id_list": fields,
                    "record_id_list": ["R1"],
                    "data": [[None] * len(fields)],
                    "has_more": False,
                }
            }
        return {"data": {"ignored_fields": {}}}

    result = apply_prefill(tmp_path, call=api)
    assert result["status"] == "acknowledged_pending_readback"
    assert result["readback_required"] is True
    assert load(tmp_path / "catalog/clips.json")[0]["revision"] == 1
    assert (
        load(tmp_path / "reports/material_prefill_apply_20261015.json")["acknowledged_record_ids"]
        == []
    )


def test_partially_ignored_prefill_is_unknown_until_reconciled(tmp_path: Path):
    test_prefill_only_populates_empty_ai_review_fields(tmp_path)
    mapping = load(tmp_path / "config/feishu_mapping.local.json")
    fields = list(mapping["tables"]["clips"]["field_ids"])

    def api(args):
        if args[0] == "+record-list":
            return {
                "data": {
                    "field_id_list": fields,
                    "record_id_list": ["R1"],
                    "data": [[None] * len(fields)],
                    "has_more": False,
                }
            }
        return {"data": {"ignored_fields": {"description": "READONLY"}}}

    with pytest.raises(Blocked) as caught:
        apply_prefill(tmp_path, call=api)
    assert caught.value.code == "PREFILL_FIELDS_IGNORED"
    assert caught.value.exit_code == 5
