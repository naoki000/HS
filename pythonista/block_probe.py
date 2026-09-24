# -*- coding: utf-8 -*-
"""ObjCBlock が本当に動くのかを、ReplayKit から切り離して確かめる。

startCapture の直後で Pythonista ごと落ちる場合、容疑者は2つある。

    A. ReplayKit 固有の問題
    B. objc_util の ObjCBlock そのもの（ObjC から Python を呼び戻せない）

B なら ReplayKit は関係ない。ブロックを使うもっと単純な API で再現するはずで、
その場合 ReplayKit をどういじっても直らない。

段階を踏んで、落ちた場所を特定する。

1 objc_basic      ObjCClass を触るだけ。ブロックなし
    2 block_create    ObjCBlock を作るだけ。呼ばない
    3 block_sync      同じスレッドから同期で呼ばれるブロック
                      （NSArray enumerateObjectsUsingBlock:）
    4 block_async     別スレッドから呼ばれるブロック
                      （NSOperationQueue addOperationWithBlock:）
    5 objc_in_thread  生の threading.Thread から autoreleasepool 付きで
                      ObjC を触れるか（参考情報。失敗しても先へ進む）

ReplayKit のフレームコールバックは 4 と同じ「別スレッドから Python を呼ぶ」形。
**3 が通って 4 で落ちるなら、ReplayKit でも必ず落ちる。**

落ちると Python は何も書けないので、状態をファイルに先に書いておく。
「開始したのに終了が記録されていない段階」＝そこで落ちた段階。
次に Run するとその段階を飛ばして先へ進むので、1回のクラッシュにつき
1つ結果が確定する。
"""

import json
import os
import threading
import time

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'probe_state.json')

STEPS = ('objc_basic', 'block_create', 'block_sync', 'block_async',
         'objc_in_thread')
# ReplayKit を試すのに必須な段階。objc_in_thread は参考情報
REQUIRED = ('objc_basic', 'block_create', 'block_sync', 'block_async')


