# -*- coding: utf-8 -*-
"""Pythonista 3 で Run するだけで動く画面キャプチャ PoC。

検証したいこと
    Pythonista だけで、他アプリ（Hearthstone）を表示している間も
    画面フレームを取り続けられるか。

やること
    1. ReplayKit が使えるか調べて Console に出す
    2. RPScreenRecorder のキャプチャを開始する
    3. 最新フレームを1枚だけ JPEG で持つ
    4. HTTP サーバ（既定 8765）を別スレッドで立てる
    5. バックグラウンドへ回ったあと何が止まるかを 0.5 秒刻みで記録する

止め方
    Pythonista の停止ボタン、または Ctrl-C。
    停止時にバックグラウンド検証のまとめを Console に出す。
"""

import os
import sys
import time
import traceback

# Pythonista から Run すると作業ディレクトリが別の場所になることがある
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from background_probe import BackgroundProbe            # noqa: E402
import background_probe                                 # noqa: E402
from capture_server import CaptureServer                # noqa: E402
import block_probe                                      # noqa: E402
import crash_trap                                       # noqa: E402
import replaykit_capture                                # noqa: E402
import ui_app                                           # noqa: E402

PORT = 8765
JPEG_QUALITY = 0.7
MIN_ENCODE_INTERVAL = 0.2     # 秒。これより短い間隔では変換しない（PHASE 2 以降）
AUTO_START = True             # Run した時点でキャプチャを始める

# Pythonista の ui で操作する。ブラウザを開かなくても iPad 単体で見られる。
# ui のコールバックは本物のメインスレッドなので、ObjC を触るのも安全。
# False にすると従来の Console ループになる。
USE_UI = True
# HTTP サーバは UI と併用する。Hearthstone を前面にしている間は
# iPad の画面が見えないので、検証には別端末のブラウザが要る。
RUN_HTTP_SERVER = True

# ObjCBlock が動くかを ReplayKit より先に確かめる。
# 落ちた段階は probe_state.json に残り、次の Run では飛ばして先へ進む。
# やり直すときは RESET_BLOCK_PROBE = True にして1回 Run する。
RUN_BLOCK_PROBE = True
RESET_BLOCK_PROBE = False
# ReplayKit の開始モードの試行記録を消して最初から試す
RESET_CAPTURE_MODES = False

# 無音を鳴らし続けて suspend を遅らせる実験用スイッチ。
# 既定は False。まず素の挙動を測り、そのあと True にして比べること。
# True のまま測ると「suspend されるかどうか」が分からなくなる。
KEEP_ALIVE_WITH_SILENT_AUDIO = False

# Pythonista ごとネイティブクラッシュすると Console は消える。
# 最後に出たログが残るよう、1行ごとに flush してファイルにも書く。
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'capture_log.txt')
# 落ちる瞬間のスタック。try/except で捕まらない種類はここに残る
CRASH_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'crash_log.txt')
_log_file = None
# UI が末尾を読む。太らせないように切り詰める
LOG_BUF = []


def log(*a):
    msg = ' '.join(str(x) for x in a)
    print(msg)
    try:
        sys.stdout.flush()
    except Exception:      # noqa: BLE001
        pass
    LOG_BUF.append(msg)
    del LOG_BUF[:-200]
    if _log_file:
        try:
            _log_file.write('%s %s\n' % (time.strftime('%H:%M:%S'), msg))
            _log_file.flush()
        except Exception:  # noqa: BLE001
            pass


def open_log():
    global _log_file
    try:
        _log_file = open(LOG_PATH, 'a', encoding='utf-8')
        _log_file.write('\n===== %s 起動 =====\n'
                        % time.strftime('%Y-%m-%d %H:%M:%S'))
        _log_file.flush()
    except Exception as e:      # noqa: BLE001
        print('ログファイルを開けません: %s' % e)


