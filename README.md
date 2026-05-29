# kanban-forum-sync

Hermes Kanban → Discord Forum 同期プラグイン

Kanban 上のタスクを Discord Forum Channel のスレッドとして自動同期します。ステータス変更は Forum のタグとして反映され、完了したタスクは自動アーカイブされます。

## Features

- **双方向同期** — Kanban ↔ Discord Forum のリアルタイム同期
  - **Phase 1 (Kanban → Forum):** 新規タスク → スレッド自動作成、ステータス変更 → タグ更新、完了/アーカイブ → スレッド自動アーカイブ
  - **Phase 2 (Forum → Kanban):** Discord のタグ変更 → Kanban ステータス更新、スレッドへの返信 → Kanban コメントとして追記
  - **Phase 3 (Forum → Kanban 新規タスク):** Forum で人間が新規スレッドを作成 → Kanban タスクとして自動作成 🆕
- **変更検出モード (選択可)**
  - **ポーリング（デフォルト）** — 設定間隔（デフォルト 15 秒）で Kanban DB を定期確認
  - **inotify（Linux）** — OS の inotify で `kanban.db` への書き込みを即時検知。追加ライブラリ不要
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
hermes plugins enable kanban-forum-sync
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
export FORUM_SYNC_POLL_INTERVAL="15"                  # オプション、デフォルト15秒
export FORUM_SYNC_LANG="ja"                           # オプション、タグ言語（en / ja、デフォルト en）
export FORUM_SYNC_EVENT_DRIVEN="1"                    # オプション、inotify モード有効化（Linux のみ）
```

#### inotify モード (`FORUM_SYNC_EVENT_DRIVEN=1`)

Linux 環境でのみ有効。`kanban.db` と `kanban.db-wal` を OS の inotify で監視し、Hermes が DB に書き込んだ瞬間に同期を実行します。ポーリング間隔による遅延がなくなり、アイドル時の CPU 消費もゼロになります。

- 追加ライブラリ不要（ctypes + select で実装）
- inotify が利用できない環境（一部の Docker、非 Linux）では自動的にポーリングにフォールバック
- `FORUM_SYNC_POLL_INTERVAL` はフォールバックタイムアウト兼 Discord 側ポーリング間隔として継続使用

> **macOS / Windows ユーザーへ**
> `FORUM_SYNC_EVENT_DRIVEN=1` を設定してもポーリングにフォールバックします（inotify は Linux 専用のため）。
> macOS の FSEvents や Windows の ReadDirectoryChangesW に対応したクロスプラットフォーム実装は
> [`watchdog`](https://pypi.org/project/watchdog/) ライブラリで実現できますが、**現在未実装**です。
> それまでは `FORUM_SYNC_POLL_INTERVAL` を短く設定することで代替できます。

`.env` ファイルまたは Hermes の `config.yaml` に設定してください。

### 4. Hermes の再起動

```bash
hermes plugins enable kanban-forum-sync
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

`FORUM_SYNC_LANG` でタグ名の言語を切り替えられます（デフォルト: `en`）。

| Kanban Status | Forum Tag (en) | Forum Tag (ja) | Action |
|---|---|---|---|
| `triage` | 🩺 Triage | 🩺 トリアージ | スレッド作成 |
| `todo` | 📝 Todo | 📝 未着手 | タグ更新 |
| `scheduled` | 📅 Scheduled | 📅 予定済み | タグ更新 |
| `ready` | ✅ Ready | ✅ 着手可能 | タグ更新 |
| `running` | 🔄 Running | 🔄 進行中 | タグ更新 |
| `blocked` | 🚧 Blocked | 🚧 停滞中 | タグ更新 |
| `review` | 👀 Review | 👀 レビュー中 | タグ更新 |
| `done` | 🎉 Done | 🎉 完了 | タグ更新 + アーカイブ |
| `archived` | 🎉 Done | 🎉 完了 | アーカイブ |

> **言語切り替え時の注意**
> `FORUM_SYNC_LANG` を変更すると、起動時に新しい言語のタグが Discord に作成されます。
> 古い言語のタグは残るため、Discord の Forum チャンネル設定から手動で削除してください。

## Project Structure

```
~/.hermes/plugins/kanban-forum-sync/
├── plugin.yaml          # プラグインマニフェスト
├── __init__.py          # register(ctx), CLI サブコマンド, 起動フック
├── syncer.py            # コア同期エンジン
├── discord_forum.py     # Discord REST API クライアント
├── kanban_bridge.py     # Kanban SQLite DB ブリッジ
├── models.py            # SyncMap (JSON永続化), SyncState, ThreadMetaTracker
├── kanban_watcher.py    # inotify ベースの DB 変更検知（Linux）
├── LICENSE              # MIT License
└── README.md
```

## Roadmap

- **Phase 1** ✅ — Kanban → Forum 一方向同期
- **Phase 2** ✅ — コメント同期 + Forum → Kanban フィードバック
- **Phase 3** ✅ — Forum 新規スレッド → Kanban タスク自動作成 🆕
- **inotify** ✅ — イベント駆動モード（Linux）
- **Phase 4** 📋 — Discord スラッシュコマンド、複数 Forum チャンネル対応

## License

MIT
