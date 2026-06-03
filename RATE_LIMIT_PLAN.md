# Rate Limit 対応 実装計画書

## 1. 背景 / 症状

稼働中の watcher が Discord REST API のレート制限（HTTP 429）を頻発させ、最終的に
リトライ枯渇でリクエストが失敗している。

```
07:00:49 WARNING syncer: Failed to fetch messages for thread 1510929381177163906:
         Request failed after 3 retries: /channels/1510929381177163906/messages?limit=50&after=...
07:00:52 WARNING discord_forum: Rate limited (attempt 1/3), retrying in 0.55s
07:00:55 WARNING discord_forum: Rate limited (attempt 1/3), retrying in 0.3s
07:00:56 WARNING discord_forum: Rate limited (attempt 2/3), retrying in 0.687s
... （以降ほぼ全リクエストで Rate limited が連発）
```

特徴:
- ほぼ **全リクエスト**で 429 が出ている → バケットが恒常的に枯渇している（瞬間的スパイクではない）。
- `Request failed after 3 retries` が出る → 429 のスロットル待ちが3回で枯渇して**ハード失敗扱い**になっている。

---

## 2. 根本原因

### 2-1. 1サイクルあたりのリクエスト数が多すぎる（主因）

`incremental_sync()`（`syncer.py:1077`）は、マップ済みスレッド数を N とすると毎サイクル
（poll: デフォルト15秒ごと / event-driven: DB書込みごと）におよそ **2N + 3 リクエスト**を発行する。

| 処理 | メソッド | リクエスト数 | 備考 |
|---|---|---|---|
| `_ensure_channel_alive` | `get_channel()` | 1 | |
| `_sync_forum_comments` | `get_thread_messages()` ×N | **N** | スレッド毎に毎回叩く（`syncer.py:531`）|
| `_sync_forum_tags` | `get_channel_by_id()` ×N | **N** | **タグを読むためだけに全スレッドオブジェクトを個別取得**（`syncer.py:611`）|
| `_sync_forum_new_threads` | `get_active_threads()` + `get_archived_public_threads()` | 2 (+orphan毎) | `syncer.py:718,726` |
| `_sync_kanban_comments_to_forum` | `send_message()` × 新規本文 | 可変 | |

**最大の無駄**: `_sync_forum_tags` は `get_channel_by_id(thread_id)` で**スレッド全体を個別 GET**しているが、
読みたいのは `applied_tags` だけ。しかも `_sync_forum_new_threads` が同サイクル内で取得する
**active+archived スレッドリストには `applied_tags` も `last_message_id` も全部入っている**のに使い回していない。

### 2-2. `_request` の 429 ハンドリングの欠陥（`discord_forum.py:59-99`）

```python
max_retries = 3
for attempt in range(max_retries):
    ...
    if e.code == 429:
        retry_after = retry_info.get("retry_after", 1)
        time.sleep(retry_after)
        continue          # ← この continue が max_retries を消費する
```

- **429スロットル待ちとハード失敗が同じ3回バジェットを共有**している。スロットルは「想定内の待て指示」で
  あって失敗ではないのに、3回 429 が続くと `Request failed after 3 retries` になる。
- **`Retry-After` HTTPヘッダ / `X-RateLimit-Scope: global` / `X-RateLimit-Global` を見ていない。**
  body の `retry_after` だけ参照。グローバル制限時に待機時間を過小評価する。
- **`X-RateLimit-Remaining` / `X-RateLimit-Reset-After` を使った先読み抑制が無い。**
  バケットが枯渇していても 429 を食らうまで突っ込む。
- 呼び出し側の `time.sleep(0.5)` は高レベルループの**反復間**にしか入らず、`_request` 自体は連射する
  （例: active + archived + orphan毎の `get_thread_messages` が無間隔で飛ぶ）。

### 2-3. サイクルレベルのバックオフが無い

`_run_loop_poll`（`syncer.py:1164`）/ `_run_loop_inotify`（`syncer.py:1177`）は、サイクルが
レート制限で失敗しても**次サイクルで同じ量を再投入**する。event-driven 時は DB 書込みが連続すると
サイクルが立て続けに発火し、制限を悪化させる。

---

## 3. 対応方針（3段構え）

