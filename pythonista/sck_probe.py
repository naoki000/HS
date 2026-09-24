# -*- coding: utf-8 -*-
"""ScreenCaptureKit が Pythonista から使えるかを、落ちない範囲で調べる。

ReplayKit は iOS 27 で非推奨になり、ScreenCaptureKit が置き換えになった。
Apple 曰く "A broadcast extension is no longer necessary."

ただし ScreenCaptureKit には2つ条件がある。
    1. Info.plist の NSScreenCaptureUsageDescription（無いと iOS がアプリを殺す）
    2. フレームは SCStreamOutput の**デリゲート**で届く

2 は block_probe.py で確認済みのとおり、objc_util では別スレッドからの
呼び返しが成立しないので、たぶん無理。だが 1 は読むだけで判定できる。
Info.plist は Pythonista 本体のもので、書き換える手段は無い。

このスクリプトは Python -> ObjC の呼び出ししかしない。
コールバックもブロックも一切作らないので、クラッシュしない。

    単体で Run する。main.py とは独立。
"""

import sys

try:
    from objc_util import ObjCClass, autoreleasepool, load_framework
except ImportError as e:
    print('objc_util がありません（PC で動かしています）: %s' % e)
    sys.exit(0)


SCK_CLASSES = (
    'SCStream',                 # 本体
    'SCStreamConfiguration',
    'SCContentFilter',
    'SCShareableContent',
    'SCContentSharingPicker',   # 選択UI。iOS ではこれが必須
    'SCScreenshotManager',      # 単発の静止画。1枚ずつ撮るならこれ
)

# 無いと iOS がプロセスごと終了させるキー
NEEDED_KEYS = (
    'NSScreenCaptureUsageDescription',
    'UIBackgroundModes',
)


def _cls(name):
    try:
        return ObjCClass(name)
    except Exception:      # noqa: BLE001 - 無いクラスは例外になる
        return None


def probe_info_plist():
    print('== Pythonista の Info.plist ==')
    with autoreleasepool():
        bundle = ObjCClass('NSBundle').mainBundle()
        print('  bundle id : %s' % bundle.bundleIdentifier())
        print('  path      : %s' % bundle.bundlePath())
        for key in NEEDED_KEYS:
            val = bundle.objectForInfoDictionaryKey_(key)
            print('  %-32s %s' % (key, val if val else '**ありません**'))


def probe_framework():
    print('')
    print('== ScreenCaptureKit ==')
    try:
        load_framework('ScreenCaptureKit')
        print('  フレームワークの読み込み  OK')
    except Exception as e:      # noqa: BLE001
        print('  フレームワークの読み込み  失敗: %s' % e)
        try:
            ok = ObjCClass('NSBundle').bundleWithPath_(
                '/System/Library/Frameworks/ScreenCaptureKit.framework').load()
            print('  NSBundle で再試行        %s' % bool(ok))
        except Exception as e2:      # noqa: BLE001
            print('  NSBundle で再試行        失敗: %s' % e2)

    found = 0
    for name in SCK_CLASSES:
        c = _cls(name)
        print('  %-24s %s' % (name, 'あり' if c else 'なし'))
        found += bool(c)
    return found


def verdict(found):
    print('')
    print('== 判定 ==')
    with autoreleasepool():
        bundle = ObjCClass('NSBundle').mainBundle()
        has_key = bool(bundle.objectForInfoDictionaryKey_(
            'NSScreenCaptureUsageDescription'))

    if not found:
        print('  ScreenCaptureKit のクラスが見つかりません。')
        print('  iPadOS 27 未満か、Pythonista からは見えない位置にあります。')
        return
    if not has_key:
        print('  クラスはありますが、NSScreenCaptureUsageDescription が')
        print('  Pythonista の Info.plist にありません。')
        print('  この状態で撮影を始めると iOS がプロセスごと終了させます。')
        print('  Info.plist は署名済みバンドルの中なので書き換えられません。')
        print('')
        print('  => Pythonista では無理です。Swift Playgrounds で')
        print('     ArenaAssistant.swiftpm をビルドする道に切り替えてください。')
        return
    print('  キーもクラスも揃っています。ただしフレームは SCStreamOutput の')
    print('  デリゲートで届くため、objc_util の呼び返し問題が残ります。')
    print('  block_probe.py の block_async が CRASH なら、やはり受け取れません。')


def main():
    print('ScreenCaptureKit 調査（呼び返しを作らないので落ちません）')
    print('')
    probe_info_plist()
    found = probe_framework()
    verdict(found)


if __name__ == '__main__':
    main()
