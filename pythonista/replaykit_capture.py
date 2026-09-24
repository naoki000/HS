# -*- coding: utf-8 -*-
"""ReplayKit の画面キャプチャを Pythonista から直接叩く。

Pythonista 3 (iPad / iOS) 専用。objc_util 経由で RPScreenRecorder を呼ぶ。
PC の CPython には objc_util が無いので、import すると AVAILABLE が False になる。

段階的に切り分ける
    PHASE 1  コールバックが来るかだけ見る。フレームの中身には一切触らない
    PHASE 2  CVPixelBuffer を取り出して幅と高さだけ読む
    PHASE 3  JPEG まで変換する

    Pythonista ごとネイティブクラッシュする場合、Python の try/except では
    捕まえられない。どの段階で落ちるかが分かれば原因を絞れるので、
    既定は PHASE 1 にしてある。

開始方法も切り替えられる（START_MODE）
    'capture'            startCaptureWithHandler:completionHandler:（本命）
    'capture_nohandler'  同じだが handler に nil を渡す。フレームは来ない。
                         「落ちるのはフレームのコールバックか、開始処理か」を分ける
    'record'             startRecordingWithHandler:（iOS 10 の旧API）。
                         ブロックは完了用の1つだけ。権限の同意フローだけを確かめる

ブロックの保持について
    objc_util の ObjCBlock は、Python 側で変数に持っているだけでは足りず、
    retain_global() で保持しないと ObjC から呼ばれる前に解放されることがある。
    解放されたブロックを ObjC が呼ぶと、try/except では捕まえられない
    ネイティブクラッシュになる。

使う API
    +[RPScreenRecorder sharedRecorder]
    -[RPScreenRecorder isAvailable]
    -[RPScreenRecorder isRecording]
    -[RPScreenRecorder setMicrophoneEnabled:]
    -[RPScreenRecorder startCaptureWithHandler:completionHandler:]   iOS 11+
    -[RPScreenRecorder stopCaptureWithHandler:]
    -[RPScreenRecorder startRecordingWithHandler:]                   iOS 10+
    -[RPScreenRecorder stopRecordingWithHandler:]
"""

import json
import os
import threading
import time

# 1 = コールバックのみ / 2 = PixelBuffer の寸法まで / 3 = JPEG まで
PHASE = 1

# 'capture' / 'capture_nohandler' / 'none' / 'record'
START_MODE = 'capture'

# 落ちるモードを避けながら自動で試す順番。
#   capture            フレーム用 + 完了用
#   capture_nohandler  完了用だけ
#   none               ブロックを1つも渡さない。Python は一切呼ばれない
#   record             旧 API。完了用だけ
# すべて落ちたら開始そのものをやめる（無限にクラッシュさせない）。
MODE_ORDER = ('capture', 'capture_nohandler', 'none', 'record')
MODE_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'capture_modes.json')
SURVIVE_SECONDS = 5.0     # 開始してこれだけ落ちなければ「通った」とみなす

AVAILABLE = False
IMPORT_ERROR = None

try:
    from ctypes import (c_void_p, c_long, c_double, c_size_t, c_bool, c_ubyte,
                        sizeof)
    from objc_util import (ObjCClass, ObjCInstance, ObjCBlock, c, sel,
                           on_main_thread, autoreleasepool, load_framework,
                           retain_global)
    AVAILABLE = True
except ImportError as e:      # PC で開いたときはここに落ちる
    IMPORT_ERROR = str(e)

    def on_main_thread(f):
        """objc_util が無い環境でも import だけは通るようにする。"""
        return f


# RPSampleBufferType
RP_VIDEO, RP_AUDIO_APP, RP_AUDIO_MIC = 0, 1, 2

MAX_ERRORS = 20
LOG_EVERY = 30      # コールバックから何フレームごとにログを出すか

RPScreenRecorder = None
CIImage = CIContext = UIImage = None
_imaging_ready = False


