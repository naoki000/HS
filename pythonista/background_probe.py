# -*- coding: utf-8 -*-
"""バックグラウンド移行後に何が止まるのかを記録する。

今回いちばん知りたいのは「Hearthstone に切り替えたあと何が止まるか」で、
止まり方は3通りある。

    A. フレームのコールバックだけ止まる（Python は動き続ける）
    B. Python ごと suspend される（HTTP サーバも応答しなくなる）
    C. 何も止まらない

自分自身が suspend されると、このスレッドも一緒に止まる。つまり
「0.5秒ごとの記録が途切れていること」自体が suspend の証拠になる。
前面に戻ってきたときに、途切れの前後を並べれば A と B を切り分けられる。

判定に使えるもの
    tick        0.5秒ごとに増える。Python が動いていた証拠
    gap         前回 tick からの経過。2秒以上空いたら suspend とみなす
    app_state   UIApplication.applicationState  0=active 1=inactive 2=background
    frames      その時点の frame_count
"""

import threading
import time

try:
    from objc_util import ObjCClass
    UIApplication = ObjCClass('UIApplication')
except ImportError:
    UIApplication = None

STATE_NAME = {0: 'active', 1: 'inactive', 2: 'background'}

TICK = 0.5          # 記録の間隔
GAP_SUSPEND = 2.0   # これ以上空いたら「動いていなかった」とみなす
MAX_EVENTS = 400


def app_state():
    if UIApplication is None:
        return None
    try:
        return int(UIApplication.sharedApplication().applicationState())
    except Exception:      # noqa: BLE001 - 取れなくても計測は続ける
        return None


class BackgroundProbe(threading.Thread):
    def __init__(self, capture, log=print):
        threading.Thread.__init__(self, daemon=True)
        self.capture = capture
        self.log = log
        self.running = True
        self.tick = 0
        self.events = []      # 状態が変わった瞬間だけ残す
        self.timeline = []    # 直近の生ログ
        self._lock = threading.Lock()
        self._last_state = None
        self._bg_since = None
        self._bg_frames = None

    def _add(self, kind, **kw):
        ev = {'t': time.time(), 'kind': kind}
        ev.update(kw)
        with self._lock:
            self.events.append(ev)
            del self.events[:-MAX_EVENTS]
        self.log('[probe] %s %s' % (kind, kw))

    def run(self):
        prev = time.time()
        while self.running:
            time.sleep(TICK)
            now = time.time()
            gap = now - prev
            prev = now
            self.tick += 1
            st = app_state()
            frames = self.capture.frame_count

            with self._lock:
                self.timeline.append({'t': round(now, 2), 'state': st,
                                      'frames': frames, 'gap': round(gap, 2)})
                del self.timeline[:-MAX_EVENTS]

            # 自分が止まっていた跡。これが出たら Python ごと suspend されている
            if gap >= GAP_SUSPEND:
                self._add('python_stalled', seconds=round(gap, 1),
                          state=STATE_NAME.get(st, st),
                          frames_during=frames - (self._bg_frames or frames))

            if st != self._last_state:
                self._add('app_state', state=STATE_NAME.get(st, st))
                if st == 2:                     # background へ入った
                    self._bg_since = now
                    self._bg_frames = frames
                elif self._last_state == 2:     # foreground へ戻った
                    self._add('returned_to_foreground',
                              background_seconds=round(now - (self._bg_since or now), 1),
                              frames_gained=frames - (self._bg_frames or frames))
                self._last_state = st

            # バックグラウンド中にフレームが止まった瞬間を拾う
            if st == 2 and self.capture.last_frame_time:
                idle = now - self.capture.last_frame_time
                if GAP_SUSPEND <= idle < GAP_SUSPEND + TICK * 1.5:
                    self._add('frames_stopped_in_background',
                              after_seconds=round(now - (self._bg_since or now), 1))

    def stop(self):
        self.running = False

    def snapshot(self):
        with self._lock:
            return {'tick': self.tick,
                    'app_state': STATE_NAME.get(self._last_state, self._last_state),
                    'events': list(self.events),
                    'timeline': list(self.timeline[-60:])}

    def report(self):
        """Console 用のまとめ。前面に戻ってきたあとに読む。"""
        snap = self.snapshot()
        lines = ['---- バックグラウンド検証 ----',
                 'tick %d 回（%.0f 秒ぶん動いた）' % (snap['tick'], snap['tick'] * TICK)]
        if not snap['events']:
            lines.append('状態の変化は記録されていません。')
        for e in snap['events']:
            when = time.strftime('%H:%M:%S', time.localtime(e['t']))
            rest = ' '.join('%s=%s' % (k, v) for k, v in e.items()
                            if k not in ('t', 'kind'))
            lines.append('  %s  %-32s %s' % (when, e['kind'], rest))
        stalls = [e for e in snap['events'] if e['kind'] == 'python_stalled']
        if stalls:
            lines.append('')
            lines.append('=> Python 自体が止まっていた時間があります。'
                         'Pythonista が suspend されています。')
        else:
            lines.append('')
            lines.append('=> Python は止まっていません。'
                         'フレームだけ止まったのならコールバック側の問題です。')
        return '\n'.join(lines)
