import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var capture: ScreenCaptureManager

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("Arena Assistant Prototype")
                .font(.largeTitle.weight(.bold))

            statusCard
            controls
            metrics

            Text("Phase 1: ScreenCaptureKit からフレームを受け取れるかだけを確認します。"
                 + "カード認識はまだ実装していません。")
                .font(.footnote)
                .foregroundStyle(.secondary)

            Spacer()
        }
        .padding(24)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var statusCard: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Capture status")
                .font(.headline)
            HStack(spacing: 8) {
                Circle()
                    .fill(capture.isRunning ? Color.green : Color.secondary)
                    .frame(width: 10, height: 10)
                Text(capture.statusText)
                    .font(.body.monospaced())
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
    }

    private var controls: some View {
        HStack(spacing: 12) {
            Button("Start Capture") { capture.startCapture() }
                .buttonStyle(.borderedProminent)
                .disabled(capture.isRunning)

            Button("Stop Capture") { capture.stopCapture() }
                .buttonStyle(.bordered)
                .disabled(!capture.isRunning)
        }
    }

    private var metrics: some View {
        VStack(spacing: 0) {
            row("Frame count", "\(capture.frameCount)")
            Divider()
            row("FPS", String(format: "%.1f", capture.fps))
            Divider()
            row("Frame size", capture.frameWidth > 0
                ? "\(capture.frameWidth) x \(capture.frameHeight)"
                : "-")
        }
        .padding(.horizontal, 16)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
    }

    private func row(_ title: String, _ value: String) -> some View {
        HStack {
            Text(title)
            Spacer()
            Text(value)
                .font(.body.monospacedDigit())
                .foregroundStyle(.primary)
        }
        .padding(.vertical, 12)
    }
}

#Preview {
    ContentView()
        .environmentObject(ScreenCaptureManager())
}
