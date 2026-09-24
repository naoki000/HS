# Pythonista 画面キャプチャ PoC

iPad の Pythonista 3 だけで、ReplayKit の画面キャプチャがどこまで動くかを
**実機で測る**ための検証コード。カード認識は入っていない。

## 実機で出た結論（2026-09-25 / iPad / iOS 27.0 / Pythonista 3.10.4）

> **Pythonista では ReplayKit のフレームを受け取れない。**
> ReplayKit の問題ではなく、**objc_util が別スレッドから Python を
> 呼び返せない**（segfault する）ため。

`block_probe.py` が ReplayKit を一切使わずに再現した。

```
  objc_basic     OK       ObjC を触るだけ
  block_create   OK       ObjCBlock を作る
  block_sync     OK       同じスレッドから呼び返す
  block_async    CRASH    別スレッドから呼び返す      <- ここで死ぬ
  objc_in_thread OK       生スレッドから ObjC を触る（autoreleasepool 付き）
```

`block_async` は `NSOperationQueue addOperationWithBlock:` だけを使っている。
それでも Pythonista ごと落ちる。
**ReplayKit の `startCaptureWithHandler:` も同じ形**（ReplayKit のキューから
Python のブロックを呼ぶ）なので、必ず同じ結果になる。

`crash_log.txt` のダンプにも裏付けがある。`Current thread` の表記が無く、
**クラッシュしたスレッドに Python のフレームが存在しない**。
Python が管理していないネイティブスレッドから Python に入ろうとして落ちている。

`objc_in_thread` が OK なのは方向が逆だから。
**Python → ObjC は `autoreleasepool` を敷けば通る。ObjC → Python が通らない。**

### この先どうするか

仮にコールバックが受け取れたとしても、次の2つが残る。

1. `startCaptureWithHandler:` は**アプリ自身の画面しか撮れない**。
   Hearthstone を撮るには Broadcast Upload Extension が必要で、
   これは Xcode で作ってアプリに同梱する App Extension。Python からは作れない
2. バックグラウンドでは suspend される

つまり **3重に塞がっている**。「Pythonista 単体で Hearthstone の画面を
撮り続ける」という道は、実機で測った結果として閉じている。

**このリポジトリの既存の方式（PC で `start.py` を動かし、iPad からは
Cast to Browser で配信する）が現実的な解のまま。**

以下はその検証に使ったコードと手順の記録。

---

## 目的

最終的にやりたいのはこれ。

```
Hearthstone の画面 → 自動でフレーム取得 → カード画像認識 → 闘技場3択の判定
```

そのうち今回確かめるのは最初の一段だけ。

> **Pythonista 単体で、他アプリ（Hearthstone）を表示している間もフレームを取り続けられるか**

「無理そう」で終わらせず、どこで止まるのかを実機のログで確定させる。
止まり方は3通りあり、次に打つ手が変わる。

| 止まり方 | 意味 | 次の手 |
|---|---|---|
| フレームだけ止まる | Python は動いている | バックグラウンド継続の手当てを探す |
| Python ごと止まる | アプリが suspend された | Pythonista では不可能。別の器が要る |
| 止まらない | 継続できている | そのまま認識へ進める |

## 段階的な切り分け（PHASE）

Pythonista **アプリごと落ちる**という症状が出ている。Python の例外なら
try/except で捕まるが、`objc_util` / `ctypes` のシグネチャやポインタ扱いを
間違えるとネイティブクラッシュになり、try/except では捕まえられない。

そこで、やることを3段階に分けた。`replaykit_capture.py` の先頭にある
`PHASE` を書き換えて進める。**既定は 1。**

```python
PHASE = 1      # replaykit_capture.py の先頭
```

| PHASE | コールバックの中でやること | 切り分けられること |
|---|---|---|
| **1** | 数えるだけ。`sbuf` に一切触らない | ReplayKit + ObjCBlock のコールバック自体が安定しているか |
| 2 | `CVPixelBuffer` を取り出して幅と高さだけ読む | CoreMedia / CoreVideo の ctypes シグネチャが正しいか |
| 3 | JPEG まで変換する | CoreImage / UIImage 経路が正しいか |

PHASE 1 では次を**一切呼ばない**。

