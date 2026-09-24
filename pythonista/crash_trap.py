# -*- coding: utf-8 -*-
"""落ちる瞬間の記録を残す。

Python の try/except で捕まるのは Python 例外だけ。
Pythonista ごと落ちる場合は、たいてい次のどちらかで、except には来ない。

    NSException        ObjC 側の例外。誰も catch しないと abort() でプロセス終了
    EXC_BAD_ACCESS     不正なメモリ参照。解放済みブロックの呼び出しなど。
                       そもそも例外ではなく SIGSEGV / SIGBUS

どちらも「止める」ことはできないが、**死ぬ直前に書き出す**ことはできる。

    faulthandler                   SIGSEGV / SIGBUS / SIGABRT などでスタックを吐く
    NSSetUncaughtExceptionHandler  NSException の名前・理由・ネイティブスタック

出力先は crash_log.txt。Console はアプリごと落ちると消えるので、
ファイルに1行ずつ flush して書く。
"""

import faulthandler
import os
import sys
import time

_fault_file = None
_exc_handler = None      # C 関数ポインタ。GC されると落ちるので保持する
_log = print
_path = None


def _write(text):
    """死にかけでも書けるよう、開いて書いて閉じるまでを一息でやる。"""
    if not _path:
        return
    try:
        with open(_path, 'a', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except Exception:      # noqa: BLE001
        pass


SESSION_MARK = '--- faulthandler 有効 '
CLEAN_MARK = '--- 正常終了 '
CRASH_SIGNS = ('Fatal Python error', 'ObjC 例外で落ちます',
               'Windows fatal exception')
MAX_BYTES = 200 * 1024


def _trim():
    """無限に太らせない。古いほうから捨てる。"""
    try:
        if os.path.getsize(_path) <= MAX_BYTES:
            return
        with open(_path, encoding='utf-8') as f:
            text = f.read()
        with open(_path, 'w', encoding='utf-8') as f:
            f.write(text[-MAX_BYTES // 2:])
    except OSError:
        pass


def _install_faulthandler():
    """SIGSEGV / SIGBUS / SIGABRT / SIGFPE / SIGILL でスタックを吐かせる。"""
    global _fault_file
    _trim()
    _fault_file = open(_path, 'a', encoding='utf-8')
    _fault_file.write('\n%s%s ---\n'
                      % (SESSION_MARK, time.strftime('%Y-%m-%d %H:%M:%S')))
    _fault_file.flush()
    # ファイルは閉じない。落ちた瞬間に書く先として使われる
    faulthandler.enable(file=_fault_file, all_threads=True)
    return True


def _install_objc_handler():
    """NSException を、死ぬ直前に名前・理由・ネイティブスタック付きで残す。

    NSSetUncaughtExceptionHandler が取るのは **C の関数ポインタ**であって
    ブロックではない。ObjCBlock ではなく CFUNCTYPE を使う。
    """
    global _exc_handler
    from ctypes import CFUNCTYPE, c_void_p, cast
    from objc_util import ObjCInstance, c

    handler_t = CFUNCTYPE(None, c_void_p)

    def on_uncaught(exc_ptr):
        try:
            exc = ObjCInstance(exc_ptr)
            lines = ['\n!!!! ObjC 例外で落ちます %s'
                     % time.strftime('%Y-%m-%d %H:%M:%S'),
                     '  name   : %s' % exc.name(),
                     '  reason : %s' % exc.reason()]
            try:
                for s in exc.callStackSymbols():
                    lines.append('    %s' % s)
            except Exception:      # noqa: BLE001
                lines.append('    （スタックを取得できません）')
            _write('\n'.join(lines) + '\n')
        except Exception:          # noqa: BLE001 - ここで落ちても意味が無い
            _write('\n!!!! ObjC 例外（詳細を取得できませんでした）\n')

    _exc_handler = handler_t(on_uncaught)
    c.NSSetUncaughtExceptionHandler.argtypes = [c_void_p]
    c.NSSetUncaughtExceptionHandler.restype = None
    c.NSSetUncaughtExceptionHandler(cast(_exc_handler, c_void_p))
    return True


def install(path, log=print):
    """両方を仕掛ける。片方が失敗しても、もう片方は使えるようにする。"""
    global _log, _path
    _log = log
    _path = path

    ok_fault = ok_objc = False
    try:
        ok_fault = _install_faulthandler()
    except Exception as e:      # noqa: BLE001
        log('[crash] faulthandler を仕掛けられません: %s: %s'
            % (type(e).__name__, e))
    try:
        ok_objc = _install_objc_handler()
    except Exception as e:      # noqa: BLE001
        log('[crash] ObjC 例外ハンドラを仕掛けられません: %s: %s'
            % (type(e).__name__, e))

    log('[crash] faulthandler=%s  NSException=%s  -> %s'
        % (ok_fault, ok_objc, path))
    return ok_fault or ok_objc


def mark_clean_exit():
    """正常に終わったことを残す。

    これが無いまま次のセッションが始まっていたら、記録できずに死んだということ。
    ヘッダだけ並んでいるのを見て「落ちなかった」と誤解しないために要る。
    """
    _write('%s%s ---\n' % (CLEAN_MARK, time.strftime('%Y-%m-%d %H:%M:%S')))


def _sessions(path):
    """ログを「1回の実行」ごとに切り分ける。"""
    try:
        with open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out = []
    cur = None
    for line in lines:
        if line.startswith(SESSION_MARK):
            cur = {'started': line[len(SESSION_MARK):].rstrip(' -'),
                   'lines': [], 'clean': False, 'crashed': False}
            out.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith(CLEAN_MARK):
            cur['clean'] = True
            continue
        if any(s in line for s in CRASH_SIGNS):
            cur['crashed'] = True
        cur['lines'].append(line)
    return out


def previous_crash(path, max_lines=40):
    """前回の実行がどう終わったかを1行目にまとめて返す。"""
    ss = _sessions(path)
    if not ss:
        return None
    last = ss[-1]
    head = '前回の実行（%s 開始）: ' % last['started']
    if last['crashed']:
        body = [l for l in last['lines'] if l.strip()]
        return head + 'クラッシュしました\n' + '\n'.join(body[:max_lines])
    if last['clean']:
        return head + '正常に終了しています'
    if not [l for l in last['lines'] if l.strip()]:
        return (head + '記録が残らずに終わっています。\n'
                '  faulthandler が捕まえられない落ち方（iOS による強制終了、'
                'メモリ不足など）の可能性があります。')
    return head + '不明\n' + '\n'.join(last['lines'][:max_lines])


def history(path, n=8):
    """直近の実行結果を一覧で返す。何回目から落ち始めたかが分かる。"""
    ss = _sessions(path)
    if not ss:
        return '（記録なし）'
    out = []
    for s in ss[-n:]:
        if s['crashed']:
            mark = 'クラッシュ'
        elif s['clean']:
            mark = '正常終了'
        elif not [l for l in s['lines'] if l.strip()]:
            mark = '記録なしで終了'
        else:
            mark = '不明'
        out.append('  %s  %s' % (s['started'], mark))
    return '\n'.join(out)


def test_segfault():
    """わざと落として仕掛けが効いているか確かめる。普段は呼ばない。"""
    import ctypes
    ctypes.string_at(1)
