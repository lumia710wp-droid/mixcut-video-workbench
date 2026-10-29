# Mixcut 接入与计划格式

工作区线索：`<WORKSPACE>`。执行前确认实际目录与单写运行机。涉及多个运行实例时，应选定唯一写入方。

## 命令

从实际工作区运行；默认不调用模型、飞书或编辑器：

```bash
./mixcut --json material-search build
./mixcut --json material-search query --request /absolute/workspace/jobs/director-run/N01.query.json --output /absolute/workspace/jobs/director-run/N01.results.json
./mixcut --json material-search show --result /absolute/workspace/jobs/director-run/N01.results.json --candidate candidate_RETURNED_ID --output /absolute/workspace/jobs/director-run/N01.detail.json
./mixcut --json material-search validate-plan --plan /absolute/workspace/jobs/director-run/director_plan.json --output /absolute/workspace/jobs/director-run/validation.json
```

也可用本 Skill 的 `scripts/search_materials.py --workspace /absolute/workspace` 后接同样参数。输出文件必须在工作区内且尚不存在。分页把上页 `next_cursor` 原样放进新的查询文件 `cursor`，其他条件保持不变。索引缓存位于 `indexes/material_search/`，不修改事实与批准。

实际字段以 `./mixcut material-search --help` 及 `src/mixcut/material_search.py` 为准。检索结果中的 `scope`、`candidate_id`、`document_id`、事实版本和源帧区间不能手工伪造。证据文件变更报错必须恢复或重整，不跳过hash校验。

## director_plan.json

下面是字段示意，ID、hash和路径必须替换为本任务实际返回值；它不是批准记录。

```json
{
  "version": 1,
  "entity": "codex_director_plan",
  "script_path": "/absolute/workspace/jobs/director-run/script.json",
  "script_sha256": "SHA256_OF_ACTUAL_SCRIPT_FILE",
  "brand_id": "示例品牌",
  "goods_id": "芒果",
  "tasks": [{
    "task_id": "ACTUAL_TASK_ID",
    "total_frames": 90,
    "shots": [{
      "need_id": "N01",
      "search_result_path": "/absolute/workspace/jobs/director-run/N01.results.json",
      "candidate_id": "ACTUAL_RETURNED_CANDIDATE_ID",
      "source_in_frame": 60,
      "source_out_frame": 159,
      "timeline_start_frame": 0,
      "duration_frames": 90,
      "speed": 1.1,
      "source_audio_gain": 0,
      "transition_out": {"type": "hard_cut", "frames": 0},
      "rationale": "依据入库标记，说明本镜头如何满足脚本与前后衔接",
      "selection_basis": {
        "type": "indexed_facts",
        "document_id": "ACTUAL_RETURNED_DOCUMENT_ID",
        "facts_sha256": "ACTUAL_RETURNED_FACTS_SHA256"
      }
    }]
  }]
}
```

可选 `group_id` 绑定实际项目约束。源出帧不含；当前速度支持1—1.25，须遵守实际项目参数。示例90帧、1.1倍需99个源帧。计划校验接受有效入库事实引用，不要求重新观看；检查脚本hash、当前商品/事实版本、源范围、时间线、占用、多样性和媒体可读性，不生成用户批准或预占。

只有发生疑点补查时才另附`visual_review`，记录`reviewer=codex`、实际补查说明`note`和源绑定的`evidence`路径/hash。已有这种记录的旧计划仍可校验。程序不把复用入库事实当作一次新观看。

## 文字与编导审核链

`workflow_mode=script_first_v1`项目只在脚本阶段生成/编辑/审核文字。读取`jobs/script_drafts_v1/<group>/current.json`及当前已批准的单条内容；同批其他稿件待修改不阻塞已确认单条。改稿只使对应单条编导审核过期，不能沿用旧文字hash或整批撤销其他批准。

在有真实完整工作副本的电脑上运行`director build-packet --task TASK --plan /工作区/director_plan.json --output /工作区/packet.json`，校验原计划、事实版本、源区间、黑边与必要裁切。将可携带packet传到所配置的单写工作区，运行`director submit --group GROUP --file /工作区/packet.json`和`director publish --task TASK`，回填同一飞书任务的编导剪辑脚本、版本、摘要、审核结果。提交和回填均不生成配音、工程或视频。

用户在工作台审核当前编导脚本；通过后用`director gate --task TASK`核对真实审核、当前文字与事实版本及同步状态。待审、退回、过期或写入结果未知均不能继续配音剪辑。聊天中的一般制作授权不能代替这个用户明确要求的审核节点。

## 程序执行，Codex调用

- `director prepare-audio --task TASK`：审核后才按原稿配音/复用缓存，固定四音色轮换和+8；时长目标读取配置，当前口播占成片85%—95%（10秒即8.5—9.5秒），0.2秒入场。超过配置目标时按程序规则加速至目标；不自动改写已审文字。
- `director template-command --task TASK --session ACTUAL_SESSION --output /工作区/template.json`：填充固定V3.1包装。
- `director compile-adds --task TASK --session ACTUAL_SESSION --file /工作区/实际read_project.json --output /工作区/adds.json`：读取真实媒体池ID并校验持久上传引用及文件hash；不猜ID，不用blob临时引用。
- `director compile-updates --task TASK --session ACTUAL_SESSION --file /工作区/实际edit_item返回.json --output /工作区/updates.json`：生成固定剪辑参数，变速和最终时长分开调用。使用真实返回的itemId，不能虚构回执。
- 工程/导出等不可重复副作用之前用`director step-intent`记录参数，真实结果用`step-receipt`记录。已完成步骤复用result，未知结果先对账；回填失败不重新配音/渲染。

详细命令以实际CLI帮助和工作区`docs/DIRECTOR_WORKFLOW.md`为准。调用真实编辑接口仍读取`openchatcut` Skill；技术检查和成片内容检查后交人工验收。索引、版本、轮换、字幕、模板、时间换算和恢复交给程序，Codex只保留素材理解、编导选择和内容判断。

现有V3完整包审批/计数与`director`新链分别保留，不能伪造旧V3批准或把独立候选当成已归档。当前环境需自行完成真实端到端验证，不能用单测冒充成片验收。


## 固定总控及标题样式

当前Mixcut执行以`./mixcut --json director run --task TASK`为唯一正常入口，`run-status`查看进度。前文的prepare-audio/template-command/compile-adds/compile-updates为底层排错工具；正常制作不得由Codex重新选择执行顺序。run固定校验→配音实测→工程/上传→已审模板→落轨/变速/时长→回读→应用→应用后回读→导出/下载→技术检查，最后等内容检查与人工验收。重复运行复用完成步骤，未知副作用结果不重发。

文字稿阶段已保存V3.1完整样式快照和四行标题，回填飞书并联合审核；剪辑只读取审核快照，不取当前全局样式、不删除标题后缀。更改标题或样式需重审对应单条；旧无样式脚本先用bind-title-style生成待审版本。程序核对真实标题属性、代码和时间线，失败不导出。

默认单机使用自己的飞书账号；如配置远程单写实例，则读取其真实已审快照，本机数据不能自行制造批准。工作区docs/DIRECTOR_WORKFLOW.md记录当前部署方式和平台确认要求。
