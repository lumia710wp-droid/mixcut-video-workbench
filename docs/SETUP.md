# 安装与配置

## 1. 准备本机

需要 Python 3.12、FFmpeg（含 ffprobe）、Git、Node.js 24、npm、飞书账号和已安装的 Codex。安装 [OpenChatCut](https://github.com/0xsline/OpenChatCut/releases) 桌面应用（适配器针对 0.2.16），并先启动应用完成必要的初始化。OpenChatCut 是独立项目，不包含在本仓库；其许可及安装条件以其官方说明为准。

在项目根目录执行：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python tools/setup.py init --source "/你的素材目录"
```

素材目录需已存在。可省略 `--source` 并在本地配置文件中稍后设置；初始化不会覆盖现有配置。项目通过 `config/config.local.json` 保存本机路径，本地配置和运行数据被 `.gitignore` 排除，不应上传。

## 2. 连接自己的飞书多维表

安装并登录 [飞书 CLI](https://github.com/larksuite/cli)，具体授权和应用配置以 CLI 提示为准：

```sh
npx @larksuite/cli@latest install
lark-cli config init
lark-cli auth login --recommend
lark-cli auth status
```

在确认当前登录账号和目标企业后，运行以下命令会在该账号下新建一个 Base 及项目所需表和字段：

```sh
.venv/bin/python tools/setup.py create-base --name "Mixcut 视频生产"
```

也可绑定已经具有**完整且类型一致的本项目表结构**的 Base：

```sh
.venv/bin/python tools/setup.py bind-base --url "https://你的企业.feishu.cn/base/你的Base地址"
```

绑定模式只读取和校验结构，不自动补建缺失字段。本工具当前只接受 `*.feishu.cn/base/` 地址。建表后会将 Base、表和字段 ID 保存到仅本机使用的 `config/feishu_mapping.local.json`，并更新本地配置。使用独立的空 Base，切勿填写原作者的 Base 地址或凭据。

建表请求如果中断或结果未知，程序会保留 `ledger/setup_feishu/` 中的本地回执并停止自动重试；先到飞书确认实际结果和本地回执，避免重复创建。不要公开这些回执。此版本的建表逻辑经过模拟测试，尚需使用新账号进行真实平台验收。

## 3. 配置模型、语音与编辑器

文字生成使用 DeepSeek 配置 `config/deepseek.example.json`；将其复制为 `config/deepseek.local.json`，根据自己的模型可用情况修改，并在本机安全设置 `DEEPSEEK_API_KEY` 环境变量。不要将密钥写进代码、截图或 Git。

配音使用豆包语音服务。复制 `config/doubao_tts.example.json` 到 `config/doubao_tts.local.json`，在本机编辑填入自己的 `api_key` 并执行 `chmod 600 config/doubao_tts.local.json`。音色配置示例在 `config/director_voice_policy.json`，能否使用取决于自己的服务及音色权限。配音在剪辑阶段生成，按约定音频时长做必要的加速；文字生成阶段不生成配音。

如需运行固定的 OpenChatCut CLI 剪辑链，另外执行：

```sh
./tools/install_occ_adapter.sh
./tools/occ --help
```

此命令从 OpenChatCut 官方仓库下载指定版本源码到被忽略的 `runtime/openchatcut` 并构建本地 CLI 适配器，不安装或打包桌面应用。脚本不会覆盖已有目录；中断时先检查目录再处理。CLI 与桌面应用版本、浏览器和渲染依赖必须兼容；该适配流程在新环境尚需实际导出验收。也可以通过 `MIXCUT_OCC_CLI` 指向兼容的现有 CLI，参见 `tools/occ`。如需 OpenChatCut 官方 Skill，请按其官方仓库指引单独安装。

## 4. 启动和运行

```sh
./mixcut --json doctor
PYTHONPATH=src .venv/bin/python -m mixcut.workbench --workspace "$PWD" --port 8768
```

本机打开 `http://127.0.0.1:8768/`。若要嵌入飞书，需在自己的多维表中配置内嵌网页并确保该地址可由实际使用设备访问；`127.0.0.1` 只指向访问者自己的电脑。建立飞书和本地服务之间的网络连接、权限和审核流程由使用者按自己的环境配置。

在项目目录打开 Codex，使用随仓库提供的两个项目 Skill。若当前 Codex 客户端未自动发现 `.agents/skills`，参照客户端的 Skill 安装文档导入对应目录后重新打开会话。先用素材整理 Skill 建立事实索引，再使用编导 Skill；详情见 [流程说明](DIRECTOR_WORKFLOW.md)。
