"""Agent avatar generator — creates unique SVG icons from agent names.

Generates deterministic, visually distinct avatars based on the agent's
name. Each agent gets a unique combination of shape, color, and pattern.

No external dependencies — pure Python SVG generation.
"""

import hashlib

# Color palettes by agent type
PALETTES = {
    "default": [
        ("#22d3ee", "#0891b2"),  # Cyan (platform accent)
        ("#818cf8", "#6366f1"),  # Indigo
        ("#34d399", "#059669"),  # Emerald
        ("#fb923c", "#ea580c"),  # Orange
        ("#f472b6", "#db2777"),  # Pink
        ("#a78bfa", "#7c3aed"),  # Violet
        ("#38bdf8", "#0284c7"),  # Sky
        ("#fbbf24", "#d97706"),  # Amber
    ],
    "sentinel": [("#22d3ee", "#0891b2")],  # Cyan — sentinel agents
    "space_agent": [("#818cf8", "#6366f1")],  # Indigo — aX concierge
    "mcp": [("#34d399", "#059669")],  # Emerald — MCP agents
    "cloud": [("#fb923c", "#ea580c")],  # Orange — cloud agents
}


def _hash_name(name: str) -> int:
    """Deterministic hash from agent name."""
    return int(hashlib.sha256(name.encode()).hexdigest(), 16)


def _pick_colors(name: str, agent_type: str = "default") -> tuple[str, str]:
    """Pick a color pair based on agent name and type."""
    palette = PALETTES.get(agent_type, PALETTES["default"])
    h = _hash_name(name)
    return palette[h % len(palette)]


def _initials(name: str) -> str:
    """Extract 1-2 character initials from agent name."""
    parts = name.replace("_", " ").replace("-", " ").split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return name[:2].upper()


def generate_avatar(
    name: str,
    agent_type: str = "default",
    size: int = 64,
) -> str:
    """Generate an SVG avatar for an agent.

    Args:
        name: Agent name (e.g., "backend_sentinel")
        agent_type: Agent type for color theming
        size: SVG size in pixels

    Returns:
        SVG string
    """
    h = _hash_name(name)
    fg, bg = _pick_colors(name, agent_type)
    initials = _initials(name)

    # Pick a shape variant based on hash
    shape_variant = h % 4

    # Pattern: geometric background based on hash bits
    pattern_bits = [(h >> i) & 1 for i in range(16)]

    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}">',
        "<defs>",
        f'  <linearGradient id="bg-{name[:8]}" x1="0%" y1="0%" x2="100%" y2="100%">',
        f'    <stop offset="0%" stop-color="{fg}" stop-opacity="0.95"/>',
        f'    <stop offset="100%" stop-color="{bg}" stop-opacity="0.98"/>',
        "  </linearGradient>",
        "</defs>",
    ]

    cx, cy = size // 2, size // 2

    # Background shape
    if shape_variant == 0:
        # Rounded square
        r = size * 0.18
        svg_parts.append(f'<rect width="{size}" height="{size}" rx="{r}" fill="url(#bg-{name[:8]})" />')
    elif shape_variant == 1:
        # Circle
        svg_parts.append(f'<circle cx="{cx}" cy="{cy}" r="{cx}" fill="url(#bg-{name[:8]})" />')
    elif shape_variant == 2:
        # Squircle (rounded square with larger radius)
        r = size * 0.3
        svg_parts.append(f'<rect width="{size}" height="{size}" rx="{r}" fill="url(#bg-{name[:8]})" />')
    else:
        # Hexagon-ish
        r = size * 0.12
        svg_parts.append(f'<rect width="{size}" height="{size}" rx="{r}" fill="url(#bg-{name[:8]})" />')

    # Geometric pattern overlay (subtle)
    grid_size = size // 4
    for i in range(16):
        if pattern_bits[i]:
            row, col = i // 4, i % 4
            x = col * grid_size
            y = row * grid_size
            opacity = 0.08 + (h >> (i + 16) & 3) * 0.03
            svg_parts.append(
                f'<rect x="{x}" y="{y}" width="{grid_size}" height="{grid_size}" '
                f'fill="white" opacity="{opacity:.2f}" />'
            )

    # Top shine
    svg_parts.append(f'<rect x="0" y="0" width="{size}" height="{size // 3}" fill="white" opacity="0.08" />')

    # Initials
    font_size = size * 0.38 if len(initials) == 2 else size * 0.45
    svg_parts.append(
        f'<text x="{cx}" y="{cy}" '
        f'font-family="-apple-system, BlinkMacSystemFont, sans-serif" '
        f'font-size="{font_size:.0f}" font-weight="700" '
        f'fill="white" fill-opacity="0.95" '
        f'text-anchor="middle" dominant-baseline="central">'
        f"{initials}</text>"
    )

    svg_parts.append("</svg>")
    return "\n".join(svg_parts)


def avatar_data_uri(name: str, agent_type: str = "default", size: int = 64) -> str:
    """Generate a data URI for inline embedding."""
    import base64

    svg = generate_avatar(name, agent_type, size)
    encoded = base64.b64encode(svg.encode()).decode()
    return f"data:image/svg+xml;base64,{encoded}"


if __name__ == "__main__":
    import sys

    name = sys.argv[1] if len(sys.argv) > 1 else "backend_sentinel"
    agent_type = sys.argv[2] if len(sys.argv) > 2 else "default"
    print(generate_avatar(name, agent_type))
