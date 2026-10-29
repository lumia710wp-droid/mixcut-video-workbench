# 剪辑后整包交接

先运行 `./mixcut --json director run-status --task TASK` 确认候选视频和技术检查。按项目实际已审事实与封面规范生成四张本地图片；Codex 在每个平台适当调整正文和标题，不复述视频全部字幕。不要虚构促销、价格或门店信息。先实际打开每张封面，检查画面与文字，确认全图及缩略图可读；图像尺寸检查不能代替内容 QA。

在任务目录编写 `delivery-spec.json`（内容仅为示意，使用实际文件路径及实际文案）：

```json
{
  "covers": {
    "douyin": "jobs/TASK/cover-douyin.png",
    "xiaohongshu": "jobs/TASK/cover-xiaohongshu.png",
    "channels_portrait": "jobs/TASK/cover-channels-portrait.png",
    "channels_landscape": "jobs/TASK/cover-channels-landscape.png"
  },
  "copy": {
    "douyin": {"body":"实际正文", "titles":["实际标题"], "hashtags":[], "pinned_comment":""},
    "xiaohongshu": {"body":"实际正文", "titles":["实际标题"], "hashtags":[], "pinned_comment":""},
    "channels": {"body":"实际正文", "titles":["实际标题"], "hashtags":[], "pinned_comment":""}
  }
}
```

执行 `./mixcut --json director delivery-prepare --task TASK --file jobs/TASK/delivery-spec.json` 后执行 `./mixcut --json director delivery-publish --task TASK`；程序会验证文件和当前脚本/编导版本，上传四张图并读回。上传中断、已存在不明附件或结果未知时先对账，不重复上传或清空。用户在工作台/飞书查看视频、封面、文案并决定通过或退回；任何修改重新准备包并等待新审核。不要代替用户审核或直接发布到平台。
