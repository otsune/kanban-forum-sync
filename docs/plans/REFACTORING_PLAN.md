# リファクタリング手順書（OpenCode 実装用）

kanban-forum-sync のコード品質改善のためのリファクタリング候補と実施手順。
**機能追加・挙動変更は一切行わない**。各ステップは独立しており、上から順に 1 ステップ = 1 コミットで進めること。

作成: 2026-07-06 / 対象リビジョン: `ca8514d`

---

## 前提条件

### テストの実行方法

プラグインルート（このファイルの2つ上のディレクトリ）で実行する:

```bash
python3 -m unittest tests.test_sync_safety                          # 必須ゲート（全ステップで green 維持）
python3 -m unittest tests.test_sync_safety tests.test_integration tests.test_default_assignee   # フルスイート
```

### テストベースライン（2026-07-06 時点、リファクタ前）

- `tests.test_sync_safety` — **全 green**。これが必須ゲート。1件でも落ちたらそのステップの変更をやり直す。
- `tests.test_integration` — 既存 FAIL 3件 + ERROR 1件:
  - `test_end_to_end_sync_new_task_creates_thread` (FAIL: created_threads 0 != 1)
  - `test_conflict_resolution_incremental_sync_serial` (FAIL)
  - `test_data_consistency_after_full_cycle` (FAIL)
  - `test_rate_limit_retry_then_success` (ERROR)
- `tests.test_default_assignee.ConfigDefaultAssigneeTest` — ERROR 6件。原因は実行環境に `hermes_cli` モジュールが無いこと（`ModuleNotFoundError`）。コードの不具合ではない。

**合格基準は「test_sync_safety が green」かつ「フルスイートの失敗数がベースラインから増えない」**。

### 共通ルール（全ステップ）

1. 挙動を変えない。ログメッセージの文言・レベルも原則維持（ステップ内で明示された場合のみ変更可）。
2. モジュールの公開名（テストが import している名前）を壊さない。特に:
   - `kanban_forum_sync.syncer` から `KanbanForumSyncer`, `_build_tag_tables`, `STATUS_TO_TAG`, `TAG_TO_STATUS`, `ARCHIVE_STATUSES`, `REQUIRED_TAGS`, `get_forum_guidelines`, `get_admin_guide_message` が import できること
   - `kanban_forum_sync.models` から `SyncMap`, `SyncState`, `ThreadMetaTracker`, `SyncOriginTracker`, `db_slug`, `watcher_lock_path` が import できること
   - `SyncMap(path=...)`, `ThreadMetaTracker(path=...)`, `SyncOriginTracker(path=...)` のコンストラクタ形式（テストが `path=` 指定で生成している）
3. 既存の日本語 docstring / コメントは保持する（移動はよいが削除しない。コードの意図説明として機能している）。
4. 状態ファイル（`sync_map.json` 等）のファイル名・JSON 構造・アトミック書き込み（tmp + `os.replace` + flock）を変えない。
5. **at-least-once セマンティクスの不変条件**: 各カーソル（`last_message_id` / `last_comment_id` / `last_kanban_event_id` / `worker_log_count`）は「投稿・処理が成功した後」にのみ前進する。前進タイミングを 1 行たりとも前倒ししない。
6. コミットメッセージは `refactor(scope): 要約` 形式。例: `refactor(syncer): replace string-matched 404 checks with NotFoundError`。
7. 各ステップ完了時に必須ゲート + フルスイートを実行し、結果をコミットメッセージ本文に記録する。

---

## Step 0（推奨・任意）: テストベースラインの修復

リファクタの安全網を強くするため、既存のテスト失敗を先に潰す。**プロダクションコードは変更しないこと**（失敗原因はテスト側のセットアップ不備の可能性が高い）。

- 対象: `tests/test_integration.py` の FAIL 3件 + ERROR 1件。
  - `test_end_to_end_sync_new_task_creates_thread` は `KanbanForumSyncer.__new__` で手組みしたオブジェクトの属性不足が疑われる（`_sync_task_to_forum` 内の `except Exception` が握りつぶして `created_threads` が 0 のまま）。テスト実行時に `logging` を有効化して実際の例外を特定し、不足属性をテスト側で補う。
  - 修正不能な環境依存（実 API 必要など）であれば `@unittest.skipIf` + 理由コメントで明示的に skip する。
