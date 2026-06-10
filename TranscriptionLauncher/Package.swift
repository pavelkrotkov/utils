// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "TranscriptionLauncher",
    platforms: [
        .macOS(.v14),
    ],
    products: [
        .library(
            name: "TranscriptionLauncherLib",
            targets: ["TranscriptionLauncherLib"]
        ),
        .executable(
            name: "TranscriptionLauncher",
            targets: ["TranscriptionLauncher"]
        ),
    ],
    targets: [
        .target(
            name: "TranscriptionLauncherLib"
        ),
        .executableTarget(
            name: "TranscriptionLauncher",
            dependencies: ["TranscriptionLauncherLib"]
        ),
        .testTarget(
            name: "TranscriptionLauncherTests",
            dependencies: ["TranscriptionLauncherLib"]
        ),
    ]
)
