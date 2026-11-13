"""MC Java -> Bedrock Vibrant Visuals 转换内核（参考实现）。

分层对应 PRD 1.6：
  core.rules.*      规则包（可热更新，来源可追溯）
  core.models       数据模型与状态枚举
  core.clock        时间能力（FR-T-01~04）
  core.archive      S1 ArchiveReader / S2 MetaParser / S3 ShaderGuard
  core.convert      S4~S8 转换、生成、校验、打包
  core.pipeline     任务状态机、历史、预设、报告
"""
__version__ = "1.0.0"
