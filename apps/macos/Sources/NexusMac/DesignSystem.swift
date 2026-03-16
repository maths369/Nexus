import SwiftUI

enum NexusPalette {
    static let ink = Color(red: 0.05, green: 0.06, blue: 0.08)
    static let fog = Color(red: 0.88, green: 0.91, blue: 0.95)
    static let steel = Color(red: 0.50, green: 0.55, blue: 0.65)
    static let ocean = Color(red: 0.0, green: 0.47, blue: 0.95)
    static let cyan = Color(red: 0.0, green: 0.9, blue: 0.8)
    static let mint = Color(red: 0.0, green: 0.95, blue: 0.5)
    static let amber = Color(red: 1.0, green: 0.7, blue: 0.0)
    static let rose = Color(red: 1.0, green: 0.2, blue: 0.4)
    static let purple = Color(red: 0.6, green: 0.2, blue: 1.0)
    static let panelTop = Color(red: 0.06, green: 0.07, blue: 0.09)
    static let panelBottom = Color(red: 0.02, green: 0.03, blue: 0.04)
    
    static let textPrimary = Color.white
    static let textSecondary = Color(white: 0.75)
    static let textTertiary = Color(white: 0.55)
}

struct NexusPanelBackground: View {
    var body: some View {
        ZStack {
            LinearGradient(
                colors: [NexusPalette.panelTop, NexusPalette.panelBottom],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
            Circle()
                .fill(NexusPalette.ocean.opacity(0.15))
                .frame(width: 300, height: 300)
                .offset(x: 100, y: -150)
                .blur(radius: 40)
            Circle()
                .fill(NexusPalette.cyan.opacity(0.12))
                .frame(width: 250, height: 250)
                .offset(x: -120, y: 180)
                .blur(radius: 50)
        }
        .ignoresSafeArea()
        .environment(\.colorScheme, .dark)
    }
}

struct NexusCard<Content: View>: View {
    let title: String
    let eyebrow: String?
    let content: Content

    init(_ title: String, eyebrow: String? = nil, @ViewBuilder content: () -> Content) {
        self.title = title
        self.eyebrow = eyebrow
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                if let eyebrow, !eyebrow.isEmpty {
                    Text(eyebrow.uppercased())
                        .font(.system(size: 10, weight: .black, design: .monospaced))
                        .foregroundStyle(NexusPalette.cyan.opacity(0.85))
                }
                Text(title)
                    .font(.system(size: 18, weight: .bold, design: .rounded))
                    .foregroundStyle(NexusPalette.textPrimary)
            }
            content
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .fill(.ultraThinMaterial)
                .environment(\.colorScheme, .dark)
                .overlay(
                    RoundedRectangle(cornerRadius: 20, style: .continuous)
                        .stroke(LinearGradient(
                            colors: [.white.opacity(0.25), .clear, .white.opacity(0.05)],
                            startPoint: .topLeading,
                            endPoint: .bottomTrailing
                        ), lineWidth: 0.5)
                )
                .shadow(color: .black.opacity(0.5), radius: 15, x: 0, y: 8)
        )
    }
}

struct NexusMetricTile: View {
    let label: String
    let value: String
    let tone: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(label.uppercased())
                .font(.system(size: 10, weight: .black, design: .monospaced))
                .foregroundStyle(tone.opacity(0.9))
            Text(value)
                .font(.system(size: 28, weight: .heavy, design: .rounded))
                .foregroundStyle(NexusPalette.textPrimary)
                .shadow(color: tone.opacity(0.4), radius: 6, x: 0, y: 0)
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(.thinMaterial)
                .environment(\.colorScheme, .dark)
                .overlay(
                    RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .stroke(tone.opacity(0.35), lineWidth: 1)
                )
        )
    }
}

struct NexusBadge: View {
    let text: String
    let tone: Color

    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 10, weight: .black, design: .monospaced))
            .foregroundStyle(tone)
            .padding(.horizontal, 10)
            .padding(.vertical, 4)
            .background(
                Capsule(style: .continuous)
                    .fill(tone.opacity(0.15))
                    .overlay(
                        Capsule(style: .continuous)
                            .stroke(tone.opacity(0.5), lineWidth: 1)
                    )
            )
            .shadow(color: tone.opacity(0.3), radius: 4, x: 0, y: 0)
    }
}

struct NexusPrimaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13, weight: .bold, design: .rounded))
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity)
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(
                        LinearGradient(
                            colors: [NexusPalette.ocean, NexusPalette.cyan],
                            startPoint: .leading,
                            endPoint: .trailing
                        )
                    )
                    .opacity(configuration.isPressed ? 0.75 : 1.0)
            )
            .foregroundStyle(.white)
            .shadow(color: NexusPalette.cyan.opacity(0.4), radius: configuration.isPressed ? 2 : 8, x: 0, y: configuration.isPressed ? 1 : 4)
            .scaleEffect(configuration.isPressed ? 0.96 : 1)
            .animation(.easeOut(duration: 0.15), value: configuration.isPressed)
    }
}

