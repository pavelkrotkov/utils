import Foundation
import TranscriptionLauncherLib

/// User-facing title and remediation message for an error surfaced by a
/// transcription run.
struct ErrorPresentation: Equatable {
    let title: String
    let message: String
}

extension ErrorPresentation {
    init(error: Error) {
        if let transcriptionError = error as? TranscriptionError {
            self.init(transcriptionError: transcriptionError)
        } else if let runnerError = error as? ProcessRunnerError {
            self.init(runnerError: runnerError)
        } else if let snapshotError = error as? EnvironmentSnapshotError {
            self.init(
                title: "Environment Capture Failed",
                message: "Could not capture your login shell environment: "
                    + "\(String(describing: snapshotError)). "
                    + "Try Settings → Refresh Environment."
            )
        } else {
            self.init(title: "Transcription Failed", message: error.localizedDescription)
        }
    }

    private init(transcriptionError: TranscriptionError) {
        switch transcriptionError {
        case .missingAPIKey(let name):
            let remediation = name == "HF_TOKEN"
                ? "Create a Hugging Face access token with access to the "
                    + "pyannote/speaker-diarization models, export it in your shell "
                    + "profile, then use Settings → Refresh Environment."
                : "Export it in your shell profile (for example in ~/.zshrc), "
                    + "then use Settings → Refresh Environment."
            self.init(title: "Missing API Key", message: "\(name) is not set. \(remediation)")
        case .missingDependency(let name):
            self.init(
                title: "Missing Dependency",
                message: "\(name) was not found. Install it "
                    + "(for example with `brew install \(name)`) and try again."
            )
        case .missingModel(let path):
            self.init(
                title: "Whisper Model Not Found",
                message: "No Whisper model at \(path). Download a model, or point "
                    + "Settings → Whisper Model at an existing one."
            )
        case .unsupportedHardware(let detail):
            self.init(
                title: "Unsupported Hardware",
                message: "\(detail). Choose a different preset."
            )
        case .apiError(let detail):
            self.init(title: "Transcription Service Error", message: detail)
        case .unknown(let detail):
            self.init(
                title: "Transcription Failed",
                message: detail.isEmpty
                    ? "The transcription failed for an unknown reason. Check the log for details."
                    : detail
            )
        }
    }

    private init(runnerError: ProcessRunnerError) {
        switch runnerError {
        case .alreadyRunning:
            self.init(
                title: "Transcription Already Running",
                message: "Wait for the current transcription to finish or cancel it first."
            )
        case .outputFileMissing(let url):
            self.init(
                title: "Output File Missing",
                message: "The transcription finished, but no output file was produced at "
                    + "\(url.path(percentEncoded: false)). Check the log for details."
            )
        }
    }
}