- 対象: `tests/test_default_assignee.py` の ERROR 6件。`hermes_cli` が import できない環境では `@unittest.skipIf(importlib.util.find_spec("hermes_cli") is None, ...)` で skip させる（`ConfigDefaultAssigneeTest` は `hermes_cli.config.load_config` のモックに実モジュールを要求している）。
- 受け入れ基準: フルスイートが green（または明示 skip のみ）。
- コミット: `test: repair integration test baseline (fix setup gaps, skip env-dependent cases)`

> このステップを飛ばす場合、以降の各ステップは「ベースラインから失敗を増やさない」で判定する。

---

## Step 1: 404 / 429 判定を型付き例外に統一（syncer.py）

**問題**: `discord_forum.py` に `NotFoundError` / `RateLimitError` という型付き例外があるのに、`syncer.py` は複数箇所で `"Resource not found" in str(e)` という文字列マッチで 404 を判定している。例外メッセージの文言変更で静かに壊れる。

**手順**:

1. `grep -n '"Resource not found" in str(e)' syncer.py` で全箇所を列挙する（`_sync_forum_comments`, `_sync_forum_tags`, `_sync_kanban_comments_to_forum` 内の3ブロック等、計5〜6箇所）。
2. 各箇所を、`except Exception` + 文字列判定から次の形へ書き換える:

   ```python
   except NotFoundError:
       logger.warning("Thread %s no longer exists; removing sync_map", thread_id)
       self._drop_stale_thread(thread_id)
   except Exception as e:
       logger.warning("Failed to fetch messages for thread %s: %s", thread_id, e)
   ```

   ※ 既存の `continue` / `break` / return の制御フローを各箇所で完全に維持すること。
3. `_is_rate_limit_cycle_error` を次に置き換える:

   ```python
   def _is_rate_limit_cycle_error(self, exc: Exception) -> bool:
       return isinstance(exc, RateLimitError) or getattr(exc, "http_code", None) == 429
   ```

   （`"Rate limit retries exhausted" in str(exc)` の文字列判定を廃止。この文言は `RateLimitError` 送出時にしか使われないため等価。）

**注意**: `NotFoundError` のメッセージは `"Resource not found: {path}"` であり、この文字列は `NotFoundError` 以外からは発生しない。したがって置き換えは意味的に等価。テストの `FakeDiscord` も `discord_forum.NotFoundError` / `DiscordForumError` を投げ分けているため互換。

- 受け入れ基準: 必須ゲート green。`grep '"Resource not found"' syncer.py` がヒットしない。
- コミット: `refactor(syncer): use typed NotFoundError/RateLimitError instead of string matching`

---

## Step 2: `_sync_kanban_comments_to_forum` の重複3ブロックを統合（syncer.py）

**問題**: コメント / ワーカーイベント / ワーカーログの3フィードで「Discord へ投稿 → 404 なら stale 掃除して break → その他エラーは warn して break → 成功ならカーソル前進」がほぼ同一のまま3回書かれている（約 90 行の重複）。

**手順**:

1. 投稿ヘルパーをメソッドとして追加する:

   ```python
   from enum import Enum

   class _PostResult(Enum):
       OK = "ok"
       GONE = "gone"      # スレッド 404 → stale 掃除済み
       FAILED = "failed"  # その他エラー（次サイクルで再試行）

   def _post_to_thread(self, thread_id: int, text: str) -> "_PostResult":
       """スレッドへ1メッセージ投稿する。404 は stale 掃除して GONE を返す。"""
       try:
           self.discord.send_message(thread_id, text[:_DISCORD_CONTENT_LIMIT])
           time.sleep(0.5)
           return _PostResult.OK
       except NotFoundError:
           logger.warning(
               "Thread %s not found while posting; removing stale mapping", thread_id
           )
           self._drop_stale_thread(thread_id)
           return _PostResult.GONE
       except Exception as e:
           logger.warning("Failed to post to thread %s: %s", thread_id, e)
           return _PostResult.FAILED
   ```

2. 3フィードのループをこのヘルパーで書き直す。**次の不変条件を厳守**:
   - カーソル前進（`set_last_comment_id` / `set_last_kanban_event_id` / `set_worker_log_count`）は `_PostResult.OK` の後にのみ行う。
   - **例外**: ワーカーイベントで `_format_worker_event(ev)` が空文字を返した場合は、投稿せずに `set_last_kanban_event_id` だけ前進する（現行挙動。空イベントで無限に足踏みしない）。
   - `GONE` / `FAILED` はそのフィードのループを `break`。`GONE` の場合はさらに同スレッドの後続フィードもスキップしてよい（sync_map から消えているため、残りフィードの API 呼び出しは無駄打ちになるだけ。`continue` で次の task へ進む）。
   - `posted` カウンタと最後の `logger.info("Synced %d comment/log(s)...")` は維持。
   - ハードコードされた `text[:2000]` は `_DISCORD_CONTENT_LIMIT` 定数参照に置き換える（値は同じ 2000）。