def banner():
    log('=' * 58)
    log(' Pythonista 画面キャプチャ PoC   PHASE %d / MODE %s'
        % (replaykit_capture.PHASE, replaykit_capture.START_MODE))
    log('=' * 58)
    log(' Python   : %s' % sys.version.split()[0])
    log(' ログ     : %s' % LOG_PATH)
    try:
        from objc_util import ObjCClass
        dev = ObjCClass('UIDevice').currentDevice()
        log(' 端末     : %s / iOS %s' % (dev.model(), dev.systemVersion()))
    except Exception as e:      # noqa: BLE001
        log(' 端末     : 取得できません（%s）' % e)
    if replaykit_capture.PHASE == 1:
        log('')
        log(' PHASE 1: コールバックが来るかだけを見ます。')
        log('          フレームの中身には触りません。JPEG 変換もしません。')
        log('          /frame.jpg は 503 で正常です。')


def start_silent_audio():
    """無音ループで suspend を遅らせられるか試す。失敗しても続行する。"""
    try:
        import wave
        from objc_util import ObjCClass, ns
        import sound

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '_silence.wav')
        if not os.path.exists(path):
            w = wave.open(path, 'wb')
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b'\x00\x00' * 44100)
            w.close()

        # Hearthstone の音を止めないよう MixWithOthers（=1）で開く
        session = ObjCClass('AVAudioSession').sharedInstance()
        session.setCategory_withOptions_error_(
            ns('AVAudioSessionCategoryPlayback'), 1, None)
        session.setActive_error_(True, None)

        player = sound.Player(path)
        player.number_of_loops = -1
        player.volume = 0.0
        player.play()
        log(' keepalive: 無音ループを開始しました（実験用）')
        return player
    except Exception as e:      # noqa: BLE001
        log(' keepalive: 使えませんでした（%s: %s）' % (type(e).__name__, e))
        return None


