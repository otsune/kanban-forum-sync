# kanban-forum-sync

Hermes Kanban → Discord Forum 同期プラグイン

Kanban 上のタスクを Discord Forum Channel のスレッドとして自動同期します。ステータス変更は Forum のタグとして反映され、完了したタスクは自動アーカイブされます。

## Features

- **一方向同期 (Phase 1)** — Kanban のタスク作成・更新・完了を Discord Forum スレッドに自動反映
  - 新規タスク → Forum スレッド自動作成
  - ステータス変更 → Forum タグ更新 (`triage` → `Triage`, `running` → `Running`, etc.)
  - タイトル変更 → スレッド名更新
  - 完了/アーカイブ → スレッド自動アーカイブ
- **リアルタイムポーリング** — 15秒間隔で Kanban DB の変更を検出
- **自動チャンネル解決** — Forum チャンネル未指定時は Bot の参加サーバーを検索し、`kanban`/`task-board` という名前の Forum を自動発見。なければ自動作成
- **管理者ガイド表示** — 権限不足などで Forum 作成不可の場合は、設定手順を表示
- **レート制限対策** — Discord API の 429 レスポンスに自動バックオフ対応
- **CLI サブコマンド** — `hermes kanban-forum-sync status|start|stop|sync`
- **自動起動** — Hermes 起動時に同期ワッチャーが自動開始

## Requirements

- [Hermes Agent](https://hermes-agent.nousresearch.com/) (Kanban 機能が有効であること)
- Discord Bot Token（[Discord Developer Portal](https://discord.com/developers/applications) で作成）

## Installation

```bash
# プラグインをクローン
git clone https://github.com/otsune/kanban-forum-sync.git \
  ~/.hermes/plugins/kanban-forum-sync

# プラグインを有効化
hermes plugin enable kanban-forum-sync
```

## Setup

### 1. Discord Bot の作成

1. [Discord Developer Portal](https://discord.com/developers/applications) で新規アプリケーションを作成
2. 「Bot」タブで Token を生成 → `FORUM_SYNC_BOT_TOKEN`
3. 「OAuth2 → URL Generator」で権限設定:
   - **SCOPES:** `bot`
   - **BOT PERMISSIONS:** `Send Messages`, `Manage Threads`, `Read Message History`, `Create Public Threads`
4. 生成された URL で Bot をサーバーに招待

### 2. Forum チャンネル（自動解決可能）

`FORUM_SYNC_CHANNEL_ID` を設定しない場合、起動時に Bot が参加している全サーバーをスキャンし、`kanban` または `task-board` という名前の Forum チャンネルを自動発見します。

見つからない場合は `#kanban` という名前の Forum チャンネルを**自動作成**します。
Bot にチャンネル管理権限がない場合は、エラーメッセージと設定手順を表示します。

手動で設定する場合は以下の手順で:

1. Discord サーバーに Forum チャンネルを作成（名前は `kanban` 推奨）
2. チャンネルに以下の8つのタグを作成（名前を正確に一致）:

| タグ名 | 色 | 対応 Kanban Status |
|--------|-----|-------------------|
| 🟡 Triage | Yellow | `triage` |
| ⬜ Todo | Grey | `todo` |
| 📅 Scheduled | Blue | `scheduled` |
| 🟢 Ready | Green | `ready` |
| 🔵 Running | Blue | `running` |
| 🔴 Blocked | Red | `blocked` |
| 👀 Review | Purple | `review` |
| ✅ Done | Green | `done`, `archived` |

3. Forum チャンネルの ID を取得 → `FORUM_SYNC_CHANNEL_ID`（未設定でも自動解決可）

### 3. 環境変数の設定

```bash
export FORUM_SYNC_BOT_TOKEN="your_discord_bot_token"
export FORUM_SYNC_CHANNEL_ID="your_forum_channel_id"  # 省略可（自動解決）
export FORUM_SYNC_POLL_INTERVAL="15"  # オプション、デフォルト15秒
```

`.env` ファイルまたは Hermes の `config.yaml` に設定してください。

### 4. Hermes の再起動

```bash
hermes plugin enable kanban-forum-sync
# Hermes を再起動するか、以下で手動開始:
hermes kanban-forum-sync start
```

## Usage

```bash
# 同期状態の確認
hermes kanban-forum-sync status

# 全タスクのフル同期を手動実行
hermes kanban-forum-sync sync

# ワッチャーの停止/開始
hermes kanban-forum-sync stop
hermes kanban-forum-sync start
```

## Status Mapping

| Kanban Status | Forum Tag | Action |
|--------------|-----------|--------|
| `triage` | 🟡 Triage | スレッド作成 |
| `todo` | ⬜ Todo | タグ更新 |
| `scheduled` | 📅 Scheduled | タグ更新 |
| `ready` | 🟢 Ready | タグ更新 |
| `running` | 🔵 Running | タグ更新 |
| `blocked` | 🔴 Blocked | タグ更新 |
| `review` | 👀 Review | タグ更新 |
| `done` | ✅ Done | タグ更新 + アーカイブ |
| `archived` | ✅ Done | アーカイブ |

## Project Structure

```
~/.hermes/plugins/kanban-forum-sync/
├── plugin.yaml          # プラグインマニフェスト
├── __init__.py          # register(ctx), CLI サブコマンド, 起動フック
├── syncer.py            # コア同期エンジン
├── discord_forum.py     # Discord REST API クライアント
├── kanban_bridge.py     # Kanban SQLite DB ブリッジ
├── models.py            # SyncMap (JSON永続化), SyncState
├── LICENSE              # MIT License
└── README.md
```

## Roadmap

- **Phase 1** ✅ — Kanban → Forum 一方向同期（完了）
- **Phase 2** 🔄 — コメント同期 + Forum → Kanban フィードバック
- **Phase 3** 📋 — エラーハンドリング強化、設定ガイドの改善

## License

MIT
