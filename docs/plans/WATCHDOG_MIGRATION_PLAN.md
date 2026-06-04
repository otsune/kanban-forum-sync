# 計画: inotify → watchdog でイベント駆動をクロスプラットフォーム対応

## Context

イベント駆動モード（`FORUM_SYNC_EVENT_DRIVEN=1`）は現在 **Linux 専用の ctypes inotify**（`kanban_watcher.py`）でのみ動作する。macOS / Windows では `FORUM_SYNC_EVENT_DRIVEN=1` を設定してもネイティブ監視が無く、ポーリングに縮退する（README に既知の未実装事項として明記済み）。

[`watchdog`](https://pypi.org/project/watchdog/) は OS ごとのネイティブ FS 監視（Linux inotify / macOS FSEvents / Windows ReadDirectoryChangesW / BSD kqueue）を1つの API で抽象化する。これを使い、**全プラットフォームでイベント駆動同期**を可能にする。ただし watchdog は外部依存（本プラグインは現在 stdlib のみが設計値）なので、**未導入でも壊れないフォールバック**が必須。

### 確定方針（ユーザー選択）

1. **3段フォールバック（既存 Linux 非回帰）**: `watchdog`（全OS）→ `ctypes inotify`（Linux・watchdog 未導入時）→ `interval`（最終）。既存 Linux ユーザーが watchdog 未導入でもポーリングに退化しない。
2. **PollingObserver 自動フォールバック**: ネイティブ Observer の初期化が失敗した時のみ `watchdog.observers.polling.PollingObserver` へ自動切替（ネットワーク FS / Docker bind mount 対策）。

---

## 現状アーキテクチャ（変更前）

- `kanban_watcher.py` の `KanbanDBWatcher`: ctypes で inotify を直叩き。**ブロッキング pull 型**の公開 IF —
  - context manager（`__enter__`/`__exit__`）、`.available: bool`、`.wait(timeout) -> bool`（変化検出 True / timeout False、inotify 不可時は `time.sleep(timeout)` して True）。
  - `kanban.db` と `kanban.db-wal` を**ファイル単位**で監視。
- `syncer.py:_run_loop_inotify()`（1256〜）: `with KanbanDBWatcher(db_path) as w:` → `w.wait(timeout=poll_interval)` → `incremental_sync()`。debounce（`_MIN_CYCLE_INTERVAL`）と rate-backoff 付き。
- `syncer.py`: `use_inotify`/`_use_inotify`（constructor 250/255）、mode ログ（1197）、`_run_loop`分岐（1209）。
- `service.py:39`: `FORUM_SYNC_EVENT_DRIVEN` env → `use_inotify`。

> watchdog は **push 型**（Observer スレッドがハンドラを呼ぶ）。既存の `wait()` pull 型 IF を**維持**し、内部で `threading.Event` を介して push→pull 変換するのが最小改修。syncer 側のループ構造は不変。

---

## 設計

### 1. `kanban_watcher.py` をバックエンド選択式に再構成

`KanbanDBWatcher` を薄いファサードにし、`__enter__` でバックエンドを優先順に試す。公開 IF（`.available` / `.wait(timeout)` / context manager）は**完全互換**。`.backend_name` を追加してログ用に公開。

```
KanbanDBWatcher (facade)
├── _WatchdogBackend   # watchdog import 可能なら最優先
├── _InotifyBackend    # 既存 ctypes ロジックをクラス化（Linux）
└── _NullBackend       # interval: time.sleep(timeout) → True
```

各バックエンド共通 IF: `available: bool` / `wait(timeout: float) -> bool` / `close()`。

選択ロジック（`__enter__`）:
```python
for factory in (_WatchdogBackend, _InotifyBackend):
    be = factory.try_create(self._db_path)   # 不可なら None
    if be is not None:
        self._backend = be; self.backend_name = be.name; break
else:
    self._backend = _NullBackend(); self.backend_name = "interval"
self.available = self._backend.available
```

#### `_WatchdogBackend`

- **親ディレクトリを監視**（`os.path.dirname(db_path)`）。ファイル単位でなくディレクトリ監視にすることで、WAL/SHM の**再生成・ローテーションも確実に捕捉**（現 inotify のファイル監視より堅牢）。
- `FileSystemEventHandler` のサブクラスで `on_modified`/`on_created`/`on_moved` を実装し、イベント `src_path`（と `dest_path`）の basename が対象集合 `{kanban.db, kanban.db-wal, kanban.db-shm, kanban.db-journal}` にマッチしたら `self._event.set()`。
- `wait(timeout)`: `fired = self._event.wait(timeout)`; `if fired: self._event.clear()`; `return fired`。
- Observer 起動: `Observer()` を試行し、`schedule(...).start()` で例外なら `PollingObserver()` で再試行。両方失敗なら `try_create` は None（→ 次段へ）。
- `close()`: `observer.stop()` → `observer.join(timeout=5)`。
- `name = "watchdog"`（polling 時は `"watchdog-polling"`）。

#### `_InotifyBackend`

- 既存 `_init_inotify` / `wait` の ctypes ロジックをそのままクラス化（**挙動不変**）。`try_create` は非 Linux / init 失敗で None。`name = "inotify"`。

#### `_NullBackend`

- `available = False`、`wait(timeout)`: `time.sleep(timeout); return True`。`name = "interval"`。

### 2. `syncer.py` 統合（最小改修）

- `_run_loop_inotify` → **`_run_loop_event`** にリネーム（inotify 非依存に）。中身は不変（`with KanbanDBWatcher(...) as w: w.wait(...)` のまま、IF 互換）。
- 起動ログをバックエンド名で出す:
  `logger.info("Event-driven sync active (%s) — watching %s", watcher.backend_name, self.kanban.db_path)`。
- `use_inotify` → **`use_event_driven`**、`_use_inotify` → `_use_event_driven`、mode 文字列を `"event" if self._use_event_driven else "poll"` に。`_run_loop` 分岐も同名に。
- env 名 `FORUM_SYNC_EVENT_DRIVEN`・状態ファイル・`KanbanForumSyncer` 公開 IF は**不変**（後方互換）。

### 3. `service.py`

- `use_inotify=...` → `use_event_driven=...`（env 解釈は不変）。

### 4. 依存宣言

- **`requirements.txt`（新規）**: `watchdog>=3,<7`。
- import は try/except でガードし、未導入でも `_InotifyBackend`/`_NullBackend` にフォールバック（プラグインロードは壊れない）。
- plugin.yaml にパッケージ依存を表す標準フィールドは無い（`requires_env` は env 変数用）ため **manifest は変更なし**。依存は `requirements.txt` + README で表現。

---

## 変更ファイル

| ファイル | 種別 | 内容 |
|---|---|---|
| `kanban_watcher.py` | 改修（再構成） | ファサード + `_WatchdogBackend` / `_InotifyBackend` / `_NullBackend`。`.backend_name` 追加。公開 IF 互換 |
| `syncer.py` | 改修 | `_run_loop_inotify`→`_run_loop_event`、backend 名ログ、`use_inotify`→`use_event_driven` リネーム |
| `service.py` | 改修 | `use_inotify`→`use_event_driven` |
| `requirements.txt` | 新規 | `watchdog>=3,<7` |
| `README.md` | 改修 | event-driven 節を全 OS 対応に更新、`pip install watchdog`、OS別バックエンド表、フォールバック順、PollingObserver 注記、ツリーに requirements.txt |
| `CLAUDE.md` | 改修 | Event-driven mode 節を watchdog ベースに更新、フォールバック順を明記 |
| `docs/plans/WATCHDOG_MIGRATION_PLAN.md` | 新規 | 本計画書 |
| `docs/plans/README.md` | 改修 | 索引に本計画書を追加 |
| `tests/test_sync_safety.py` | 改修 | フォールバック選択・Event 発火・Null backend のテスト追加 |

---

## テスト方針（`tests/test_sync_safety.py`）

1. **バックエンド選択**: watchdog import を mock で不可にし、Linux 風環境では `_InotifyBackend`、それ以外では `_NullBackend`（`backend_name == "interval"`、`available is False`）が選ばれることを検証。
2. **`_NullBackend.wait`**: 短い timeout で `~timeout` 後に True を返す（時間境界はゆるめに assert）。
3. **`_WatchdogBackend` 実発火**（`@unittest.skipUnless(watchdog 実在)`）: 一時ディレクトリに `kanban.db` を作り backend 起動 → ファイルを modify → `wait(短timeout)` が速やかに True を返す。`-shm`/`無関係ファイル` でフィルタが効くことも確認。
4. **push→pull 変換**: ハンドラ直接呼び出しで `Event` がセットされ `wait()` が True→以降 False（clear 済み）になること。
5. **リグレッション**: rename 後も既存テスト（直列化 `test_incremental_sync_serializes_concurrent_cycles` 等）が通る。
6. `python3 -m py_compile kanban_watcher.py syncer.py service.py` + `python3 -m unittest tests.test_sync_safety`。

---

## 検証（手動・要 Hermes 完全再起動）

- **Linux + watchdog 導入**: ログ `Event-driven sync active (watchdog)`。`kanban.db` 書込み→即同期。
- **Linux + watchdog 未導入**: `... (inotify)`（非回帰）。
- **macOS / Windows + watchdog 導入**: `... (watchdog)`。DB 書込みで即同期。
- **ネットワーク FS / bind mount**: ネイティブ Observer 失敗→ `... (watchdog-polling)`。
- **いずれも監視不可**: `... (interval)` で従来ポーリング動作。
- `FORUM_SYNC_EVENT_DRIVEN=0`（既定）: 従来の `_run_loop_poll` のまま影響なし。

---

## 留意点 / リスク

- **イベント多発の吸収**: watchdog は1書込みで複数イベントを出しうる。`threading.Event` への合流 + 既存 `_MIN_CYCLE_INTERVAL` debounce で重複サイクルを抑止（現行の挙動を踏襲）。
- **WAL モード**: 書込みは主に `-wal` に出る。親ディレクトリ監視で確実に捕捉。
- **スレッド安全**: Observer は別スレッドだが Event 経由でループへブリッジ。`incremental_sync` は既存 `RLock` で直列化済みのため、二重実行の懸念なし。
- **後方互換**: env（`FORUM_SYNC_EVENT_DRIVEN`）・状態ファイル・`KanbanDBWatcher` 公開 IF・`KanbanForumSyncer` 公開 IF を不変に保つ。内部 rename（`use_event_driven` 等）のみ。
- **依存の任意性**: watchdog 未導入は「機能低下（フォールバック）」であってエラーにしない。CI/テストは watchdog の有無どちらでも緑になるよう skip ガードを使う。
- **スコープ外**: PollingObserver の常用強制オプション化、複数 DB の同時監視最適化、watchdog の細粒度デバウンス設定の外部公開。
