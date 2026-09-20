import SwiftUI

@main
struct ArenaAssistantApp: App {
    @StateObject private var capture = ScreenCaptureManager()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(capture)
        }
    }
}
