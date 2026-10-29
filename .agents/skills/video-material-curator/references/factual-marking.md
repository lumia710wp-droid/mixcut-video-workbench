# 全片事实标记契约 V1

每份原片一条 `schema_version: material_facts_v1`；允许聚合到 `materials` 列表。源文件身份与内容标记分开。

- `source`：source_id、file_sha256、local_path、duration_sec、技术探测及原位置线索。
- `parent`：clip_id、引用时内容hash、已有人工商品/品牌/审核值，不改签。
- `summary`：仅描述实际可见内容。
- `phases`：按源时间排序、连续覆盖0至原片时长；每段 start_sec/end_sec/title/observation/quality，可加scene/shot_size/camera/semantic_tags。左闭右开，边界标为sampled_observation；秒定位不冒充原始VFR帧精度。
- `events`：源time_sec/text/evidence_time_sec，表示内容变化或动作点，不能表示推荐裁切点。
- `identity`：visual_facts、directory_hint、confirmed_goods/brand、uncertainties，各项来源清楚。
- `evidence`：review路径/hash/时间映射、frames的源time_sec/path/hash，实际看过的sheet路径/hash及覆盖范围。
- `coverage`：sampling_method、actually_viewed_sheet_ids、audio_reviewed、continuous_video_watched、limitations。有密集帧不表示全帧审核。
- `similarity`：同源重复动作、跨文件疑似重复及证据；未检索状态不能写无重复。
- `review_status: AI_marked_pending_confirmation`、`production_approved: false`。现有批准仅在parent引用，不能复制成新标记已获批准。
- `content_sha256`：去除自身hash后canonical JSON的SHA256；UTF8、sort_keys、紧凑分隔符、禁NaN。

事实层禁用：recommendation、usage、priority、role、speed、speed_range、transition、crop、source_audio_gain、recommended_in/out等决策字段。原文件fps/rotation/原音是否存在是技术事实，可以保留。观察时段可以包含不清楚或重复内容，不等于已批准的剪辑范围。

全片标记质量不要求每秒机械建飞书行；每秒证据用于定位，连续阶段与动作点供脚本检索。审核页只展内容/时间/质量，不展取用建议。
