import type { AgentProviderId } from "../types";

// Brand marks for the agent CLIs, drawn as small inline SVGs (no asset files,
// no dependency): simplified shapes in each brand's colour, enough to tell
// the three apart at 16-20px. `currentColor` is used for OpenAI's, which is
// monochrome by design, so it follows the theme.
const MARKS: Record<AgentProviderId, (size: number) => JSX.Element> = {
  // Anthropic / Claude: the eight-ray burst.
  claude: (size) => (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden>
      <g stroke="#D97757" strokeWidth="2.6" strokeLinecap="round">
        {[0, 45, 90, 135, 180, 225, 270, 315].map((deg) => (
          <line key={deg} x1="12" y1="4" x2="12" y2="9.2" transform={`rotate(${deg} 12 12)`} />
        ))}
      </g>
    </svg>
  ),
  // OpenAI / Codex: six interlocked petals around a hexagon.
  codex: (size) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" aria-hidden>
      {[0, 60, 120, 180, 240, 300].map((deg) => (
        <path key={deg} d="M12 2.6 L17.2 5.6 V10.4 L12 13.4" transform={`rotate(${deg} 12 12)`} />
      ))}
    </svg>
  ),
  // Google Antigravity: an arch (the "A") in Google's four colours.
  agy: (size) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" strokeWidth="3" strokeLinecap="round" aria-hidden>
      <path d="M3.5 20 L9 7.5" stroke="#EA4335" />
      <path d="M9 7.5 Q12 2 15 7.5" stroke="#FBBC04" />
      <path d="M15 7.5 L20.5 20" stroke="#4285F4" />
      <path d="M8 15.5 H16" stroke="#34A853" />
    </svg>
  ),
};

export function AgentProviderIcon({ provider, size = 16 }: { provider: AgentProviderId | undefined; size?: number }) {
  return <span className="agent-provider-icon">{MARKS[provider ?? "claude"](size)}</span>;
}
