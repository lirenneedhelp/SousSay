# Security notes (hackathon demo)

## Phase 1 audit: what was wrong (all demonstrated against the previous build)

| Severity | Finding | Status |
|---|---|---|
| Critical | `/mcp/` had no authentication. Anyone could call every tool, and read or change any session or timer by id (demonstrated). | Fixed: bearer key, disabled unless configured |
| Critical | `/api/chat`, `/api/state`, `/api/timers/cancel` were open. Anyone reaching the URL could spend your Bedrock quota or read any session id. | Fixed: signed-in cookie required |
| High | Session ids were the only access control; no ownership. | Fixed: every session is owned by its creator, enforced in the data layer and MCP tools |
| High | No per-user or global limit on model calls (cost abuse). | Fixed: per-user, per-IP and global hourly caps |
| High | `/docs` and `/openapi.json` publicly listed every route. | Fixed: disabled |
| High | No Host-header check; 5 MB bodies were read before rejection; unknown JSON fields were ignored. | Fixed: allow-list, 16 KB streaming limit, strict models |
| Medium | SQLite file was world-readable (0644). | Fixed: 0600 |
| Medium | No CSP or security headers; the page needed a third-party CDN script plus inline script. | Fixed: strict CSP, local CSS/JS |
| Medium | All Bedrock failures looked identical (502); no explicit timeouts. | Fixed: mapped to safe, actionable errors; 5 s connect, 30 s read, 45 s per turn |
| Low | Access log would record session ids in URLs. | Mitigated: Docker image runs with `--no-access-log` |
| Checked, not a problem | Duplicate timer notifications under concurrency. | Verified exactly-once with a 12-thread test |

## Design

- **Web users:** one shared `ACCESS_CODE` (simple, suited to a demo) is exchanged at `/api/login` for an HttpOnly,
  `SameSite=Strict`, signed, 24 h cookie holding a random per-browser id (`Secure` in production). The code and the
  signing key exist only in server environment variables. Each browser's sessions belong to that id, so two people
  with the code cannot see each other's kitchens. Cross-origin POSTs are refused (Origin check); no CORS is enabled.
- **MCP clients:** `Authorization: Bearer <MCP_API_KEY>` on `/mcp/`. With no key set, `/mcp/` returns 404. MCP
  clients act as one `mcp` principal and can only reach sessions they created.
- **Trade-off:** the MCP specification's native authorisation is OAuth-based. Implementing an OAuth server is
  disproportionate for a hackathon, so this uses a static bearer key, which common MCP clients and proxies
  support. Treat the key like a password; rotate it by changing the env var.
- **Hosts:** `ALLOWED_HOSTS` is enforced for the whole app and for FastMCP's own guard.
- **Model-call budget:** 12 chats/min per user, 36/min per IP, 300/hour overall (all configurable).

## Pantry, servings and deduction (added later)

- **Ownership:** a pantry belongs to the signed-in identity and is read and written only under that identity
  (enforced in the data layer; the HTTP and MCP paths both go through it). MCP clients have their own pantry.
- **Endpoints** (`/api/pantry`, `/api/preferences`, `/api/servings`, `/api/pantry/deduct`) require sign-in, the same
  Origin check as other writes, strict request models (unknown fields rejected), and 30-60 writes per minute per user.
  Caps: 100 items per pantry, 20 per request, 80 characters per item, 1-24 servings, 10 generated recipes.
- **Deduction** claims the session's decision atomically in the same transaction as the pantry update and the undo
  log, so it happens exactly once even with concurrent requests (tested with 10 threads).
- **Unit detection** uses only the browser's language setting; no IP geolocation or other personal data is collected.
- **Display safety:** pantry text is rendered with `textContent` only, never as HTML.

## Remaining risks (read before making it public)

1. **Shared access code:** anyone with the code can use the demo and spend quota up to the hourly cap. Share it
   only with the audience, and change it afterwards. There are no per-user accounts or lockouts beyond rate limits.
2. **Static MCP key:** no scopes, no expiry, and it grants the `mcp` principal's full tool set.
3. **Prompt injection:** the model only has recipe/timer tools scoped to the caller's own session, but users can
   still try to steer it. Food-safety checks are enforced in code for generated recipes, not only in the prompt.
4. **Single process:** rate limits and the in-memory limiter reset on restart and are per worker. Run one worker.
5. **Timers need the page open** (browser audio and polling); background tabs may be throttled.
6. **Cookies last 24 h** and cannot be revoked individually; rotating `SESSION_SECRET` signs everyone out.
7. **Signing in again creates a new identity,** so a browser's earlier session is unreachable and the page
   silently starts a fresh one (earlier timers are lost to that browser).
8. **The database holds user data** (conversation text, pantry contents, unit choice). Back it up or wipe it deliberately.
   A pantry is tied to the sign-in identity, so after cookie expiry or a new sign-in the old pantry is unreachable.
   Deduction is an estimate from recipe amounts and unit conversions; it skips what it cannot compare and offers undo.
9. **Not verified here:** Docker image build, Caddy/HTTPS issuance, EC2/IAM, real Bedrock permissions, and a real
   browser (Tailwind rendering and Web Speech). See DEPLOY.md for how to check each.
