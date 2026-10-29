# Mixcut素材整理接入

已验证工作区：`<WORKSPACE>`。仅作定位线索，不是新任务的默认授权。先读实际工作区的AGENTS、PROJECT_STATE、TASKS、docs/WORKFLOW和CLI。

## 当前职责和数据版本

本项目用户已确认：素材整理只做全片事实标记，选材、速度、转场等由脚本决定。V3.0 Skill执行[事实标记契约](factual-marking.md)。当前catalog/sources.json、clips.json保留人工有效字段；全片事实记录使用版本化catalog/material_facts_v1.json及analysis/<run_id>/逐源证据，原候选索引保留历史但不当作全片新标记。部署以使用者自己的单写实例为准，写前备份/持锁/核对源和父记录hash；不把本地旧catalog覆盖到线上。

以下proposal命令是旧链兼容。重新整理不得用旧start_sec/end_sec裁掉观察范围，不得自动重签可用区间或商品。历史固定词表缺动作时保留版本化semantic_tags和缺口，不伪造已映射状态。

## 入库与证据

从config/config.local.json读取授权source_roots、工具路径与范围，不输出认证配置。先确认根路径可读再scan，防止未挂载被当空库。

```bash
./mixcut --json scan --root store_shots --limit 6
./mixcut --json candidates
./mixcut --json previews
./mixcut --json review-pack
```

6是历史小样本示例，按当轮授权调整。candidates只生成整文件候选，不代表语义整理完成。

- catalog/sources.json保存源hash、位置和探测；catalog/clips.json保存有效字段、revision/hash及审核。
- 每个analysis/<run_id>/独立保存观察、接触表、proposals.json，不覆盖旧证据。
- 沿用src/mixcut/analyze.py、proposal_adoption.py和实际proposal结构。逐条analysis_sha256用mixcut.core.digest对删除自身hash字段的record计算。
- 记录分析方式、实际分析者、visually_analyzed状态、源hash及当时catalog revision/hash。建议保留production_approved=false；后来批准存在独立审核记录，不改写当时观察。
- proposal精确字段为description/tags/start_sec/end_sec/evidence_times/product_uncertain，tags四轴content/action/shot_size/risk。
- evidence保存proxy、sheet、candidate_sheet的绝对路径/hash及源采样时间。未映射动作保存semantic_tags与semantic_taxonomy_version，报告映射缺口。
- 秒区间左闭右开；后续剪辑才按30fps向内取整，不把抽样当逐帧切点。

## 只读检查

用本Skill脚本实际绝对路径运行（从Skill目录运行时可用相对路径）：

```bash
python3 scripts/check_handoff.py --workspace /absolute/mixcut-workspace --root-id store_shots --proposals /absolute/run/proposals.json --report /absolute/mixcut-workspace/reports/new-material-check.json
```

需要现有src/mixcut模块，复用proposal、词表和内容hash契约，不安装依赖、不调用CLI/飞书/模型。报告须为工作区内不存在的新路径；退出0只证明本地结构与证据门禁，不能证明语义、人审或同步。原片未挂载应阻断，已有分析和批准保留。

## 飞书采纳

先读当前映射、快照和CLI参数。使用lark-base操作；上传只覆盖授权的附件范围。

```bash
./mixcut --json sync pull
./mixcut --json material-prefill prepare --proposals /absolute/run/proposals.json --report /absolute/new-prefill-plan.json
./mixcut --json material-prefill apply --plan /absolute/new-prefill-plan.json --report /absolute/new-prefill-apply.json
```

apply在授权采纳建议时执行。冲突或已进入人审的有效字段保留；用户已明确确认品牌/目录商品时，可按现有confirm流程转录真实决定，先查看真实参数，不补造actor或批准。预览图上传许可不扩为原视频上传。

sync push收到ack后，再读回业务主键、有效字段、review revision/hash并核销outbox；失败只补同步，不重做扫描/分析/渲染。

## 交接边界

素材整理到事实标记交接结束；选材、脚本和剪辑由后续阶段负责。