> 優先度: **B（量削減）> A（429ハードニング）> C（バックオフ）**。
> B が効けば 429 自体がほぼ消えるが、A は安全網として先に入れておく。

### A. `_request` の 429 ハードニング — `discord_forum.py`

即効の安全網。スロットル待ちを「失敗」から切り離す。

1. **429専用の待機バジェットを分離。** ハード失敗（ネットワーク等）は従来どおり3回。
   429 は別カウンタで最大 8 回まで待つ（スロットルは想定内なので寛容に）。
2. **待機時間 = max(`Retry-After`ヘッダ, body `retry_after`, 最小値)** を採用。
   `X-RateLimit-Scope`/`X-RateLimit-Global` が global の場合はログを WARNING で強調。
3. 1回の sleep を `min(wait, 60)` でキャップし、小さなジッタ（±10%程度）を足す。
4. **グローバル最小間隔**を `_request` 内に導入。プロセス内で直近リクエスト時刻を保持し、
   `MIN_REQUEST_INTERVAL`（例 0.25s）未満なら差分だけ sleep してから送る。
   → caller 依存の `time.sleep(0.5)` のムラを底上げし、連射を平滑化。
5. **先読み抑制（任意・推奨）**: レスポンスヘッダ `X-RateLimit-Remaining` が 0 かつ
   `X-RateLimit-Reset-After` があれば、その秒数を「次リクエスト解禁時刻」として記録し、
   次の `_request` 冒頭で必要分だけ待つ。バケット単位の厳密管理まではせず、
   **グローバル1スロット**の簡易実装で十分（このプラグインは単一スレッドから直列発行のため）。
6. 429バジェット枯渇時は専用例外 **`RateLimitError(DiscordForumError)`** を投げ、
   caller がサイクルごと中断/バックオフできるようにする。

#### `_request` 改修イメージ

```python
class RateLimitError(DiscordForumError):
    """429 リトライを使い切った場合のエラー。"""
    pass

# クラス属性
_MIN_REQUEST_INTERVAL = 0.25
_last_request_ts = 0.0       # プロセス内グローバル抑制
_next_allowed_ts = 0.0       # 先読み抑制（Remaining=0 時）

def _request(self, method, path, body=None):
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, headers=self._headers, method=method)

    hard_retries = 3
    rate_retries = 8
    attempt_hard = 0
    attempt_rate = 0

    while True:
        # --- 送信前スロットル（グローバル最小間隔 + 先読み解禁待ち） ---
        now = time.monotonic()
        wait = max(self._next_allowed_ts - now,
                   self._last_request_ts + self._MIN_REQUEST_INTERVAL - now)
        if wait > 0:
            time.sleep(wait)
        self._last_request_ts = time.monotonic()

        try:
            with urlopen(req, timeout=30, context=_SSL_CONTEXT) as resp:
                # Remaining=0 を見たら次回まで先読みで待つ
                self._note_ratelimit_headers(resp.headers)
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except HTTPError as e:
            error_body = e.read().decode()
            if e.code == 429:
                attempt_rate += 1
                wait = self._parse_retry_after(e, error_body)
                is_global = (e.headers.get("X-RateLimit-Scope") == "global"
                             or e.headers.get("X-RateLimit-Global") == "true")
                logger.warning(
                    "Rate limited%s (429 %d/%d), waiting %.2fs: %s",
                    " [GLOBAL]" if is_global else "",
                    attempt_rate, rate_retries, wait, path,
                )
                self._next_allowed_ts = time.monotonic() + wait
                if attempt_rate >= rate_retries:
                    raise RateLimitError(
                        "Rate limit retries exhausted: %s" % path,
                        http_code=429, body=error_body,
                    )
                continue
            if e.code == 403:
                raise DiscordPermissionError(...)
            if e.code == 404:
                raise NotFoundError(...)
            # その他は hard_retries 内でのみリトライ（5xx等）。それ以外は即 raise
            attempt_hard += 1
            if e.code >= 500 and attempt_hard < hard_retries:
                time.sleep(min(2 ** attempt_hard, 10))
                continue
            raise DiscordForumError(...)
```

補助メソッド:
- `_parse_retry_after(e, body)` … `Retry-After` ヘッダ（秒）と body `retry_after` の max、`min(.., 60)`、+ジッタ。
- `_note_ratelimit_headers(headers)` … `X-RateLimit-Remaining == "0"` なら
  `_next_allowed_ts = now + float(X-RateLimit-Reset-After)`。

