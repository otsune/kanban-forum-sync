# 計画: kanban-forum-sync を Hermes 公式ガイド準拠の体裁に整える

## Context

このプラグインは動作はするが、Hermes 公式プラグイン開発ガイドの「標準的な体裁」を満たしていない:

- **エージェント面が無い**: ツール (`register_tool`) もセッション内スラッシュコマンド (`register_command`) も未登録。CLI サブコマンド (`hermes kanban-forum-sync …`) だけで、LLM／Discord セッションからは操作できない。`CLAUDE.md` の "What's not yet implemented (Phase 3)" にスラッシュコマンドとタグ変更通知が未実装として残っている。
- **manifest が不完全/不整合**: `provides_tools`/`provides_hooks` が無い。`requires_env` に任意変数（`FORUM_SYNC_CHANNEL_ID`/`POLL_INTERVAL`/`LANG`/`EVENT_DRIVEN`）と、ガイドのスキーマに無い `default:` キーが混在。ガイドでは `requires_env` の項目は `hermes plugins install` 時に**プロンプトされる必須前提**なので、任意変数を入れるのは不適切。
- **レイアウトがガイド標準でない**: ガイドの推奨は `plugin.yaml / __init__.py / schemas.py / tools.py`。本プラグインはツール定義用の `schemas.py`・`tools.py` を持たない。
- **作業ツリーに stray ファイル**: `sync_map.json.tmp.*`・`*.json.lock` の中途半端な残骸（gitignore 済みだが残っている）。

