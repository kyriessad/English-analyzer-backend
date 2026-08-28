import http from "node:http";
import { Dialect, LocalLinter } from "harper.js";
import { binaryInlined as binary } from "harper.js/binaryInlined";

const HOST = "127.0.0.1";
const PORT = 8082;
const MAX_BODY_BYTES = 1024 * 1024;

const linter = new LocalLinter({
  binary,
  dialect: Dialect.American,
});
await linter.setup();

function sendJson(response, status, body) {
  const payload = JSON.stringify(body);
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(payload),
  });
  response.end(payload);
}

function readBody(request) {
  return new Promise((resolve, reject) => {
    let body = "";
    let bytes = 0;
    request.setEncoding("utf8");
    request.on("data", (chunk) => {
      bytes += Buffer.byteLength(chunk);
      if (bytes > MAX_BODY_BYTES) {
        reject(new Error("request body too large"));
        request.destroy();
        return;
      }
      body += chunk;
    });
    request.on("end", () => resolve(body));
    request.on("error", reject);
  });
}

const server = http.createServer(async (request, response) => {
  if (request.method === "GET" && request.url === "/health") {
    sendJson(response, 200, { status: "ok", service: "harper-sidecar" });
    return;
  }

  if (request.method !== "POST" || request.url !== "/lint") {
    sendJson(response, 404, { error: "not found" });
    return;
  }

  try {
    const payload = JSON.parse(await readBody(request));
    if (typeof payload.text !== "string") {
      sendJson(response, 400, { error: "text must be a string" });
      return;
    }

    const lints = await linter.lint(payload.text, { language: "plaintext" });
    sendJson(response, 200, {
      lints: lints.map((lint) => ({
        kind: lint.lint_kind(),
        message: lint.message(),
        offset: lint.span().start,
        length: lint.span().len(),
        replacements: lint.suggestions().map((suggestion) =>
          suggestion.get_replacement_text()
        ),
      })),
    });
  } catch (error) {
    console.error(error);
    sendJson(response, 500, { error: "Harper lint failed" });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`Harper sidecar listening on http://${HOST}:${PORT}`);
});

async function shutdown() {
  server.close();
  await linter.dispose();
}

process.once("SIGINT", shutdown);
process.once("SIGTERM", shutdown);