struct NexusSecondaryButtonStyle: ButtonStyle {
    let tone: Color

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13, weight: .bold, design: .rounded))
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity)
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(tone.opacity(configuration.isPressed ? 0.18 : 0.08))
                    .environment(\.colorScheme, .dark)
            )
            .foregroundStyle(tone)
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .stroke(tone.opacity(configuration.isPressed ? 0.6 : 0.35), lineWidth: 1)
            )
            .scaleEffect(configuration.isPressed ? 0.96 : 1)
            .animation(.easeOut(duration: 0.15), value: configuration.isPressed)
    }
}

struct NexusKeyValueRow: View {
    let key: String
    let value: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 14) {
            Text(key.uppercased())
                .font(.system(size: 11, weight: .bold, design: .monospaced))
                .foregroundStyle(NexusPalette.textSecondary)
                .frame(width: 110, alignment: .leading)
            Text(value)
                .font(.system(size: 13, weight: .medium, design: .monospaced))
                .foregroundStyle(NexusPalette.textPrimary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .textSelection(.enabled)
        }
    }
}

struct NexusField: View {
    let title: String
    @Binding var text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title.uppercased())
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .foregroundStyle(NexusPalette.cyan)
            TextField(title, text: $text)
                .textFieldStyle(.plain)
                .font(.system(size: 13, weight: .medium, design: .monospaced))
                .foregroundStyle(NexusPalette.textPrimary)
                .padding(.horizontal, 14)
                .padding(.vertical, 12)
                .background(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .fill(.regularMaterial)
                        .environment(\.colorScheme, .dark)
                        .overlay(
                            RoundedRectangle(cornerRadius: 12, style: .continuous)
                                .stroke(NexusPalette.steel.opacity(0.3), lineWidth: 1)
                        )
                )
        }
    }
}

struct NexusInlineStatusRow: View {
    let label: String
    let value: String
    let tone: Color

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            Text(label.uppercased())
                .font(.system(size: 10, weight: .black, design: .monospaced))
                .foregroundStyle(tone.opacity(0.9))
                .frame(width: 86, alignment: .leading)
            Text(value)
                .font(.system(size: 12, weight: .semibold, design: .rounded))
                .foregroundStyle(NexusPalette.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

struct NexusTextComposer: View {
    @Binding var text: String
    let placeholder: String

    var body: some View {
        ZStack(alignment: .topLeading) {
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(.regularMaterial)
                .environment(\.colorScheme, .dark)
                .overlay(
                    RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .stroke(NexusPalette.steel.opacity(0.3), lineWidth: 1)
                )

            if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                Text(placeholder)
                    .font(.system(size: 13, weight: .medium, design: .rounded))
                    .foregroundStyle(NexusPalette.textTertiary)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 14)
            }

            TextEditor(text: $text)
                .scrollContentBackground(.hidden)
                .font(.system(size: 13, weight: .medium, design: .rounded))
                .foregroundStyle(NexusPalette.textPrimary)
                .padding(10)
                .frame(minHeight: 112, idealHeight: 128, maxHeight: 144)
        }
    }
}

struct NexusConversationBubble: View {
    let entry: HubConversationEntry

    private var tone: Color {
        switch entry.kind {
        case .command:
            return NexusPalette.ocean
        case .ack:
            return NexusPalette.cyan
        case .status:
            return NexusPalette.amber
        case .blocked:
            return NexusPalette.rose
        case .result:
            return NexusPalette.mint
        case .clarify:
            return NexusPalette.purple
        case .error:
            return NexusPalette.rose
        case .note:
            return NexusPalette.steel
        }
    }

    private var eyebrow: String {
        switch entry.role {
        case .user:
            return "You"
        case .assistant:
            return entry.kind.rawValue.capitalized
        case .system:
            return "System"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(eyebrow.uppercased())
                    .font(.system(size: 10, weight: .black, design: .monospaced))
                    .foregroundStyle(tone.opacity(0.95))
                Spacer()
                if let sessionID = entry.sessionID, !sessionID.isEmpty {
                    Text(sessionID)
                        .font(.system(size: 10, weight: .medium, design: .monospaced))
                        .foregroundStyle(NexusPalette.textTertiary)
                        .lineLimit(1)
                }
            }
            Text(entry.content)
                .font(.system(size: 13, weight: .medium, design: .rounded))
                .foregroundStyle(NexusPalette.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .fill(tone.opacity(0.09))
                .overlay(
                    RoundedRectangle(cornerRadius: 18, style: .continuous)
                        .stroke(tone.opacity(0.28), lineWidth: 1)
                )
        )
    }
}
