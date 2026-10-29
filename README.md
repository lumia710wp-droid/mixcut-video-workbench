# Mixcut 视频生产工作台

面向本地素材的短视频制作流程：素材事实入库 → 用户输入需求 → 生成并审核文字脚本与标题样式 → Codex 基于已有标记检索并规划镜头 → 编导方案回填飞书并审核 → 生成配音、调用 OpenChatCut 剪辑 → Codex 制作封面与发布文案 → 回填飞书整包验收。素材检索和流程校验在本地执行，不把整个素材库塞给脚本模型。素材标签足够时不要求逐条重新看素材；信息不足时按需补查。

项目附带两个 Codex Skill：`.agents/skills/video-material-curator` 和 `.agents/skills/video-script-material-director`。程序、模板和示例不含原使用者的素材、飞书表或凭据。每个使用者需要配置自己的飞书多维表、模型、语音服务和本地素材。OpenChatCut 是单独安装的软件，本仓库不包含它。

## 开始使用

请先阅读 [安装与配置](docs/SETUP.md) 和 [工作流程](docs/DIRECTOR_WORKFLOW.md)。脚本模型、语音接口、飞书建表和视频导出会访问外部服务或消耗资源，应使用自己的账户并确认服务权限。当前版本以 macOS 本机工作流为目标；其他系统及不同版本的 OpenChatCut 尚未验证。

## 验证

安装 Python 依赖后可执行 `.venv/bin/python -m pytest -q`。自动测试中的模拟飞书及合成数据不能证明真实账号授权、建表、配音或视频导出已通过。旧版 montage 示例里的素材 ID 是占位值，不属于可直接使用的编辑器素材；实际任务使用当前编导工作流生成并校验自己的标题素材。

项目代码与项目 Skill 使用 [MIT](LICENSE)；外部软件和服务遵循各自许可，见 [第三方说明](THIRD_PARTY_NOTICES.md)。
