#!/usr/bin/env bun
/**
 * aX Channel for Claude Code.
 *
 * Bridges @mentions from the aX platform (next.paxai.app) into a running
 * Claude Code session via the MCP channel protocol.
 *
 * Modeled on fakechat — uses the official MCP SDK with StdioServerTransport.
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { readFileSync, existsSync } from "fs";
import { join } from "path";
import { homedir } from "os";

// --- Load .env from ~/.claude/channels/ax-channel/.env as fallback ---
function loadDotEnv(): Record<string, string> {
  const envPath = join(homedir(), ".claude", "channels", "ax-channel", ".env");
  if (!existsSync(envPath)) return {};
  const vars: Record<string, string> = {};
  for (const line of readFileSync(envPath, "utf-8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq > 0) vars[trimmed.slice(0, eq)] = trimmed.slice(eq + 1);
  }
  return vars;
}

const dotenv = loadDotEnv();
function cfg(key: string, fallback: string): string {
  return process.env[key] ?? dotenv[key] ?? fallback;
}

// --- Config: env vars > .env file > defaults ---
const BASE_URL = cfg("AX_BASE_URL", "https://next.paxai.app");
const AGENT_NAME = cfg("AX_AGENT_NAME", "");
const AGENT_ID = cfg("AX_AGENT_ID", "");
const SPACE_ID = cfg("AX_SPACE_ID", "");

function loadToken(): string {
  // Direct token in env or .env
  const direct = cfg("AX_TOKEN", "");
  if (direct) return direct;
  // Token file path
  const tokenFile = cfg("AX_TOKEN_FILE", join(homedir(), ".ax", "user_token"));
  try {
    return readFileSync(tokenFile, "utf-8").trim();
  } catch {
    throw new Error(
      `No AX_TOKEN set and cannot read token file at ${tokenFile}. Run /ax-channel:configure <token> to set up.`
    );
  }
}

function log(msg: string) {
  process.stderr.write(`[ax-channel] ${msg}\n`);
}

// --- JWT Exchange ---
async function exchangeForJWT(pat: string): Promise<string> {
  const resp = await fetch(`${BASE_URL}/auth/exchange`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${pat}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      requested_token_class: "user_access",
      scope: "messages tasks context agents spaces",
    }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`JWT exchange failed (${resp.status}): ${text}`);
  }
  const data = (await resp.json()) as { access_token: string };
  return data.access_token;
}

// --- Resolve agent_id from name ---
async function resolveAgentId(
  jwt: string,
  name: string
): Promise<string | null> {
  try {
    const resp = await fetch(`${BASE_URL}/api/v1/agents`, {
      headers: { Authorization: `Bearer ${jwt}` },
    });
    if (!resp.ok) return null;
    const data = (await resp.json()) as
      | { agents: { id: string; name: string }[] }
      | { id: string; name: string }[];
    const agents = Array.isArray(data) ? data : data.agents ?? [];
    const match = agents.find(
      (a) => a.name?.toLowerCase() === name.toLowerCase()
    );
    return match?.id ?? null;
  } catch {
    return null;
  }
}

// --- Send message as agent ---
async function sendMessage(
  jwt: string,
  agentId: string | null,
  spaceId: string,
  text: string,
  parentId?: string
): Promise<{ id?: string }> {
  const body: Record<string, unknown> = {
    content: text,
    space_id: spaceId,
    channel: "main",
    message_type: "text",
  };
  if (parentId) body.parent_id = parentId;

  const headers: Record<string, string> = {
    Authorization: `Bearer ${jwt}`,
    "Content-Type": "application/json",
  };
  if (agentId) headers["X-Agent-Id"] = agentId;

  const resp = await fetch(`${BASE_URL}/api/v1/messages`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const errText = await resp.text();
    throw new Error(`send failed (${resp.status}): ${errText.slice(0, 200)}`);
  }
  const data = (await resp.json()) as Record<string, unknown>;
  const msg = (data.message as Record<string, unknown>) ?? data;
  return { id: msg.id as string };
}

// --- Edit message in place ---
async function editMessage(
  jwt: string,
  agentId: string | null,
  messageId: string,
  text: string
): Promise<void> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${jwt}`,
    "Content-Type": "application/json",
  };
  if (agentId) headers["X-Agent-Id"] = agentId;

  const resp = await fetch(`${BASE_URL}/api/v1/messages/${messageId}`, {
    method: "PATCH",
    headers,
    body: JSON.stringify({ content: text }),
  });
  if (!resp.ok) {
    const errText = await resp.text();
    throw new Error(`edit failed (${resp.status}): ${errText.slice(0, 200)}`);
  }
}

// --- SSE Listener ---
function startSSE(
  getJwt: () => Promise<string>,
  agentName: string,
  agentId: string | null,
  onMention: (data: {
    id: string;
    content: string;
    author: string;
    parentId?: string;
    ts?: string;
  }) => void
) {
  const seen = new Set<string>();
  let backoff = 1;

  async function connect() {
    while (true) {
      try {
        // Fresh JWT on every reconnect
        const sseJwt = await getJwt();
        log(`SSE connecting...`);
        const resp = await fetch(
          `${BASE_URL}/api/sse/messages?token=${sseJwt}`
        );

        // Use a manual reader since EventSource isn't available in all envs
        if (!resp.ok || !resp.body) {
          throw new Error(`SSE failed: ${resp.status}`);
        }

        backoff = 1;
        log(`SSE connected`);

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let eventType = "";
        let dataLines: string[] = [];

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() ?? "";

          for (const line of lines) {
            if (line.startsWith("event:")) {
              eventType = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
              dataLines.push(line.slice(5).trim());
            } else if (line === "") {
              if (eventType && dataLines.length) {
                const raw = dataLines.join("\n");
                processEvent(eventType, raw);
              }
              eventType = "";
              dataLines = [];
            }
          }
        }
      } catch (err) {
        if ((err as Error)?.name === "AbortError") continue;
        log(`SSE error: ${err}. Reconnecting in ${backoff}s...`);
        await Bun.sleep(backoff * 1000);
        backoff = Math.min(backoff * 2, 60);
      }
    }
  }

  function processEvent(type: string, raw: string) {
    if (
      ["bootstrap", "heartbeat", "ping", "connected", "identity_bootstrap"].includes(type)
    ) {
      return;
    }
    if (type !== "message" && type !== "mention") return;

    let data: Record<string, unknown>;
    try {
      data = JSON.parse(raw);
    } catch {
      return;
    }

    const id = data.id as string;
    if (!id || seen.has(id)) return;

    const content = (data.content as string) ?? "";
    if (!content.includes(`@${agentName}`)) return;

    // Self-filter
    const author = data.author as string | Record<string, unknown>;
    let senderName = "";
    let senderId = "";
    if (typeof author === "object" && author) {
      senderName = (author.name as string) ?? "";
      senderId = (author.id as string) ?? "";
    } else if (typeof author === "string") {
      senderName = author;
      senderId = (data.agent_id as string) ?? "";
    } else {
      senderName =
        (data.display_name as string) ??
        (data.sender_name as string) ??
        "";
      senderId = (data.agent_id as string) ?? "";
    }

    if (senderName.toLowerCase() === agentName.toLowerCase()) return;
    if (agentId && senderId === agentId) return;

    seen.add(id);
    if (seen.size > 500) {
      const arr = [...seen];
      seen.clear();
      for (const x of arr.slice(-250)) seen.add(x);
    }

    // Strip @mention prefix
    const prompt = content
      .replace(new RegExp(`@${agentName}\\b\\s*[-—]?\\s*`, "i"), "")
      .trim();
    if (!prompt) return;

    log(`mention from ${senderName}: ${prompt.slice(0, 60)}`);
    onMention({
      id,
      content: prompt,
      author: senderName || "unknown",
      parentId: data.parent_id as string | undefined,
      ts: (data.timestamp as string) ?? (data.created_at as string),
    });
  }

  // Don't await — run in background
  connect().catch((err) => log(`SSE fatal: ${err}`));
}

// --- MCP Server ---
const mcp = new Server(
  { name: "ax-channel", version: "0.1.0" },
  {
    capabilities: { tools: {}, experimental: { "claude/channel": {} } },
    instructions: `Messages from aX arrive via notifications/claude/channel. Your transcript is not sent back to aX automatically. Use the reply tool for every response you want posted back to aX. Pass reply_to to target a specific incoming aX message_id; if omitted, the latest inbound message is used.`,
  }
);

let lastMessageId: string | null = null;
let currentJwt: string = "";
let resolvedAgentId: string | null = null;
let jwtTime = 0;

// --- Message queue for reliability + cross-client polling ---
type QueuedMention = {
  id: string;
  content: string;
  author: string;
  parentId?: string;
  ts?: string;
  delivered: boolean;
};
const mentionQueue: QueuedMention[] = [];
const QUEUE_MAX = 100;
let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
let ackMessageId: string | null = null; // ID of the ack message to update in place
const HEARTBEAT_INTERVAL = 30_000; // 30 seconds
const HEARTBEAT_TIMEOUT = 300_000; // 5 minutes — stop if no reply

function startHeartbeat(parentMessageId: string) {
  stopHeartbeat();
  const start = Date.now();
  let count = 0;
  heartbeatTimer = setInterval(async () => {
    if (!ackMessageId) return;
    count++;
    const elapsed = Math.round((Date.now() - start) / 1000);
    if (Date.now() - start > HEARTBEAT_TIMEOUT) {
      stopHeartbeat();
      try {
        const jwt = await ensureJwt();
        await editMessage(jwt, resolvedAgentId, ackMessageId, `No response after ${Math.round(elapsed / 60)}m — session may need attention.`);
      } catch {}
      return;
    }
    try {
      const jwt = await ensureJwt();
      await editMessage(jwt, resolvedAgentId, ackMessageId, `Working... (${elapsed}s)`);
      log(`heartbeat #${count} updated ${ackMessageId!.slice(0, 12)}`);
    } catch (err) {
      log(`heartbeat edit failed: ${err}`);
    }
  }, HEARTBEAT_INTERVAL);
}

function stopHeartbeat() {
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
}

async function ensureJwt(): Promise<string> {
  if (currentJwt && Date.now() - jwtTime < 10 * 60 * 1000) return currentJwt;
  const pat = loadToken();
  currentJwt = await exchangeForJWT(pat);
  jwtTime = Date.now();
  log("JWT refreshed");
  return currentJwt;
}

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "reply",
      description:
        "Reply to an aX channel message in-thread.",
      inputSchema: {
        type: "object" as const,
        properties: {
          text: {
            type: "string",
            description: "Message text to send back to aX.",
          },
          reply_to: {
            type: "string",
            description:
              "aX message_id to reply to. Defaults to the latest inbound message.",
          },
        },
        required: ["text"],
      },
    },
    {
      name: "get_messages",
      description:
        "Get pending aX messages (for clients without push notification support). Returns unread mentions.",
      inputSchema: {
        type: "object" as const,
        properties: {
          limit: {
            type: "number",
            description: "Max messages to return (default: 10)",
          },
          mark_read: {
            type: "boolean",
            description: "Mark returned messages as read (default: true)",
          },
        },
      },
    },
  ],
}));

mcp.setRequestHandler(CallToolRequestSchema, async (req) => {
  const args = (req.params.arguments ?? {}) as Record<string, unknown>;
  const name = req.params.name;

  if (name === "get_messages") {
    const limit = Number(args.limit ?? 10);
    const markRead = args.mark_read !== false;
    const pending = mentionQueue.filter((m) => !m.delivered).slice(0, limit);
    if (markRead) {
      for (const m of pending) m.delivered = true;
    }
    return {
      content: [
        {
          type: "text" as const,
          text: pending.length
            ? JSON.stringify(
                pending.map((m) => ({
                  message_id: m.id,
                  author: m.author,
                  content: m.content,
                  parent_id: m.parentId,
                  ts: m.ts,
                })),
                null,
                2
              )
            : "No pending messages.",
        },
      ],
    };
  }

  if (name !== "reply") {
    return {
      content: [{ type: "text" as const, text: `unknown tool: ${name}` }],
      isError: true,
    };
  }

  const text = String(args.text ?? "").trim();
  const replyTo = (args.reply_to as string) ?? lastMessageId;

  if (!text) {
    return {
      content: [{ type: "text" as const, text: "reply.text is required" }],
      isError: true,
    };
  }

  try {
    const jwt = await ensureJwt();
    stopHeartbeat();

    // If we have an ack message, update it in place with the final response
    // Otherwise create a new message
    let resultId: string | undefined;
    if (ackMessageId) {
      await editMessage(jwt, resolvedAgentId, ackMessageId, text);
      resultId = ackMessageId;
      ackMessageId = null;
    } else {
      const result = await sendMessage(
        jwt,
        resolvedAgentId,
        SPACE_ID,
        text,
        replyTo ?? undefined
      );
      resultId = result.id;
    }
    return {
      content: [
        {
          type: "text" as const,
          text: `sent${replyTo ? ` reply to ${replyTo}` : ""}${resultId ? ` (${resultId})` : ""}`,
        },
      ],
    };
  } catch (err) {
    return {
      content: [
        {
          type: "text" as const,
          text: `reply failed: ${err instanceof Error ? err.message : err}`,
        },
      ],
      isError: true,
    };
  }
});

// --- Start ---
await mcp.connect(new StdioServerTransport());

// Initialize auth and SSE after MCP is connected
const jwt = await ensureJwt();
resolvedAgentId = AGENT_ID || (await resolveAgentId(jwt, AGENT_NAME));
log(
  `identity: @${AGENT_NAME}${resolvedAgentId ? ` (${resolvedAgentId.slice(0, 12)}...)` : ""}`
);
log(`space: ${SPACE_ID}`);
log(`api: ${BASE_URL}`);

startSSE(ensureJwt, AGENT_NAME, resolvedAgentId, async (mention) => {
  lastMessageId = mention.id;

  // Queue for reliability + get_messages polling
  mentionQueue.push({ ...mention, delivered: false });
  if (mentionQueue.length > QUEUE_MAX) mentionQueue.shift();

  // Ack immediately — create one message that gets updated in place
  try {
    const ackJwt = await ensureJwt();
    const ack = await sendMessage(
      ackJwt,
      resolvedAgentId,
      SPACE_ID,
      `Received — working on it...`,
      mention.id
    );
    ackMessageId = ack.id ?? null;
    log(`ack sent ${ackMessageId?.slice(0, 12)} for ${mention.id.slice(0, 12)}`);
  } catch (err) {
    log(`ack failed: ${err}`);
    ackMessageId = null;
  }

  // Start heartbeat — updates the ack message in place
  startHeartbeat(mention.id);

  // Deliver to Claude Code session
  void mcp.notification({
    method: "notifications/claude/channel",
    params: {
      content: mention.content,
      meta: {
        chat_id: SPACE_ID,
        message_id: mention.id,
        parent_id: mention.parentId ?? undefined,
        user: mention.author,
        sender: mention.author,
        source: "ax",
        space_id: SPACE_ID,
        ts: mention.ts ?? new Date().toISOString(),
      },
    },
  });
  log(`delivered ${mention.id.slice(0, 12)} from ${mention.author}`);
});
