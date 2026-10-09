import Foundation

public struct PresetPreferences {
    private let defaults: UserDefaults

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    public var selectedPreset: TranscriptionPreset {
        get {
            defaults.string(forKey: "selectedPreset")
                .flatMap(TranscriptionPreset.init(rawValue:)) ?? .privateLocal
        }
        nonmutating set { defaults.set(newValue.rawValue, forKey: "selectedPreset") }
    }

    public var cloudUploadApproved: Bool {
        get { defaults.bool(forKey: "cloudUploadApproved") }
        nonmutating set { defaults.set(newValue, forKey: "cloudUploadApproved") }
    }

    public func requiresConsent(for preset: TranscriptionPreset) -> Bool {
        preset.isCloud && !cloudUploadApproved
    }
}
