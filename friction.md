# Friction Log: Alexa+ Talk Sous

**Project:** a voice-first cooking assistant. Browser speech in and out → FastAPI → Amazon Bedrock (Nova Lite, Converse tool calling) → self-hosted MCP server (Streamable HTTP) → SQLite. Deployed on Amazon EC2 with an IAM instance role, Docker Compose and Caddy.

**Severity scale**
- **Blocker:** stopped all progress until solved.
- **High:** a core feature was broken or unreliable.
- **Medium:** cost real time, with a clear workaround.
- **Low:** a minor annoyance.

| # | Issue | Area | Severity |
|---|---|---|---|
| 1 | Nova Lite claims actions it never performed (skips tool calls) | Bedrock / Nova Lite | **High** |
| 2 | `ValidationException` calling Nova Lite in `ap-southeast-1` | Bedrock | **High** |
| 3 | `AccessDeniedException` after moving to an inference profile | IAM | **High** |
| 4 | `aws bedrock-runtime converse` JSON fails in PowerShell | AWS CLI | Low |
| 5 | Deadlock calling our own MCP endpoint over HTTP | MCP | Medium |
| 6 | No Alexa voice for web apps; browser voices unpredictable | Voice | Medium |
| 7 | Stale browser cache hid a new deploy | Deployment | Low |

---

## 1. Nova Lite claims actions it never performed
**Severity: High**

- **Task attempted:** let a user run a whole cooking session by voice: start a recipe, step through it, set named timers, and change a pantry or units setting, using Bedrock Converse with tool calling.
- **Steps taken:**
  1. Defined the app's actions as tools (set timer, start recipe, next step, update pantry and others), exposed through an MCP server.
  2. Gave Nova Lite the tool specs through Converse `toolConfig` and a system prompt saying to call tools for these actions.
  3. Spoke or typed requests such as "set an 8 minute timer for eggs", "let's make the lemon soy chicken", "switch to US units".
- **Expected result:** the model returns a `toolUse` block, the app runs the tool, and the reply reflects the result.
- **Actual result:** the model often answered in plain text as if it had acted, without calling a tool:
  - It reported two running timers ("Thai Beef Curry", "Rice") while the database contained none.
  - It read out recipe steps that are not in any recipe ("Preheat the oven to 200°C…") when no recipe was active.
  - It said it had switched units when no such tool existed.
  - It replied "The 15 minute cooking timer has been cancelled" about a timer that did not exist.
  - Because only the final text is stored in conversation history, the model then repeated its own earlier false claims.
- **Workaround used:**
  - The application, not the model, owns all state: recipe progress, timer deadlines, pantry amounts, units.
  - Plain phrases are handled directly in code ("set an 8 minute timer…", next / repeat / back, "let's make…", "add 6 eggs to my pantry", "cook for 4", "switch to US units", yes / no / undo).
  - The live state is injected into every prompt.
  - Guards replace any claim the model cannot back up with an honest message.
  - Each guard has a test that fails if the guard is removed: 363 backend tests and 65 page checks in total.
- **Actionable suggestion (for AWS):** publish guidance on making tool use reliable with smaller Nova models: recommended prompt and schema patterns, known failure modes, and how `toolChoice` behaves per Nova model. We did not rely on `toolChoice` because we could not verify its behaviour in time. A reference sample showing "app-owned state, model only requests actions" would have saved us the most time.

## 2. `ValidationException` calling Nova Lite in `ap-southeast-1`
**Severity: High**

- **Task attempted:** call Nova Lite from the deployed app in the Singapore region.
- **Steps taken:**
  1. Set `AWS_DEFAULT_REGION=ap-southeast-1` and `BEDROCK_MODEL_ID=amazon.nova-lite-v1:0`.
  2. Sent a chat message from the page.
- **Expected result:** a normal model reply.
- **Actual result:** HTTP 503 from our API. The server log showed only `Bedrock error: ValidationException`, because our first error handler logged the error code and not Bedrock's message, so the cause was not visible.
- **Workaround used:** called the model through the `apac.amazon.nova-lite-v1:0` inference profile, which worked. We believe the plain model ID could not be used on demand in this region. We also changed the app to log Bedrock's error message on the server (never shown to users).
- **Actionable suggestion:** make this error name the fix (for example, "invoke this model through an inference profile in this region"), and show in the console or docs which models need an inference profile per region.

