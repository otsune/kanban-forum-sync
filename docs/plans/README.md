# 開発計画書・監査記録

kanban-forum-sync の設計・実装計画および監査の記録を集約したフォルダ。
実装の経緯と意図を残すためのもので、プラグインの動作には影響しない。

| ファイル | 種別 | 概要 |
|---|---|---|
| [PLUGIN_CONFORMANCE_PLAN.md](PLUGIN_CONFORMANCE_PLAN.md) | 実装計画 | Hermes 公式ガイド準拠の体裁整備（エージェントツール / スラッシュコマンド / タグ変更通知 / 標準レイアウト）の計画 |
| [WATCHDOG_MIGRATION_PLAN.md](WATCHDOG_MIGRATION_PLAN.md) | 実装計画 | イベント駆動監視を inotify から watchdog へ移行しクロスプラットフォーム対応する計画（3段フォールバック） |
| [RATE_LIMIT_PLAN.md](RATE_LIMIT_PLAN.md) | 実装計画 | Discord レート制限対策（429 分離・共有スレッドリスト・サイクルバックオフ）の計画 |
| [ATTACHMENT_TOOLSET_PR_PLAN.md](ATTACHMENT_TOOLSET_PR_PLAN.md) | 上流 PR 計画 | Discord 添付ファイル本同期に必要な hermes-agent 側 attachment toolset（PR #36019）の計画と切替手順 |
| [AUDIT.md](AUDIT.md) | 監査記録 | コードレビュー/監査で見つかった項目と対応状況 |
| [REFACTORING_PLAN.md](REFACTORING_PLAN.md) | リファクタ手順書 | 挙動不変のコード品質改善 8 ステップ（OpenCode 実装用。型付き例外統一・重複排除・i18n 分離など） |

> コード本体はガイド標準どおりプラグインルート直下にフラット配置。計画/監査系 md のみここへ集約している。
