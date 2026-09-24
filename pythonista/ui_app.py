# -*- coding: utf-8 -*-
"""Pythonista の ui で操作する画面。

ブラウザを開かなくても、iPad 単体で状態を見て開始・停止できるようにする。

なぜ ui を使うと安全か
    ui のボタン操作や ui.delay() のコールバックは**本物のメインスレッド**で走る。
    このアプリで何度も踏んだ「Pythonista が作っていないスレッドから ObjC を
    触ると segfault する」問題が、構造的に起きなくなる。
    ObjC を触る処理（pump / refresh_availability / app_state）は
    すべてこの更新ループから呼ぶ。

HTTP サーバは残してある
    Hearthstone を前面にしている間は iPad の画面が見えない。
    バックグラウンド検証には別端末のブラウザが要る。
"""

import time

try:
    import ui
    AVAILABLE = True
except ImportError:
    ui = None
    AVAILABLE = False

TICK = 0.5          # 更新間隔（秒）
SLOW_EVERY = 10     # 何回に1回、重い読み直しをするか（0.5s x 10 = 5s）
LOG_LINES = 14

BG = '#0d1117'
FG = '#f0f3f6'
DIM = '#9da7b3'
OK = '#3fb950'
NG = '#f85149'
CARD = '#151b23'


def _label(text='', size=13, color=FG, mono=False):
    lb = ui.Label()
    lb.text = text
    lb.font = ('Menlo' if mono else '<system>', size)
    lb.text_color = color
    lb.number_of_lines = 0
    return lb


