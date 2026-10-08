import Foundation
import Testing
import TranscriptionLauncherLib

@Test
func freshInstallDefaultsToLocalWithoutCloudConsent() throws {
    let suite = "PresetPreferencesTests-\(UUID().uuidString)"
    let defaults = try #require(UserDefaults(suiteName: suite))
    defer { defaults.removePersistentDomain(forName: suite) }

    let preferences = PresetPreferences(defaults: defaults)
    #expect(preferences.selectedPreset == .privateLocal)
    #expect(!preferences.requiresConsent(for: .privateLocal))
    #expect(preferences.requiresConsent(for: .fastCloud))
}

@Test
func savedPresetPersistsWithoutImplicitCloudConsent() throws {
    let suite = "PresetPreferencesTests-\(UUID().uuidString)"
    let defaults = try #require(UserDefaults(suiteName: suite))
    defer { defaults.removePersistentDomain(forName: suite) }

    defaults.set("bestCloud", forKey: "selectedPreset")
    let preferences = PresetPreferences(defaults: defaults)
    #expect(preferences.selectedPreset == .bestCloud)
    #expect(preferences.requiresConsent(for: .bestCloud))

    preferences.cloudUploadApproved = true
    #expect(!PresetPreferences(defaults: defaults).requiresConsent(for: .compatibleCloud))
    preferences.selectedPreset = .privateLocalWithSpeakers
    #expect(PresetPreferences(defaults: defaults).selectedPreset == .privateLocalWithSpeakers)

    defaults.set("invalid", forKey: "selectedPreset")
    #expect(PresetPreferences(defaults: defaults).selectedPreset == .privateLocal)
}
