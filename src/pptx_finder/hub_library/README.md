# Hub 索引与报告协议

从 AI Hub 基线 84fbaa89 的 core/company-assistant/pptutor/pptutor_core 移植
library、text、selection、assessment、report 与版本常量，保留经过使用的查询、
来源指纹、条件判定和引语校验。只含本仓库代码，不依赖 Hub 安装或 skill 目录。

Doctor 的 hub_cli 负责文件协议、全局来源编号、状态/进度、组件与转字编排。
新增读取时的文件路径比对，防止重建索引后行号复用指向另一文件。