def _load():
    try:
        with open(STATE_PATH, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(state):
    try:
        with open(STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass


def reset():
    try:
        os.remove(STATE_PATH)
    except OSError:
        pass


def _mark_started(state, name):
    state[name] = {'started': time.time()}
    _save(state)


def _mark_ok(state, name, note=''):
    state[name]['ok'] = True
    state[name]['note'] = note
    _save(state)


def _resolve_crashes(state, log):
    """開始しただけで終わっていない段階は、そこで落ちたということ。"""
    crashed = []
    for name in STEPS:
        s = state.get(name)
        if s and 'ok' not in s and not s.get('crashed'):
            s['crashed'] = True
            crashed.append(name)
    if crashed:
        _save(state)
        for name in crashed:
            log('[probe] !! 前回 %s で落ちました' % name)
    return crashed


# ------------------------------------------------------------- 各段階

def _step_objc_basic(log):
    from objc_util import ns
    arr = ns(['a', 'b', 'c'])
    return 'NSArray count=%s' % arr.count()


def _step_block_create(log):
    from ctypes import c_void_p
    from objc_util import ObjCBlock, retain_global

    def noop(_blk):
        return None

    blk = ObjCBlock(noop, restype=None, argtypes=[c_void_p])
    retain_global(blk)
    return 'ObjCBlock を作成できました'


def _step_block_sync(log):
    """同じスレッドから同期で呼ばれるブロック。"""
    from ctypes import c_void_p, c_ulong
    from objc_util import ObjCBlock, retain_global, ns

    hits = []

    def on_obj(_blk, obj, idx, stop):
        hits.append(int(idx))

    blk = ObjCBlock(on_obj, restype=None,
                    argtypes=[c_void_p, c_void_p, c_ulong, c_void_p])
    retain_global(blk)
    arr = ns(['a', 'b', 'c'])
    log('[probe]   enumerateObjectsUsingBlock: を呼びます...')
    arr.enumerateObjectsUsingBlock_(blk)
    log('[probe]   戻ってきました')
    if len(hits) != 3:
        raise RuntimeError('ブロックが %d 回しか呼ばれませんでした（3回のはず）'
                           % len(hits))
    return '同期ブロックが 3 回呼ばれました'


def _step_block_async(log):
    """別スレッドから呼ばれるブロック。ReplayKit と同じ形。

    ブロックの中では ObjC を一切触らない。触ると「ブロックが呼べない」のか
    「別スレッドから ObjC を触れない」のか区別がつかなくなる。
    """
    from ctypes import c_void_p
    from objc_util import ObjCClass, ObjCBlock, retain_global

    done = threading.Event()
    info = {}

    def on_run(_blk):
        info['thread'] = threading.current_thread().name
        info['ident'] = threading.get_ident()
        done.set()

    blk = ObjCBlock(on_run, restype=None, argtypes=[c_void_p])
    retain_global(blk)
    q = ObjCClass('NSOperationQueue').new()
    main_ident = threading.get_ident()
    log('[probe]   addOperationWithBlock: を呼びます...')
    q.addOperationWithBlock_(blk)
    log('[probe]   戻ってきました。呼び返しを待ちます...')
    if not done.wait(5.0):
        raise RuntimeError('5秒待ってもブロックが呼ばれませんでした')
    same = info.get('ident') == main_ident
    return ('別スレッドから Python を呼び返せました（別スレッド=%s）'
            % (not same))


def _step_objc_in_thread(log):
    """生のスレッドから ObjC を触れるか。autoreleasepool ありで試す。

    HTTP サーバのスレッドから ObjC を触って segfault した経緯があるので、
    autoreleasepool で包めば安全なのかを確かめる。通れば設計を緩められる。
    落ちても本筋には影響しないので最後に置く。
    """
    from objc_util import ObjCClass, autoreleasepool

    out = {}

    def work():
        try:
            with autoreleasepool():
                out['v'] = int(ObjCClass('NSProcessInfo').processInfo()
                               .processorCount())
        except Exception as e:      # noqa: BLE001
            out['error'] = '%s: %s' % (type(e).__name__, e)

    t = threading.Thread(target=work, daemon=True)
    log('[probe]   生のスレッドから ObjC を呼びます...')
    t.start()
    t.join(5.0)
    if t.is_alive():
        raise RuntimeError('5秒待っても終わりませんでした')
    if out.get('error'):
        raise RuntimeError(out['error'])
    if 'v' not in out:
        raise RuntimeError('結果が返りませんでした')
    return 'autoreleasepool ありなら触れました (processorCount=%s)' % out['v']


_RUNNERS = {
    'objc_basic': _step_objc_basic,
    'block_create': _step_block_create,
    'block_sync': _step_block_sync,
    'block_async': _step_block_async,
    'objc_in_thread': _step_objc_in_thread,
}


def run(log=print, stop_on_crash=True):
    """未確定の段階を順に試す。戻り値は (全部通ったか, 結果の辞書)。"""
    state = _load()
    crashed_before = _resolve_crashes(state, log)

    log('[probe] ObjCBlock の動作確認')
    for name in STEPS:
        s = state.get(name) or {}
        if s.get('ok'):
            log('[probe]   %-14s OK（前回確認済み: %s）' % (name, s.get('note', '')))
            continue
        if s.get('crashed'):
            log('[probe]   %-14s クラッシュ済みなので飛ばします' % name)
            continue
        log('[probe]   %-14s 実行中...' % name)
        _mark_started(state, name)
        try:
            note = _RUNNERS[name](log)
        except Exception as e:      # noqa: BLE001 - Python 例外はここで捕まる
            state[name]['ok'] = False
            state[name]['error'] = '%s: %s' % (type(e).__name__, e)
            _save(state)
            log('[probe]   %-14s 失敗: %s' % (name, state[name]['error']))
            if stop_on_crash:
                return False, state
            continue
        _mark_ok(state, name, note)
        log('[probe]   %-14s OK  %s' % (name, note))

    ok = all((state.get(n) or {}).get('ok') for n in REQUIRED)
    return ok, state


def summary(state):
    out = []
    for name in STEPS:
        s = state.get(name) or {}
        if s.get('ok'):
            mark = 'OK'
        elif s.get('crashed'):
            mark = 'CRASH（Pythonista ごと落ちた）'
        elif 'ok' in s:
            mark = '失敗: %s' % s.get('error', '')
        else:
            mark = '未実行'
        out.append('  %-14s %s' % (name, mark))
    return '\n'.join(out)


def verdict(state):
    """どこで落ちたかから、次に何を疑うべきかを決める。"""
    def st(n):
        return state.get(n) or {}

    if st('block_async').get('crashed'):
        return ('別スレッドから Python を呼び返すと落ちます。\n'
                '    ReplayKit のフレームコールバックも同じ形なので、\n'
                '    この方式では原理的に受け取れません。')
    if st('block_sync').get('crashed'):
        return ('同期呼び出しですら落ちます。objc_util の ObjCBlock 自体が\n'
                '    この Pythonista では使えません。ReplayKit は無関係です。')
    if st('block_create').get('crashed'):
        return 'ObjCBlock を作る時点で落ちます。objc_util のバージョンを疑ってください。'
    if st('objc_basic').get('crashed'):
        return 'ObjC を触るだけで落ちます。objc_util が壊れています。'
    if all(st(n).get('ok') for n in REQUIRED):
        extra = ''
        s = st('objc_in_thread')
        if s.get('ok'):
            extra = ('\n    なお、autoreleasepool で包めば生のスレッドからでも '
                     'ObjC を触れます。')
        elif s.get('crashed'):
            extra = ('\n    生のスレッドからは autoreleasepool を使っても ObjC を\n'
                     '    触れません。現行の「ObjC はスクリプトスレッドだけ」が正解です。')
        return ('ObjCBlock は別スレッドからでも正常に呼ばれます。\n'
                '    つまり原因は ReplayKit 固有です。START_MODE で絞ってください。'
                + extra)
    return '未確定です。もう一度 Run してください。'
