# 版本管理策略（Versioning）

MC 材质转换器（离线版）采用**三套相互独立的版本号**，互不耦合，避免"改个 UI 也要动
转换规则版本"之类的问题。

## 1. 应用版本（App Version）— SemVer

| 字段 | 含义 | 当前 |
|---|---|---|
| `versionName` | 对外展示，语义化 `主.次.修订` | **1.4.5** |
| `versionCode` | 单调递增整数，Google Play / 系统判新依据 | **10** |

- 规则：**每次发布 `versionCode += 1`**；`versionName` 按语义化版本递进。
- 递增指引：
  - **主版本（MAJOR）**：不兼容的架构/行为变更（如更换内核、改动产物格式）。
  - **次版本（MINOR）**：新增功能且向后兼容（如本次的预设选择、预览画廊、历史、提速）。
  - **修订（PATCH）**：缺陷修复、规则无关的微调。
- 历史：`1.0.0`(code 1) → `1.1.0`(code 2) → `1.2.0`(code 3) → `1.3.0`(code 4) →
  `1.4.0`(code 5) → `1.4.1`(code 6) → `1.4.2`(code 7) → `1.4.3`(code 8) →
  `1.4.4`(code 9) → `1.4.5`(code 10)。

## 2. 规则包版本（rules_version）

- 位置：`app/src/main/python/core/rules/bundle.json` → `"rules_version"`。
- 当前：**2026.11.13-3**。
- 含义：LabPBR→VV 映射表、校验 Schema、能力白名单、约束的版本。**仅当转换逻辑 /
  映射 / 校验规则变更时才递增**，与 App 版本解耦。
- 格式建议：`YYYY.MM.DD-N`（当日第 N 次规则修订）。

## 3. 引擎版本（engine_version）

- 位置：`core/pipeline.py` → `bootstrap()` 由 `bundle.engine_min_version` 推导。
- 含义：核心转换管线（pipeline/convert/archive）的版本，供报告与日志追溯。
- 仅当**管线代码本身**有重大变更时更新。

## 发版检查清单

- [ ] `build.gradle` 的 `versionCode` +1、`versionName` 同步更新
- [ ] 如改动转换规则，`bundle.json` 的 `rules_version` 递增
- [ ] `CHANGELOG.md` 追加本次变更
- [ ] `git tag v<versionName>` 打标签
- [ ] 构建产物重命名为 `MC材质转换器-离线版-v<versionName>.apk`

## 分支约定

- `main`：稳定可发布。
- `dev`：开发集成分支。
- 功能/修复用 `feature/*`、`fix/*` 短分支，合并回 `dev` 后发版。
