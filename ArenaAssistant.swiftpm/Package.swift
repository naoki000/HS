// swift-tools-version: 5.9

// Swift Playgrounds の App Project 用マニフェスト。
// .iOSApplication は AppleProductTypes が提供するもので、
// Swift Playgrounds と Xcode のツールチェーンでのみ解決できる。

import AppleProductTypes
import PackageDescription

let package = Package(
    name: "ArenaAssistant",
    // ScreenCaptureKit は iPadOS 27.0 で追加されたため、これ未満では動かない。
    platforms: [
        .iOS("27.0")
    ],
    products: [
        .iOSApplication(
            name: "Arena Assistant",
            targets: ["AppModule"],
            bundleIdentifier: "local.arena.assistant",
            displayVersion: "0.1",
            bundleVersion: "1",
            supportedDeviceFamilies: [
                .pad
            ],
            // 省略不可。省略すると
            // "Missing argument for parameter 'supportedInterfaceOrientations'"
            // になる。関連値を取る形（.portraitUpsideDown(.when(...)) など）は
            // 仕様を確認できなかったので、単純な列挙子だけで構成する。
            supportedInterfaceOrientations: [
                .portrait,
                .landscapeLeft,
                .landscapeRight
            ]

            // 背面での取り込みに必要な UIBackgroundModes を入れる場合はここを使う。
            // この引数が Swift Playgrounds のツールチェーンで解決できるかは未検証。
            // ビルドが通らなければ、Swift Playgrounds だけでは設定できないと判断する。
            //
            // , additionalInfoPlistContentFilePath: "Resources/Info.plist"
        )
    ],
    targets: [
        .executableTarget(
            name: "AppModule",
            path: ".",
            exclude: [
                "README.md",
                "Resources/Info.plist"
            ]
        )
    ]
)
