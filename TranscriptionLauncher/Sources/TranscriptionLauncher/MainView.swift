import SwiftUI
import TranscriptionLauncherLib

struct MainView: View {
    @ObservedObject var repoRootStore: RepoRootStore
    @ObservedObject var model: LauncherModel
    @ObservedObject var runner: ProcessRunner
    @State private var isDropTargeted = false
    @State private var isCheckingReadiness = true
    @State private var readinessItems: [DependencyChecker.Item] = []
    @State private var readinessError: String?

    private let metadata = AppMetadata()

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            dropTarget
            presetPicker
            readinessSection
            controls
            progressSection
            logSection
            repoRootSummary
        }
        .padding()
        .frame(minWidth: 520, minHeight: 460)
        .navigationTitle(metadata.displayName)
        .onAppear {
            repoRootStore.detectRepoRootIfNeeded()
        }
        .task(id: readinessKey) {
            await refreshReadiness()
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
                model.acceptInputFiles(urls)
            } isTargeted: { targeted in
                isDropTargeted = targeted
            }
    }

    private var presetPicker: some View {
        VStack(alignment: .leading, spacing: 4) {
            Picker("Preset", selection: Binding(
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
        }
        .disabled(runner.isRunning || model.isPreparing)
    }

    private var readinessKey: String {
        "\(model.selectedPreset.rawValue)|\(model.whisperModelPath)|" +
        "\(repoRootStore.repoRootURL?.path ?? "")|\(model.inputFileURL?.path ?? "")"
    }

    private var readinessSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Readiness for \(model.selectedPreset.displayName)")
                    .font(.headline)
                Spacer()
                Button("Recheck") {
                    Task { await refreshReadiness(force: true) }
                }
                .disabled(isCheckingReadiness)
            }
            if isCheckingReadiness {
                ProgressView("Checking requirements...")
            } else if let readinessError {
                Text(readinessError).foregroundStyle(.red).font(.caption)
            } else {
                ScrollView {
                    ReadinessChecklist(items: readinessItems)
                }
                .frame(maxHeight: 165)
            }
        }
    }

    private func refreshReadiness(force: Bool = false) async {
        isCheckingReadiness = true
        readinessError = nil
        do {
            let environment = try await (force ? EnvironmentSnapshot.refresh() : EnvironmentSnapshot.capture())
            guard !Task.isCancelled else { return }
            readinessItems = DependencyChecker.check(
                preset: model.selectedPreset,
                environment: environment,
                repoRoot: repoRootStore.repoRootURL,
                whisperModelPath: model.whisperModelPath,
                inputFile: model.inputFileURL
            )
        } catch {
            if !Task.isCancelled {
                readinessItems = []
                readinessError = ErrorPresentation(error: error).message
            }
        }
        isCheckingReadiness = false
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
                .disabled(model.inputFileURL == nil || isCheckingReadiness || readinessError != nil || readinessItems.contains { !$0.isAvailable })
            }

            Spacer()

            if model.lastOutputURL != nil {
                Button("Reveal in Finder") {
                    model.revealLastOutputInFinder()
                }
            }
        }
        .confirmationDialog(
            "Allow local Python setup?",
            isPresented: downloadConfirmationPresented,
            presenting: model.pendingDownloadRun
        ) { run in
            Button("Allow Downloads & Run") {
                model.approveLocalRun(run)
            }
            Button("Cancel", role: .cancel) {}
        } message: { _ in
            Text("uv may download Python packages on first use (some are large). " +
                 "Downloads and subsequent transcription progress appear in the log. " +
                 "Installed packages are cached for later runs. No cloud API key is needed.")
        }
    }

    @ViewBuilder
    private var progressSection: some View {
        if model.isPreparing {
            HStack(spacing: 8) {
                ProgressView()
                    .controlSize(.small)
                Text("Checking environment and model...")
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

    private var downloadConfirmationPresented: Binding<Bool> {
        Binding(
            get: { model.pendingDownloadRun != nil },
            set: { isPresented in
                if !isPresented {
                    model.pendingDownloadRun = nil
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
