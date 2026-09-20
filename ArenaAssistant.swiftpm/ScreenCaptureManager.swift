import AVFoundation
import Combine
import CoreMedia
import CoreVideo
import Foundation
import ScreenCaptureKit

/// ScreenCaptureKit で画面フレームを受け取り、数を数えるだけの Phase 1 実装。
///
/// iPadOS では取り込む対象をアプリ側で列挙できず、システムの共有ピッカー
/// (`SCContentSharingPicker`) が返す `SCContentFilter` を使う。
/// ReplayKit の Broadcast Extension は不要。
///
/// 必要環境: iPadOS 27.0 以上。
final class ScreenCaptureManager: NSObject, ObservableObject {

    // MARK: - 画面に出す状態

    @Published private(set) var statusText: String = "Idle"
    @Published private(set) var frameCount: Int = 0
    @Published private(set) var fps: Double = 0
    @Published private(set) var frameWidth: Int = 0
    @Published private(set) var frameHeight: Int = 0
    @Published private(set) var isRunning: Bool = false

    // MARK: - 内部状態

    private var stream: SCStream?
    private var isObserving = false

    /// フレームの計数はこのキューの上だけで行う。
    private let sampleQueue = DispatchQueue(label: "ArenaAssistant.SampleHandler")
    private var countedFrames = 0
    private var windowFrames = 0
    private var windowStart = CFAbsoluteTimeGetCurrent()

    // MARK: - 操作

    func startCapture() {
        guard stream == nil else { return }

        sampleQueue.async { [weak self] in
            guard let self else { return }
            self.countedFrames = 0
            self.windowFrames = 0
            self.windowStart = CFAbsoluteTimeGetCurrent()
        }
        publish {
            self.frameCount = 0
            self.fps = 0
            self.frameWidth = 0
            self.frameHeight = 0
            self.statusText = "Selecting content…"
        }

        let picker = SCContentSharingPicker.shared
        if !isObserving {
            picker.add(self)
            isObserving = true
        }
        picker.defaultConfiguration = SCContentSharingPickerConfiguration()
        picker.isActive = true
        picker.present()
    }

    func stopCapture() {
        SCContentSharingPicker.shared.isActive = false

        guard let current = stream else {
            publish {
                self.isRunning = false
                self.statusText = "Stopped"
                self.fps = 0
            }
            return
        }
        stream = nil

        Task { [weak self] in
            // 停止に失敗してもアプリ側の状態は落とす。
            try? await current.stopCapture()
            self?.publish {
                self?.isRunning = false
                self?.statusText = "Stopped"
                self?.fps = 0
            }
        }
    }

    // MARK: - ストリーム開始

    private func beginStream(with filter: SCContentFilter) {
        let configuration = SCStreamConfiguration()
        // Phase 1 は取得の確認だけなので低めに抑える。
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 10)
        configuration.queueDepth = 5
        configuration.pixelFormat = kCVPixelFormatType_32BGRA

        do {
            let newStream = SCStream(filter: filter,
                                     configuration: configuration,
                                     delegate: self)
            try newStream.addStreamOutput(self,
                                          type: .screen,
                                          sampleHandlerQueue: sampleQueue)
            stream = newStream

            sampleQueue.async { [weak self] in
                self?.windowFrames = 0
                self?.windowStart = CFAbsoluteTimeGetCurrent()
            }

            Task { [weak self] in
                do {
                    try await newStream.startCapture()
                    self?.publish {
                        self?.isRunning = true
                        self?.statusText = "Capturing"
                    }
                } catch {
                    self?.stream = nil
                    self?.publish {
                        self?.isRunning = false
                        self?.statusText = "Failed to start: \(error.localizedDescription)"
                    }
                }
            }
        } catch {
            stream = nil
            publish {
                self.isRunning = false
                self.statusText = "Failed to configure: \(error.localizedDescription)"
            }
        }
    }

    // MARK: - 計数

    /// サンプルキューの上でだけ呼ばれる。
    private func countFrame(width: Int, height: Int) {
        countedFrames += 1
        windowFrames += 1

        let now = CFAbsoluteTimeGetCurrent()
        let elapsed = now - windowStart
        var newFPS: Double?
        if elapsed >= 1 {
            newFPS = Double(windowFrames) / elapsed
            windowFrames = 0
            windowStart = now
        }

        let total = countedFrames
        publish {
            self.frameCount = total
            self.frameWidth = width
            self.frameHeight = height
            if let newFPS { self.fps = newFPS }
        }
    }

    private func publish(_ work: @escaping () -> Void) {
        if Thread.isMainThread {
            work()
        } else {
            DispatchQueue.main.async(execute: work)
        }
    }
}

// MARK: - フレーム受信

extension ScreenCaptureManager: SCStreamOutput {
    func stream(_ stream: SCStream,
                didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .screen, CMSampleBufferIsValid(sampleBuffer) else { return }
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        countFrame(width: CVPixelBufferGetWidth(pixelBuffer),
                   height: CVPixelBufferGetHeight(pixelBuffer))
    }
}

// MARK: - ストリームの異常終了

extension ScreenCaptureManager: SCStreamDelegate {
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        let message = error.localizedDescription
        self.stream = nil
        publish {
            self.isRunning = false
            self.statusText = "Stream stopped: \(message)"
            self.fps = 0
        }
    }
}

// MARK: - システム共有ピッカー

extension ScreenCaptureManager: SCContentSharingPickerObserver {
    func contentSharingPicker(_ picker: SCContentSharingPicker,
                              didUpdateWith filter: SCContentFilter,
                              for stream: SCStream?) {
        beginStream(with: filter)
    }

    func contentSharingPicker(_ picker: SCContentSharingPicker,
                              didCancelFor stream: SCStream?) {
        publish {
            self.isRunning = false
            self.statusText = "Cancelled"
        }
    }

    func contentSharingPickerStartDidFailWithError(_ error: Error) {
        let message = error.localizedDescription
        publish {
            self.isRunning = false
            self.statusText = "Picker failed: \(message)"
        }
    }
}