- `_to_jpeg()` / `_pixel_size()`
- `CMSampleBufferGetImageBuffer` / `CMSampleBufferIsValid`
- `CVPixelBufferGetWidth` / `CVPixelBufferGetHeight`
- `CIImage.imageWithCVPixelBuffer_`
- `CIContext.createCGImage_fromRect_`
- `UIImageJPEGRepresentation`

CoreMedia / CoreVideo / CoreImage の読み込みと ctypes の型定義も、
PHASE 2 以降で初めて実行する（`_load_imaging()`）。import 時にやると
**ログが1行も出ないうちに落ちて**、位置が分からなくなるため。

### 判定のしかた

| PHASE 1 の結果 | 読み取れること |
|---|---|
| `frame_count` が増える | コールバックは安定。PHASE 2 へ進む |
| 開始ログは出るが `frame_count` が 0 のまま | コールバックが来ていない。ブロックの型か保持を疑う |
| **PHASE 1 でもアプリごと落ちる** | JPEG 変換は無関係。`ObjCBlock` か `startCaptureWithHandler:completionHandler:` 周辺が原因 |

PHASE 1 が通り、PHASE 2 で落ちるなら CoreMedia の ctypes 定義。
PHASE 2 が通り、PHASE 3 で落ちるなら CoreImage / UIImage 経路。
**実機では PHASE 1 にも到達しなかった**（`block_async` で落ちるため）。
PHASE 2 / 3 のコードは書いてあるが、一度も実行されていない。
### クラッシュ位置の特定

`start()` は1手ごとにログを出す。

```
[capture] start() entered  (PHASE 1 / MODE capture)
[capture] sharedRecorder OK
[capture] isAvailable OK
[capture] microphone disabled
[capture] sizeof(c_long)=8  (NSInteger は 8 のはず)
[capture] selector check OK
[capture] handler block created (retained)
[capture] completion block created (retained)
[capture] calling startCapture...
[capture] startCapture returned
[capture] startCapture OK          <- 完了ハンドラ。ここが来れば権限も通っている
[capture] frames=30                <- フレームのコールバックが来ている
```

**アプリごと落ちると Console は消える。** そのため同じログを
`pythonista/capture_log.txt` に1行ずつ flush して書いている。
落ちたあとはこのファイルの最終行を見れば、どこまで進んだか分かる。

### `startCapture returned` の直後で落ちる場合

**まず `crash_log.txt` で「どのスレッドが落ちたか」を見る。**
上の「いちばん重要な落とし穴」のとおり、HTTP スレッドや監視スレッドから
ObjC を触ったのが原因なら、ReplayKit は無関係。
`capture_log.txt` の最終行が `startCapture returned` でも、
そこで止まったとは限らない（別スレッドが落ちただけ）。

ReplayKit のコールバックが本当に原因だと確認できたら、次の順で疑う。

#### 1. ブロックの解放

objc_util の `ObjCBlock` は、**Python の変数に入れておくだけでは足りない**。
`retain_global()` を通す必要がある。

```python
blk = ObjCBlock(fn, restype=None, argtypes=[...])
retain_global(blk)          # これが無いと ObjC から呼ばれる前に解放されうる
```

このコードは `_make_block()` で必ず `retain_global()` を通し、さらに
`self._blocks` にも残して二重に保持している。ログの `(retained)` が目印。

#### 2. ObjCBlock そのもの（ReplayKit と切り離して確かめる）

`retain_global()` でも直らないなら、**ReplayKit ではなく ObjCBlock 自体**を
疑う。`block_probe.py` が ReplayKit を使わずに4段階で確かめる。
`main.py` が ReplayKit より先に自動で実行する。

| 段階 | やること | 落ちたら分かること |
|---|---|---|
| `objc_basic` | `ObjCClass` を触るだけ。ブロックなし | objc_util が壊れている |
| `block_create` | ブロックを作るだけ。呼ばない | 作る時点で無理 |
| `block_sync` | **同じスレッド**から同期で呼ばせる<br>`NSArray enumerateObjectsUsingBlock:` | ObjCBlock 自体が使えない |
| `block_async` | **別スレッド**から呼ばせる<br>`NSOperationQueue addOperationWithBlock:` | 別スレッドから Python を呼べない |
| `objc_in_thread` | 生の `threading.Thread` から<br>`autoreleasepool` 付きで ObjC を触る | 参考情報。通れば設計を緩められる |

