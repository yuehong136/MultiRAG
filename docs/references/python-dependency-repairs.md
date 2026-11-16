# Python 依赖修复记录

2026-11-16 的 CI 修复保留 Go 的冻结模块版本；本记录仅覆盖 Python 锁文件与对应加载接口。
锁文件是安装输入，扫描通过不能代替运行兼容验证。首轮失败见
[CI 43715823506](https://github.com/yuehong136/MultiRAG/actions/runs/43715823506)。

- FastMCP 与其 slim 运行时固定为 4.1.2，MCP SDK 固定为 2.2.0。
  新组合通过 55 项相关单测、22 项双向协议矩阵与真实 Agent 双服务器专项；
  旧 oracle 的版本及锁文件保持不变，独立脚本声明其既有镜像源并关闭项目配置继承。
- 本地嵌入栈固定 FlagEmbedding 1.4.2、Sentence Transformers 5.6.0、Transformers 5.23.0。
  BCEmbedding 0.1.5 的专属依赖元数据只移除已验证可放开的旧 Transformers 上界，保留其余声明。
  六处模型下载回退删除新版 Hub 已移除的 `local_dir_use_symlinks` 参数。
  `test_embedding_dependency_compatibility.py` 用自行生成的 tinyBERT 权重实际运行两种文件格式的
  embedding、query、rerank 与 CrossEncoder；`test_model_snapshot_download_contract.py` 验证六条回退。
- Infinity SDK 保持 0.7.0；其 setuptools 上界与实际源码使用不符，覆盖为 `>=84.1,<85`。
  适配经过该 SDK 实际导入及真实 Infinity 读写合同验证，不据此放宽 SDK 的其他版本范围。
- Accelerate 暂时固定到公开 PR 的不可变提交
  [`a35af225361fe0097900680e850c84bae97340b7`](https://github.com/huggingface/accelerate/pull/4214)。
  该补丁尚未合入发布版，需在有等效正式修复后替换；单纯升级到 1.15.0 仍保留原加载实现。
  `test_accelerate_checkpoint_contract.py` 对实际安装的加载器验证本地分片、Hub 式符号链接，以及
  非法分片在权重 I/O 前被拒绝。当前补丁会拒绝检查点目录字符串 `.`；已验证的应用模型缓存
  使用绝对目录，不把本次结果扩大为该第三方库所有路径形式的兼容证明。
- 其余可修告警通过锁文件定向升级；未增加扫描豁免，删除已解除约束的 Transformers 整包豁免
  和 setuptools 单项豁免。仍保留的历史约束及复查条件以 `osv-scanner.toml` 为唯一来源。

本地模型组合 39 项通过，OSV 2.4.0 在当前豁免配置下无未豁免告警。
原始退出、输入哈希和首败保存在本机验证目录
`20261115-remote-go-alignment-ci-4d8d7f20`。这些结果不代表真实供应商或生产环境验收；
最终组合还须通过同一提交的完整远程 `ci-required`。