3. 発展（任意）: 3フィードを「(項目取得, テキスト整形, カーソル前進) のタプル列」として汎用ループ化してもよいが、可読性が落ちるなら3つの短いループのままでよい。ヘルパー抽出だけで重複の大半は消える。

- 受け入れ基準: 必須ゲート green。特に `test_comment_failure_does_not_advance_past_failed_message` と `test_sync_failure_does_not_advance_cursor_on_error` が green であること（カーソル前進の不変条件を検証している）。
- コミット: `refactor(syncer): extract _post_to_thread helper, dedupe comment/event/log feeds`

---

## Step 3: `ThreadMetaTracker` のアクセサ汎用化（models.py）

**問題**: `get/set_last_message_id`, `get/set_last_comment_id`, `get/set_last_kanban_event_id`, `get/set_worker_log_count` の4ペアが同一パターンのコピペ（各 8 行 × 8 メソッド）。

**手順**:

1. private 汎用アクセサを追加:

   ```python
   def _get_field(self, thread_id: int, field: str) -> int:
       with self._lock:
           return self._data.get(str(thread_id), {}).get(field, 0)

   def _set_field(self, thread_id: int, field: str, value: int) -> None:
       with self._lock:
           self._data.setdefault(str(thread_id), {})[field] = value
           self._save()
   ```

2. 既存の8つの公開メソッドは**シグネチャ・名前を維持したまま**、この2つへの1行委譲にする（docstring は維持）。呼び出し側（syncer.py / テスト）は一切変更しない。

- 受け入れ基準: 必須ゲート green。`thread_meta.json` の JSON 構造が不変。
- コミット: `refactor(models): dedupe ThreadMetaTracker field accessors`

---

## Step 4: JSON 永続ストアの基底クラス抽出（models.py）

**問題**: `SyncMap` / `SyncOriginTracker` / `ThreadMetaTracker` が `_path` / `_lock` / `_load` / `_save` / `clear` の同一ボイラープレートを3回持っている。

**手順**:

1. 基底クラスを追加:

   ```python
   class _JsonStore:
       """flock + アトミック書き込みで永続化されるスレッドセーフな JSON dict。"""
       _DUMP_KWARGS: dict = {"indent": 2}

       def __init__(self, path: str, slug: str = ""):
           self._path = _slugged(path, slug)
           self._lock = threading.RLock()
           self._data: dict = _load_json_dict(self._path)

       def _save(self) -> None:
           _atomic_save_json(self._path, self._data, **self._DUMP_KWARGS)

       def clear(self) -> None:
           with self._lock:
               self._data.clear()
               self._save()
   ```

2. 3クラスを `_JsonStore` 継承に変え、各クラス固有のメソッド（`get`/`set`/`items`/`contains_thread` 等）だけ残す。
   - `ThreadMetaTracker` は `_DUMP_KWARGS = {"indent": 2, "sort_keys": True}` をクラス属性で上書き（現行の `sort_keys=True` を維持）。
   - デフォルトパス引数は現行どおり各クラスに残す: `SyncMap(path=SYNC_MAP_PATH, slug="")` / `SyncOriginTracker(path=SyncOriginTracker.ORIGIN_PATH, ...)` / `ThreadMetaTracker(path=THREAD_META_PATH, ...)`。**コンストラクタシグネチャを変えない**（テストが `path=` キーワードで生成している）。
   - `SyncOriginTracker.ORIGIN_PATH` クラス属性は維持。

- 受け入れ基準: 必須ゲート green。既存 JSON ファイルがそのまま読める（構造・ファイル名不変）。
- コミット: `refactor(models): extract _JsonStore base for persistent JSON state`

---

## Step 5: `KanbanBridge` 読み取りクエリの共通化（kanban_bridge.py）

**問題**: `get_all_tasks` / `get_tasks_changed_since_event` / `get_task` / `get_latest_event_id` / `get_comments_since` / `get_events_since` が「`_connect()` → execute → `[dict(r) for r in rows]` → `finally: close()`」を6回繰り返している。

**手順**:

1. ヘルパーを追加:

   ```python
   def _query(self, sql: str, params: tuple = ()) -> list[dict]:
       conn = self._connect()
       try:
           return [dict(r) for r in conn.execute(sql, params).fetchall()]
       finally:
           conn.close()

   def _query_one(self, sql: str, params: tuple = ()) -> Optional[dict]:
       conn = self._connect()
       try:
           row = conn.execute(sql, params).fetchone()
           return dict(row) if row else None
       finally:
           conn.close()
   ```

