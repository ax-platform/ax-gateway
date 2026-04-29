/**
 * Headless remote MCP smoke test using the same agent config as axctl/channel.
 *
 * Usage:
 *   AX_CONFIG_FILE=/home/ax-agent/agents/orion/.ax/config.toml \
 *   AX_SPACE_ID=<space-id> \
 *   bun channel/test-headless-mcp.mjs
 */
import { MCPClientManager } from "@mcpjam/sdk";
import { readFileSync } from "fs";
import { dirname, join, resolve } from "path";
import { homedir } from "os";

function expandHome(path) {
  return path.startsWith("~/") ? join(homedir(), path.slice(2)) : path;
}

function parseFlatToml(text) {
  const vars = {};
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq <= 0) continue;
    const key = trimmed.slice(0, eq).trim();
    let value = trimmed.slice(eq + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    vars[key] = value;
  }
  return vars;
}

function findAxConfig(startDir) {
  let dir = resolve(startDir);
  while (true) {
    const candidate = join(dir, ".ax", "config.toml");
    try {
      readFileSync(candidate, "utf-8");
      return candidate;
    } catch {}
    const parent = dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

const configPath = process.env.AX_CONFIG_FILE || findAxConfig(process.cwd());
if (!configPath) {
  console.error("AX_CONFIG_FILE not set and no .ax/config.toml found from CWD.");
  process.exit(1);
}

const config = parseFlatToml(readFileSync(expandHome(configPath), "utf-8"));
const baseUrl = process.env.AX_BASE_URL || config.base_url || "https://paxai.app";
const agentName = process.env.AX_AGENT_NAME || config.agent_name;
const agentId = process.env.AX_AGENT_ID || config.agent_id;
const spaceId = process.env.AX_SPACE_ID || config.space_id;
const tokenFile = process.env.AX_TOKEN_FILE || config.token_file;
const token = process.env.AX_TOKEN || (tokenFile ? readFileSync(expandHome(tokenFile), "utf-8").trim() : config.token);

if (!agentName || !agentId || !token) {
  console.error("Config must provide agent_name, agent_id, and token_file/token.");
  process.exit(1);
}
if (token.startsWith("axp_u_")) {
  console.error("Refusing headless MCP test with a user PAT. Use axctl token mint to create an agent PAT.");
  process.exit(1);
}

async function exchangeAgentJwt() {
  const resp = await fetch(`${baseUrl}/auth/exchange`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      requested_token_class: "agent_access",
      audience: "ax-mcp",
      scope: "messages tasks context agents spaces search",
      requested_ttl: 900,
      agent_id: agentId,
      agent_name: agentName,
    }),
  });
  if (!resp.ok) {
    throw new Error(`exchange failed (${resp.status}): ${await resp.text()}`);
  }
  const data = await resp.json();
  return data.access_token;
}

function textOf(result) {
  return JSON.stringify(result, null, 2);
}

const jwt = await exchangeAgentJwt();
const manager = new MCPClientManager();
const serverName = "ax";

try {
  await manager.connectToServer(serverName, {
    url: new URL(`${baseUrl}/mcp/agents/${encodeURIComponent(agentName)}`),
    requestInit: {
      headers: {
        Authorization: `Bearer ${jwt}`,
        ...(spaceId ? { "X-Space-Id": spaceId } : {}),
      },
    },
  });

  const listed = await manager.listTools(serverName);
  const tools = Array.isArray(listed) ? listed : listed.tools ?? [];
  const toolNames = tools.map((tool) => tool.name).sort();
  if (!toolNames.includes("whoami")) {
    throw new Error(`whoami tool missing; tools=${toolNames.join(", ")}`);
  }

  const whoami = await manager.executeTool(serverName, "whoami", {});
  const whoamiText = textOf(whoami);
  if (!whoamiText.toLowerCase().includes(agentName.toLowerCase())) {
    throw new Error(`whoami did not include agent ${agentName}: ${whoamiText.slice(0, 500)}`);
  }

  console.log("Headless MCP smoke passed");
  console.log(`  API: ${baseUrl}`);
  console.log(`  Agent: ${agentName} (${agentId.slice(0, 12)}...)`);
  console.log(`  Space: ${spaceId || "(not set)"}`);
  console.log(`  Tools: ${toolNames.join(", ")}`);
} finally {
  await manager.disconnectServer(serverName).catch(() => {});
}
