import assert from "node:assert/strict";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server renders the authenticated OVAEL entry point", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);
  const html = await response.text();
  assert.match(html, /<title>OVAEL .* Learn what is holding you back<\/title>/i);
  assert.match(html, /Teaching that remembers what actually changed/);
  assert.match(html, /Sign in to continue/);
  assert.match(html, /Fill synthetic Bharat demo/);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton|Your site is taking shape/i);
});

test("exposes accessible authentication and privacy structure", async () => {
  const html = await (await render()).text();
  assert.match(html, /<main class="auth-page" id="main-content">/);
  assert.match(html, /aria-labelledby="sign-in-title"/);
  assert.match(html, /autoComplete="username"/);
  assert.match(html, /autoComplete="current-password"/);
  assert.match(html, /Access tokens remain in this browser tab/);
});