def main():
    open_log()
    banner()

    # 前回ネイティブクラッシュしていたら、その跡が残っている
    prev = crash_trap.previous_crash(CRASH_PATH)
    if prev:
        log('')
        log(' !! %s' % prev.splitlines()[0])
        for line in prev.splitlines()[1:]:
            log('    %s' % line)
        log('')
        log(' これまでの実行')
        log(crash_trap.history(CRASH_PATH))
        log('')
    crash_trap.install(CRASH_PATH, log=log)

    capture = replaykit_capture.ReplayKitCapture(
        jpeg_quality=JPEG_QUALITY,
        min_encode_interval=MIN_ENCODE_INTERVAL,
        log=log)

    avail = capture.refresh_availability()
    log('')
    log(' ReplayKit の状態')
    for k in ('objc_util', 'recorder', 'available', 'recording',
              'has_start_capture', 'has_start_recording'):
        log('   %-20s %s' % (k, avail.get(k)))
    if avail.get('import_error'):
        log('   import_error       %s' % avail['import_error'])
    if avail.get('error'):
        log('   error              %s' % avail['error'])
    if not avail.get('objc_util'):
        log('')
        log(' !! objc_util がありません。Pythonista 3 で実行してください。')
        return 1

    probe = BackgroundProbe(capture, log=log)
    probe.start()

    server = None
    urls = []
    if RUN_HTTP_SERVER:
        server = CaptureServer(capture, probe, port=PORT, log=log)
        urls = server.start()
        log('')
        log(' HTTP サーバ')
        for u in urls:
            log('   %s' % u)
        log('   /frame.jpg  /frame  /status  /timeline  /start  /stop')
        log('')
        log(' ※ 認証はありません。信頼できるネットワークでだけ使ってください。')

    keepalive = start_silent_audio() if KEEP_ALIVE_WITH_SILENT_AUDIO else None

    if RUN_BLOCK_PROBE:
        log('')
        if RESET_BLOCK_PROBE:
            block_probe.reset()
            log('[probe] 記録を消しました')
        _, pstate = block_probe.run(log=log)
        log('')
        log(' ObjCBlock の確認')
        log(block_probe.summary(pstate))
        log('')
        log(' => %s' % block_probe.verdict(pstate))
        log('')

    # 落ちたモードは二度と試さない。無限にクラッシュさせないための歯止め。
    if RESET_CAPTURE_MODES:
        replaykit_capture.reset_modes()
        log(' 開始モードの記録を消しました')
    mode = replaykit_capture.pick_mode(log=log)
    log(' ReplayKit 開始モードの試行状況')
    log(replaykit_capture.mode_summary())
    log('')
    log(' => %s' % replaykit_capture.mode_verdict())
    log('')
    if mode is None:
        log(' 試せるモードが残っていません。ReplayKit は開始しません。')
        log(' やり直すときは capture_modes.json を消してください。')
    else:
        capture.start_mode = mode
        log(' 今回は MODE %s で試します。' % mode)

    if AUTO_START and mode is not None:
        log('')
        log(' キャプチャを開始します…')
        capture.start()

    log('')
    log(' --- 実機での手順 ---')
    log(' 1. iPad の画面で frames が増えるのを確認する')
    log(' 2. バックグラウンド検証をするなら、別の端末のブラウザで上の URL を開く')
    log('    （Hearthstone を前面にすると iPad の画面は見えないため）')
    log(' 3. iPad で Hearthstone に切り替える')
    log(' 4. 別端末のブラウザを見続ける')
    log('      frame_count が止まる      -> コールバックが止まった')
    log('      応答自体が返らなくなる    -> Pythonista ごと suspend された')
    log(' 5. Pythonista に戻ってきて画面を閉じるとまとめが出る')
    log('')
    log(' Pythonista ごと落ちたときは次の2つを見ること。')
    log('   capture_log.txt  どこまで進んだか（最終行）')
    log('   crash_log.txt    落ちた瞬間のスタック')
    log('')

    last = 0
    last_report = 0.0
    try:
        if USE_UI and ui_app.AVAILABLE:
            log(' 画面を出します。閉じると終了します。')
            ui_app.run(capture, probe, urls, LOG_BUF,
                       background_probe.app_state)
            raise KeyboardInterrupt
        while True:
            # ObjC はこのスレッドでしか触らない。他スレッドから触ると segfault する
            time.sleep(0.5)
            probe.set_state(background_probe.app_state())
            capture.pump()

            now = time.time()
            if now - last_report < 5.0:
                continue
            last_report = now
            capture.refresh_availability()
            st = capture.status()
            gained = st['frame_count'] - last
            last = st['frame_count']
            age = ('%.1fs前' % (now - st['last_frame_time'])
                   if st['last_frame_time'] else '—')
            log('[%s] capturing=%-5s frames=%-7d (+%d/5s) last=%s size=%s'
                % (time.strftime('%H:%M:%S'), st['capturing'], st['frame_count'],
                   gained, age, st['frame_size']))
            if st['errors']:
                log('        最新エラー: %s' % st['errors'][-1]['msg'])
    except KeyboardInterrupt:
        log('')
        log(' 停止します…')
    finally:
        try:
            capture.stop()
        except Exception:      # noqa: BLE001
            traceback.print_exc()
        probe.stop()
        if server:
            server.stop()
        if keepalive:
            try:
                keepalive.stop()
            except Exception:  # noqa: BLE001
                pass
        log('')
        log(probe.report())
        st = capture.status()
        log('')
        log(' 最終  PHASE=%d  frames=%d  encoded=%d  has_frame=%s'
            % (st['phase'], st['frame_count'], st['encoded_count'],
               st['has_frame']))
        for e in st['errors']:
            log('   error %s' % e['msg'])
        if st['phase'] == 1:
            log('')
            if st['frame_count'] > 0:
                log(' => PHASE 1 は通りました。コールバックは安定しています。')
                log('    replaykit_capture.py の PHASE を 2 にして次を試してください。')
            else:
                log(' => コールバックが1回も来ていません。')
                log('    start のログがどこで止まったかを確認してください。')
        if _log_file:
            try:
                _log_file.close()
            except Exception:      # noqa: BLE001
                pass
        crash_trap.mark_clean_exit()
    return 0


if __name__ == '__main__':
    sys.exit(main())