目的: 公式ガイド（[build-a-hermes-plugin](https://hermes-agent.nousresearch.com/docs/guides/build-a-hermes-plugin) / [features/plugins](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins)）に沿って、**エージェント向けツール・スラッシュコマンド・タグ変更通知**を追加し、manifest とディレクトリ構成を標準形に整える。**pip パッケージ化は今回スコープ外**（ユーザー選択により除外）。

---

## ガイド準拠の要点（根拠）

- **handler 契約**: `def handler(args: dict, **kwargs) -> str`。**常に JSON 文字列を返す／例外を投げない／`**kwargs` を受ける**。
- **manifest**: `name/version/description` 必須。`provides_tools`・`provides_hooks` で提供物を宣言。`requires_env` は install 時にプロンプトされる（= 必須のみ列挙）。`kind` は特殊型（platform/model-provider/backend/memory）用で、本プラグインは標準型なので **付けない**。
- **ツール登録**: `ctx.register_tool(name, schema, handler, toolset=…, check_fn=…)`。`toolset` で論理グルーピング。有効化された瞬間から LLM が呼べる。
- **スラッシュコマンド**: `ctx.register_command(name, handler, description)`。handler は raw args 文字列を受け **文字列を返す**。CLI とゲートウェイ（Discord/Telegram）両方で `/name` として使える。同期/非同期どちらも可。
- **タグ変更通知**: Hermes ライフサイクルフック（`pre/post_tool_call` 等）**ではない**。`ctx.inject_message(content, role)` でエージェント会話にメッセージを差し込む方式（`CLAUDE.md` 既述）。したがって manifest の `provides_hooks` には該当せず、**`provides_hooks` は空のまま**。
- **有効化**: `plugins.enabled`（config.yaml）＋ `hermes plugins enable <name>`。ツールはプロファイル単位フィルタが無く、有効化で即露出。

---

## 目標ディレクトリ構成（標準レイアウトへ）

既存のエンジン系モジュールは維持し、ガイド標準のツール面（`schemas.py`/`tools.py`）と、循環 import を避ける小さなランタイムアクセサを追加する。

```
kanban_forum_sync/
├── plugin.yaml          # ← provides_tools 追加 / requires_env を必須のみに整理
├── __init__.py          # ← register(ctx): 既存CLI + ツール + スラッシュコマンド登録
├── schemas.py           # ★新規: ツールスキーマ（LLM が見る定義）
├── tools.py             # ★新規: ツールハンドラ（JSON文字列返却・例外を投げない）
├── service.py           # ★新規: シングルトン _get_syncer() を集約（__init__/tools 共用）
├── syncer.py            # 既存エンジン（タグ変更通知の inject_message を追加）
├── discord_forum.py     # 既存
├── kanban_bridge.py     # 既存
├── kanban_watcher.py    # 既存
├── models.py            # 既存
├── README.md            # ← 新ツール/スラッシュ/通知/任意env を記載
├── CLAUDE.md            # ← Phase3 未実装項目を実装済みに更新
└── docs/                # （任意）計画系mdの集約はスコープ外。今回はルート維持
```

> 注: `syncer.py` 等の追加モジュールはガイド標準を逸脱しない（標準は最小構成であって制限ではない）。`schemas.py`/`tools.py` を「ツール面の標準形」として追加するのが主眼。

---

## 詳細変更

### 1. `service.py`（新規）— シングルトン集約

`__init__.py` 内の `_get_syncer()` / `_syncer_instance` / `_plugin_ctx` をここへ移し、`__init__.py` と `tools.py` の双方から循環 import なしで使えるようにする。

```python
# service.py（要旨）
import os, logging
from .syncer import KanbanForumSyncer

logger = logging.getLogger(__name__)
_syncer_instance = None
_plugin_ctx = None

def set_ctx(ctx): ...
def get_ctx(): ...
def get_syncer():           # 既存 _get_syncer のロジックをそのまま移植
    ...
def get_syncer_or_none():   # RuntimeError を握って None（ツール/CLIの安全版）
    ...
```

`__init__.py` は `from . import service` で参照し、既存の `cli_*` 関数は `service.get_syncer_or_none()` を使う形に置換（挙動不変）。

### 2. `schemas.py`（新規）— ツールスキーマ

エージェントから同期状態の確認と再同期を行えるツールを2つ定義。説明文は具体的に（ガイドの "vague description" 回避）。

- `KANBAN_FORUM_SYNC_STATUS` … 引数なし。Forum 同期 watcher の状態（state/channel/last_sync/counters/last_error）を返す。
- `KANBAN_FORUM_SYNC_RESYNC` … 任意引数 `mode`（`"incremental"`|`"full"`、既定 `incremental`）。即時に1サイクル同期を実行。

```python
KANBAN_FORUM_SYNC_STATUS = {
    "name": "kanban_forum_sync_status",
    "description": "Report the Kanban↔Discord Forum sync watcher status: "
                   "running state, resolved forum channel id, last sync time, "
                   "counts of synced tasks/comments/tags, and last error if any.",
    "parameters": {"type": "object", "properties": {}},
}

KANBAN_FORUM_SYNC_RESYNC = {
    "name": "kanban_forum_sync_resync",
    "description": "Trigger an immediate Kanban↔Discord Forum sync cycle. "
                   "mode='incremental' (default) processes new changes; "
                   "mode='full' re-runs initial_sync without clearing the map.",
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["incremental", "full"],
                     "description": "Sync depth. Default 'incremental'."},
        },
    },
}
```

### 3. `tools.py`（新規）— ツールハンドラ

handler 契約厳守: **JSON 文字列を返す／例外を投げない／`**kwargs`**。

```python
import json
from . import service

def kanban_forum_sync_status(args: dict, **kwargs) -> str:
    try:
        syncer = service.get_syncer_or_none()
        if syncer is None:
            return json.dumps({"error": "syncer unavailable (missing bot token?)"})
        s = syncer.get_state()
        return json.dumps({
            "state": s.state, "channel_id": syncer.channel_id,
            "last_sync": str(s.last_sync), "tasks": s.task_count,
            "comments": s.comment_count, "tag_syncs": s.tag_sync_count,
            "forum_tasks": s.forum_task_count, "last_error": s.last_error,
        })
    except Exception as e:
        return json.dumps({"error": f"status failed: {e}"})

def kanban_forum_sync_resync(args: dict, **kwargs) -> str:
    try:
        syncer = service.get_syncer_or_none()
        if syncer is None:
            return json.dumps({"error": "syncer unavailable"})
        mode = (args.get("mode") or "incremental").strip().lower()
        if mode == "full":
            syncer.full_sync()
        else:
            syncer.incremental_sync()
        return json.dumps({"ok": True, "mode": mode})
    except Exception as e:
        return json.dumps({"error": f"resync failed: {e}"})
```

> `full_sync()` は `sync_map.clear()` しない安全版（既存仕様）なので重複スレッドは作らない。

### 4. `__init__.py` — register(ctx) でツール＋スラッシュコマンド登録

`register(ctx)` に追記（既存の CLI 登録・watcher 自動起動は維持）:

```python
from . import service, schemas, tools

def register(ctx):
    service.set_ctx(ctx)
    # 既存: CLI コマンド + watcher 自動起動 ...

    # ツール（toolset="kanban_forum_sync"）
    ctx.register_tool(name="kanban_forum_sync_status",
                      toolset="kanban_forum_sync",
                      schema=schemas.KANBAN_FORUM_SYNC_STATUS,
                      handler=tools.kanban_forum_sync_status)
    ctx.register_tool(name="kanban_forum_sync_resync",
                      toolset="kanban_forum_sync",
                      schema=schemas.KANBAN_FORUM_SYNC_RESYNC,
                      handler=tools.kanban_forum_sync_resync)

    # スラッシュコマンド /kanban-forum-sync
    ctx.register_command(name="kanban-forum-sync",
                         handler=_slash_handler,
                         description="Kanban↔Discord Forum sync: status|sync|start|stop")
```

スラッシュハンドラ（raw args → 文字列）:

```python
def _slash_handler(raw_args: str) -> str:
    arg = (raw_args or "").strip().split() or ["status"]
    action = arg[0].lower()
    syncer = service.get_syncer_or_none()
    if syncer is None:
        return "kanban-forum-sync: syncer unavailable (bot token 未設定?)"
    if action == "status":
        s = syncer.get_state()
        return (f"state={s.state} channel={syncer.channel_id or '(auto)'} "
                f"tasks={s.task_count} comments={s.comment_count} "
                f"tags={s.tag_sync_count} forum_tasks={s.forum_task_count}"
                + (f"\nlast_error={s.last_error}" if s.last_error else ""))
    if action == "sync":
        syncer.full_sync();  return "Full sync complete."
    if action == "start":
        syncer.start();      return "Watcher started."
    if action == "stop":
        syncer.stop();       return "Watcher stopped."
    return f"unknown action '{action}'. use: status|sync|start|stop"
```

### 5. タグ変更通知（`syncer.py` + ctx）

`__init__`（syncer）で `self._ctx = ctx` を保持（現状は `self.kanban.ctx` のみ）。`_sync_forum_tags()` がステータス更新に成功した箇所（`syncer.py` の `update_task_status` 成功後、`changed += 1` 付近）で、ctx があれば通知を差し込む:

```python
if self._ctx is not None:
    try:
        self._ctx.inject_message(
            f"[forum-sync] Discord タグ変更により task-{task_id} を "
            f"{current['status']} → {new_status} に更新しました。",
            role="user",
        )
    except Exception as e:
        logger.debug("inject_message failed (gateway無し等): %s", e)
```

> `inject_message` は CLI 参照の無いゲートウェイモードで `False` を返す等あり得るため必ず try/except。失敗は debug ログのみで同期は継続。

### 6. `plugin.yaml` — manifest 整合

- `provides_tools: [kanban_forum_sync_status, kanban_forum_sync_resync]` を追加。
- `provides_hooks`: 追加**しない**（タグ通知は inject_message でライフサイクルフックではない）。
- `requires_env` は **必須の `FORUM_SYNC_BOT_TOKEN` のみ**に整理（install 時プロンプト対象）。`default:` キーは全廃。
- 任意変数（`FORUM_SYNC_CHANNEL_ID`/`POLL_INTERVAL`/`LANG`/`EVENT_DRIVEN`、および `HERMES_KANBAN_DB`）は README の「任意環境変数」表へ移設。

```yaml
name: kanban-forum-sync
version: "1.1.0"          # ツール/スラッシュ/通知の追加に伴いマイナーバンプ
description: Sync Hermes Kanban tasks to Discord Forum channels
author: otsune
provides_tools:
  - kanban_forum_sync_status
  - kanban_forum_sync_resync
requires_env:
  - name: FORUM_SYNC_BOT_TOKEN
    description: >-
      Discord Bot Token (Developer Portal). 未設定時は DISCORD_BOT_TOKEN を使用。
    secret: true
```

### 7. ドキュメント

- **README.md**: 「Tools」「Slash command」「Tag-change notification」節を追加。任意環境変数の表（plugin.yaml から移したもの）を追加。ディレクトリ構成をガイド標準に合わせて記載。有効化手順（`hermes plugins enable kanban-forum-sync`）。
- **CLAUDE.md**: "What's not yet implemented (Phase 3)" からスラッシュコマンド／タグ変更通知を削除し、実装済みとして «Tools / Slash command / inject_message 通知» の説明を追加。`schemas.py`/`tools.py`/`service.py` をモジュール責務一覧に追記。

### 8. リポジトリ整理（stray ファイル）

- 作業ツリーの **`sync_map.json.tmp.*` と空の `*.json.lock`** 残骸を削除（中途半端な atomic-write/lock の名残。`.gitignore` 済みで追跡対象外）。
- **`sync_map.json` / `thread_meta.json` 本体は削除しない**（稼働中 watcher の生きた状態。消すと task↔thread マッピングを喪失）。
- `.gitignore` は既に網羅済み（変更不要）。

---

## スコープ外（今回やらない）

- **pip パッケージ化**（`pyproject.toml` + `[project.entry-points."hermes_agent.plugins"]`）— ユーザー選択により除外。将来配布時に別途。
- 添付ツールの本実装（別途 `ATTACHMENT_TOOLSET_PR_PLAN.md` / hermes-agent#36019 待ち）。

> 注: 計画/監査系 md（AUDIT.md / *_PLAN.md）は後に `docs/plans/` へ集約した（コードはガイド標準どおりルート直下フラットのまま）。

---

## 検証

1. **構文/単体**:
   ```
   python3 -m py_compile __init__.py service.py schemas.py tools.py syncer.py
   python3 -m unittest discover -s tests
   ```
   `tests/test_sync_safety.py` に、(a) ツールハンドラが JSON 文字列を返し例外を投げないこと（syncer 未初期化時も `{"error": …}`）、(b) `resync` が mode を分岐して正しい syncer メソッドを呼ぶこと（モック）、(c) スラッシュハンドラが各 action で期待文字列を返すこと、を追加。

2. **manifest 検証**:
   ```
   HERMES_PLUGINS_DEBUG=1 hermes plugins list   # ロード成功・manifest パースを確認
   ```

3. **エージェント面**（要 Hermes 完全再起動）:
   ```
   systemctl --user restart hermes-gateway.service
   ```
   - セッション内 `/plugins` で kanban-forum-sync が enabled。
   - `/kanban-forum-sync status` が状態文字列を返す（CLI / Discord 両方）。
   - LLM に「Forum 同期の状態を教えて」→ `kanban_forum_sync_status` ツールが呼ばれ JSON が返る。
   - Discord でタグを変更 → Kanban 反映に加え、エージェント会話に `[forum-sync] … 更新しました` が inject される（ゲートウェイ接続時）。

4. **既存機能リグレッション**: `hermes kanban-forum-sync status/start/stop/sync` が従来どおり動作（service.py 移設後も挙動不変）。

5. **反映の注意**: コード反映には Hermes プロセス完全再起動が必要（`hermes kanban-forum-sync stop/start` はモジュール再読込しない既知挙動）。

---

## 主要変更ファイル

| ファイル | 種別 | 内容 |
|---|---|---|
| `service.py` | 新規 | シングルトン `get_syncer()`/ctx 集約（循環import回避） |
| `schemas.py` | 新規 | 2ツールのスキーマ |
| `tools.py` | 新規 | 2ツールのハンドラ（JSON返却・例外非送出） |
| `__init__.py` | 改修 | register で register_tool ×2 + register_command。CLI は service 経由に置換 |
| `syncer.py` | 改修 | `self._ctx` 保持 + タグ変更時 inject_message |
| `plugin.yaml` | 改修 | provides_tools 追加 / requires_env を必須のみ / default 廃止 / version 1.1.0 |
| `README.md` | 改修 | tools/slash/通知/任意env/構成/有効化 |
| `CLAUDE.md` | 改修 | Phase3 実装済み反映・新モジュール追記 |
| （作業ツリー） | 整理 | `*.json.tmp.*` / `*.json.lock` 残骸削除（state本体は保持） |
