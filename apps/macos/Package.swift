// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "NexusMac",
    platforms: [
        .macOS(.v13),
    ],
    products: [
        .executable(name: "NexusMac", targets: ["NexusMac"]),
    ],
    targets: [
        .executableTarget(
            name: "NexusMac",
            path: "Sources/NexusMac"
        ),
        .testTarget(
            name: "NexusMacTests",
            dependencies: ["NexusMac"],
            path: "Tests/NexusMacTests"
        ),
    ]
)
