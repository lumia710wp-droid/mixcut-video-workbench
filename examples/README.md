# 合成测试数据说明

本目录所有对象`is_fixture=true`。TEST_BRAND、TEST_GOODS、fixture审批人、视频文件名和哈希均为软件测试数据；**视频文件不存在，哈希不是任何用户真实文件的指纹。**不能用于生产、真实审核、成片交付或商品宣传。

source.fixture.jsonl包含5个源文件对象；clip.fixture.jsonl包含5个连续区间；其余6份JSON依次描述词表、商品、批次、脚本、精确计划与尚未运行的状态，共16条记录。计划为5镜头、720帧、24秒；music_only允许无音轨，仅验证契约，不代表已经生成静音MP4。

snapshot_sha256按sources/clips/product/taxonomy合并对象计算；plan hash不含plan自身hash和build_id。校验器会交叉检查引用、品牌商品、批准摘要、源区间、时间线和字幕。

G1真正测试时，Codex应生成或选用获授权的真实媒体文件，重新probe/hash、替换source和working媒体记录、导入OpenChatCut、回读并渲染。不能把这里的假文件hash作为对真实视频的验证结果。生产代码必须拒绝fixture数据及fixture审批channel。
