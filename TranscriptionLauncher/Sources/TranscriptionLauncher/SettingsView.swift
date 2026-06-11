import AppKit
import SwiftUI
import TranscriptionLauncherLib

struct SettingsView: View {
    @ObservedObject var repoRootStore: RepoRootStore
    @ObservedObject var model: LauncherModel
    @State private var isRefreshingEnvironment = false
    @State private var environmentStatusMessage: String?

    var body: some View {
        Form {
            repoRootSection
            environmentSection
            presetOptionsSection
        }
        .padding()
        .frame(minWidth: 560)
    }

    @ViewBuilder
    private var repoRootSection: some View {
        LabeledContent("Repository Root") {
            HStack(spacing: 8) {
                Text(repoRootStore.repoRootDisplayPath)
                    .foregroundStyle(repoRootStore.repoRootURL == nil ? .secondary : .primary)
                    .lineLimit(1)
                    .truncationMode(.middle)

                Button("Change...") {
                    repoRootStore.chooseRepoRoot()
                }
                Button("Auto-detect") {
                    repoRootStore.autoDetectRepoRoot()
                }
            }
            .disabled(repoRootStore.isDetectingRepoRoot || repoRootStore.isChoosingRepoRoot)
        }

        if let validationMessage = repoRootStore.repoRootValidationMessage {
            Text(validationMessage)
                .foregroundStyle(.red)
        }
    }

    @ViewBuilder
    private var environmentSection: some View {
        LabeledContent("Shell Environment") {
            HStack(spacing: 8) {
                Button("Refresh Environment") {
                    refreshEnvironment()
                }
                .disabled(isRefreshingEnvironment)

                if isRefreshingEnvironment {
                    ProgressView()
                        .controlSize(.small)
                }
            }
        }

        if let environmentStatusMessage {
            Text(environmentStatusMessage)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var presetOptionsSection: some View {
        if model.selectedPreset.usesWhisperModel {
            LabeledContent("Whisper Model") {
                HStack(spacing: 8) {
                    TextField("Default model", text: $model.whisperModelPath)
                        .textFieldStyle(.roundedBorder)

                    Button("Choose...") {
                        chooseWhisperModel()
                    }
                }
            }
        }

        if model.selectedPreset.usesVibeVoiceContext {
            LabeledContent("VibeVoice Context") {
                TextField("Hotwords or domain context", text: $model.vibevoiceContext)
                    .textFieldStyle(.roundedBorder)
            }
        }
    }

    private func refreshEnvironment() {
        isRefreshingEnvironment = true
        environmentStatusMessage = nil
        Task {
            do {
                _ = try await EnvironmentSnapshot.refresh()
                environmentStatusMessage = "Environment refreshed."
            } catch {
                environmentStatusMessage = "Refresh failed: \(String(describing: error))"
            }
            isRefreshingEnvironment = false
        }
    }

    private func chooseWhisperModel() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.title = "Choose Whisper Model"
        panel.prompt = "Choose"

        let model = self.model
        panel.begin { response in
            Task { @MainActor in
                guard response == .OK, let url = panel.url else {
                    return
                }
                model.whisperModelPath = url.path(percentEncoded: false)
            }
        }
    }
}
