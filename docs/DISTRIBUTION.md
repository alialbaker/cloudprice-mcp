# Distribution checklist

Where cloudprice-mcp is listed, and how to submit it to the rest. This file is
for the maintainer — no end-user content.

## Already listed

- **PyPI** — auto-published via GitHub Actions Trusted Publishing on every
  tagged release. https://pypi.org/project/cloudprice-mcp/
- **Glama** — https://glama.ai/mcp/servers/alialbaker/cloudprice-mcp (score
  badge in README).

## To submit (one-time, ~10 min each)

### 1. Official MCP Registry — `registry.modelcontextprotocol.io`

This is Anthropic's canonical registry, launched late 2025. Smithery and
PulseMCP are increasingly federating from it.

Prereq: `server.json` lives in the repo root (already there, v0.18.0).

Steps:

1. Install the publisher CLI:
   ```
   curl -L https://github.com/modelcontextprotocol/registry/releases/latest/download/mcp-publisher_linux_amd64.tar.gz | tar xz
   sudo mv mcp-publisher /usr/local/bin/
   ```
   Windows / macOS: grab the right asset from
   <https://github.com/modelcontextprotocol/registry/releases>.

2. From the repo root:
   ```
   mcp-publisher login github
   mcp-publisher publish
   ```
   The login opens a GitHub OAuth flow in your browser; the publish reads
   `server.json` and posts it.

3. Future updates: bump `version` in `server.json` to match the new release,
   then re-run `mcp-publisher publish`.

### 2. Smithery — `smithery.ai`

Prereq: `smithery.yaml` in the repo root (already there).

Steps:

1. Visit <https://smithery.ai/new>.
2. Sign in with GitHub.
3. Authorize the Smithery app on `Albaker-Group/cloudprice-mcp`.
4. Smithery picks up `smithery.yaml` automatically and lists the server.
5. Future updates auto-deploy on every commit to `main`.

### 3. mcp.so — community directory

GitHub-issue-based submission.

1. Open <https://github.com/chatmcp/mcpso/issues/new/choose>.
2. Pick "Submit MCP Server".
3. Fields:
   - Name: `cloudprice-mcp`
   - Author: `Ali Albaker`
   - GitHub: `https://github.com/Albaker-Group/cloudprice-mcp`
   - Description: copy from `server.json`'s description.
   - Tags: `finops`, `pricing`, `aws`, `azure`, `gcp`, `oci`, `llm`, `cost`.
4. Submit. The maintainers review and merge within a few days.

### 4. (Optional) PulseMCP — auto-federates from the official registry

No action needed once step 1 lands; PulseMCP picks it up on its next sync.

### 5. (Optional) Cursor MCP directory

Cursor maintains its own catalog at <https://cursor.directory/mcp>.
Submission is via the form on the page or via PR to the underlying repo.
Lower priority than the three above.