2. 6つの読み取りメソッドを書き換える。SQL 文字列・パラメータ・返り値の形は完全に維持。
   - `get_latest_event_id` は `_query_one` を使い `(row or {}).get("max_id") or 0` 相当で 0 フォールバックを維持。
   - `get_events_since` の `kinds` 分岐（IN 句のプレースホルダ生成）はそのまま `_query` に渡す形にする。
3. ついでに型ヒントを直す: `kinds: list[str] = None` → `kinds: Optional[list[str]] = None`。

- 受け入れ基準: 必須ゲート green + `tests.test_integration` の bridge 系テスト（`test_kanban_bridge_reads_from_custom_db_path` 等）がベースラインどおり。
- コミット: `refactor(bridge): extract _query/_query_one SQLite read helpers`

---

## Step 6: i18n・タグテーブルを `i18n.py` へ分離（syncer.py の軽量化）

**問題**: `syncer.py`（1454 行）の先頭 ~250 行がロケールデータ・タグテーブル生成・ガイド文言で占められ、同期ロジックと無関係。

**手順**:

1. 新規ファイル `i18n.py` を作成し、以下を **そのまま移動**（ロジック変更禁止）:
   - `_LOCALE_DATA`, `_EXTRA_TAGS`, `_SUPPORTED_LANGS`, `_KANBAN_STATUSES`, `_STATUS_ALIASES`
   - `_build_tag_tables()`, `_normalize_kanban_status()`, `_make_tag_dict()`
   - `_LANG` の決定ロジック（`FORUM_SYNC_LANG` 読み取り + 未対応言語の warning）
   - `STATUS_TO_TAG`, `TAG_TO_STATUS`, `STATUS_TAG_EMOJI`, `REQUIRED_TAGS`, `ARCHIVE_STATUSES`
   - `ADMIN_GUIDE_MESSAGES`, `get_admin_guide_message()`, `FORUM_GUIDELINES`, `get_forum_guidelines()`, `_FORUM_GUIDE_URL`
2. `syncer.py` は `i18n.py` から import する。**後方互換のための再エクスポートを必ず残す**:

   ```python
   from .i18n import (  # noqa: F401 — 再エクスポート（テスト・外部参照の後方互換）
       _build_tag_tables, _normalize_kanban_status,
       STATUS_TO_TAG, TAG_TO_STATUS, STATUS_TAG_EMOJI, REQUIRED_TAGS,
       ARCHIVE_STATUSES, ADMIN_GUIDE_MESSAGES, FORUM_GUIDELINES,
       get_admin_guide_message, get_forum_guidelines, _LANG,
   )
   ```

   テストは `from kanban_forum_sync.syncer import _build_tag_tables` 等を参照しているため、これを欠かすと即失敗する。
3. `_format_worker_event()` と `_WORKER_LOG_KINDS` も i18n ではなくメッセージ整形なので、同時に新規 `formatting.py` へ移してよい（任意。移す場合も syncer から再エクスポートする）。移さない場合は syncer に残す。
4. `_ensure_tags()` 内のログ `"All 8 status tags already exist"` を `"All %d status tags already exist", len(REQUIRED_TAGS)` に変更（ハードコード 8 の除去。この文言変更のみ許可）。
5. `CLAUDE.md` の「Status ↔ tag mapping」節と「Module responsibilities」節に `i18n.py`（および作成した場合 `formatting.py`）を1行追記する。

- 受け入れ基準: 必須ゲート green（特に `test_build_tag_tables_keeps_backlog_out_of_status_to_tag`）。`FORUM_SYNC_LANG=ja python3 -c "from kanban_forum_sync.syncer import STATUS_TO_TAG; print(STATUS_TO_TAG)"` が日本語タグを出す。
- コミット: `refactor(syncer): move i18n data and tag tables to i18n.py`

---

## Step 7: ステータス payload の一元化（tools.py / __init__.py / models.py）

**問題**: 同期状態の整形が3箇所に分散している — `tools._state_payload()`（dict）、`__init__.cli_status()`（print 列挙）、`__init__._format_status()`(1行テキスト)。フィールド追加時に3箇所の更新漏れが起きる。

**手順**:

