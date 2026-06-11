import SwiftUI
import TranscriptionLauncherLib

struct MainView: View {
    @ObservedObject var repoRootStore: RepoRootStore
    @ObservedObject var model: LauncherModel
    @ObservedObject var runner: ProcessRunner
    @State private var isDropTargeted = false

    private let metadata = AppMetadata()

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            dropTarget
            presetPicker
            controls
            progressSection
            logSection
            repoRootSummary
        }
        .padding()
        .frame(minWidth: 520, minHeight: 460)
        .navigationTitle(metadata.displayName)
        .onAppear {
            repoRootStore.detectRepoRootIfNeeded(promptOnFailure: true)
        }
        .alert(
            model.errorAlert?.title ?? "Error",
            isPresented: errorAlertPresented,
            presenting: model.errorAlert
        ) { _ in
            Button("OK", role: .cancel) {}
        } message: { alert in
            Text(alert.message)
        }
        .confirmationDialog(
            "Overwrite Existing Transcript?",
            isPresented: overwriteConfirmationPresented,
            presenting: model.pendingOverwriteRun
        ) { run in
            Button("Replace", role: .destructive) {
                model.start(run)
            }
            Button("Cancel", role: .cancel) {}
        } message: { run in
            Text("\"\(run.output.lastPathComponent)\" already exists. Running will replace it.")
        }
    }

    private var dropTarget: some View {
        DropTargetView(fileURL: model.inputFileURL, isTargeted: isDropTargeted)
            .dropDestination(for: URL.self) { urls, _ in
                model.acceptDroppedFiles(urls)
            } isTargeted: { targeted in
                isDropTargeted = targeted
            }
    }

    private var presetPicker: some View {
        Picker("Preset", selection: $model.selectedPreset) {
            Section("Cloud") {
                ForEach(TranscriptionPreset.cloudPresets, id: \.self) { preset in
                    Text(preset.displayName).tag(preset)
                }
            }
            Section("Local") {
                ForEach(TranscriptionPreset.localPresets, id: \.self) { preset in
                    Text(preset.displayName).tag(preset)
                }
            }
        }
        .disabled(runner.isRunning || model.isPreparing)
    }

    private var controls: some View {
        HStack {
            if runner.isRunning || model.isPreparing {
                Button("Cancel", role: .destructive) {
                    model.cancel()
                }
                .disabled(model.isPreparing)
            } else {
                Button("Run") {
                    model.requestRun(repoRoot: repoRootStore.repoRootURL)
                }
                .keyboardShortcut(.defaultAction)
                .disabled(model.inputFileURL == nil)
            }

            Spacer()

            if model.lastOutputURL != nil {
                Button("Reveal in Finder") {
                    model.revealLastOutputInFinder()
                }
            }
        }
    }

    @ViewBuilder
    private var progressSection: some View {
        if model.isPreparing {
            HStack(spacing: 8) {
                ProgressView()
                    .controlSize(.small)
                Text("Preparing environment...")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        } else if runner.isRunning {
            let progress = runner.progress
            if let progress, let percent = progress.percent {
                ProgressView(value: percent, total: 100) {
                    Text(progressLabel(for: progress))
                        .font(.caption)
                }
            } else {
                HStack(spacing: 8) {
                    ProgressView()
                        .controlSize(.small)
                    Text(progress.map(progressLabel(for:)) ?? "Starting...")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private var logSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Log")
                .font(.caption)
                .foregroundStyle(.secondary)
            LogView(lines: runner.logLines)
        }
    }

    @ViewBuilder
    private var repoRootSummary: some View {
        Group {
            if let repoRootURL = repoRootStore.repoRootURL {
                Text("Repository: \(repoRootURL.path(percentEncoded: false))")
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            } else if let validationMessage = repoRootStore.repoRootValidationMessage {
                Text(validationMessage)
                    .foregroundStyle(.red)
            } else if repoRootStore.isDetectingRepoRoot {
                Text("Detecting repository root...")
                    .foregroundStyle(.secondary)
            } else {
                Text("Repository root is not configured. Set it in Settings.")
                    .foregroundStyle(.secondary)
            }
        }
        .font(.caption)
    }

    private func progressLabel(for progress: ProgressEvent) -> String {
        if let detail = progress.detail {
            return "\(progress.stage) — \(detail)"
        }
        return progress.stage
    }

    private var errorAlertPresented: Binding<Bool> {
        Binding(
            get: { model.errorAlert != nil },
            set: { isPresented in
                if !isPresented {
                    model.errorAlert = nil
                }
            }
        )
    }

    private var overwriteConfirmationPresented: Binding<Bool> {
        Binding(
            get: { model.pendingOverwriteRun != nil },
            set: { isPresented in
                if !isPresented {
                    model.pendingOverwriteRun = nil
                }
            }
        )
    }
}

private struct DropTargetView: View {
    let fileURL: URL?
    let isTargeted: Bool

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 8)
                .strokeBorder(
                    isTargeted ? Color.accentColor : Color.secondary,
                    style: StrokeStyle(lineWidth: 1.5, dash: [6])
                )

            VStack(spacing: 6) {
                if let fileURL {
                    Image(systemName: "waveform")
                        .font(.title2)
                    Text(fileURL.lastPathComponent)
                        .lineLimit(1)
                        .truncationMode(.middle)
                } else {
                    Image(systemName: "arrow.down.doc")
                        .font(.title2)
                        .foregroundStyle(.secondary)
                    Text("Drop an audio or video file here")
                        .foregroundStyle(.secondary)
                }
            }
            .padding(8)
        }
        .frame(maxWidth: .infinity, minHeight: 90)
    }
}

private struct LogView: View {
    let lines: [String]

    private static let bottomAnchorID = "logBottom"

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(lines.indices, id: \.self) { index in
                        Text(lines[index])
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    Color.clear
                        .frame(height: 1)
                        .id(Self.bottomAnchorID)
                }
                .padding(6)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 6))
            .onChange(of: lines.count) {
                proxy.scrollTo(Self.bottomAnchorID, anchor: .bottom)
            }
        }
    }
}