ブロックの中では ObjC を触らない。触ると「ブロックが呼べない」のか
「別スレッドから ObjC を触れない」のか区別がつかなくなるため。
その2つを分けるのが最後の `objc_in_thread`。

**ReplayKit のフレームコールバックは `block_async` と同じ形**（別スレッドから
Python を呼び返す）。つまり `block_sync` が通って `block_async` で落ちるなら、
ReplayKit をどういじっても受け取れない。

落ちても困らないよう、進捗は `probe_state.json` に**先に**書いてある。
「開始したのに終了が記録されていない段階」＝そこで落ちた段階。
次に Run するとその段階を飛ばして先へ進むので、
**1回のクラッシュにつき1つ結果が確定する**。何度か Run すれば表が埋まる。

```
  objc_basic    OK
  block_create  OK
  block_sync    OK
  block_async   CRASH（Pythonista ごと落ちた）

 => 別スレッドから Python を呼び返すと落ちます。
    ReplayKit のフレームコールバックも同じ形なので、
    この方式では原理的に受け取れません。
```

やり直すときは `main.py` の `RESET_BLOCK_PROBE = True` にして1回 Run する
（または `probe_state.json` を消す）。

#### 3. ReplayKit 固有（開始モードを自動で試す）

`block_probe` が全部 OK なら、原因は ReplayKit 側にある。
開始モードは**自動で順に試す**。落ちたモードは二度と試さないので、
無限にクラッシュし続けることはない。

| モード | 渡すブロック | 落ちなければ分かること |
|---|---|---|
| `capture` | フレーム用 + 完了用 | 本命。これが通れば PHASE 2 へ |
| `capture_nohandler` | 完了用だけ | 完了ブロックは無事。フレーム用が原因 |
| `none` | **1つも渡さない** | ReplayKit の開始自体は通る。ブロックだけが原因 |
| `record` | 完了用だけ（旧 API） | `startCapture` 固有の問題 |

開始する前に `capture_modes.json` へ `open` と書き、**正しく停止できたときだけ**
消す。次の起動で `open` が残っていれば、そのモードは落ちたと判定して次へ進む。

`open` を「開始から5秒」で消してはいけない。`none` は startCapture が
素直に戻ってくるので5秒は平気だが、**録画が実際に始まった瞬間に落ちる**
（ReplayKit が nil のハンドラを呼ぶため）。5秒で消す作りにしていたせいで
「生き延びた」と誤記録し、同じモードを選び続けて無限にクラッシュした。

一度生き延びてから後で落ちたモードは `開始は通ったが、そのあとクラッシュ`
と表示され、ちゃんと次へ進む。

```
  capture            クラッシュ
  capture_nohandler  クラッシュ
  none               開始は通ったが、そのあとクラッシュ
  record             クラッシュ

 => ブロックを渡さなくても落ちます。startCapture は戻ってきますが、
    録画が実際に始まった瞬間に落ちます。
    ReplayKit が nil のハンドラを呼ぶためで、
    ブロック無しで使う道はありません。
```

全部落ちると開始そのものをやめる。無限にクラッシュし続けることはない。
やり直すときは `main.py` の `RESET_CAPTURE_MODES = True` にして1回 Run する
（または `capture_modes.json` を消す）。

### 権限の同意について

ReplayKit のアプリ内キャプチャは、初回に OS の同意ダイアログを出す。
**同意していない場合は完了ハンドラが error 付きで呼ばれる**のが正常な流れで、
その場合は次のログが出る。

```
[capture] ERROR startCapture failed: <エラー内容>
```

つまり **完了ハンドラすら呼ばれずに落ちているなら、それは権限の問題ではなく
ブロックの問題**、という切り分けになる。逆に `'record'` モードで同意
ダイアログが出れば、権限のフロー自体は生きていると分かる。

同意を一度拒否したあとに試し直す場合は、
`設定 > スクリーンタイム > コンテンツとプライバシーの制限 > 画面収録` が
「許可」になっているかを確認する。ここが「許可しない」だと
`isAvailable` が false になる。

### ObjCBlock の型について

ReplayKit のハンドラは次の形。

```objc
void (^)(CMSampleBufferRef sampleBuffer,
         RPSampleBufferType sampleBufferType,
         NSError *error)
```

