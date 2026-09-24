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


def _install_faulthandler():
    """SIGSEGV / SIGBUS / SIGABRT / SIGFPE / SIGILL でスタックを吐かせる。"""
    global _fault_file
    _fault_file = open(_path, 'a', encoding='utf-8')
    _fault_file.write('\n--- faulthandler 有効 %s ---\n'
                      % time.strftime('%Y-%m-%d %H:%M:%S'))
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


def previous_crash(path, max_lines=40):
    """前回の実行で何か残っていれば、その末尾を返す。"""
    try:
        with open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    if not lines:
        return None
    return '\n'.join(lines[-max_lines:])


def test_segfault():
    """わざと落として仕掛けが効いているか確かめる。普段は呼ばない。"""
    import ctypes
    ctypes.string_at(1)
