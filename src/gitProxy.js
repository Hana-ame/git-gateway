// src/gitProxy.js — Read-only Git Smart-HTTP clone/fetch proxy (strictly blocks git-receive-pack)
'use strict';

const { Readable } = require('stream');

const HOP_BY_HOP = new Set([
  'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
  'te', 'trailers', 'transfer-encoding', 'upgrade', 'host', 'content-length', 'authorization',
]);

function upstreamAuthHeader(settings) {
  const tok = settings.resolveGithubToken();
  if (!tok) return '';
  return 'Basic ' + Buffer.from(`x-access-token:${tok}`, 'utf-8').toString('base64');
}

async function readBoundedBody(req, maxBytes) {
  const cl = Number(req.headers['content-length'] || 0);
  if (Number.isFinite(cl) && cl > maxBytes) return null;
  const chunks = [];
  let total = 0;
  for await (const chunk of req) {
    const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    total += buf.length;
    if (total > maxBytes) return null;
    chunks.push(buf);
  }
  return Buffer.concat(chunks);
}

function sendJson(res, status, data) {
  const payload = JSON.stringify(data);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(payload),
  });
  res.end(payload);
}

async function handleGitSmartHttp(req, res, urlObj, settings, owner, repo, repoPath, servicePath) {
  const services = urlObj.searchParams.getAll('service');
  if (servicePath === 'git-receive-pack' || services.includes('git-receive-pack')) {
    return sendJson(res, 403, {
      ok: false,
      error: 'direct git push is disabled on this read-only clone proxy',
      hint: `Submit your changes as a patch via POST /${repoPath}/patches?nickname=<your_nick>&base_branch=<branch>`,
    });
  }

  let upstreamUrl;
  if (servicePath === 'info/refs') {
    if (req.method !== 'GET') {
      return sendJson(res, 405, { ok: false, error: 'info/refs only allows GET' });
    }
    // Strict whitelist against HTTP Parameter Pollution (?service=git-receive-pack&service=git-upload-pack)
    if (services.length !== 1 || services[0] !== 'git-upload-pack') {
      return sendJson(res, 403, {
        ok: false,
        error: 'only service=git-upload-pack is allowed on this read-only clone proxy',
      });
    }
    upstreamUrl = `${settings.githubWebBase}/${owner}/${repo}.git/info/refs?service=git-upload-pack`;
  } else if (servicePath === 'git-upload-pack') {
    if (req.method !== 'POST') {
      return sendJson(res, 405, { ok: false, error: 'git-upload-pack only allows POST' });
    }
    upstreamUrl = `${settings.githubWebBase}/${owner}/${repo}.git/git-upload-pack`;
  } else {
    return sendJson(res, 404, { ok: false, error: 'unsupported git endpoint' });
  }

  const fwdHeaders = {};
  for (const [k, v] of Object.entries(req.headers)) {
    if (!HOP_BY_HOP.has(k.toLowerCase()) && v !== undefined) {
      fwdHeaders[k] = Array.isArray(v) ? v.join(', ') : String(v);
    }
  }
  if (!fwdHeaders['user-agent'] && !fwdHeaders['User-Agent']) {
    fwdHeaders['User-Agent'] = 'git/2.39.0';
  }
  const auth = upstreamAuthHeader(settings);
  if (auth) fwdHeaders['Authorization'] = auth;

  let body = undefined;
  if (req.method === 'POST') {
    const buf = await readBoundedBody(req, settings.maxGitProxyBodyBytes);
    if (buf === null) {
      return sendJson(res, 413, {
        ok: false,
        error: `git-upload-pack request body exceeds limit (${settings.maxGitProxyBodyBytes} bytes)`,
      });
    }
    body = buf;
  }

  let upstreamResp;
  try {
    upstreamResp = await fetch(upstreamUrl, {
      method: req.method,
      headers: fwdHeaders,
      body,
      redirect: 'follow',
    });
  } catch (err) {
    return sendJson(res, 502, { ok: false, error: `upstream git clone proxy error: ${err.message}` });
  }

  const respHeaders = {};
  for (const [k, v] of upstreamResp.headers.entries()) {
    const lower = k.toLowerCase();
    // Node fetch auto-decompresses gzip responses, so skip content-encoding to avoid double decode
    if (!HOP_BY_HOP.has(lower) && lower !== 'content-encoding') {
      respHeaders[k] = v;
    }
  }
  res.writeHead(upstreamResp.status, respHeaders);
  if (upstreamResp.body) {
    const nodeStream = Readable.fromWeb(upstreamResp.body);
    nodeStream.on('error', () => res.end());
    nodeStream.pipe(res);
  } else {
    res.end();
  }
}

module.exports = { handleGitSmartHttp, readBoundedBody };
