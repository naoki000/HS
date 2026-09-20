# Arena Assistant Prototype — Phase 1

iPad の Swift Playgrounds だけでビルド・実行する検証用アプリ。
**ScreenCaptureKit から画面フレームを受け取れるか**だけを確認する。

Mac と Xcode は使わない。Windows の VS Code で書いたソースを iPad へ移して動かす。

## 含まれるファイル

```
ArenaAssistant.swiftpm/
├── Package.swift              App Project のマニフェスト
├── ArenaAssistantApp.swift    エントリポイント
├── ContentView.swift          画面表示
├── ScreenCaptureManager.swift ScreenCaptureKit の制御とフレーム計数
├── Resources/
│   └── Info.plist             UIBackgroundModes 検証用（既定では未参照）
└── README.md
```

ソースはパッケージ直下に置く（`path: "."`）。
Swift Playgrounds が作る App Project と同じ配置。

画面に出すもの。

```
Arena Assistant Prototype
Capture status
[Start Capture] [Stop Capture]
Frame count
FPS
Frame size
```

## 動作要件

**iPadOS 27.0 以上が必須。** ScreenCaptureKit は iPadOS 27.0 で追加された。

Apple のドキュメントに以下の記載がある。

> ScreenCaptureKit replaces ReplayKit for screen streaming and mirroring.
> A broadcast extension is no longer necessary.

つまり ReplayKit の Broadcast Extension は不要で、通常のアプリから画面を取り込める。
Swift Playgrounds は App Extension を作れないため、この点は好都合。

## iPad への移送

`ArenaAssistant.swiftpm` はフォルダだが、iPadOS では拡張子によって
1つの書類として扱われる。

### 方法A　フォルダごと渡す（推奨）

1. Windows で `ArenaAssistant.swiftpm` を iCloud Drive に置く
2. iPad のファイルアプリで開く
3. Swift Playgrounds で開く

フォルダ名に `.swiftpm` が付いていれば、タップした時点で
Swift Playgrounds が App Project として認識する。

ZIP で渡す場合は、iPad 側で展開したあとに
フォルダ名が `ArenaAssistant.swiftpm` のままか確かめる。
ZIP の展開で拡張子が外れるとフォルダのままになるので、名前を付け直す。

### 方法B　手で作って貼る

移送がうまくいかない場合。

1. Swift Playgrounds で **App** プロジェクトを新規作成
2. 既定の `MyApp.swift` を削除し、`ArenaAssistantApp.swift` を追加
   （`@main` が重複するため）
3. `ContentView.swift` を本リポジトリの内容で置き換える
4. `ScreenCaptureManager.swift` を追加
5. アプリ設定で対応端末を iPad にする

この場合 `Package.swift` は Swift Playgrounds が生成したものを使うので、
`platforms: [.iOS("27.0")]` になっているかだけ確かめる。

## 使い方

1. **Start Capture** を押す
2. システムの共有ピッカーが出るので、取り込む対象を選ぶ
3. フレームが届き始めると Frame count と FPS が増える
4. **Stop Capture** で止める

取り込む対象をアプリ側で勝手に選ぶことはできない。
iPadOS では `SCContentSharingPicker` が返す `SCContentFilter` を使う決まりになっている。

## 使用している API

すべて iPadOS 27.0 で実在するもの。架空の API は使っていない。

| API | 用途 |
|---|---|
| `SCContentSharingPicker` | 取り込む対象の選択 |
| `SCContentSharingPickerObserver` | 選択結果 (`SCContentFilter`) の受け取り |
| `SCStream` | ストリームの生成と開始・停止 |
| `SCStreamConfiguration` | フレーム間隔・ピクセル形式の設定 |
| `SCStreamOutput` | `CMSampleBuffer` の受信 |
| `SCStreamDelegate` | 異常終了の検知 |

`SCShareableContent` によるディスプレイ列挙は macOS 向けの流儀なので使っていない。

---

## UIBackgroundModes = screen-capture について

**結論: Swift Playgrounds の画面上の設定だけでは追加できない。**

Apple のサンプル「Capturing screen content on iOS」には次の記載がある。

> The sample declares two background modes so ScreenCaptureKit continues to run
> while the app isn't frontmost:
> `screen-capture` in UIBackgroundModes, so the stream survives backgrounding
> for full-display capture.

つまり**アプリが前面にない状態で取り込みを続けるには `screen-capture` が必須**。
そしてこの設定は Xcode の Signing & Capabilities ペインで行う前提で書かれている。

### Swift Playgrounds での状況

Swift Playgrounds の App プロジェクトは `.swiftpm` 形式で、`Package.swift` の
`.iOSApplication(...)` が構成を持つ。アプリ設定の UI から触れるのは、
名前・アイコン・アクセントカラー・対応端末・画面の向き、および
`capabilities:` に列挙された定型の項目（カメラ、マイク、写真、位置情報、
ローカルネットワークなど、主に用途説明文を伴う権限）に限られる。

**この一覧に Background Modes は無い。** したがって、
アプリ設定の画面から `UIBackgroundModes` を追加することはできない。

### 回避策として考えられるもの（未検証）

`Package.swift` を直接編集し、`.iOSApplication(...)` の
`additionalInfoPlistContentFilePath:` で任意の Info.plist を差し込む方法が
知られている。ただし**この引数の存在を Apple の公開ドキュメントで確認できなかった**ため、
iPad 上の Swift Playgrounds のツールチェーンで通るかは実機で確かめる必要がある。

すぐに試せるようにしてある。`Package.swift` の末尾、
`supportedDeviceFamilies` の下にあるコメントを外すだけでよい。

```swift
, additionalInfoPlistContentFilePath: "Resources/Info.plist"
```

`Resources/Info.plist` にはすでにこう書いてある。

```xml
<key>UIBackgroundModes</key>
<array>
    <string>screen-capture</string>
</array>
```

ビルドが通らない、あるいはキーが反映されない場合、
**Swift Playgrounds だけでは背景での取り込みは実現できない。**
そのときはコメントを戻す（戻さないとビルドできないままになる）。

### Phase 1 への影響

無い。Phase 1 はアプリが前面にある状態でフレームが届くかだけを見るので、
`UIBackgroundModes` は不要。

### Phase 2 以降への影響（重要）

大きい。最終目的は **Hearthstone を前面で遊びながら画面を認識する**ことなので、
その間このアプリは背面に回る。`screen-capture` を設定できなければ、
背面に回った時点でストリームが止まる可能性が高い。

Phase 2 に進む前に、上の回避策が通るかを先に確かめることを勧める。
ここが通らなければ、アプリ構成そのものを考え直す必要がある。

---

## 制約として守っていること

- Xcode 固有の操作を前提にしていない
- `.xcodeproj` を前提にしていない
- 外部 SDK を使っていない
- ReplayKit の Broadcast Extension を使っていない
- カード認識は実装していない

## 確認できていないこと

- 実機での動作（Windows 環境のためビルド検証ができていない）
- `NSScreenCaptureUsageDescription` が必要かどうか。macOS では必須とされるが、
  iPadOS ではシステムピッカーが同意を兼ねるため不要の可能性がある。
  実行時に権限エラーが出る場合は、この用途説明文が必要かを疑う
- `additionalInfoPlistContentFilePath` が Swift Playgrounds で使えるか
- `swift-tools-version: 5.9` で Swift Playgrounds が受け付けるか。
  古いと言われたら先頭行を `6.0` などに上げる。
  ただし Swift 6 の厳格な並行性チェックが有効になると、
  デリゲートの Sendable 関連で警告が出る可能性がある