def _load_replaykit():
    """PHASE 1 で要るのは ReplayKit だけ。import 時に触るのはここまで。"""
    global RPScreenRecorder
    try:
        load_framework('ReplayKit')
    except Exception:      # noqa: BLE001 - NSBundle で直接ロードし直す
        ObjCClass('NSBundle').bundleWithPath_(
            '/System/Library/Frameworks/ReplayKit.framework').load()
    RPScreenRecorder = ObjCClass('RPScreenRecorder')


def _load_imaging():
    """PHASE 2 以降で初めて必要になる。呼ばれるまで触らない。

    import 時にここまでやると、ログが1行も出ないうちに落ちる可能性があり、
    クラッシュ位置が分からなくなる。
    """
    global _imaging_ready, CIImage, CIContext, UIImage
    if _imaging_ready:
        return
    load_framework('CoreMedia')
    load_framework('CoreVideo')

    c.CMSampleBufferIsValid.argtypes = [c_void_p]
    c.CMSampleBufferIsValid.restype = c_bool
    c.CMSampleBufferGetImageBuffer.argtypes = [c_void_p]
    c.CMSampleBufferGetImageBuffer.restype = c_void_p
    c.CVPixelBufferGetWidth.argtypes = [c_void_p]
    c.CVPixelBufferGetWidth.restype = c_size_t
    c.CVPixelBufferGetHeight.argtypes = [c_void_p]
    c.CVPixelBufferGetHeight.restype = c_size_t

    if PHASE >= 3:
        load_framework('CoreImage')
        CIImage = ObjCClass('CIImage')
        CIContext = ObjCClass('CIContext')
        UIImage = ObjCClass('UIImage')
        # CGFloat は arm64 で double
        c.UIImageJPEGRepresentation.argtypes = [c_void_p, c_double]
        c.UIImageJPEGRepresentation.restype = c_void_p
        c.CGImageRelease.argtypes = [c_void_p]
        c.CGImageRelease.restype = None
    _imaging_ready = True


if AVAILABLE:
    try:
        _load_replaykit()
    except Exception as e:    # noqa: BLE001 - 環境差でここが落ちても import は通す
        AVAILABLE = False
        IMPORT_ERROR = '%s: %s' % (type(e).__name__, e)


# ------------------------------------------------- 開始モードの自動切り替え
#
# ブロックを渡すと落ちるので、落ちたモードを覚えて次のモードへ進む。
# 開始する前に「試した」と記録しておき、次の起動でその記録が
# 「落ちずに済んだ」で閉じていなければ、そのモードは落ちたということ。