`objc_util` の `ObjCBlock` は第1引数にブロック自身を取るので、

```python
ObjCBlock(self._on_sample, restype=None,
          argtypes=[c_void_p, c_void_p, c_long, c_void_p])
#                   ^block   ^sbuf     ^type   ^NSError*
```

arm64 (LP64) では `RPSampleBufferType` = `NSInteger` = `long` = 8 バイトなので
`c_long` で正しい。実機の `sizeof(c_long)` を起動ログに出しているので、
8 以外ならそこで分かる。

なお **arm64 の呼び出し規約では引数4個はすべて x0–x3 のレジスタ渡し**になる。
整数幅を取り違えても上位ビットにゴミが入るだけで、それ自体が即クラッシュに
なる類の間違いではない。実際、Pythonista で ReplayKit を動かしている
既存の例（LukeMonson の gist）でも argtypes の個数は一貫していないが動いている。
**したがって argtypes より先に `retain_global()` を疑うべき。**

## 実行環境

| | |
|---|---|
| 端末 | iPad 実機（シミュレータ不可） |
| アプリ | Pythonista 3（App Store 版） |
| Python | 3.10 系 |
| 起動 | Pythonista の Run ボタン |

使わないもの: Mac / Xcode / Swift / 新規 iOS アプリ拡張 / pip のネイティブ拡張 /
subprocess / 外部バイナリ。iOS API は `objc_util` から直接呼ぶ。

## ファイル

| ファイル | 役割 |
|---|---|
| `main.py` | 入口。Run するのはこれだけ |
| `ui_app.py` | Pythonista の ui で操作する画面 |
| `replaykit_capture.py` | ReplayKit を objc_util から叩く。先頭に `PHASE` と `MODE_ORDER` |
| `capture_server.py` | HTTP サーバ（別スレッド） |
| `background_probe.py` | バックグラウンド移行後に何が止まるかを記録 |
| `block_probe.py` | ObjCBlock が本当に動くかを ReplayKit と切り離して確かめる |
| `crash_trap.py` | try/except で捕まらない種類のクラッシュを記録する |
| `web/index.html` | ブラウザ確認用（別端末から見るとき） |
| `capture_log.txt` | 実行時に作られる。どこまで進んだか（git 管理外） |
| `crash_log.txt` | 実行時に作られる。落ちた瞬間のスタック（git 管理外） |
| `probe_state.json` | ObjCBlock 確認の進捗（git 管理外） |
| `capture_modes.json` | 開始モードの試行記録（git 管理外） |

`objc_util` が無い環境（PC）でも import は通り、`available: false` を返して
終わる。HTTP サーバ部分は PC でも動くので、API の形だけなら PC で確認できる。

## 画面

`main.py` を Run すると Pythonista の画面が出る。ブラウザは要らない。

```
MODE / PHASE   none / PHASE 1
capturing      False
frames         0
app_state      active
ReplayKit      available=True recording=False

[ 開始 ] [ 停止 ] [ 記録を消す ]

（最新フレーム）
（ログの末尾）
```

**ui を使うのには安全上の理由もある。**
ui のボタン操作や `ui.delay()` のコールバックは**本物のメインスレッド**で走るので、
下の「いちばん重要な落とし穴」が構造的に起きなくなる。
ObjC を触る処理（`pump()` / `refresh_availability()` / `app_state()`）は
すべてこの更新ループから呼んでいる。

HTTP サーバも並行して動く。**Hearthstone を前面にしている間は iPad の画面が
見えない**ので、バックグラウンド検証には別端末のブラウザが必要。

`main.py` の先頭で切り替えられる。

```python
USE_UI = True            # False にすると従来の Console ループ
RUN_HTTP_SERVER = True   # False にすると HTTP サーバを立てない
```

## いちばん重要な落とし穴: ObjC はスクリプトスレッドからしか触れない

**Pythonista が作っていないスレッドから ObjC を呼ぶと、アプリごと segfault する。**

`threading.Thread` で自分が立てたスレッド（HTTP サーバや監視スレッド）には
NSAutoreleasePool が無い。そこから `objc_util` 経由で ObjC を呼ぶと、
Python の try/except では捕まらないネイティブクラッシュになる。

実際に踏んだ。`crash_log.txt` に出たのがこれ。

