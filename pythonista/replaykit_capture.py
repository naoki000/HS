# -*- coding: utf-8 -*-
"""ReplayKit の画面キャプチャを Pythonista から直接叩く。

Pythonista 3 (iPad / iOS) 専用。objc_util 経由で RPScreenRecorder を呼ぶ。
PC の CPython には objc_util が無いので、import すると AVAILABLE が False になる。

使う API
    +[RPScreenRecorder sharedRecorder]
    -[RPScreenRecorder isAvailable]
    -[RPScreenRecorder setMicrophoneEnabled:]
    -[RPScreenRecorder startCaptureWithHandler:completionHandler:]   iOS 11+
    -[RPScreenRecorder stopCaptureWithHandler:]

フレームの変換
    CMSampleBuffer -> CVPixelBuffer -> CIImage -> CGImage -> UIImage -> JPEG

    毎フレーム JPEG にすると Python 側が追いつかない。ハンドラは 60fps で
    呼ばれうるので、変換は MIN_ENCODE_INTERVAL 秒に1回までに絞る。
    frame_count はハンドラが呼ばれた回数そのものなので、間引いても
    「コールバックが生きているか」の判定には影響しない。
"""

import threading
import time

AVAILABLE = False
IMPORT_ERROR = None

try:
    from ctypes import c_void_p, c_long, c_double, c_size_t, c_bool, c_ubyte
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


def _prepare():
    """フレームワークと C 関数の型を用意する。import 時に1回だけ。"""
    load_framework('ReplayKit')
    load_framework('CoreMedia')
    load_framework('CoreVideo')
    load_framework('CoreImage')

    c.CMSampleBufferGetImageBuffer.argtypes = [c_void_p]
    c.CMSampleBufferGetImageBuffer.restype = c_void_p
    c.CMSampleBufferIsValid.argtypes = [c_void_p]
    c.CMSampleBufferIsValid.restype = c_bool
    c.CVPixelBufferGetWidth.argtypes = [c_void_p]
    c.CVPixelBufferGetWidth.restype = c_size_t
    c.CVPixelBufferGetHeight.argtypes = [c_void_p]
    c.CVPixelBufferGetHeight.restype = c_size_t
    # CGFloat は arm64 で double
    c.UIImageJPEGRepresentation.argtypes = [c_void_p, c_double]
    c.UIImageJPEGRepresentation.restype = c_void_p
    c.CGImageRelease.argtypes = [c_void_p]
    c.CGImageRelease.restype = None


if AVAILABLE:
    try:
        _prepare()
        RPScreenRecorder = ObjCClass('RPScreenRecorder')
        CIImage = ObjCClass('CIImage')
        CIContext = ObjCClass('CIContext')
        UIImage = ObjCClass('UIImage')
    except Exception as e:    # noqa: BLE001 - 環境差でここが落ちても import は通す
        AVAILABLE = False
        IMPORT_ERROR = '%s: %s' % (type(e).__name__, e)


class ReplayKitCapture(object):
    """最新フレームを1枚だけ持ち続ける。保存はしない。"""

    def __init__(self, jpeg_quality=0.7, min_encode_interval=0.2, log=print):
        self.jpeg_quality = jpeg_quality
        self.min_encode_interval = min_encode_interval
        self.log = log

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
                'has_start_capture': False}
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

    # --------------------------------------------------------- 変換処理

    def _context(self):
        if self._ci_context is None:
            self._ci_context = CIContext.context()
        return self._ci_context

    def _to_jpeg(self, sbuf):
        """CMSampleBuffer から JPEG bytes を作る。失敗したら None。"""
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
        """ReplayKit のワーカースレッドから呼ばれる。絶対に例外を投げない。"""
        try:
            if int(buf_type) != RP_VIDEO:
                return
            now = time.time()
            with self._lock:
                self.capturing = True
                self.frame_count += 1
                self.last_frame_time = now
                due = (now - (self.last_jpeg_time or 0)) >= self.min_encode_interval
                if not due:
                    self.dropped_count += 1
                else:
                    # 二重に走らないよう、変換前に時刻だけ進めておく
                    self.last_jpeg_time = now
            if not due:
                return
            jpeg = self._to_jpeg(sbuf)
            if jpeg:
                with self._lock:
                    self._jpeg = jpeg
                    self.encoded_count += 1
                    self.last_jpeg_time = time.time()
        except Exception as e:      # noqa: BLE001 - ObjC 側へ例外を返さない
            self.note_error('frame handler: %s: %s' % (type(e).__name__, e))

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
        """キャプチャ開始。UIKit を触るのでメインスレッドで実行する。"""
        if not AVAILABLE:
            self.note_error('objc_util を import できません: %s' % IMPORT_ERROR)
            return False
        try:
            rec = RPScreenRecorder.sharedRecorder()
            if not rec:
                self.note_error('sharedRecorder が nil です')
                return False
            if not rec.isAvailable():
                self.note_error('RPScreenRecorder.isAvailable == False '
                                '（他アプリが録画中／機能制限の可能性）')
                return False
            if not rec.respondsToSelector_(
                    sel('startCaptureWithHandler:completionHandler:')):
                self.note_error('startCaptureWithHandler: がありません（iOS 11 未満）')
                return False
            if rec.isRecording():
                self.log('[capture] すでに録画中です')
                self.capturing = True
                return True

            rec.setMicrophoneEnabled_(False)
            self._handler_block = ObjCBlock(
                self._on_sample, restype=None,
                argtypes=[c_void_p, c_void_p, c_long, c_void_p])
            self._start_block = ObjCBlock(
                self._on_start_done, restype=None,
                argtypes=[c_void_p, c_void_p])

            self.start_requested = True
            self.started_at = time.time()
            rec.startCaptureWithHandler_completionHandler_(
                self._handler_block, self._start_block)
            self.log('[capture] startCaptureWithHandler: を呼びました')
            return True
        except Exception as e:      # noqa: BLE001
            self.note_error('start: %s: %s' % (type(e).__name__, e))
            return False

    @on_main_thread
    def stop(self):
        if not AVAILABLE:
            return False
        try:
            rec = RPScreenRecorder.sharedRecorder()
            self._stop_block = ObjCBlock(
                self._on_stop_done, restype=None,
                argtypes=[c_void_p, c_void_p])
            rec.stopCaptureWithHandler_(self._stop_block)
            self.start_requested = False
            self.log('[capture] stopCaptureWithHandler: を呼びました')
            return True
        except Exception as e:      # noqa: BLE001
            self.note_error('stop: %s: %s' % (type(e).__name__, e))
            return False
