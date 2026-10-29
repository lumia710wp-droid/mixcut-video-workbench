import importlib.util
from pathlib import Path

import pytest
from mixcut.core import Blocked, atomic_write, load

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("public_setup", ROOT / "tools/setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


@pytest.fixture
def root(tmp_path):
    import shutil

    shutil.copytree(ROOT / "config", tmp_path / "config")
    return tmp_path


def test_init_is_safe_and_repeat_never_overwrites(root):
    result = setup.init(root, None)
    assert result["status"] == "created"
    assert load(root / "catalog/sources.json") == []
    before = (root / "config/config.local.json").read_bytes()
    with pytest.raises(Blocked):
        setup.init(root, None)
    assert (root / "config/config.local.json").read_bytes() == before


def test_create_builds_two_tables_and_reads_all_fields(root):
    setup.init(root, None)
    plan = setup.full_schema(root)
    calls = []

    def fake(argv, workspace):
        calls.append(argv[0])
        if argv[0] == "+base-create":
            return {
                "data": {
                    "base": {"base_token": "dummy", "url": "https://test.feishu.cn/base/dummy"}
                }
            }
        if argv[0] == "+table-create":
            return {"data": {}}
        if argv[0] == "+table-list":
            return {
                "data": {
                    "tables": [
                        {"name": plan["tables"][k]["name"], "id": "tbl_" + k}
                        for k in ("clips", "tasks")
                    ],
                    "total": 2,
                }
            }
        if argv[0] == "+field-list":
            name = argv[argv.index("--table-id") + 1].removeprefix("tbl_")
            fields = [
                {"id": "field_" + str(i), "name": f["name"], "type": f["type"]}
                for i, f in enumerate(plan["tables"][name]["fields"])
            ]
            return {"data": {"fields": fields, "total": len(fields)}}
        raise AssertionError(argv[0])

    result = setup.create(root, "Example Base", fake)
    assert result["status"] == "bound"
    assert calls.count("+base-create") == 1 and calls.count("+table-create") == 1
    assert load(root / "config/config.local.json")["feishu"]["enabled"] is True
    assert len(
        load(root / "config/feishu_mapping.local.json")["tables"]["tasks"]["field_ids"]
    ) == len(plan["tables"]["tasks"]["fields"])
    with pytest.raises(Blocked):
        setup.create(root, "Again", fake)
    assert calls.count("+base-create") == 1


def test_unknown_creation_is_never_replayed(root):
    setup.init(root, None)
    calls = []

    def fail(argv, workspace):
        calls.append(argv[0])
        raise Blocked("UNKNOWN", "uncertain", 6)

    with pytest.raises(Blocked):
        setup.create(root, "Example", fail)
    with pytest.raises(Blocked, match="结果未知"):
        setup.create(root, "Example", fail)
    assert calls == ["+base-create"]


def test_bind_rejects_incomplete_or_ambiguous_table_list(root):
    setup.init(root, None)

    def incomplete(argv, workspace):
        return {"data": {"tables": [], "total": 101}}

    with pytest.raises(Blocked):
        setup.bind(root, "dummy", "https://test.feishu.cn/base/dummy", incomplete)
    assert not (root / "config/feishu_mapping.local.json").exists()