```
Fatal Python error: Segmentation fault

Thread 0x000000016f7ef000 (most recent call first):
  File ".../objc_util.py", line 898 in __call__
  File ".../objc_util.py", line 1095 in new_func          <- on_main_thread の中
  File ".../pythonista/capture_server.py", line 103 in do_GET
  File ".../http/server.py", line 414 in handle_one_request
```

`capture_server.py:103` は `capture.start()`。つまり
**HTTP のリクエストを処理するスレッドから ReplayKit を開始しようとして落ちていた。**
`@on_main_thread` を付けていても駄目で、その dispatch 自体が落ちる。

**ReplayKit は無関係だった。** `capture_log.txt` の最終行が
`startCapture returned` だったのは、そこで止まったからではなく、
別のスレッドが落ちてプロセスごと消えたから。

### この PoC での約束ごと

| スレッド | ObjC | やること |
|---|---|---|
| スクリプト（`main()` のループ） | **触ってよい** | `refresh_availability()` / `start()` / `stop()` / `app_state()` |
| HTTP サーバ | **触らない** | 写しを読む。開始・停止は `request_start()` で依頼するだけ |
| 監視（`BackgroundProbe`） | **触らない** | 渡された状態を記録するだけ（`set_state()`） |
| ReplayKit のコールバック | 最小限 | 触るときは `autoreleasepool()` の中で |

`/start` と `/stop` は**その場では実行されない**。依頼を積むだけで、
スクリプトスレッドの `pump()` が 0.5 秒以内に拾って実行する。
応答に `"queued": true` が入るのはそのため。

```python
# 他スレッドから
capture.request_start()      # 積むだけ。ObjC は触らない

# スクリプトスレッドのループで
capture.pump()               # ここで初めて start() が走る
```

同じ理由で `capture.availability()` は写しを返すだけにしてある。
実際に ObjC を読むのは `refresh_availability()` で、これはスクリプト
スレッドから 5 秒おきに呼んでいる。

### 症状の見分けかた

`crash_log.txt` の `Thread 0x... (most recent call first)` を見て、
**どのスレッドが落ちたか**を必ず確認する。

| スタックの中身 | 意味 |
|---|---|
| `http/server.py` / `socketserver.py` がある | HTTP スレッドから ObjC を触った |
| `background_probe.py` がある | 監視スレッドから ObjC を触った |
| `replaykit_capture.py` の `_on_sample` がある | ReplayKit のコールバックで落ちた |
| `objc_util.py` だけで上が `main.py` | スクリプトスレッド。ObjC 呼び出し自体の問題 |



**種類によって違う。** try/except で捕まるのは1つ目だけ。

| 種類 | 例 | `try/except` | このコードの対処 |
|---|---|---|---|
| Python 例外 | `AttributeError` | **捕まる** | 全ハンドラを try/except で包んである |
| ObjC 例外 (`NSException`) | 無いセレクタの呼び出し | 捕まらない。誰も catch しないと `abort()` | `NSSetUncaughtExceptionHandler` で死ぬ直前に記録 |
| メモリ違反 (`EXC_BAD_ACCESS`) | 解放済みブロックの呼び出し | そもそも例外ではない（`SIGSEGV` / `SIGBUS`） | `faulthandler` で死ぬ直前にスタックを記録 |

下の2つは**止められない**。プロセスは必ず死ぬ。できるのは「死ぬ直前に
書き出す」ことだけで、それを `crash_trap.py` がやっている。

```
[crash] faulthandler=True  NSException=True  -> .../crash_log.txt
```

次に起動したとき、前回の記録があれば Console の先頭に出す。

```
 !! 前回のクラッシュ記録（.../crash_log.txt）
    --- faulthandler 有効 2026-09-25 04:43:35 ---
    Fatal Python error: Segmentation fault
    Current thread 0x... (most recent call first):
      File ".../replaykit_capture.py", line NNN in _on_sample
```

`NSException` なら名前・理由・ネイティブスタックまで残る。

```
!!!! ObjC 例外で落ちます 2026-09-25 04:43:35
  name   : NSInvalidArgumentException
  reason : -[NSObject someSelector]: unrecognized selector sent to instance
    0   CoreFoundation   0x...
    1   libobjc.A.dylib  0x...
```

### それでも何も残らない場合

`crash_log.txt` は実行ごとに区切って書いてあり、起動時に前回の結果を判定する。