---

### B. リクエスト量の削減 — `syncer.py` ★最重要

定常状態（変化なし）の **2N+3 → 約3 req/cycle** を目標。

1. **1サイクル1回の共有スレッドリストを構築。**
   `incremental_sync` 冒頭（`_ensure_channel_alive` 直後）で
   active（guild→parent_id絞り込み）+ archived を1回だけ取得し、
   `threads_by_id: dict[int, dict]` を作る。これを各サブ処理に引数で渡す。

   ```python
   def _fetch_forum_threads(self) -> dict[int, dict]:
       by_id = {}
       try:
           gid = self.discord.get_current_guild_id()
           if gid:
               for t in self.discord.get_active_threads(gid):
                   if str(t.get("parent_id")) == str(self.channel_id):
                       by_id[int(t["id"])] = t
       except Exception as e:
           logger.warning("Failed to fetch active threads: %s", e)
       try:
           for t in self.discord.get_archived_public_threads():
               by_id.setdefault(int(t["id"]), t)
       except Exception as e:
           logger.debug("Failed to fetch archived threads: %s", e)
       return by_id
   ```

2. **`_sync_forum_tags(threads_by_id)`**: per-thread `get_channel_by_id`（**N GET**）を全廃し、
   `threads_by_id[thread_id]["applied_tags"]` を読む。マップにあるがリストに無い thread_id は
   404相当（消滅）として `_drop_stale_thread`。→ **N → 0**。

3. **`_sync_forum_comments(threads_by_id)`**: 各スレッドの `last_message_id`（共有リストの
   thread オブジェクトに含まれる）とカーソル `get_last_message_id(thread_id)` を比較し、
   **新着があるスレッドだけ** `get_thread_messages` を呼ぶ。→ 多くのサイクルで **N → ほぼ0**。

   ```python
   meta = threads_by_id.get(thread_id)
   newest = int(meta.get("last_message_id") or 0) if meta else 0
   if meta is not None and newest <= last_id:
       continue   # 新着なし → API を叩かない
   ```

   ※ `last_message_id` が thread オブジェクトに無い／信頼できない場合のフォールバックとして、
   `meta is None`（共有リストに居ない）なら従来どおり取得を試みる。

4. **`_sync_forum_new_threads(threads_by_id)`**: 自前の active/archived 取得をやめ、
   共有リストを反復。orphan 回復・新規タスク作成ロジックは現状維持。

5. **`incremental_sync` の組み立て**:
   ```python
   threads_by_id = self._fetch_forum_threads()
   ...
   self._sync_forum_comments(threads_by_id)
   self._sync_forum_tags(threads_by_id)
   self._sync_forum_new_threads(threads_by_id)
   self._sync_kanban_comments_to_forum()   # 送信系。新規があるときだけ送るので据え置き
   ```

#### 削減効果（N=マップ済みスレッド数）

| | 改修前 | 改修後（定常） |
|---|---|---|
| スレッドリスト取得 | 2 | 2 |
| tags | N | 0 |
| comments | N | 0〜（新着スレッドのみ）|
| ensure_channel_alive | 1 | 1 |
| **合計** | **2N+3** | **約3** |

---

### C. サイクルレベルのバックオフ — `syncer.py` ループ

1. **`incremental_sync` で `RateLimitError` を捕捉**し、`self._rate_backoff` を指数的に伸ばす
   （例: 15→30→60→120s、cap 120s）。クリーンに完走したサイクルで 0 にリセット。
   待機は `self._stop_event.wait(self.poll_interval + self._rate_backoff)`。

   ```python
   # _run_loop_poll
   while not self._stop_event.is_set():
       try:
           self.incremental_sync()
           self._rate_backoff = 0
       except RateLimitError:
           self._rate_backoff = min(max(self._rate_backoff * 2, self.poll_interval), 120)
           logger.warning("Rate limited cycle; backing off extra %ds", self._rate_backoff)
       except Exception as e:
           logger.error("Incremental sync failed: %s", e, exc_info=True)
           self._state.last_error = str(e)
       self._stop_event.wait(self.poll_interval + self._rate_backoff)
   ```

