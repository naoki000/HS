# Pythonista 画面キャプチャ PoC

iPad の Pythonista 3 だけで、ReplayKit の画面キャプチャがどこまで動くかを
**実機で測る**ための検証コード。カード認識は入っていない。

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

**ブロックの解放を最初に疑う。**

`startCapture returned` が出ているなら、呼び出し自体は成功している。
その次に起きるのは「ObjC 側がブロックを呼ぶ」ことなので、ブロックが
すでに解放されていれば、そこで落ちる。Python の try/except では捕まらない。

objc_util の `ObjCBlock` は、**Python の変数に入れておくだけでは足りない**。
`retain_global()` を通す必要がある。

```python
blk = ObjCBlock(fn, restype=None, argtypes=[...])
retain_global(blk)          # これが無いと ObjC から呼ばれる前に解放されうる
```

このコードは `_make_block()` で必ず `retain_global()` を通し、さらに
`self._blocks` にも残して二重に保持している。ログの
`(retained)` はそれが通ったことを示す。

### 開始方法を切り替えて絞る（START_MODE）

それでも落ちる場合、`replaykit_capture.py` の `START_MODE` を変えて、
どのブロックが原因かを分ける。

| START_MODE | 渡すブロック | 落ちなければ分かること |
|---|---|---|
| `'capture'` | フレーム用 + 完了用 | 本命。これが通れば PHASE 2 へ |
| `'capture_nohandler'` | 完了用だけ（フレーム用は nil） | 完了ブロックは無事。**フレーム用ブロックが原因** |
| `'record'` | 完了用だけ（旧 API `startRecordingWithHandler:`） | 権限の同意も録画開始も通る。`startCapture` 固有の問題 |

切り分けの順番。

1. `'capture'` で落ちる
2. → `'capture_nohandler'` を試す
   - 落ちない: フレーム用ブロックが原因。argtypes か呼び出し規約を疑う
   - 落ちる: 完了ブロックか `startCapture` 自体が原因
3. → `'record'` を試す
   - 落ちない: `startCaptureWithHandler:` 固有の問題
   - 落ちる: ObjCBlock の仕組みそのものか、権限まわり

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
| `replaykit_capture.py` | ReplayKit を objc_util から叩く。先頭に `PHASE` |
| `capture_server.py` | HTTP サーバ（別スレッド） |
| `background_probe.py` | バックグラウンド移行後に何が止まるかを記録 |
| `web/index.html` | ブラウザ確認用 |
| `capture_log.txt` | 実行時に作られる。クラッシュしても残る（git 管理外） |

`objc_util` が無い環境（PC）でも import は通り、`available: false` を返して
終わる。HTTP サーバ部分は PC でも動くので、API の形だけなら PC で確認できる。

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
| 画面収録の許可 | 必要 | 可（ユーザが許可すれば） |
| 他アプリの画面を撮る | Broadcast Upload Extension が必須 | **不可**（拡張を追加できない） |
| バックグラウンド継続 | Background Mode が必要 | 要測定（手順3） |

**予想される結論は「Pythonista 単体では Hearthstone の画面は撮れない」。**
ただしそれは実機のログで確定させる。このコードはそのためにある。

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
