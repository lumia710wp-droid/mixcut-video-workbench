"""Explicit material scopes; old single-product policies retain their meaning."""

from .core import Blocked, load

KEYS = ("brand_id", "goods_id", "goods_scope", "goods_ids")


def scope_values(value):
    return {k: value[k] for k in KEYS if k in value}


def scope_key(value):
    return scope_values(value) if "goods_scope" in value else value["goods_id"]


def describe(value):
    goods = (
        "全部口味"
        if value.get("goods_scope") == "brand_all"
        else "、".join(value.get("goods_ids", [])) or value.get("goods_id", "")
    )
    return f"{value['brand_id']} · {goods}"


def validate_scope(value):
    if "goods_scope" not in value:
        if "goods_ids" in value:
            raise Blocked("PRODUCT_SCOPE", "多口味列表须指定素材范围方式", 2)
        return
    mode, goods = value["goods_scope"], value.get("goods_ids")
    if (
        mode not in ("brand_all", "selected")
        or not isinstance(goods, list)
        or len(goods) > 100
        or any(not isinstance(g, str) or not 1 <= len(g) <= 120 for g in goods)
        or len(set(goods)) != len(goods)
        or (mode == "brand_all" and goods)
        or (mode == "selected" and not goods)
    ):
        raise Blocked("PRODUCT_SCOPE", "请选择同品牌全部口味，或至少一个指定口味", 2)
    label = "全部口味" if mode == "brand_all" else goods[0] if len(goods) == 1 else "多口味混剪"
    if value.get("goods_id") != label:
        raise Blocked("PRODUCT_SCOPE", "素材范围标签与口味集合不一致", 2)


def includes(value, goods):
    validate_scope(value)
    if value.get("goods_scope") == "brand_all":
        return True
    if value.get("goods_scope") == "selected":
        return goods in value["goods_ids"]
    return goods == value["goods_id"]


def available_scopes(root):
    """Use the same eligible facts as retrieval, including confirmed multi-product windows."""
    from .fact_planning import planning_segments, workflow_policy, uses_facts

    clips = load(root / "catalog/clips.json")
    brands = {c["brand_id"] for c in clips if c.get("brand_id")}
    if uses_facts(root, {}):
        brands = set(workflow_policy(root).get("brand_scopes", {}))
    result = []
    for brand in sorted(brands):
        segments = planning_segments(
            root,
            {
                "brand_id": brand,
                "goods_id": "全部口味",
                "goods_scope": "brand_all",
                "goods_ids": [],
            },
        )
        goods = sorted({s["goods_id"] for s in segments if s.get("goods_id")})
        if goods:
            result.append({"brand": brand, "goods": goods})
    return result


def project_scope(root, body):
    brand = body.get("brand_id")
    if not isinstance(brand, str) or not brand:
        raise Blocked("PRODUCT_SCOPE", "请选择素材品牌", 2)
    if "goods_scope" not in body:
        # Compatibility for existing clients and their already recorded submissions.
        validate_scope(body)
        scopes = {(c.get("brand_id"), c.get("goods_id")) for c in load(root / "catalog/clips.json")}
        if not brand or not body.get("goods_id") or (brand, body["goods_id"]) not in scopes:
            raise Blocked("V3_PRODUCT_REQUIRED", "请选择已登记的品牌和商品", 2)
        return {"brand_id": brand, "goods_id": body["goods_id"]}
    goods = body.get("goods_ids")
    mode = body["goods_scope"]
    label = (
        "全部口味"
        if mode == "brand_all"
        else goods[0]
        if isinstance(goods, list) and len(goods) == 1
        else "多口味混剪"
    )
    value = {"brand_id": brand, "goods_id": label, "goods_scope": mode, "goods_ids": goods}
    validate_scope(value)
    options = next((x["goods"] for x in available_scopes(root) if x["brand"] == brand), None)
    if options is None or any(g not in options for g in goods):
        raise Blocked("PRODUCT_SCOPE", "所选品牌或口味没有当前有效的素材记录，请刷新素材范围", 2)
    return {**value, "goods_ids": sorted(goods)}
