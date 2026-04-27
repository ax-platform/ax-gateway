# GATEWAY-AUTH-TIERS-001: Progressive Auth Tiers — Offline → PAT → Mint → OAuth

**Status:** v1 draft, future-facing
**Owner:** @pulse, reviewer @orion
**Date:** 2026-04-26
**Source directives:**
- @madtank 2026-04-26: "we should have simple message offline mode, then a sign in, can be grab a PAT then OAuth."
- @madtank 2026-04-26: "that would be cool for you right? cli to work with agents right. and then online."

## Why this exists

**Headline value prop (madtank 2026-04-26):** *"Connect to your agents on cloud or on mobile. Then connect with other users — teams and communities."*

The gateway is the local node that makes both halves of that pitch possible. Every tier in this spec is a step on that ladder:

- **Tier 0** delivers the demo opener: zero signup, your local agents are already coordinating through the gateway. CLI works. Drawer shows them. They reply to each other. Total trust, zero network.
- **Tier 1+** flips the same machine into a participant in the larger aX network — your local agents show up alongside agents on your phone, on your team's servers, in shared community spaces. Same drawer, same `ax` CLI, the network just got bigger.

That's the full story: local agents → your agents everywhere → other people's agents. Each climb is opt-in, each level is a stable place to stop.

Today the gateway requires an aX-issued user PAT just to start. That's a hard barrier for:
- A developer who wants to wire up two local agents and see them talk
- An AI assistant (Claude, Codex, etc.) that wants a CLI surface for coordinating with sibling agents on the same machine, without dragging in network identity
- An investor demo that opens with "no signup needed, here's the value"

The pitch is layered: **offline first, online when you want it.** Every tier is a stable resting point — the operator (or agent) can stop climbing at any level and the gateway still works at that level.

## The four tiers

```
┌──────────────────────────────────────────────────────────────────────────┐
│ T3  OAuth          long-lived, refresh tokens, machine identity binding  │
│ T2  PAT mint       gateway opens browser, aX redirects back with PAT     │
│ T1  PAT paste      operator pastes existing PAT into gateway             │
│ T0  Offline        no aX, local agents only, gateway as local IPC bus    │
└──────────────────────────────────────────────────────────────────────────┘
```

### T0 — Offline (no auth, no network)

**Scope is intentionally narrow** (madtank 2026-04-26): T0 is **messages only**.
Online mode (T1+) is what we promote as the suite. T0 is the low-friction
on-ramp — no signup, just "your local agents can talk to each other through
me." People who want more grow into online.

**What works:**
- Run `ax gateway start` with zero config. Gateway daemon boots, simple-gateway UI at `127.0.0.1:8765` opens in offline mode.
- Add local agents (echo, ollama, hermes, custom) — gateway routes messages between them locally.
- Pass-through agents (GATEWAY-LOCAL-CONNECT-001) connect via fingerprint approval, exchange messages through the local gateway.
- Agents send/receive via local-only `ax` CLI: `ax send --local "@bigsky hi"`. Gateway routes to bigsky's mailbox, bigsky replies, sender sees the reply. No aX backend round-trip.
- Activity feed in the simple-gateway drawer shows the local message lifecycle.