2. **event-driven の最小サイクル間隔（デバウンス）。**
   `_run_loop_inotify` で、前回サイクル完了からの経過が `MIN_CYCLE_INTERVAL`（例 5s）未満なら
   差分を待ってから `incremental_sync`。連続 DB 書込みでのサイクル乱発を抑える。
   `RateLimitError` 時は poll 同様にバックオフ。

---

## 4. 変更ファイル一覧

| ファイル | 変更内容 |
|---|---|
| `discord_forum.py` | `RateLimitError` 追加。`_request` の 429 ロジック刷新（バジェット分離・`Retry-After`ヘッダ対応・グローバル最小間隔・先読み抑制）。補助 `_parse_retry_after` / `_note_ratelimit_headers`。 |
| `syncer.py` | `_fetch_forum_threads()` 追加。`_sync_forum_comments` / `_sync_forum_tags` / `_sync_forum_new_threads` を `threads_by_id` 受け取りに変更（tags の per-thread GET 全廃、comments の last_message_id ゲート）。`incremental_sync` で共有リスト生成。`_run_loop_poll` / `_run_loop_inotify` にサイクルバックオフ + デバウンス。コンストラクタに `self._rate_backoff = 0`。 |
| `CLAUDE.md` | 「Rate limit handling」節を追記（バケット設計・共有リスト最適化・バックオフの説明）。 |

---

## 5. 実装順序

1. **A（`_request` ハードニング）** — 単体で安全網になる。まずこれで `Request failed after 3 retries` を解消。
2. **B（共有リスト化）** — 429 の発生源そのものを削減。`_sync_forum_tags` の N GET 全廃が最大効果。
3. **C（サイクルバックオフ）** — 残った瞬間的スパイクへの保険。
4. **CLAUDE.md 追記**。

各段階ごとに Hermes プロセス再起動 → `~/.hermes/logs/agent.log` で `Rate limited` の頻度低下を確認。

---

## 6. 検証手順

1. コード適用後、**Hermes プロセスを完全再起動**（`stop/start` ではモジュール再読込されない）。
   ```
   systemctl --user restart hermes-gateway.service
   ```
2. ログ監視:
   ```
   tail -f ~/.hermes/logs/agent.log | grep -E "Rate limited|retries|Incremental sync"
   ```
   - `Rate limited` の出現頻度が激減（理想は定常時ゼロ）すること。
   - `Request failed after N retries` が出ないこと。
   - `Incremental sync: M changed tasks` は従来どおり機能していること。
3. **機能リグレッション確認**:
   - Discord でタグ変更 → Kanban ステータス反映（共有リスト経由でも tags 同期が動く）。
   - Discord スレッドにコメント投稿 → Kanban に反映（last_message_id ゲートで取りこぼさない）。
   - Kanban コメント/ワーカーログ → Discord 投稿。
   - 新規 Discord スレッド → Kanban タスク作成（共有リスト反復で検出）。
4. **バックオフ確認**（任意）: 意図的に短い poll_interval で負荷をかけ、`backing off extra Ns` が
   出てから回復し、`_rate_backoff` が 0 に戻ることを確認。

---

## 7. リスク / 留意点

- **`last_message_id` ゲートの取りこぼし防止**: thread オブジェクトに `last_message_id` が
  欠落/null のケースがあるため、「共有リストに居ない or last_message_id 不明」のときは
  従来どおり `get_thread_messages` を叩くフォールバックを必ず残す（at-least-once を維持）。
- **アーカイブ済みスレッドのページング**: `get_archived_public_threads(limit=50)` は 50 件上限。
  スレッドが 50 を超える運用では `has_more`/`before` ページングが将来必要（本プランの範囲外、TODO）。
- **先読み抑制はグローバル1スロットの簡易版**: 厳密なバケット単位管理ではないが、本プラグインは
  単一 watcher スレッドから直列発行するため実用上十分。複数 watcher を同一 Bot で動かす場合は
  別途プロセス間協調が要る（現状非対応・運用で回避）。
- **モジュール再読込の注意**: 例外クラス（`RateLimitError`）を増やすため、文字列ベースの 404 判定
  （`"Resource not found" in str(e)`）と同様、429 判定も**文字列/属性ベース**で行い、
  `isinstance` 依存にしない（モジュール再読込でクラス同一性が崩れる既知問題）。
