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

使う API
    +[RPScreenRecorder sharedRecorder]
    -[RPScreenRecorder isAvailable]
    -[RPScreenRecorder isRecording]
    -[RPScreenRecorder setMicrophoneEnabled:]
    -[RPScreenRecorder startCaptureWithHandler:completionHandler:]   iOS 11+
    -[RPScreenRecorder stopCaptureWithHandler:]
"""

import threading
import time

# 1 = コールバックのみ / 2 = PixelBuffer の寸法まで / 3 = JPEG まで
PHASE = 1

AVAILABLE = False
IMPORT_ERROR = None

try:
    from ctypes import (c_void_p, c_long, c_double, c_size_t, c_bool, c_ubyte,
                        sizeof)
    from objc_util import (ObjCClass, ObjCInstance, ObjCBlock, c, sel,
                           on_main_thread, autoreleasepool, load_framework)
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
    load_framework('ReplayKit')
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

        self._lock = threading.Lock()
        self._jpeg = None
        self._ci_context = None
        # ObjCBlock は自分で参照を持たないと GC されてコールバックが死ぬ
        self._handler_block = None
        self._start_block = None
        self._stop_block = None

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

    # ------------------------------------------------------------- 状態

    def note_error(self, msg):
        with self._lock:
            self.errors.append({'t': time.time(), 'msg': str(msg)})
            del self.errors[:-MAX_ERRORS]
        self.log('[capture] ERROR %s' % msg)

    def availability(self):
        """ReplayKit が使えるか。呼べない理由もあわせて返す。"""
        info = {'objc_util': AVAILABLE, 'import_error': IMPORT_ERROR,
                'recorder': False, 'available': False,
                'has_start_capture': False, 'phase': self.phase}
        if not AVAILABLE:
            return info
        try:
            rec = RPScreenRecorder.sharedRecorder()
            info['recorder'] = bool(rec)
            if rec:
                info['available'] = bool(rec.isAvailable())
                info['has_start_capture'] = bool(rec.respondsToSelector_(
                    sel('startCaptureWithHandler:completionHandler:')))
        except Exception as e:    # noqa: BLE001
            info['error'] = '%s: %s' % (type(e).__name__, e)
        return info

    def status(self):
        with self._lock:
            return {
                'phase': self.phase,
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
                msg = ObjCInstance(err).localizedDescription()
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
                self.note_error('stopCapture: %s'
                                % ObjCInstance(err).localizedDescription())
            else:
                self.log('[capture] stopCapture OK')
        except Exception as e:      # noqa: BLE001
            self.note_error('stop completion: %s: %s' % (type(e).__name__, e))

    # ------------------------------------------------------------- 操作

    @on_main_thread
    def start(self):
        """キャプチャ開始。UIKit を触るのでメインスレッドで実行する。

        Pythonista ごと落ちたときに「どこまで進んだか」を Console と
        capture_log.txt から読めるよう、1手ごとにログを出す。
        """
        self.log('[capture] start() entered  (PHASE %d)' % self.phase)
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

            if not rec.respondsToSelector_(
                    sel('startCaptureWithHandler:completionHandler:')):
                self.note_error('startCaptureWithHandler: がありません（iOS 11 未満）')
                return False
            self.log('[capture] selector check OK')

            if rec.isRecording():
                self.log('[capture] すでに録画中です。stop してから開始してください')
                self.capturing = True
                return True

            rec.setMicrophoneEnabled_(False)
            self.log('[capture] microphone disabled')

            # RPSampleBufferType は NSInteger。arm64 では 8 バイト = c_long。
            # 引数は x0-x3 のレジスタ渡しなので、整数幅を取り違えても
            # ここで即座に落ちる類の間違いではない。実機の値をログに残す。
            self.log('[capture] sizeof(c_long)=%d  (NSInteger は 8 のはず)'
                     % sizeof(c_long))

            self._handler_block = ObjCBlock(
                self._on_sample, restype=None,
                argtypes=[c_void_p, c_void_p, c_long, c_void_p])
            self.log('[capture] handler block created')

            self._start_block = ObjCBlock(
                self._on_start_done, restype=None,
                argtypes=[c_void_p, c_void_p])
            self.log('[capture] completion block created')

            self.start_requested = True
            self.started_at = time.time()
            self.log('[capture] calling startCapture...')
            rec.startCaptureWithHandler_completionHandler_(
                self._handler_block, self._start_block)
            self.log('[capture] startCapture returned')
            return True
        except Exception as e:      # noqa: BLE001
            self.note_error('start: %s: %s' % (type(e).__name__, e))
            return False

    @on_main_thread
    def stop(self):
        self.log('[capture] stop() entered')
        if not AVAILABLE:
            return False
        try:
            rec = RPScreenRecorder.sharedRecorder()
            self._stop_block = ObjCBlock(
                self._on_stop_done, restype=None,
                argtypes=[c_void_p, c_void_p])
            self.log('[capture] calling stopCapture...')
            rec.stopCaptureWithHandler_(self._stop_block)
            self.start_requested = False
            self.log('[capture] stopCapture returned')
            return True
        except Exception as e:      # noqa: BLE001
            self.note_error('stop: %s: %s' % (type(e).__name__, e))
            return False
