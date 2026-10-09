import SwiftUI
import TranscriptionLauncherLib

/// Tracks whether the first-run onboarding flow has been completed, persisted
/// in UserDefaults so it only runs once. `restart()` re-triggers the flow.
@MainActor
final class OnboardingState: ObservableObject {
    @Published private(set) var isComplete: Bool

    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        self.isComplete = defaults.bool(forKey: DefaultsKeys.hasCompletedOnboarding)
    }

    func markComplete() {
        defaults.set(true, forKey: DefaultsKeys.hasCompletedOnboarding)
        isComplete = true
    }

    func restart() {
        defaults.set(false, forKey: DefaultsKeys.hasCompletedOnboarding)
        isComplete = false
    }
}

/// First-run setup: capture the login shell environment, locate the
/// transcription scripts, then show an advisory dependency checklist.
struct OnboardingView: View {
    @ObservedObject var repoRootStore: RepoRootStore
    @ObservedObject var model: LauncherModel
    let onComplete: () -> Void

    private enum Step {
        case capturingEnvironment
        case locatingRepo
        case reviewingDependencies
    }

    @State private var step: Step = .capturingEnvironment
    @State private var capturedEnvironment: [String: String] = [:]
    @State private var environmentWarning: String?
    @State private var isRefreshing = false
    @State private var dependencyItems: [DependencyChecker.Item] = []

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Welcome to Transcription Launcher")
                .font(.title2)

            stepContent
        }
        .padding(24)
        .frame(minWidth: 480, minHeight: 400, alignment: .topLeading)
        .task {
            await captureEnvironment()
        }
        .onChange(of: repoRootStore.repoRootURL) { _, repoRootURL in
            if step == .locatingRepo, repoRootURL != nil {
                advanceToDependencies()
            } else if step == .reviewingDependencies {
                updateDependencies()
            }
        }
        .onChange(of: model.selectedPreset) { _, _ in updateDependencies() }
        .onChange(of: model.whisperModelPath) { _, _ in updateDependencies() }
    }

    @ViewBuilder
    private var stepContent: some View {
        switch step {
        case .capturingEnvironment:
            ProgressView("Loading your environment...")

        case .locatingRepo:
            repoStep

        case .reviewingDependencies:
            dependenciesStep
        }
    }

    @ViewBuilder
    private var repoStep: some View {
        if repoRootStore.isDetectingRepoRoot {
            ProgressView("Looking for your transcription scripts...")
        } else {
            Text("Choose the folder containing your utils transcription scripts.")
            Text("If you don't have it, run in Terminal: git clone https://github.com/pavelkrotkov/utils.git")
                .font(.caption.monospaced())
                .textSelection(.enabled)

            if let validationMessage = repoRootStore.repoRootValidationMessage {
                Text(validationMessage)
                    .foregroundStyle(.red)
            }

            HStack(spacing: 8) {
                Button("Choose Folder...") {
                    repoRootStore.chooseRepoRoot()
                }
                .disabled(repoRootStore.isChoosingRepoRoot)

                Button("Skip for Now") {
                    advanceToDependencies()
                }
            }

            Text("You can change this later in Settings.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private var dependenciesStep: some View {
        VStack(alignment: .leading, spacing: 10) {
            Picker("Preset to prepare", selection: Binding(
                get: { model.selectedPreset },
                set: { model.selectPreset($0) }
            )) {
                Section("Local — on device") {
                    ForEach(TranscriptionPreset.localPresets, id: \.self) { preset in
                        Text(preset.displayName).tag(preset)
                    }
                }
                Section("OpenAI cloud — upload and charges") {
                    ForEach(TranscriptionPreset.cloudPresets, id: \.self) { preset in
                        Text(preset.displayName).tag(preset)
                    }
                }
            }
            Text(model.selectedPreset.privacyDescription)
                .font(.caption)
                .foregroundStyle(.secondary)
            if model.selectedPreset.usesWhisperModel {
                TextField("Custom Whisper model absolute path (optional)", text: $model.whisperModelPath)
                    .textFieldStyle(.roundedBorder)
            }
            Text("Setup is manual: run only the commands you approve in Terminal, where installation and model download progress is visible. Python packages are managed by uv, not pip.")
                .font(.caption)
                .foregroundStyle(.secondary)
            ScrollView {
                ReadinessChecklist(items: dependencyItems)
            }
            .frame(maxHeight: 260)
            if let environmentWarning {
                Label(environmentWarning, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
                    .font(.caption)
            }
            HStack {
                Button("Recheck") { Task { await refreshEnvironment() } }
                    .disabled(isRefreshing)
                if isRefreshing { ProgressView().controlSize(.small) }
                Spacer()
                Button(dependencyItems.allSatisfy(\.isAvailable) ? "Continue" : "Finish Setup Later") {
                    onComplete()
                }
                .keyboardShortcut(.defaultAction)
            }
        }
    }

    private func captureEnvironment() async {
        do {
            capturedEnvironment = try await EnvironmentSnapshot.capture()
        } catch {
            capturedEnvironment = ProcessInfo.processInfo.environment
            environmentWarning =
                "Couldn't load your login shell environment; checked the app's own environment instead."
        }

        if repoRootStore.repoRootURL != nil {
            advanceToDependencies()
        } else {
            step = .locatingRepo
            repoRootStore.detectRepoRootIfNeeded()
        }
    }

    private func refreshEnvironment() async {
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            capturedEnvironment = try await EnvironmentSnapshot.refresh()
            environmentWarning = nil
        } catch {
            environmentWarning = "Could not refresh login shell environment. Check ~/.zprofile."
        }
        updateDependencies()
    }

    private func advanceToDependencies() {
        step = .reviewingDependencies
        updateDependencies()
    }

    private func updateDependencies() {
        guard step == .reviewingDependencies else { return }
        dependencyItems = DependencyChecker.check(
            preset: model.selectedPreset,
            environment: capturedEnvironment,
            repoRoot: repoRootStore.repoRootURL,
            whisperModelPath: model.whisperModelPath
        )
    }
}
