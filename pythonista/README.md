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
| `replaykit_capture.py` | ReplayKit を objc_util から叩く。最新1フレームを保持 |
| `capture_server.py` | HTTP サーバ（別スレッド） |
| `background_probe.py` | バックグラウンド移行後に何が止まるかを記録 |
| `web/index.html` | ブラウザ確認用 |

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
| `GET /frame.jpg` | 最新 JPEG。無ければ 503 |
| `GET /frame` | `/frame.jpg` と同じ（既存コードとの互換用。後述） |
| `GET /status` | JSON |
| `GET /timeline` | バックグラウンド検証の記録（JSON） |
| `GET /start` | キャプチャ開始 |
| `GET /stop` | キャプチャ停止 |

`/status` の例。

```json
{
  "capturing": true,
  "frame_count": 123,
  "encoded_count": 24,
  "dropped_count": 99,
  "has_frame": true,
  "last_frame_time": 1234567890.12,
  "frame_size": [1620, 2160],
  "app_state": "active",
  "errors": []
}
```

`frame_count` はコールバックが呼ばれた回数そのもの。`encoded_count` は JPEG に
変換した回数で、既定では 0.2 秒に1回までに絞っている（60fps ぶんを毎回
変換すると Python が追いつかないため）。**コールバックが生きているかの判定には
`frame_count` を見る。**

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
| フレームは来るが Pythonista の画面が映る | 制限1。仕様どおりで、Hearthstone は撮れない |
| 切り替えた瞬間に `frame_count` が止まる | 制限3 |
| `/status` も返らなくなる | 制限2。Pythonista ごと suspend |
| `frame handler: ...` が errors に出る | 変換の失敗。サーバは生きているので継続する |

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