| 記録 | 意味 |
|---|---|
| `--- faulthandler 有効 ...` のあとにスタック | **クラッシュした**。内容がそのまま原因 |
| `--- 正常終了 ...` がある | 落ちていない |
| ヘッダだけで何も続かない | **記録が残らずに死んだ**。faulthandler が捕まえられない落ち方 |

3つ目（ヘッダだけ）は、iOS による強制終了やメモリ不足が疑われる。
起動時に直近8回ぶんの一覧が出るので、何回目から落ち始めたかが分かる。

```
 これまでの実行
  2026-09-25 04:50:34  クラッシュ
  2026-09-25 04:52:08  記録なしで終了
  2026-09-25 04:52:31  正常終了
```

それでも分からないときは **iOS のクラッシュレポート**を見る。
ここにはネイティブのバックトレースが完全な形で残る。

```
設定 > プライバシーとセキュリティ > 解析と改善 > 解析データ
  -> Pythonista3-2026-09-25-......ips
```

`Exception Type` を見れば種類が分かる。

| Exception Type | 意味 |
|---|---|
| `EXC_BAD_ACCESS (SIGSEGV)` | 不正なメモリ参照。解放済みブロックの呼び出しなど |
| `EXC_CRASH (SIGABRT)` | ObjC 例外か `abort()` |
| `EXC_BREAKPOINT (SIGTRAP)` | Swift/ObjC のアサーション |

`Thread N Crashed` のスタックに `ReplayKit` や `libffi`、`_ctypes` が
並んでいれば、ブロック呼び出しで落ちたと確定できる。

## 起動手順

1. `pythonista/` 以下を iPad の Pythonista へ入れる
   （iCloud Drive / Working Copy / `pythonista://` のどれでもよい。
   フォルダ構成は `web/index.html` も含めてそのまま保つこと）
2. `main.py` を開いて **Run**
3. Console に ReplayKit の状態と HTTP サーバの URL が出る
4. 初回は録画の許可を求めるダイアログが出ることがある。許可する

止めるときは Pythonista の停止ボタン。停止時に検証のまとめが Console に出る。

## 必要な iOS 権限

| | 要否 | 備考 |
|---|---|---|
| 画面収録の許可 | 必要 | 初回に OS のダイアログ。拒否すると開始が失敗する |
| マイク | 不要 | `setMicrophoneEnabled:NO` にしている |
| entitlement | アプリ内キャプチャには不要 | 後述の Broadcast は話が別 |
| Background Mode | **ここが争点** | 下の「Pythonista 固有の制限」を参照 |

`設定 > スクリーンタイム > コンテンツとプライバシーの制限 > 画面収録` が
「許可しない」だと `isAvailable` が false になる。

## HTTP サーバ

既定ポート **8765**。`0.0.0.0` で待ち受けるので同じネットワークの端末から開ける。

```
http://<iPadのIP>:8765/
```

起動時に Console へ URL が出る。

| パス | 返すもの |
|---|---|
| `GET /` | 確認用 HTML |
| `GET /frame.jpg` | 最新 JPEG。**PHASE 1 では常に 503**（変換しないので当然） |
| `GET /frame` | `/frame.jpg` と同じ（既存コードとの互換用。後述） |
| `GET /status` | JSON |
| `GET /timeline` | バックグラウンド検証の記録（JSON） |
| `GET /start` | キャプチャ開始 |
| `GET /stop` | キャプチャ停止 |

`/status` の例（PHASE 1）。

```json
{
  "phase": 1,
  "capturing": true,
  "frame_count": 123,
  "last_frame_time": 1234567890.12,
  "has_frame": false,
  "errors": []
}
```

PHASE 1 で見るのは `capturing` / `frame_count` / `last_frame_time` の3つだけ。
`has_frame` が false、`/frame.jpg` が 503 なのは仕様どおり。

`frame_count` はコールバックが呼ばれた回数そのもの。PHASE 2 以降で使う
`encoded_count` は変換した回数で、0.2 秒に1回までに絞っている（60fps ぶんを
毎回変換すると Python が追いつかないため）。**コールバックが生きているかの
判定には常に `frame_count` を見る。**

**認証は無い。信頼できるネットワークでだけ使うこと。**

## テスト方法

### 手順1: 前面で動くか