def _mode_load():
    try:
        with open(MODE_STATE_PATH, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _mode_save(state):
    try:
        with open(MODE_STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass


def reset_modes():
    try:
        os.remove(MODE_STATE_PATH)
    except OSError:
        pass


def pick_mode(log=print):
    """まだ落ちていないモードを選ぶ。全部落ちていたら None。"""
    state = _mode_load()
    for m in MODE_ORDER:
        s = state.get(m)
        if s and 'survived' not in s and not s.get('crashed'):
            s['crashed'] = True
            log('[capture] !! 前回 MODE %s で落ちました' % m)
    _mode_save(state)

    for m in MODE_ORDER:
        s = state.get(m) or {}
        if s.get('crashed'):
            continue
        return m
    return None


def mode_summary():
    state = _mode_load()
    out = []
    for m in MODE_ORDER:
        s = state.get(m) or {}
        if s.get('survived'):
            mark = '開始できた（落ちなかった）'
        elif s.get('crashed'):
            mark = 'クラッシュ'
        else:
            mark = '未試行'
        out.append('  %-18s %s' % (m, mark))
    return '\n'.join(out)


def mode_verdict():
    state = _mode_load()
    if (state.get('none') or {}).get('survived'):
        if (state.get('capture_nohandler') or {}).get('crashed'):
            return ('ブロックを1つも渡さなければ開始できます。\n'
                    '    つまり ReplayKit の開始自体は通り、\n'
                    '    **Python のブロックを渡した瞬間だけ**落ちます。\n'
                    '    フレームはブロックでしか受け取れないので、'
                    'この経路は使えません。')
        return 'ブロックなしなら開始できます。'
    if all((state.get(m) or {}).get('crashed') for m in MODE_ORDER):
        return ('どのモードでも落ちます。ReplayKit の呼び出し自体が\n'
                '    この環境では成立しません。')
    return '試行中です。もう一度 Run してください。'


class ReplayKitCapture(object):
    """最新フレームを1枚だけ持ち続ける。保存はしない。

    PHASE 1 ではフレームの中身に一切触らないので latest_jpeg() は常に None で、
    /frame.jpg は 503 を返す。それで正しい。
    """

    def __init__(self, jpeg_quality=0.7, min_encode_interval=0.2, log=print):
        self.jpeg_quality = jpeg_quality
        self.min_encode_interval = min_encode_interval
        self.log = log
        self.phase = PHASE
        self.start_mode = START_MODE

        self._lock = threading.Lock()
        self._jpeg = None
        self._ci_context = None
        # 作ったブロックは全部ここに残す。retain_global と二重に保持する
        self._blocks = []

        self.capturing = False
        self.start_requested = False
        self.frame_count = 0
        self.encoded_count = 0
        self.dropped_count = 0
        self.last_frame_time = None
        self.last_jpeg_time = None
        self.frame_size = None
        self.errors = []
        self.started_at = None

        # ObjC はスクリプトスレッドからしか触らない。他スレッドはこの写しを読む
        self._avail = {'objc_util': AVAILABLE, 'import_error': IMPORT_ERROR,
                       'recorder': False, 'available': False,
                       'recording': False, 'has_start_capture': False,
                       'has_start_recording': False,
                       'phase': self.phase, 'start_mode': self.start_mode}
        self._pending = None      # 'start' / 'stop' / None
        self._survived = False

    # ------------------------------------------------------------- 状態

    def note_error(self, msg):
        with self._lock:
            self.errors.append({'t': time.time(), 'msg': str(msg)})
            del self.errors[:-MAX_ERRORS]
        self.log('[capture] ERROR %s' % msg)

    def refresh_availability(self):
        """ReplayKit の状態を読み直す。**スクリプトスレッドから呼ぶこと。**

        HTTP サーバのスレッドなど、Pythonista が作っていないスレッドから
        ObjC を触ると segfault する（NSAutoreleasePool が無いため）。
        """
        info = dict(self._avail)
        info['phase'] = self.phase
        info['start_mode'] = self.start_mode
        if not AVAILABLE:
            self._avail = info
            return info
        try:
            rec = RPScreenRecorder.sharedRecorder()
            info['recorder'] = bool(rec)
            if rec:
                info['available'] = bool(rec.isAvailable())
                info['recording'] = bool(rec.isRecording())
                info['has_start_capture'] = bool(rec.respondsToSelector_(
                    sel('startCaptureWithHandler:completionHandler:')))
                info['has_start_recording'] = bool(rec.respondsToSelector_(
                    sel('startRecordingWithHandler:')))
            info.pop('error', None)
        except Exception as e:    # noqa: BLE001
            info['error'] = '%s: %s' % (type(e).__name__, e)
        self._avail = info
        return info

    def availability(self):
        """他スレッドから安全に読める写し。ObjC には触らない。"""
        return dict(self._avail)

    # ------------------------------------- 他スレッドからの依頼（ObjC を触らない）

    def request_start(self):
        with self._lock:
            self._pending = 'start'

    def request_stop(self):
        with self._lock:
            self._pending = 'stop'

    def pump(self):
        """依頼をここで実行する。**スクリプトスレッドから定期的に呼ぶこと。**"""
        with self._lock:
            todo, self._pending = self._pending, None
        if todo == 'start':
            self.start()
        elif todo == 'stop':
            self.stop()
        self._note_survived()
        return todo

    def _note_survived(self):
        """開始してから一定時間落ちなければ、そのモードは通ったと記録する。"""
        if self._survived or not self.start_requested or not self.started_at:
            return
        if time.time() - self.started_at < SURVIVE_SECONDS:
            return
        self._survived = True
        state = _mode_load()
        entry = state.setdefault(self.start_mode, {})
        entry['survived'] = time.time()
        entry.pop('crashed', None)
        _mode_save(state)
        self.log('[capture] MODE %s は %.0f 秒落ちませんでした'
                 % (self.start_mode, SURVIVE_SECONDS))

    def status(self):
        with self._lock:
            return {
                'phase': self.phase,
                'start_mode': self.start_mode,
                'capturing': self.capturing,
                'start_requested': self.start_requested,
                'frame_count': self.frame_count,
                'encoded_count': self.encoded_count,
                'dropped_count': self.dropped_count,
                'has_frame': self._jpeg is not None,
                'jpeg_bytes': len(self._jpeg) if self._jpeg else 0,
                'last_frame_time': self.last_frame_time,
                'last_jpeg_time': self.last_jpeg_time,
                'frame_size': self.frame_size,
                'started_at': self.started_at,
                'errors': list(self.errors),
            }

    def latest_jpeg(self):
        with self._lock:
            return self._jpeg

    # ------------------------------------------- フレームの中身（PHASE 2+）

    def _context(self):
        if self._ci_context is None:
            self._ci_context = CIContext.context()
        return self._ci_context

    def _pixel_size(self, sbuf):
        """PHASE 2。CVPixelBuffer を取り出して寸法だけ読む。"""
        _load_imaging()
        if not c.CMSampleBufferIsValid(sbuf):
            return None
        pixel_buffer = c.CMSampleBufferGetImageBuffer(sbuf)
        if not pixel_buffer:
            return None
        return [int(c.CVPixelBufferGetWidth(pixel_buffer)),
                int(c.CVPixelBufferGetHeight(pixel_buffer))]

    def _to_jpeg(self, sbuf):
        """PHASE 3。CMSampleBuffer から JPEG bytes を作る。失敗したら None。"""
        _load_imaging()
        if not c.CMSampleBufferIsValid(sbuf):
            return None
        pixel_buffer = c.CMSampleBufferGetImageBuffer(sbuf)
        if not pixel_buffer:
            return None

        w = int(c.CVPixelBufferGetWidth(pixel_buffer))
        h = int(c.CVPixelBufferGetHeight(pixel_buffer))

        with autoreleasepool():
            ci = CIImage.imageWithCVPixelBuffer_(pixel_buffer)
            if not ci:
                return None
            cg = self._context().createCGImage_fromRect_(ci, ci.extent())
            if not cg:
                return None
            try:
                img = UIImage.imageWithCGImage_(cg)
                data_ptr = c.UIImageJPEGRepresentation(img, self.jpeg_quality)
                if not data_ptr:
                    return None
                data = ObjCInstance(data_ptr)
                n = int(data.length())
                if n <= 0:
                    return None
                buf = (c_ubyte * n)()
                data.getBytes_length_(buf, n)
                jpeg = bytes(bytearray(buf))
            finally:
                # createCGImage: は +1 された CGImage を返す。放っておくと溜まる
                c.CGImageRelease(cg)

        self.frame_size = [w, h]
        return jpeg

    # ------------------------------------------------------- コールバック

    def _on_sample(self, _blk, sbuf, buf_type, err):
        """ReplayKit のワーカースレッドから呼ばれる。絶対に例外を投げない。

        PHASE 1 では sbuf に触らない。数えるだけ。
        ここで CoreMedia / CoreImage を触るとネイティブクラッシュしうるので、
        まず「コールバックそのものが安定して来るか」を確かめる。
        """
        try:
            if int(buf_type) != RP_VIDEO:
                return

            now = time.time()
            with self._lock:
                self.capturing = True
                self.frame_count += 1
                self.last_frame_time = now
                n = self.frame_count

            # 毎フレーム print するとコンソールが詰まる。60fps なら約2回/秒
            if n % LOG_EVERY == 0:
                self.log('[capture] frames=%d' % n)

            if self.phase >= 2:
                self._on_sample_phase2(sbuf, now)
        except Exception as e:      # noqa: BLE001 - ObjC 側へ例外を返さない
            self.note_error('frame handler: %s: %s' % (type(e).__name__, e))

    def _on_sample_phase2(self, sbuf, now):
        """PHASE 2 以降だけが通る道。PHASE 1 では呼ばれない。"""
        with self._lock:
            due = (now - (self.last_jpeg_time or 0)) >= self.min_encode_interval
            if not due:
                self.dropped_count += 1
            else:
                self.last_jpeg_time = now
        if not due:
            return
        if self.phase == 2:
            size = self._pixel_size(sbuf)
            if size:
                with self._lock:
                    self.frame_size = size
                    self.encoded_count += 1
            return
        jpeg = self._to_jpeg(sbuf)
        if jpeg:
            with self._lock:
                self._jpeg = jpeg
                self.encoded_count += 1
                self.last_jpeg_time = time.time()

    def _on_start_done(self, _blk, err):
        try:
            if err:
                # ReplayKit のキューから呼ばれる。ObjC を触るならプールを敷く
                with autoreleasepool():
                    msg = str(ObjCInstance(err).localizedDescription())
                self.capturing = False
                self.note_error('startCapture failed: %s' % msg)
            else:
                self.capturing = True
                self.started_at = time.time()
                self.log('[capture] startCapture OK')
        except Exception as e:      # noqa: BLE001
            self.note_error('start completion: %s: %s' % (type(e).__name__, e))

    def _on_stop_done(self, _blk, err):
        try:
            self.capturing = False
            if err:
                with autoreleasepool():
                    msg = str(ObjCInstance(err).localizedDescription())
                self.note_error('stopCapture: %s' % msg)
            else:
                self.log('[capture] stopCapture OK')
        except Exception as e:      # noqa: BLE001
            self.note_error('stop completion: %s: %s' % (type(e).__name__, e))

    # ------------------------------------------------------------- 操作

    def _make_block(self, fn, argtypes, label):
        """ブロックを作って ObjC 側から解放されないよう保持する。

        Python 変数に入れておくだけでは足りない。retain_global() を通さないと、
        ObjC がブロックを呼ぶ前に解放され、呼ばれた瞬間にネイティブクラッシュする。
        """
        blk = ObjCBlock(fn, restype=None, argtypes=argtypes)
        retain_global(blk)
        self._blocks.append(blk)
        self.log('[capture] %s block created (retained)' % label)
        return blk

    @on_main_thread
    def start(self):
        """キャプチャ開始。

        **スクリプトスレッドからしか呼んではいけない。**
        HTTP サーバなど Pythonista が作っていないスレッドから呼ぶと
        on_main_thread の中で segfault する。他スレッドからは
        request_start() を使い、pump() でここまで持ってくる。
        """
        self.log('[capture] start() entered  (PHASE %d / MODE %s)'
                 % (self.phase, self.start_mode))
        if not AVAILABLE:
            self.note_error('objc_util を import できません: %s' % IMPORT_ERROR)
            return False
        try:
            rec = RPScreenRecorder.sharedRecorder()
            if not rec:
                self.note_error('sharedRecorder が nil です')
                return False
            self.log('[capture] sharedRecorder OK')

            if not rec.isAvailable():
                self.note_error('RPScreenRecorder.isAvailable == False '
                                '（他アプリが録画中／機能制限の可能性）')
                return False
            self.log('[capture] isAvailable OK')

            if rec.isRecording():
                self.log('[capture] すでに録画中です。stop してから開始してください')
                self.capturing = True
                return True

            rec.setMicrophoneEnabled_(False)
            self.log('[capture] microphone disabled')

            # 開始する前に記録しておく。落ちてもここまでは残るので、
            # 次の起動で「このモードは落ちた」と判定できる。
            state = _mode_load()
            state[self.start_mode] = {'tried': time.time()}
            _mode_save(state)

            # RPSampleBufferType は NSInteger。arm64 では 8 バイト = c_long。
            # 引数は x0-x3 のレジスタ渡しなので、整数幅を取り違えても
            # ここで即座に落ちる類の間違いではない。実機の値をログに残す。
            self.log('[capture] sizeof(c_long)=%d  (NSInteger は 8 のはず)'
                     % sizeof(c_long))

            if self.start_mode == 'record':
                return self._start_record(rec)
            return self._start_capture(rec)
        except Exception as e:      # noqa: BLE001
            self.note_error('start: %s: %s' % (type(e).__name__, e))
            return False

    def _start_capture(self, rec):
        """本命の startCaptureWithHandler:completionHandler:。"""
        if not rec.respondsToSelector_(
                sel('startCaptureWithHandler:completionHandler:')):
            self.note_error('startCaptureWithHandler: がありません（iOS 11 未満）')
            return False
        self.log('[capture] selector check OK')

        handler = completion = None
        if self.start_mode == 'none':
            # Python のブロックを1つも渡さない。ObjC から Python は呼ばれない。
            # フレームは来ないが、開始そのものが通るかだけを確かめられる。
            self.log('[capture] ブロックを渡しません（Python は呼ばれません）')
        else:
            if self.start_mode == 'capture':
                handler = self._make_block(
                    self._on_sample, [c_void_p, c_void_p, c_long, c_void_p],
                    'handler')
            else:
                self.log('[capture] handler は nil で呼びます（フレームは来ません）')
            completion = self._make_block(
                self._on_start_done, [c_void_p, c_void_p], 'completion')

        self.start_requested = True
        self.started_at = time.time()
        self.log('[capture] calling startCapture...')
        rec.startCaptureWithHandler_completionHandler_(handler, completion)
        self.log('[capture] startCapture returned')
        if self.start_mode == 'none':
            # 完了ハンドラが無いので、成否は isRecording() で見るしかない
            self.capturing = bool(rec.isRecording())
            self.log('[capture] isRecording=%s' % self.capturing)
        return True

    def _start_record(self, rec):
        """旧 API。フレームは来ないが、権限の同意フローだけを確かめられる。"""
        if not rec.respondsToSelector_(sel('startRecordingWithHandler:')):
            self.note_error('startRecordingWithHandler: がありません')
            return False
        self.log('[capture] selector check OK (startRecordingWithHandler:)')
        completion = self._make_block(
            self._on_start_done, [c_void_p, c_void_p], 'completion')
        self.start_requested = True
        self.started_at = time.time()
        self.log('[capture] calling startRecording...')
        rec.startRecordingWithHandler_(completion)
        self.log('[capture] startRecording returned')
        return True

    @on_main_thread
    def stop(self):
        self.log('[capture] stop() entered')
        if not AVAILABLE:
            return False
        try:
            rec = RPScreenRecorder.sharedRecorder()
            block = self._make_block(
                self._on_stop_done, [c_void_p, c_void_p], 'stop')
            if self.start_mode == 'record':
                self.log('[capture] calling stopRecording...')
                rec.stopRecordingWithHandler_(block)
            else:
                self.log('[capture] calling stopCapture...')
                rec.stopCaptureWithHandler_(block)
            self.start_requested = False
            self.log('[capture] stop returned')
            return True
        except Exception as e:      # noqa: BLE001
            self.note_error('stop: %s: %s' % (type(e).__name__, e))
            return False
