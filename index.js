#!/usr/bin/env node
// index.js — Zero-dependency Node.js entrypoint for git-gateway
'use strict';

const { Settings } = require('./src/config');
const { createGatewayServer } = require('./src/app');

function parseCliArgs(argv) {
  const out = {};
  for (let i = 2; i < argv.length; i++) {
    if (argv[i] === '--host' && argv[i + 1]) {
      out.host = argv[++i];
    } else if (argv[i] === '--port' && argv[i + 1]) {
      out.port = Number.parseInt(argv[++i], 10);
    }
  }
  return out;
}

const overrides = parseCliArgs(process.argv);
const settings = new Settings(overrides);
const server = createGatewayServer(settings);

server.listen(settings.port, settings.host, () => {
  console.log(
    `[git-gateway] Node.js server listening on http://${settings.host}:${settings.port} ` +
      `(rules=${settings.activeRules().length}, token=${settings.tokenSource()})`
  );
});

let shuttingDown = false;
function shutdown(signal) {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log(`[git-gateway] received ${signal}, shutting down...`);
  server.close(() => process.exit(0));
  if (typeof server.closeIdleConnections === 'function') {
    server.closeIdleConnections();
  }
  setTimeout(() => process.exit(1), 8000).unref();
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