1. iPad で `main.py` を Run
2. Console に `startCapture OK` が出るか見る
3. **別の端末**（PC やスマホ）のブラウザで `http://<iPadのIP>:8765/` を開く
4. `frame_count` が増え続けるか、`/frame.jpg` に画が出るかを見る

ここで映るのは **Pythonista 自身の画面**のはず（理由は後述）。

### 手順2: Hearthstone へ切り替えたあと（今回の本題）

**必ず別端末のブラウザを開いたまま**行う。iPad 側で見ていては何も分からない。

1. 前面のまま `frame_count` が増えているのを確認
2. iPad で Hearthstone に切り替える
3. 別端末のブラウザを見続けて、次のどれになるかを記録する

| 見えるもの | 判定 |
|---|---|
| `frame_count` が増え続ける | 取得は継続している |
| `frame_count` が止まるが `/status` は返る | **コールバックだけ止まった** |
| `/status` 自体が返らなくなる | **Pythonista ごと suspend された** |

何秒で止まったかも控える。画面に「応答なし」と出た時刻が目安。

4. Pythonista に戻って Console のまとめを読む

```
---- バックグラウンド検証 ----
tick 84 回（42 秒ぶん動いた）
  12:00:03  app_state                        state=background
  12:00:05  frames_stopped_in_background     after_seconds=1.5
  12:00:52  python_stalled                   seconds=47.3 state=background frames_during=0
  12:00:52  app_state                        state=active
  12:00:52  returned_to_foreground           background_seconds=49.0 frames_gained=0

=> Python 自体が止まっていた時間があります。Pythonista が suspend されています。
```

`python_stalled` は 0.5 秒ごとの記録が途切れた跡。**自分が止まっていた証拠**なので、
これが出たらコールバックの問題ではなくアプリの suspend が原因と確定する。

### 手順3: 無音ループで延命できるか（任意）

`main.py` の `KEEP_ALIVE_WITH_SILENT_AUDIO = True` にしてもう一度測る。
音は 0 で、Hearthstone の音を止めないよう `MixWithOthers` で開く。

手順2と比べて `python_stalled` が出なくなれば、Pythonista は `audio` の
Background Mode を持っていて延命できるということ。変わらなければ持っていない。

**最初から True にしないこと。** 素の挙動が分からなくなる。

## 使っている ReplayKit API

```objc
+[RPScreenRecorder sharedRecorder]
-[RPScreenRecorder isAvailable]
-[RPScreenRecorder isRecording]
-[RPScreenRecorder setMicrophoneEnabled:]
-[RPScreenRecorder startCaptureWithHandler:completionHandler:]   // iOS 11+
-[RPScreenRecorder stopCaptureWithHandler:]
```

ハンドラの型は
`void (^)(CMSampleBufferRef, RPSampleBufferType, NSError *)`。
`objc_util.ObjCBlock` で作り、`RPSampleBufferType == 0`（video）だけ拾う。
audio は使わない。

フレームの変換。

```
CMSampleBuffer
  -> CMSampleBufferGetImageBuffer()      CVPixelBuffer
  -> +[CIImage imageWithCVPixelBuffer:]
  -> -[CIContext createCGImage:fromRect:]
  -> +[UIImage imageWithCGImage:]
  -> UIImageJPEGRepresentation(img, 0.7)
```

`createCGImage:fromRect:` は +1 された CGImage を返すので `CGImageRelease` する。
変換は `autoreleasepool` の中で行う。**コールバックからは絶対に例外を投げない**
（ObjC 側へ抜けるとプロセスごと落ちる）ので、全体を try/except で包んである。

## Pythonista 固有の制限

実機で測る前に分かっている制約を先に書く。**測る意味が無いという意味ではなく、
どこで詰まるかの当たりをつけるため。**

### 1. `startCaptureWithHandler:` はアプリ自身の画面しか撮れない

これが一番効く。ReplayKit の in-app recording は **呼び出したアプリ自身の
描画内容**を返す API で、他アプリや OS 全体は対象外。
つまり Hearthstone の画面はそもそも来ない。

OS 全体を撮るには **Broadcast Upload Extension**
（`RPBroadcastSampleHandler` を持つ App Extension）が要り、
`RPSystemBroadcastPickerView` から起動する。拡張はアプリのバンドルに
同梱された別バイナリで、**実行時に Python から作ることはできない**。
Pythonista はこの拡張を持っていない。

