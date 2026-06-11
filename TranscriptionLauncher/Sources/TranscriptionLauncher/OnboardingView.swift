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
    let onComplete: () -> Void

    private enum Step {
        case capturingEnvironment
        case locatingRepo
        case reviewingDependencies
    }

    @State private var step: Step = .capturingEnvironment
    @State private var capturedEnvironment: [String: String] = [:]
    @State private var environmentWarning: String?
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
            }
        }
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
            Text("Select the folder containing your transcription scripts.")

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

    @ViewBuilder
    private var dependenciesStep: some View {
        Text("Here's what's available for the transcription presets:")

        VStack(alignment: .leading, spacing: 8) {
            ForEach(dependencyItems, id: \.name) { item in
                dependencyRow(item)
            }
        }

        if let environmentWarning {
            Label(environmentWarning, systemImage: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
                .font(.caption)
        }

        Text("Missing items only disable the matching presets — you can set them up later.")
            .font(.caption)
            .foregroundStyle(.secondary)

        Button("Continue") {
            onComplete()
        }
        .keyboardShortcut(.defaultAction)
    }

    private func dependencyRow(_ item: DependencyChecker.Item) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: item.isAvailable ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                .foregroundStyle(item.isAvailable ? .green : .orange)

            VStack(alignment: .leading, spacing: 2) {
                Text(item.name)
                    .font(.body.monospaced())

                Text(detailText(for: item))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func detailText(for item: DependencyChecker.Item) -> String {
        let purpose: String
        switch item.requirement {
        case .localPresets:
            purpose = "Needed for local presets"
        case .cloudPresets:
            purpose = "Needed for cloud presets"
        case .speakerDiarization:
            purpose = "Needed for the speaker-labeled local preset"
        }

        if let resolvedPath = item.resolvedPath {
            return "\(purpose) — found at \(resolvedPath)"
        }

        return item.isAvailable ? purpose : "\(purpose) — not found"
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

    private func advanceToDependencies() {
        dependencyItems = DependencyChecker.check(environment: capturedEnvironment)
        step = .reviewingDependencies
    }
}
