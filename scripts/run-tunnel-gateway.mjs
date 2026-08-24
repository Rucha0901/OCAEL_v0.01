import http from "node:http";

const frontend = new URL(process.env.OVAEL_FRONTEND_ORIGIN ?? "http://127.0.0.1:3000");
const backend = new URL(process.env.OVAEL_BACKEND_ORIGIN ?? "http://127.0.0.1:8000");
const port = Number(process.env.OVAEL_GATEWAY_PORT ?? 3100);

function proxyRequest(request, response) {
  const requestUrl = request.url ?? "/";
  const isMcpRequest = requestUrl === "/mcp" || requestUrl.startsWith("/mcp/");
  const isBackendRequest = requestUrl === "/api/ovael" || requestUrl.startsWith("/api/ovael/") || isMcpRequest;
  const target = isBackendRequest ? backend : frontend;
  const path = isMcpRequest ? requestUrl : isBackendRequest ? requestUrl.slice("/api/ovael".length) || "/" : requestUrl;
  const headers = {
    ...request.headers,
    host: target.host,
    "x-forwarded-host": request.headers.host ?? "",
    "x-forwarded-proto": request.headers["x-forwarded-proto"] ?? "https",
  };

  const upstream = http.request(
    {
      protocol: target.protocol,
      hostname: target.hostname,
      port: target.port,
      method: request.method,
      path,
      headers,
    },
    (upstreamResponse) => {
      response.writeHead(upstreamResponse.statusCode ?? 502, upstreamResponse.headers);
      upstreamResponse.pipe(response);
    },
  );

  upstream.on("error", (error) => {
    if (!response.headersSent) {
      response.writeHead(502, { "content-type": "application/json; charset=utf-8" });
    }
    response.end(JSON.stringify({ detail: "OVAEL service is starting", cause: error.message }));
  });

  request.on("aborted", () => upstream.destroy());
  request.pipe(upstream);
}

const server = http.createServer(proxyRequest);

server.listen(port, "127.0.0.1", () => {
  process.stdout.write(`OVAEL gateway ready at http://127.0.0.1:${port}\n`);
});

function shutdown() {
  server.close(() => process.exit(0));
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