## 3. `AccessDeniedException` after moving to an inference profile
**Severity: High**

- **Task attempted:** let the EC2 instance role call Bedrock with least privilege.
- **Steps taken:**
  1. Wrote a policy allowing `bedrock:InvokeModel` on the `us-east-1` foundation-model ARN for Nova Lite.
  2. Attached it to the instance role and ran the app with the `apac.` inference profile in `ap-southeast-1`.
- **Expected result:** the role's permission on the model would be enough.
- **Actual result:** `AccessDeniedException`, although the server log confirmed the role's credentials were being found.
- **Workaround used:** allowed `bedrock:InvokeModel` on the inference-profile ARN, plus the underlying foundation-model ARNs in the regions it can route to, restricted with a `bedrock:InferenceProfileArn` condition.
- **Actionable suggestion:** add a least-privilege IAM example for invoking through an inference profile (profile ARN plus underlying model ARNs), and mention the requirement in the inference-profile documentation.

## 4. `aws bedrock-runtime converse` JSON fails in PowerShell
**Severity: Low**

- **Task attempted:** test model access from the command line on Windows.
- **Steps taken:** ran `aws bedrock-runtime converse --region ap-southeast-1 --model-id … --messages '[{"role":"user","content":[{"text":"hi"}]}]'` in PowerShell.
- **Expected result:** a model reply.
- **Actual result:** `ParamValidation: Invalid JSON … JSON received: [{role:user,…}]`. PowerShell removed the inner double quotes.
- **Workaround used:** saved the JSON to a file and used `--messages file://msgs.json`.
- **Actionable suggestion:** add a PowerShell (`file://`) example next to the bash example in the Converse CLI documentation.

## 5. Deadlock calling our own MCP endpoint over HTTP
**Severity: Medium**

- **Task attempted:** run the chat agent and the MCP server in one FastAPI process, so the agent calls the MCP tools.
- **Steps taken:** mounted the MCP server on `/mcp/` (Streamable HTTP) and had the chat handler call that URL as an MCP client.
- **Expected result:** tool calls run through the MCP server.
- **Actual result:** the single Uvicorn process stalled, because the request handler was waiting on an HTTP call to itself.
- **Workaround used:** the chat path now uses FastMCP's in-memory client against the same server object, so one tool definition serves both the browser path and external MCP clients. We also pinned library versions, because the FastMCP API differed between versions (`input_schema` vs `inputSchema`, decorator return values, host and origin protection settings).
- **Actionable suggestion:** document a pattern for "agent and MCP server in the same app", including an in-process client example.

## 6. No Alexa voice for web apps; browser voices unpredictable
**Severity: Medium**

- **Task attempted:** speak replies in a female, Alexa-like voice in the browser.
- **Steps taken:** used the browser's speech synthesis voices.
- **Expected result:** a consistent, good-sounding voice.
- **Actual result:** the voice depends on the device, and the browser voice list has no gender or quality field. Speech recognition also works only in Chrome/Edge and only over HTTPS or localhost.
- **Workaround used:** a voice picker that ranks voices by known names and prefers natural female voices; HTTPS through a DuckDNS domain and Caddy automatic certificates. Amazon Polly was the AWS-native option, but we did not adopt it, to avoid adding an untested dependency before the deadline.
- **Actionable suggestion:** a short sample showing how a browser app can safely play Amazon Polly audio (server-side synthesis, rate-limited, with a browser-voice fallback) would make an Alexa-like voice an easy default for demos.

## 7. Stale browser cache hid a new deploy
**Severity: Low**

- **Task attempted:** deploy a new version of the page.
- **Steps taken:** rebuilt the container and reloaded the page.
- **Expected result:** the new pantry panel would load its data.
- **Actual result:** the browser kept the old `app.js` (`typeof loadPantry` returned `undefined`), so the panel looked empty while the server had the data.
- **Workaround used:** serve static files with `Cache-Control: no-cache`, and show "Could not load your pantry." instead of failing silently.
- **Actionable suggestion:** for sample apps that serve static assets from containers, include cache headers or hashed filenames in the template.

---

## What worked well
- The Converse tool loop is simple once every `toolUse` block gets a matching `toolResult`.
- The EC2 instance role with the standard credential chain meant no AWS keys on the server.
- A scripted fake Bedrock let us test every flow and guard without paying for model calls.