### 2. バックグラウンドでは suspend される

該当する `UIBackgroundModes` を持たないアプリは、バックグラウンドへ回ると
数秒で suspend される。suspend されると Python のスレッドも HTTP サーバも止まる。
Pythonista が `audio` を宣言していれば無音再生で延命できる可能性があり、
それを手順3で測る。

### 3. ReplayKit 自体もバックグラウンドで止まる

仮に Python が延命できても、in-app キャプチャはアプリが前面でなくなった
時点でフレームを止める。

### まとめ

| 項目 | 要否 | Pythonista で可能か |
|---|---|---|
| entitlement（in-app capture） | 不要 | 可 |
| 画面収録の許可 | 必要 | 可（`isAvailable` は True だった） |
| **フレームのコールバック受信** | 必須 | **不可**（別スレッドから Python を呼べない） |
| 他アプリの画面を撮る | Broadcast Upload Extension が必須 | **不可**（拡張を追加できない） |
| バックグラウンド継続 | Background Mode が必要 | 未測定（手前で詰まった） |

**実機の結果として「Pythonista 単体では Hearthstone の画面は撮れない」で確定。**
しかも詰まったのは想定していた制限1ではなく、その手前の
「ObjC からの Python コールバック」だった。

## 取得できない場合の原因の切り分け

| 症状 | 原因 |
|---|---|
| `objc_util: false` | Pythonista 以外で実行している |
| `available: false` | スクリーンタイムで画面収録が禁止／他アプリが録画中 |
| `has_start_capture: false` | iOS 11 未満 |
| `startCapture failed: ...` | ユーザが許可しなかった／他の録画と競合 |
| 開始は成功するが `frame_count` が 0 のまま | コールバックが来ていない。ブロックの型か保持漏れを疑う |
| **`startCapture returned` の直後で落ちる** | ブロックの解放。`retain_global()` を疑う。次に `START_MODE` で絞る |
| **PHASE 1 でアプリごと落ちる** | JPEG 変換は無関係。`ObjCBlock` か `startCaptureWithHandler:` 周辺 |
| PHASE 2 で落ちる | CoreMedia / CoreVideo の ctypes シグネチャ |
| PHASE 3 で落ちる | CoreImage / UIImage 経路 |
| フレームは来るが Pythonista の画面が映る | 制限1。仕様どおりで、Hearthstone は撮れない |
| 切り替えた瞬間に `frame_count` が止まる | 制限3 |
| `/status` も返らなくなる | 制限2。Pythonista ごと suspend |
| `frame handler: ...` が errors に出る | Python 例外。サーバは生きているので継続する |

落ちた位置は `capture_log.txt` の最終行で判断する。

## 既存のカード認識へ繋ぐとき

このリポジトリの既存コードは、**配信元の `GET {host}/frame` が JPEG を返す**
という前提で書かれている。

- `start.py` の `/castframe?host=...` が `host + '/frame'` を取りに行く
- `arena_judge.py` も `'%s/frame' % host` を叩く

そこで、この PoC でも `/frame` を `/frame.jpg` の別名として用意してある。
**もしフレーム取得が成立したなら、`start.py` 側の接続先にこのサーバの
アドレスを入れるだけで既存の認識がそのまま動く。**

```
iPad: Pythonista (main.py, :8765)  ->  PC: start.py  ->  arena_assistant.html
```

同じ iPad の中で完結させる場合は、HTTP を挟まず直接渡せる。

```python
jpeg = capture.latest_jpeg()          # bytes
# PIL などで start.py の CARD_W x CARD_H へ切り出し
#   -> start.py: shape_descriptors(card_gray, card_color)
#   -> start.py: recognize(q, hero, ...)
```

`ReplayKitCapture` は最新1枚を持つだけで、保存も認識もしない。
`latest_jpeg()` を差し替え先として使えるよう、意図的にそこで切ってある。

## この PoC でやっていないこと

- カード認識（既存の `start.py` 側にある）
- フレームの連番保存
- 向きの補正（CIImage の向きはそのまま。必要なら後段で回す）
- 音声の取り込み
- **PHASE 2 / 3 の実機検証**。コードはあるが PHASE 1 が通ってから試すこと
