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
    #expect(preferences.requiresConsent(for: .cloud))
}

@Test
func savedPresetPersistsWithoutImplicitCloudConsent() throws {
    let suite = "PresetPreferencesTests-\(UUID().uuidString)"
    let defaults = try #require(UserDefaults(suiteName: suite))
    defer { defaults.removePersistentDomain(forName: suite) }

    defaults.set("cloud", forKey: "selectedPreset")
    let preferences = PresetPreferences(defaults: defaults)
    #expect(preferences.selectedPreset == .cloud)
    #expect(preferences.requiresConsent(for: .cloud))

    preferences.cloudUploadApproved = true
    #expect(!PresetPreferences(defaults: defaults).requiresConsent(for: .cloud))
    preferences.selectedPreset = .privateLocalWithSpeakers
    #expect(PresetPreferences(defaults: defaults).selectedPreset == .privateLocalWithSpeakers)

    defaults.set("invalid", forKey: "selectedPreset")
    #expect(PresetPreferences(defaults: defaults).selectedPreset == .privateLocal)
}

@Test(arguments: ["fastCloud", "bestCloud", "compatibleCloud"])
func oldCloudSelectionsMigrateWithoutLosingConsent(_ oldValue: String) throws {
    let suite = "PresetPreferencesTests-\(UUID().uuidString)"
    let defaults = try #require(UserDefaults(suiteName: suite))
    defer { defaults.removePersistentDomain(forName: suite) }

    defaults.set(oldValue, forKey: "selectedPreset")
    let preferences = PresetPreferences(defaults: defaults)
    #expect(preferences.selectedPreset == .cloud)
    #expect(preferences.requiresConsent(for: .cloud))
    preferences.cloudUploadApproved = true
    #expect(!PresetPreferences(defaults: defaults).requiresConsent(for: .cloud))
}