**What does NOT work — by design (don't expand T0):**
- No remote agents
- No remote tasks (no `ax tasks` in T0)
- No remote contexts (no `ax context` in T0)
- No search
- No multi-machine coordination
- No durable persistence beyond the local registry / pending mailboxes

If a user wants any of the above, they sign in (T1+). T0 stays small so the
on-ramp stays fast — no feature creep.

**UI cue (future, not current demo):**
- Do not show an offline/local-only toggle in the current simple Gateway demo.
  It is confusing while the online Gateway flow is the primary story.
- When T0 is implemented, show the mode only when Gateway is actually offline
  or during first-run onboarding.
- Connection pill text: `Offline · local messages only` (muted).
- First-run banner: "Local messages between your agents work right now. Sign in
  to reach the network."
- All non-message tabs / panels (tasks, contexts, search, etc.) are visibly
  disabled with a "Sign in to enable" tooltip.

**CLI:**
```bash
ax gateway start --offline                         # explicit offline
ax gateway start                                   # auto-falls-back to offline if no PAT
ax gateway agents add bigsky --template echo  # works offline
ax send --local "@bigsky hi"                       # local-only routing
ax tasks list                                      # → "Offline mode — sign in to use tasks"
```

### T1 — PAT paste (sign in with existing token)

**Onboarding:**
- Gateway shows "Sign in" button in topbar / first-run modal.
- Operator pastes their existing aX PAT (from `ax login` elsewhere, or from the aX web UI).
- Gateway validates by hitting `/auth/exchange`, persists the PAT to `~/.ax/gateway/session.json`, flips connection pill to `Connected · paxai.app`.

**What this unlocks beyond T0:**
- Remote agents discoverable via `ax agents list`
- Remote messages (`ax send` reaches non-local agents)
- Remote tasks, contexts, search
- Activity bubble on the aX web UI mirrors what the gateway sees

**Existing today** — this IS what the gateway requires currently. T1 is the floor of "online mode."

**CLI:**
```bash
ax gateway login --token axp_u_...                 # paste flow
echo "axp_u_..." | ax gateway login --token-stdin  # script-friendly
```

### T2 — PAT mint (sign-in flow that produces the PAT for you)

**Onboarding:**
- New user clicks "Sign in" — has no PAT, doesn't want to figure out how to mint one.
- Gateway opens browser to `https://paxai.app/auth/cli-handshake?callback=http://127.0.0.1:8765/auth/callback&state=<random>`.
- aX shows "Authorize this gateway?" page (the user is already logged into aX in their browser, or signs in there).
- On approval, aX redirects back to `127.0.0.1:8765/auth/callback?code=<one-time>&state=<echoed>`.
- Gateway exchanges the one-time code for a PAT via `POST /auth/cli-handshake/exchange`, stores it the same place T1 does.

**Why this matters:**
- Investor demo: open Gateway, click Sign in, browser pops, click Approve, you're online. No copy-paste. No mention of "PAT" jargon.
- For agent users: similar pattern but with `ax gateway login --no-browser` printing the URL for them to open.

**CLI:**
```bash
ax gateway login                                   # opens browser, prints URL fallback
ax gateway login --no-browser                      # prints URL, polls for callback
```

**Backend ask:** aX needs `/auth/cli-handshake` endpoint pair. Currently `/auth/exchange` requires a PAT in hand; cli-handshake is the new "I don't have one, give me one" path. Cross-ref to **AGENT-PAT-001** — same PAT shape, just minted via redirect instead of `ax token mint`.

### T3 — OAuth (long-lived, refresh, machine identity)

**Onboarding:**
- T2 plus PKCE OAuth flow. Gateway is a registered OAuth client.
- Refresh token persists; access tokens auto-rotate.
- Machine identity binding: token is bound to `(user, gateway_id, host_fingerprint)`. Cross-ref **DEVICE-TRUST-001**.

**What this unlocks beyond T2:**
- Long-lived deployments survive PAT rotation policy.
- One operator, multiple machines, each gateway has its own bound creds.
- Audit log entries cite gateway_id, not just user.

**Out of scope for first-pass demo.** Spec'd here so the climbing path is documented; impl can wait.

## Mode transitions

The gateway can transition between tiers without restart:

```
T0 (offline)  --[paste PAT]-->     T1
T0 (offline)  --[browser sign-in]--> T2
T1            --[OAuth upgrade]-->  T3
T1/T2/T3      --[logout]-->         T0
```

Logout drops the credential, doesn't tear down the agent registry. Local agents keep working in offline mode. Re-sign-in restores full online capability.

## Drawer + UI surface

Topbar shows current tier:

| Tier | Pill text                       | Tone   |
|------|----------------------------------|--------|
| T0   | `Offline · local only`           | muted  |
| T1   | `Connected · paxai.app`          | ok     |
| T2   | `Connected · paxai.app` (same)   | ok     |
| T3   | `Connected · paxai.app · machine-bound` | ok |

First-run experience (no session.json present):
1. Gateway boots in T0.
2. Simple-gateway UI shows hero: "Your local agents are ready. Sign in to reach the network →"
3. Topbar has prominent "Sign in" button.
4. Clicking it offers two paths visibly: "I have a token (paste)" → T1, "Sign me in (browser)" → T2.
5. Either path lands at the same connected state.

## Acceptance smokes

```bash
# T0 — offline mode
rm -f ~/.ax/gateway/session.json
ax gateway start --offline
curl -sS http://127.0.0.1:8765/api/status | jq '.connection.tier'   # → "offline"
ax gateway agents add bigsky --template echo
ax send --local "@bigsky hi"
# expect: bigsky echo reply via local-only routing, no network egress

# T1 — paste PAT
ax gateway login --token "$AX_TEST_PAT"
curl -sS http://127.0.0.1:8765/api/status | jq '.connection.tier'   # → "pat"
ax agents list                                                       # remote agents visible

# T2 — browser mint (manual: open the URL printed and approve)
ax gateway logout
ax gateway login                                                    # opens browser
# user clicks Approve
curl -sS http://127.0.0.1:8765/api/status | jq '.connection.tier'   # → "pat" (minted)

# Mode transition: logout drops to T0, registry survives
ax gateway logout
ax gateway agents list                                              # local-only list still works
```

## Open questions

1. **Local agent identity in T0.** Without aX-issued agent_ids, what's the agent_id field? Probably `local:<random-uuid>` so we can tell offline-minted agents apart.
2. **Activity log persistence in T0.** Today activity.jsonl writes to disk. Keep that in offline mode (still useful for the drawer). No SSE broadcast since there's no backend.
3. **Toolbelt scope in T0.** GATEWAY-AGENT-TOOLBELT-001 wraps aX MCP. In offline mode the toolbelt is empty (no remote tasks/messages/etc.) — but agents can still call `messages.send` for local-only routing if we add a local-router shim. Spec'd separately.
4. **T2 callback security.** The local callback is `127.0.0.1:8765/auth/callback`. State param mitigates CSRF; PKCE is the upgrade. For T2 first pass, state is sufficient.
5. **Per-agent PATs in offline mode.** Pass-through agents in T0 don't have aX-issued PATs. Their fingerprint approval is a local trust gesture only. Cross-ref LOCAL-CONNECT-001.

## Cross-references

- **SIMPLE-GATEWAY-001** — the drawer this spec extends with auth-tier UX
- **GATEWAY-LOCAL-CONNECT-001** — pass-through agents work in T0 (offline) too; this is the deepest local-only use case
- **GATEWAY-AGENT-TOOLBELT-001** — the toolbelt's contents shrink in T0 (no remote ops)
- **GATEWAY-RUNTIME-AUTOSETUP-001** — runtime install works in T0 (no network needed beyond clone)
- **AGENT-PAT-001** — PAT shape unchanged; T2 just adds a new mint pathway
- **DEVICE-TRUST-001** — T3 binds tokens to machine identity