1. `KanbanForumSyncer` に公開メソッドを追加:

   ```python
   def status_dict(self) -> dict:
       """CLI / slash / agent tool が共有するステータス payload。"""
       state = self._state
       return {
           "state": state.state,
           "channel_id": self.channel_id,
           "last_sync": state.last_sync,
           "last_event_id": state.last_event_id,
           "tasks": state.task_count,
           "comments": state.comment_count,
           "tag_syncs": state.tag_sync_count,
           "forum_tasks": state.forum_task_count,
           "errors": state.error_count,
           "last_error": state.last_error,
       }
   ```

2. `tools._state_payload(syncer)` を `syncer.status_dict()` の呼び出しに置き換える（キー名・値は完全一致させ、agent tool の JSON 出力を変えない）。
3. `__init__.cli_status` と `_format_status` も `status_dict()` から値を取る形に書き換える。**表示文言（"Syncer state: ..." 等のラベルと並び順）は現行どおり**。
4. `tools.py` の `_state_payload` は削除（プライベート関数でテスト参照なしを `grep -rn _state_payload tests/` で確認してから）。

- 受け入れ基準: 必須ゲート green（`test_tool_handlers_return_json_and_never_raise_for_basic_paths` が JSON 形を検証）。`kanban_forum_sync_status` ツールの JSON キー集合が不変。
- コミット: `refactor: single source of truth for sync status payload (status_dict)`

---

## Step 8: 型ヒント・微細クリーンアップ（全ファイル）

まとめて1コミットで行う軽微な整理:

1. `discord_forum.py`: `_request(..., body: dict = None)` → `Optional[dict]`、`get_thread_messages(..., after: int = None)` → `Optional[int]`。
2. `syncer.py`: 使われていない import があれば削除（Step 1 適用後は `RateLimitError` が使用されるはず。`python3 -m pyflakes` があれば流す、無ければ目視）。
3. `kanban_bridge.py`: Step 5 で未対応の `Optional` 型ヒントを揃える。
4. **やらないこと**: フォーマッタ一括適用（black 等）は diff が膨れるので禁止。変更行の周辺のみ。

- 受け入れ基準: 必須ゲート green。`python3 -c "import kanban_forum_sync.syncer, kanban_forum_sync.discord_forum, kanban_forum_sync.kanban_bridge"` が警告なく通る。
- コミット: `refactor: tighten Optional type hints, drop dead imports`

---

## スコープ外（検討したが今回やらないもの）

| 候補 | 見送り理由 |
|---|---|
| `KanbanForumSyncer` のクラス分割（ChannelResolver / Phase1 / Phase2 コラボレータ抽出） | 効果は大きいがリスクも大きい。Step 0 でテスト基盤を green にし、Step 1〜7 で行数を減らした後の**別計画**とする。`__new__` で属性を手組みする現行テストが分割で全滅するため、先にテスト側をファクトリ関数経由に直す必要がある |
| フィードループ内の `time.sleep(0.5)` 削減 | `DiscordForumClient` 側に 0.25s の最小間隔スムージングがあり冗長に見えるが、削除はレートペーシングの挙動変更。レート制限は過去に実害があった領域（RATE_LIMIT_PLAN.md）なので触らない |
| `SyncMap.contains_thread` / `get_by_thread_id` の O(n) 走査を逆引き index 化 | マップ規模は高々数百エントリで実測上問題なし。複雑化に見合わない |
| `ThreadMetaTracker` の書き込みバッチ化（毎 set で fsync している） | 毎回の永続化は at-least-once 耐久性の意図的設計。性能問題が観測されたら再検討 |
| `_sync_task_to_forum` の1段再帰 → ループ化 | 再帰は1段で停止することがコメントで保証済み。書き換えの利益が薄い |
| `_request()` のリトライループ分解 | 429/403/404/5xx の分岐が密結合しており、分解すると却って追いにくい。現状維持 |

---

## 完了時の後片付け

1. フルスイートを実行し、結果（green / skip 数）をこのファイル末尾に追記する。
2. `docs/plans/README.md` の索引にはこのファイルが登録済み（追加作業不要）。
3. `CLAUDE.md` の Architecture 節が Step 6 のモジュール分離を反映しているか最終確認する。

---

## 実行結果（2026-07-06）

全ステップ完了。1 ステップ = 1 コミットで計 9 コミット（Step 0 含む）。

```
python3 -m unittest tests.test_sync_safety tests.test_integration tests.test_default_assignee
Ran 39 tests in 2.461s
OK (skipped=6)
```

- `tests.test_sync_safety`: 16 tests OK
- `tests.test_integration`: 7 tests OK
- `tests.test_default_assignee`: 10 tests OK, 6 skipped（hermes_cli 未インストール環境のため）

`CLAUDE.md` Architecture 節に `i18n.py` を追記済み。