class CaptureView(ui.View if AVAILABLE else object):
    def __init__(self, capture, probe, urls, log_buf, app_state, on_close=None):
        super().__init__()
        self.capture = capture
        self.probe = probe
        self.urls = urls
        self.log_buf = log_buf
        self.app_state = app_state
        self.on_close = on_close
        self._ticks = 0
        self._last_jpeg_at = None

        self.name = 'Capture PoC'
        self.background_color = BG

        self.head = _label('', 12, DIM)
        self.add_subview(self.head)

        self.rows = {}
        for key in ('mode', 'capturing', 'frames', 'app_state', 'replaykit'):
            k = _label('', 12, DIM)
            v = _label('—', 14, FG, mono=True)
            self.rows[key] = (k, v)
            self.add_subview(k)
            self.add_subview(v)
        self.rows['mode'][0].text = 'MODE / PHASE'
        self.rows['capturing'][0].text = 'capturing'
        self.rows['frames'][0].text = 'frames'
        self.rows['app_state'][0].text = 'app_state'
        self.rows['replaykit'][0].text = 'ReplayKit'

        self.btn_start = ui.Button(title='開始')
        self.btn_stop = ui.Button(title='停止')
        self.btn_reset = ui.Button(title='記録を消す')
        for b, bg in ((self.btn_start, '#238636'),
                      (self.btn_stop, '#2d1618'),
                      (self.btn_reset, '#21262d')):
            b.background_color = bg
            b.tint_color = FG
            b.corner_radius = 8
            b.font = ('<system-bold>', 15)
            self.add_subview(b)
        self.btn_start.action = self._on_start
        self.btn_stop.action = self._on_stop
        self.btn_reset.action = self._on_reset

        self.shot = ui.ImageView()
        self.shot.content_mode = ui.CONTENT_SCALE_ASPECT_FIT
        self.shot.background_color = '#000000'
        self.shot.corner_radius = 8
        self.add_subview(self.shot)

        self.logview = ui.TextView()
        self.logview.editable = False
        self.logview.font = ('Menlo', 10)
        self.logview.background_color = CARD
        self.logview.text_color = DIM
        self.logview.corner_radius = 8
        self.add_subview(self.logview)

    # ------------------------------------------------------------ 配置

    def layout(self):
        w = self.width
        pad = 12
        y = pad
        self.head.frame = (pad, y, w - pad * 2, 30)
        y += 34
        for key in ('mode', 'capturing', 'frames', 'app_state', 'replaykit'):
            k, v = self.rows[key]
            k.frame = (pad, y, 110, 20)
            v.frame = (pad + 115, y, w - pad * 2 - 115, 20)
            y += 22
        y += 6
        bw = (w - pad * 2 - 16) / 3.0
        for i, b in enumerate((self.btn_start, self.btn_stop, self.btn_reset)):
            b.frame = (pad + i * (bw + 8), y, bw, 40)
        y += 50
        log_h = LOG_LINES * 13 + 12
        shot_h = max(80, self.height - y - log_h - pad * 2)
        self.shot.frame = (pad, y, w - pad * 2, shot_h)
        y += shot_h + pad
        self.logview.frame = (pad, y, w - pad * 2, log_h)

    # ---------------------------------------------------------- 操作

    def _on_start(self, sender):
        # ボタンのアクションはメインスレッド。ObjC を直接触ってよい
        self.capture.start()

    def _on_stop(self, sender):
        self.capture.stop()

    def _on_reset(self, sender):
        if self.on_close:
            pass
        import block_probe
        import replaykit_capture
        block_probe.reset()
        replaykit_capture.reset_modes()
        self.logview.text = ('記録を消しました。Pythonista を一度止めて、'
                             'もう一度 Run してください。\n' + self.logview.text)

    # ---------------------------------------------------------- 更新

    def start_updating(self):
        self.update()

    def update(self):
        if not self.on_screen:
            return
        try:
            self._tick()
        except Exception as e:      # noqa: BLE001 - 画面は落とさない
            self.logview.text = 'UI エラー: %s: %s\n%s' % (
                type(e).__name__, e, self.logview.text)
        ui.delay(self.update, TICK)

    def _tick(self):
        self._ticks += 1
        # ここはメインスレッド。ObjC を触ってよい唯一の場所
        self.probe.set_state(self.app_state())
        self.capture.pump()
        if self._ticks % SLOW_EVERY == 1:
            self.capture.refresh_availability()

        st = self.capture.status()
        av = self.capture.availability()

        self.head.text = 'Pythonista 画面キャプチャ PoC\n%s' % (
            self.urls[0] if self.urls else '')
        self._set('mode', '%s / PHASE %d' % (st['start_mode'], st['phase']))
        self._set('capturing', str(st['capturing']),
                  OK if st['capturing'] else DIM)
        age = ('  最終 %.1fs前' % (time.time() - st['last_frame_time'])
               if st['last_frame_time'] else '')
        self._set('frames', '%d%s' % (st['frame_count'], age),
                  OK if st['frame_count'] else DIM)
        self._set('app_state', str(self.probe.snapshot()['app_state']))
        self._set('replaykit',
                  'available=%s recording=%s' % (av.get('available'),
                                                 av.get('recording')),
                  OK if av.get('available') else NG)

        jpeg = self.capture.latest_jpeg()
        if jpeg and st['last_jpeg_time'] != self._last_jpeg_at:
            self._last_jpeg_at = st['last_jpeg_time']
            try:
                self.shot.image = ui.Image.from_data(jpeg)
            except Exception:      # noqa: BLE001
                pass

        text = '\n'.join(self.log_buf[-LOG_LINES:])
        if text != self.logview.text:
            self.logview.text = text

    def _set(self, key, text, color=FG):
        lb = self.rows[key][1]
        if lb.text != text:
            lb.text = text
        lb.text_color = color

    def will_close(self):
        if self.on_close:
            self.on_close()


def run(capture, probe, urls, log_buf, app_state, on_close=None):
    """画面を出して、閉じられるまで待つ。"""
    v = CaptureView(capture, probe, urls, log_buf, app_state, on_close)
    v.present('fullscreen', hide_title_bar=False)
    v.start_updating()
    v.wait_modal()
