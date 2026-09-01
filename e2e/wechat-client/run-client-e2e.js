'use strict';

const fs = require('fs');
const path = require('path');
const childProcess = require('child_process');
const net = require('net');
const WebSocket = require('ws');
const automator = require('miniprogram-automator');

const mode = process.argv[2] || '';
const sourceProject = process.env.LEVEL7_MINIAPP_SOURCE || '';
const runtimeProject = process.env.LEVEL7_MINIAPP_RUNTIME || '';
const artifactDir = process.env.LEVEL7_ARTIFACT_DIR || '';
const runId = process.env.LEVEL7_RUN_ID || '';
const cliPath = process.env.LEVEL7_WECHAT_CLI || '';
const servicePort = Number(process.env.LEVEL7_WECHAT_SERVICE_PORT || 0);
const automationPort = Number(process.env.LEVEL7_WECHAT_AUTOMATION_PORT || 19420);
const e2eBaseUrl = process.env.LEVEL7_BACKEND_BASE_URL || 'http://127.0.0.1:18000';
const clientLabel = String(process.env.LEVEL7_CLIENT_LABEL || '').replace(/[^A-Za-z0-9_-]/g, '');
const resultStem = clientLabel ? `${mode}-${clientLabel}` : mode;

const resultPath = path.join(artifactDir, `wechat-client-${resultStem}.json`);
const consolePath = path.join(artifactDir, `wechat-client-${resultStem}.console.jsonl`);
const exceptionPath = path.join(artifactDir, `wechat-client-${resultStem}.exceptions.jsonl`);

let miniProgram = null;
let consoleEvents = [];
let exceptionEvents = [];
let steps = [];
let shouldCloseProject = false;

process.on('unhandledRejection', (error) => {
  const entry = {
    at: now(),
    error: sanitize({ message: error && error.message, stack: error && error.stack }),
  };
  exceptionEvents.push(entry);
  try {
    appendJsonLine(exceptionPath, entry);
  } catch (_) {}
  process.stderr.write(`[wechat-client] unhandled rejection captured: ${redactString(error && error.message ? error.message : error)}\n`);
});

function now() {
  return new Date().toISOString();
}

function requireValue(value, label) {
  if (!value) throw new Error(`${label} is required`);
  return value;
}

function redactString(value) {
  return String(value)
    .replace(/(Bearer\s+)[A-Za-z0-9._~+\/-]+/gi, '$1[REDACTED]')
    .replace(/("?(?:access_token|secret|js_code|code)"?\s*[:=]\s*")[^"]+("?)/gi, '$1[REDACTED]$2')
    .replace(/([?&](?:secret|js_code|code)=)[^&\s]+/gi, '$1[REDACTED]');
}

function sanitize(value, depth = 0) {
  if (depth > 8) return '[TRUNCATED]';
  if (typeof value === 'string') return redactString(value);
  if (Array.isArray(value)) return value.map((item) => sanitize(item, depth + 1));
  if (value && typeof value === 'object') {
    const out = {};
    for (const [key, item] of Object.entries(value)) {
      if (/secret|token|authorization|js_code|(^|_)code$/i.test(key)) {
        out[key] = typeof item === 'string' ? '[REDACTED]' : sanitize(item, depth + 1);
      } else {
        out[key] = sanitize(item, depth + 1);
      }
    }
    return out;
  }
  return value;
}

function appendJsonLine(filePath, value) {
  fs.appendFileSync(filePath, `${JSON.stringify(sanitize(value))}\n`, 'utf8');
}

function saveResult(value) {
  fs.writeFileSync(resultPath, `${JSON.stringify(sanitize(value), null, 2)}\n`, 'utf8');
}

function step(name, details = {}) {
  const entry = { at: now(), name, details: sanitize(details) };
  steps.push(entry);
  process.stdout.write(`[wechat-client] ${entry.at} ${name}\n`);
  return entry;
}

function replaceOnce(source, needle, replacement, label) {
  const first = source.indexOf(needle);
  if (first < 0 || source.indexOf(needle, first + needle.length) >= 0) {
    throw new Error(`Runtime instrumentation anchor mismatch: ${label}`);
  }
  return source.slice(0, first) + replacement + source.slice(first + needle.length);
}

function prepareRuntimeProject() {
  requireValue(sourceProject, 'LEVEL7_MINIAPP_SOURCE');
  requireValue(runtimeProject, 'LEVEL7_MINIAPP_RUNTIME');
  requireValue(runId, 'LEVEL7_RUN_ID');

  if (fs.existsSync(runtimeProject)) {
    fs.rmSync(runtimeProject, { recursive: true, force: true });
  }

  const excluded = new Set(['.git', '.claude', '.cursor', 'tests', 'node_modules', 'miniprogram_npm']);
  fs.cpSync(sourceProject, runtimeProject, {
    recursive: true,
    filter(source) {
      const relative = path.relative(sourceProject, source);
      if (!relative) return true;
      const first = relative.split(path.sep)[0];
      return !excluded.has(first) && !/\.log$/i.test(relative);
    },
  });

  const runtimeConfigPath = path.join(runtimeProject, 'utils', 'localBackendConfig.js');
  fs.writeFileSync(
    runtimeConfigPath,
    [
      '// Generated inside an isolated Level 7 runtime copy. Never used by normal development/release.',
      `const BACKEND_BASE_URL = ${JSON.stringify(e2eBaseUrl)};`,
      'module.exports = { BACKEND_BASE_URL };',
      '',
    ].join('\n'),
    'utf8',
  );

  const storagePrefix = `__level7_e2e_${runId}${clientLabel ? `_${clientLabel}` : ''}__:`;
  const appPath = path.join(runtimeProject, 'app.js');
  const originalApp = fs.readFileSync(appPath, 'utf8');
  const storageBootstrap = [
    '// Level 7 runtime-only storage namespace. The real wx Storage implementation is preserved.',
    '(function installLevel7StorageNamespace() {',
    `  const prefix = ${JSON.stringify(storagePrefix)};`,
    '  const originalGet = wx.getStorageSync.bind(wx);',
    '  const originalSet = wx.setStorageSync.bind(wx);',
    '  const originalRemove = wx.removeStorageSync.bind(wx);',
    '  const originalInfo = wx.getStorageInfoSync.bind(wx);',
    '  wx.__level7E2EStoragePrefix = prefix;',
    '  wx.getStorageSync = function (key) { return originalGet(prefix + String(key)); };',
    '  wx.setStorageSync = function (key, value) { return originalSet(prefix + String(key), value); };',
    '  wx.removeStorageSync = function (key) { return originalRemove(prefix + String(key)); };',
    '  wx.clearStorageSync = function () {',
    '    const info = originalInfo();',
    '    const keys = info && Array.isArray(info.keys) ? info.keys : [];',
    '    keys.filter(function (key) { return String(key).indexOf(prefix) === 0; }).forEach(originalRemove);',
    '  };',
    '})();',
    '',
  ].join('\n');
  fs.writeFileSync(appPath, storageBootstrap + originalApp, 'utf8');

  const indexWxmlPath = path.join(runtimeProject, 'pages', 'index', 'index.wxml');
  const originalIndexWxml = fs.readFileSync(indexWxmlPath, 'utf8');
  fs.writeFileSync(
    indexWxmlPath,
    '<view id="level7-auth-status" wx:if="{{level7E2EAuthStatus}}">{{level7E2EAuthStatus}}</view>\n' + originalIndexWxml,
    'utf8',
  );

  const appJsonPath = path.join(runtimeProject, 'app.json');
  const appJson = JSON.parse(fs.readFileSync(appJsonPath, 'utf8'));
  const bootstrapPage = 'pages/e2e_bootstrap/index';
  appJson.pages = [bootstrapPage].concat((appJson.pages || []).filter((item) => item !== bootstrapPage));
  fs.writeFileSync(appJsonPath, `${JSON.stringify(appJson, null, 2)}\n`, 'utf8');

  const bootstrapDir = path.join(runtimeProject, 'pages', 'e2e_bootstrap');
  fs.mkdirSync(bootstrapDir, { recursive: true });
  fs.writeFileSync(path.join(bootstrapDir, 'index.js'), 'Page({ data: { ready: true } });\n', 'utf8');
  fs.writeFileSync(path.join(bootstrapDir, 'index.json'), '{}\n', 'utf8');
  fs.writeFileSync(
    path.join(bootstrapDir, 'index.wxml'),
    '<view class="level7-bootstrap">Level 7 E2E bootstrap</view>\n',
    'utf8',
  );
  fs.writeFileSync(
    path.join(bootstrapDir, 'index.wxss'),
    '.level7-bootstrap { padding: 32rpx; color: #666; }\n',
    'utf8',
  );

  const apiPath = path.join(runtimeProject, 'utils', 'apiClient.js');
  let apiSource = fs.readFileSync(apiPath, 'utf8');
  apiSource = replaceOnce(
    apiSource,
    "let autoRefreshSuppressed = false;",
    [
      "let autoRefreshSuppressed = false;",
      "let level7RequestSequence = 0;",
      "function level7Log(event, details = {}) {",
      "  const entry = { at: new Date().toISOString(), event, ...details };",
      "  try {",
      "    const existing = wx.getStorageSync('__level7Diagnostics');",
      "    const items = Array.isArray(existing) ? existing.slice(-199) : [];",
      "    items.push(entry);",
      "    wx.setStorageSync('__level7Diagnostics', items);",
      "  } catch (_) {}",
      "  console.log('[LEVEL7_CLIENT_E2E]', JSON.stringify(entry));",
      "}",
      "function level7RequestId() { level7RequestSequence += 1; return 'wx-e2e-' + Date.now().toString(36) + '-' + level7RequestSequence.toString(36); }",
    ].join('\n'),
    'E2E diagnostic helpers',
  );
  apiSource = replaceOnce(
    apiSource,
    '  return mergedHeaders;\n}',
    [
      "  if (!mergedHeaders['X-Request-ID'] && !mergedHeaders['x-request-id']) {",
      "    mergedHeaders['X-Request-ID'] = level7RequestId();",
      '  }',
      '  return mergedHeaders;',
      '}',
    ].join('\n'),
    'request ID injection',
  );
  apiSource = replaceOnce(
    apiSource,
    'function sendRequest(options) {\n  const requestOptions = options || {};\n\n  return new Promise((resolve, reject) => {',
    [
      'function sendRequest(options) {',
      '  const requestOptions = options || {};',
      '  const requestHeaders = buildHeaders(requestOptions.header || requestOptions.headers, requestOptions);',
      "  const requestPath = requestOptions.url || requestOptions.path || '';",
      "  level7Log('request_start', { path: requestPath, method: requestOptions.method || 'GET', requestId: requestHeaders['X-Request-ID'] || requestHeaders['x-request-id'] || '' });",
      '',
      '  return new Promise((resolve, reject) => {',
    ].join('\n'),
    'request start diagnostics',
  );
  apiSource = replaceOnce(
    apiSource,
    '        header: buildHeaders(requestOptions.header || requestOptions.headers, requestOptions),',
    '        header: requestHeaders,',
    'request headers reuse',
  );
  apiSource = replaceOnce(
    apiSource,
    '        success(response) {\n          if (response.statusCode >= 200 && response.statusCode < 300) {',
    [
      '        success(response) {',
      "          level7Log('request_finish', { path: requestPath, method: requestOptions.method || 'GET', statusCode: response.statusCode, requestId: requestHeaders['X-Request-ID'] || requestHeaders['x-request-id'] || '' });",
      '          if (response.statusCode >= 200 && response.statusCode < 300) {',
    ].join('\n'),
    'request finish diagnostics',
  );
  apiSource = replaceOnce(
    apiSource,
    '            data: response.data\n          }));\n        },\n        fail(error) {\n          reject(normalizeRequestError(error));\n        }',
    [
      '            data: response.data',
      '          }));',
      '        },',
      '        fail(error) {',
      "          level7Log('request_fail', { path: requestPath, method: requestOptions.method || 'GET', requestId: requestHeaders['X-Request-ID'] || requestHeaders['x-request-id'] || '', errMsg: error && error.errMsg ? String(error.errMsg) : '' });",
      '          reject(normalizeRequestError(error));',
      '        }',
    ].join('\n'),
    'request failure diagnostics',
  );
  apiSource = replaceOnce(
    apiSource,
    'function requestWechatLoginCode() {\n  return new Promise((resolve, reject) => {\n    try {\n      wx.login({',
    [
      'function requestWechatLoginCode() {',
      '  return new Promise((resolve, reject) => {',
      '    try {',
      "      level7Log('wx_login_start');",
      '      wx.login({',
    ].join('\n'),
    'wx.login start diagnostics',
  );
  apiSource = replaceOnce(
    apiSource,
    '        success(response) {\n          if (response.code) {\n            resolve(response.code);',
    [
      '        success(response) {',
      "          level7Log('wx_login_success', { hasCode: Boolean(response && response.code), codeLength: response && response.code ? String(response.code).length : 0 });",
      '          if (response.code) {',
      '            resolve(response.code);',
    ].join('\n'),
    'wx.login success diagnostics',
  );
  apiSource = replaceOnce(
    apiSource,
    'function loginWithWechatCode(code) {\n  return sendRequest({',
    [
      'function loginWithWechatCode(code) {',
      "  level7Log('code2session_request_start', { hasCode: Boolean(code) });",
      '  return sendRequest({',
    ].join('\n'),
    'code2session request diagnostics',
  );
  apiSource = replaceOnce(
    apiSource,
    'function refreshBackendAuth() {\n  if (refreshPromise) {\n    return refreshPromise;\n  }\n\n  autoRefreshSuppressed = false;',
    [
      'function refreshBackendAuth() {',
      '  if (refreshPromise) {',
      "    level7Log('auth_refresh_join');",
      '    return refreshPromise;',
      '  }',
      '',
      '  autoRefreshSuppressed = false;',
      "  level7Log('auth_refresh_start');",
    ].join('\n'),
    'single-flight auth diagnostics',
  );
  apiSource = replaceOnce(
    apiSource,
    '  updateAppBackendAuth(authState);\n  return authState;',
    [
      '  updateAppBackendAuth(authState);',
      "  level7Log('jwt_saved', { hasUserId: Boolean(backendUserId), hasAccessToken: Boolean(backendAccessToken) });",
      '  return authState;',
    ].join('\n'),
    'JWT save diagnostics',
  );
  apiSource = replaceOnce(
    apiSource,
    "        endpoint: '/api/analyze-english/stream'",
    "        endpoint: '/api/analyze-english/stream',\n        requestId: headers['X-Request-ID'] || headers['x-request-id'] || ''",
    'stream request ID diagnostics',
  );
  fs.writeFileSync(apiPath, apiSource, 'utf8');

  return {
    source_project: sourceProject,
    runtime_project: runtimeProject,
    backend_base_url: e2eBaseUrl,
    storage_namespace_enabled: true,
    bootstrap_page: bootstrapPage,
  };
}

function attachObservers(instance) {
  instance.on('console', (message) => {
    const entry = { at: now(), message: sanitize(message) };
    consoleEvents.push(entry);
    appendJsonLine(consolePath, entry);
  });
  instance.on('exception', (error) => {
    const entry = {
      at: now(),
      error: sanitize({ message: error && error.message, stack: error && error.stack }),
    };
    exceptionEvents.push(entry);
    appendJsonLine(exceptionPath, entry);
  });
}

function consoleText(entry) {
  try {
    return JSON.stringify(entry && entry.message ? entry.message : entry);
  } catch (_) {
    return String(entry);
  }
}

function countConsoleSince(startIndex, pattern) {
  return consoleEvents.slice(startIndex).filter((entry) => pattern.test(consoleText(entry))).length;
}

async function waitUntil(label, predicate, timeoutMs = 30000, intervalMs = 150) {
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const value = await predicate();
      if (value) return value;
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error(`Timed out waiting for ${label}${lastError ? `: ${lastError.message}` : ''}`);
}

async function withTimeout(promise, timeoutMs, label) {
  let timer = null;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${label} timed out after ${timeoutMs}ms`)), timeoutMs);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

async function rawAutomationSend(method, params = {}, timeoutMs = 5000) {
  return new Promise((resolve) => {
    const ws = new WebSocket(`ws://127.0.0.1:${automationPort}`);
    const id = `level7-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try {
        ws.close();
      } catch (_) {}
      resolve(value);
    };
    const timer = setTimeout(() => finish({ ok: false, error: `${method} timeout` }), timeoutMs);
    ws.on('open', () => {
      ws.send(JSON.stringify({ id, method, params }));
    });
    ws.on('message', (data) => {
      try {
        finish({ ok: true, response: JSON.parse(data.toString()) });
      } catch (error) {
        finish({ ok: false, error: String(error && error.message ? error.message : error) });
      }
    });
    ws.on('error', (error) => finish({ ok: false, error: String(error && error.message ? error.message : error) }));
    ws.on('close', () => finish({ ok: false, error: 'closed before response' }));
  });
}

function runPowerShell(command, timeoutMs = 15000) {
  const completed = childProcess.spawnSync(
    'powershell.exe',
    ['-NoProfile', '-NonInteractive', '-Command', command],
    { encoding: 'utf8', timeout: timeoutMs, windowsHide: true },
  );
  return {
    exit_code: completed.status,
    stdout: completed.stdout || '',
    stderr: completed.stderr || '',
    error: completed.error ? String(completed.error.message || completed.error) : null,
  };
}

function readJsonOutput(command, timeoutMs = 15000) {
  const completed = runPowerShell(command, timeoutMs);
  const raw = (completed.stdout || '').trim();
  if (!raw) return { completed, value: null };
  try {
    return { completed, value: JSON.parse(raw) };
  } catch (error) {
    return { completed, value: null, parse_error: String(error && error.message ? error.message : error), raw };
  }
}

function listWechatProcesses() {
  const command = [
    "$items = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match 'wechat|devtools|nwjs|微信|开发者' } | ForEach-Object {",
    "  [pscustomobject]@{ Pid=$_.Id; ProcessName=$_.ProcessName; Path=$_.Path; StartTime=($_.StartTime.ToString('o')) }",
    "});",
    "$items | ConvertTo-Json -Compress",
  ].join(' ');
  const result = readJsonOutput(command);
  const value = result.value;
  return Array.isArray(value) ? value : (value ? [value] : []);
}

function processForAutomationPort() {
  const command = [
    `$line = netstat -ano -p tcp | Select-String 'LISTENING' | Where-Object { $_.Line -match ':${automationPort}\\s' } | Select-Object -First 1;`,
    'if($line){',
    "  $parts = ($line.Line -split '\\s+') | Where-Object { $_ };",
    '  $pidValue = [int]$parts[4];',
    '  $p = Get-Process -Id $pidValue -ErrorAction SilentlyContinue;',
    '  if($p){ [pscustomobject]@{ Pid=$pidValue; ProcessName=$p.ProcessName; Path=$p.Path; StartTime=($p.StartTime.ToString(\'o\')) } | ConvertTo-Json -Compress }',
    '}',
  ].join(' ');
  return readJsonOutput(command).value;
}

async function waitForAutomationSdkVersion(timeoutMs = 120000) {
  return waitUntil('automation Tool.getInfo SDKVersion', async () => {
    const info = await rawAutomationSend('Tool.getInfo', {}, 5000);
    const result = info && info.response && info.response.result ? info.response.result : null;
    if (result && result.SDKVersion) {
      return {
        version: result.version || null,
        SDKVersion: result.SDKVersion,
      };
    }
    return null;
  }, timeoutMs, 1000);
}

async function rawPageControlSnapshot(label) {
  const toolInfo = await rawAutomationSend('Tool.getInfo', {}, 10000);
  const currentPage = await rawAutomationSend('App.getCurrentPage', {}, 10000);
  step('raw_page_control_snapshot', {
    label,
    tool_get_info: toolInfo,
    app_get_current_page: currentPage,
  });
  return { toolInfo, currentPage };
}

async function warmRuntimeCurrentPageBeforeConnect() {
  const currentPage = await rawAutomationSend('App.getCurrentPage', {}, 10000);
  step('raw_current_page_before_connect_warmup', { result: currentPage });
  return currentPage;
}

async function closeAutomationBestEffort(label) {
  if (miniProgram) {
    try {
      miniProgram.disconnect();
    } catch (_) {}
    miniProgram = null;
  }
  const result = await rawAutomationSend('Tool.close', {}, 10000);
  step('tool_close_attempt', { label, result });
  await new Promise((resolve) => setTimeout(resolve, 1000));
}

async function reopenRuntimeProject(label) {
  await closeAutomationBestEffort(`${label}_close`);
  await waitForAutomationPortReleased(30000);
  launchWechatCliAuto(label);
  await waitUntil(`automation TCP listener ${label}`, automationPortListening, 120000, 1000);
  step(`wechat_processes_after_cli_auto_${label}`, {
    automation_port_owner: processForAutomationPort(),
    processes: listWechatProcesses(),
  });
  const toolInfo = await waitForAutomationSdkVersion(120000);
  step(`automation_sdk_ready_after_${label}`, toolInfo);
  await warmRuntimeCurrentPageBeforeConnect();
  await connectWithRetry({ retries: 5, timeoutMs: 20000, skipSdkWait: true });
}

function normalizePagePath(value) {
  return String(value || '').replace(/^\//, '');
}

function pageStackDetails(stack) {
  return (Array.isArray(stack) ? stack : []).map((page) => ({
    path: page && page.path ? page.path : null,
    query: page && page.query ? page.query : null,
  }));
}

async function pageStackSnapshot(label, timeoutMs = 5000) {
  const attemptedAt = now();
  let stack = [];
  let error = null;
  try {
    stack = await withTimeout(miniProgram.pageStack(), timeoutMs, `${label} pageStack`);
  } catch (caught) {
    error = caught;
  }
  const pages = pageStackDetails(stack);
  step('page_stack_probe', {
    label,
    attempted_at: attemptedAt,
    page_count: pages.length,
    pages,
    error: error ? String(error.message || error) : null,
  });
  return { stack: Array.isArray(stack) ? stack : [], pages, error };
}

async function currentPageSafe(label, timeoutMs = 7000, { allowEmptyStack = false } = {}) {
  const attemptedAt = now();
  try {
    const page = await withTimeout(miniProgram.currentPage(), timeoutMs, `${label} currentPage`);
    step('current_page_attempt', {
      label,
      attempted_at: attemptedAt,
      path: page && page.path ? page.path : null,
      ok: Boolean(page),
    });
    return page || null;
  } catch (error) {
    step('current_page_attempt', {
      label,
      attempted_at: attemptedAt,
      ok: false,
      error: String(error && error.message ? error.message : error),
    });
    return null;
  }
}

async function waitForPageListStable(timeoutMs = 60000) {
  let previousSignature = null;
  let stableCount = 0;
  const deadline = Date.now() + timeoutMs;
  let lastState = { stack: [], pages: [], error: null };
  while (Date.now() < deadline) {
    const state = await pageStackSnapshot('wait_for_page_list');
    lastState = state;
    const signature = JSON.stringify(state.pages);
    if (state.pages.length && signature === previousSignature) {
      stableCount += 1;
    } else {
      previousSignature = signature;
      stableCount = 0;
    }
    if (stableCount >= 1) return { ...state, stable: true };
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  step('page_list_wait_timeout', {
    page_count: lastState.pages.length,
    pages: lastState.pages,
    proceeding_to_relaunch: true,
  });
  return { ...lastState, stable: false };
}

async function waitForReadyPage(expectedPath, timeoutMs = 60000) {
  const normalized = normalizePagePath(expectedPath);
  let attempt = 0;
  return waitUntil(`ready page ${normalized}`, async () => {
    attempt += 1;
    const page = await currentPageSafe(
      `wait_for_ready_page:${normalized}:attempt_${attempt}`,
      7000,
      { allowEmptyStack: true },
    );
    return page && normalizePagePath(page.path) === normalized ? page : null;
  }, timeoutMs, 1000);
}

async function connectWithRetry({ retries = 5, timeoutMs = 20000, skipSdkWait = false } = {}) {
  let lastError = null;
  for (let attempt = 1; attempt <= retries; attempt += 1) {
    try {
      if (!skipSdkWait || attempt > 1) {
        const toolInfo = await waitForAutomationSdkVersion(timeoutMs);
        step('automation_sdk_ready_before_connect', { attempt, ...toolInfo });
      } else {
        step('automation_sdk_ready_before_connect_skipped', { attempt });
      }
      const connected = await withTimeout(
        automator.connect({ wsEndpoint: `ws://127.0.0.1:${automationPort}` }),
        timeoutMs,
        `automator.connect attempt ${attempt}`,
      );
      miniProgram = connected;
      if (
        mode === 'auth' ||
        mode === 'core' ||
        mode === 'crud' ||
        mode === 'recovery' ||
        mode === 'system-validation'
      ) attachObservers(miniProgram);
      step('automation_connected', {
        attempt,
        service_port: servicePort,
        automation_port: automationPort,
      });
      return miniProgram;
    } catch (error) {
      lastError = error;
      step('automation_connect_retry', {
        attempt,
        retries,
        error: String(error && error.message ? error.message : error),
      });
      if (miniProgram) {
        try {
          miniProgram.disconnect();
        } catch (_) {}
        miniProgram = null;
      }
      await new Promise((resolve) => setTimeout(resolve, Math.min(2000 * attempt, 5000)));
    }
  }
  throw new Error(`Unable to connect to WeChat automation after ${retries} attempts: ${lastError ? lastError.message : 'unknown error'}`);
}

async function safeReLaunch(expectedPath, timeoutMs = 90000) {
  const normalized = normalizePagePath(expectedPath);
  step('relaunch_before', {
    target: normalized,
  });

  let lastError = null;
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    try {
      step('relaunch_attempt', { attempt, target: normalized });
      const relaunched = await withTimeout(
        miniProgram.evaluate(function (targetUrl) {
          return new Promise(function (resolve) {
            wx.reLaunch({
              url: targetUrl,
              success: function () { resolve({ ok: true }); },
              fail: function (error) { resolve({ ok: false, error: error && error.errMsg ? error.errMsg : String(error) }); },
            });
          });
        }, `/${normalized}`),
        30000,
        `runtime wx.reLaunch attempt ${attempt}`,
      );
      if (!relaunched || relaunched.ok !== true) {
        step('runtime_relaunch_failed', { attempt, target: normalized, result: relaunched || null });
        // automator.reLaunch() calls currentPage() before wx.reLaunch. That is
        // unsafe while DevTools has a connection but no current runtime page, so
        // keep it as a secondary path only after runtime wx.reLaunch fails.
        await withTimeout(
          miniProgram.callWxMethod('reLaunch', { url: `/${normalized}` }),
          30000,
          `raw reLaunch attempt ${attempt}`,
        );
      }
      const page = await waitForReadyPage(normalized, timeoutMs);
      step('relaunch_after', {
        attempt,
        target: normalized,
        path: page && page.path ? page.path : null,
      });
      return page;
    } catch (error) {
      lastError = error;
      step('relaunch_retry', {
        attempt,
        target: normalized,
        error: String(error && error.message ? error.message : error),
      });
      if (attempt < 2) {
        await reopenRuntimeProject(`relaunch_reopen_${attempt}`);
      }
    }
  }
  throw new Error(`Unable to reLaunch ${normalized}: ${lastError ? lastError.message : 'unknown error'}`);
}

async function waitForPage(expectedPath, timeoutMs = 60000) {
  return waitForReadyPage(expectedPath, timeoutMs);
}

async function navigateBackTo(expectedPath, timeoutMs = 30000) {
  const normalized = normalizePagePath(expectedPath);
  try {
    const returnedPage = await withTimeout(
      miniProgram.navigateBack(),
      20000,
      `automator.navigateBack:${normalized}`,
    );
    const returnedPath = returnedPage && returnedPage.path
      ? normalizePagePath(returnedPage.path)
      : null;
    step('automator_navigate_back', { target: normalized, returned_path: returnedPath });
    if (returnedPath === normalized) return returnedPage;
    return await waitForReadyPage(normalized, Math.min(timeoutMs, 10000));
  } catch (error) {
    step('automator_navigate_back_fallback_native', {
      target: normalized,
      error: String(error && error.message ? error.message : error),
    });
    try {
      await withTimeout(miniProgram.native().navigateLeft(), 15000, `native.navigateLeft:${normalized}`);
      return await waitForReadyPage(normalized, Math.min(timeoutMs, 15000));
    } catch (nativeError) {
      step('native_navigate_back_fallback_relaunch', {
        target: normalized,
        error: String(nativeError && nativeError.message ? nativeError.message : nativeError),
      });
      if (miniProgram) {
        try { miniProgram.disconnect(); } catch (_) {}
        miniProgram = null;
      }
      await connectWithRetry({ retries: 3, timeoutMs: 20000 });
      return safeReLaunch(normalized, Math.max(timeoutMs, 60000));
    }
  }
}

async function currentPageOrReLaunch(expectedPath, timeoutMs = 90000) {
  const normalized = normalizePagePath(expectedPath);
  const current = await currentPageSafe(
    `reuse_current_page:${normalized}`,
    10000,
    { allowEmptyStack: true },
  );
  if (current && normalizePagePath(current.path) === normalized) {
    step('reused_current_page', { target: normalized, path: current.path });
    return current;
  }
  try {
    const direct = await withTimeout(
      miniProgram.currentPage(),
      30000,
      `direct currentPage:${normalized}`,
    );
    if (direct && normalizePagePath(direct.path) === normalized) {
      step('reused_current_page_direct', { target: normalized, path: direct.path });
      return direct;
    }
  } catch (error) {
    step('direct_current_page_failed', {
      target: normalized,
      error: String(error && error.message ? error.message : error),
    });
  }
  return safeReLaunch(normalized, timeoutMs);
}

async function firstElement(page, selectors) {
  for (const selector of selectors) {
    const element = await page.$(selector);
    if (element) return { selector, element };
  }
  throw new Error(`No element found for selectors: ${selectors.join(', ')}`);
}

async function tap(page, selectors, label) {
  const selected = await firstElement(page, selectors);
  await selected.element.tap();
  step(label, { selector: selected.selector });
}

async function input(page, selector, value, label) {
  const element = await page.$(selector);
  if (!element || typeof element.input !== 'function') {
    throw new Error(`Input element not found: ${selector}`);
  }
  await element.input(value);
  step(label, { selector, value_length: String(value).length });
}

async function tapCardByText(page, text, label) {
  const items = await page.$$('.home-card-item');
  for (const item of items) {
    const itemText = await item.text();
    if (String(itemText || '').indexOf(text) >= 0) {
      await item.tap();
      step(label, { text, matched: true });
      return;
    }
  }
  throw new Error(`Card item not found for text: ${text}`);
}

async function screenshot(name) {
  const target = path.join(artifactDir, `${name}.png`);
  await miniProgram.screenshot({ path: target });
  const stat = fs.statSync(target);
  if (stat.size < 1000) throw new Error(`Screenshot is unexpectedly small: ${target}`);
  step('screenshot', { name, path: target, bytes: stat.size });
  return { path: target, bytes: stat.size };
}

async function validationSnapshot(page) {
  const form = (await page.data('form')) || {};
  return {
    status: String((await page.data('validationStatus')) || ''),
    input: String(form.englishText || ''),
    category: String(form.category || ''),
    issues: (await page.data('validationIssues')) || [],
    visibleIssues: (await page.data('validationVisibleIssues')) || [],
    hiddenCount: Number((await page.data('validationHiddenCount')) || 0),
    formatMessage: String((await page.data('validationFormatMessage')) || ''),
    canSave: await page.data('validationCanSave'),
    canAnalyze: await page.data('validationCanAnalyze'),
    canPronounce: await page.data('validationCanPronounce'),
  };
}

async function waitForValidation(page, label, predicate, timeoutMs = 20000) {
  let latest = null;
  try {
    return await waitUntil(label, async () => {
      const state = await validationSnapshot(page);
      latest = state;
      return predicate(state) ? state : false;
    }, timeoutMs, 100);
  } catch (error) {
    step('validation_wait_timeout_snapshot', {
      label,
      latest,
      error: String(error && error.message ? error.message : error),
    });
    throw error;
  }
}

function hasValidationIssue(state, type) {
  return Array.isArray(state && state.issues) && state.issues.some((item) => item && item.type === type);
}

function countAiRequestSince(index) {
  return countConsoleSince(index, /stream_http_start/) +
    countConsoleSince(index, /request_start.*\/api\/analyze-english/);
}

async function runValidationUx(page, screenshots) {
  const validationLogStart = consoleEvents.length;
  const evidence = {};

  await input(page, '.textarea-english', 'because', 'validation_enter_pass');
  const passState = await waitForValidation(page, 'validation PASS', (state) => state.status === 'pass');
  const passElements = await page.$$('.validation-issue');
  if (passState.visibleIssues.length !== 0 || passElements.length !== 0) {
    throw new Error(`PASS was not silent: ${JSON.stringify(passState)}`);
  }
  evidence.pass_silent = passState;

  await input(page, '.textarea-english', '  absolutely  ', 'validation_enter_normalize');
  evidence.normalization = await waitForValidation(
    page,
    'validation normalization',
    (state) => state.status === 'pass' && state.input === 'absolutely' && state.formatMessage === '已自动整理格式',
  );

  await input(page, '.textarea-english', 'becuase', 'validation_enter_spelling_warning');
  const spellingState = await waitForValidation(
    page,
    'validation spelling warning',
    (state) => state.status === 'warning' && hasValidationIssue(state, 'spelling'),
  );
  const spellingIssue = spellingState.issues.find((item) => item && item.type === 'spelling');
  if (!String((spellingIssue && spellingIssue.detail) || '').includes('because')) {
    throw new Error(`Spelling suggestion was not user-visible: ${JSON.stringify(spellingState)}`);
  }
  if (spellingState.canSave !== true || spellingState.canAnalyze !== false || spellingState.canPronounce !== false) {
    throw new Error(`CONTENT_WARNING capabilities were not consumed: ${JSON.stringify(spellingState)}`);
  }
  screenshots.push(await screenshot('validation-01-spelling-warning'));

  const contentAiLogStart = consoleEvents.length;
  await tap(page, ['.ai-analyze-btn'], 'validation_content_warning_tap_ai');
  await new Promise((resolve) => setTimeout(resolve, 350));
  const contentAiRequests = countAiRequestSince(contentAiLogStart);
  if (contentAiRequests !== 0 || Boolean(await page.data('isAnalyzing'))) {
    throw new Error(`CONTENT_WARNING reached AI: request_events=${contentAiRequests}`);
  }
  evidence.content_warning_blocked = {
    request_events: contentAiRequests,
    state: await validationSnapshot(page),
  };

  await input(page, '.textarea-english', 'because', 'validation_fix_spelling');
  const immediateAfterEdit = await validationSnapshot(page);
  if (immediateAfterEdit.status !== 'idle' || immediateAfterEdit.visibleIssues.length !== 0) {
    throw new Error(`Old warning survived input edit: ${JSON.stringify(immediateAfterEdit)}`);
  }
  evidence.warning_cleared_to_pass = await waitForValidation(
    page,
    'warning changed to PASS',
    (state) => state.status === 'pass' && state.visibleIssues.length === 0,
  );

  await input(page, '.textarea-english', 'This is a complete sentence.', 'validation_enter_category_mismatch');
  const categoryState = await waitForValidation(
    page,
    'validation category auto switch',
    (state) => state.category === '句子'
      && !hasValidationIssue(state, 'category_mismatch')
      && ['pass', 'warning'].includes(state.status),
  );
  evidence.category_action = { auto_switched: true, after: categoryState };

  await input(page, '.textarea-english', 'I goed there yesterday.', 'validation_enter_grammar_warning');
  evidence.grammar_warning = await waitForValidation(
    page,
    'validation grammar warning',
    (state) => state.status === 'warning' && hasValidationIssue(state, 'grammar'),
  );

  await input(page, '.textarea-english', 'Really?!', 'validation_enter_allowed_punctuation');
  const punctuationState = await waitForValidation(
    page,
    'validation allowed punctuation',
    (state) => state.status === 'pass' && state.issues.length === 0 && state.visibleIssues.length === 0,
  );
  evidence.punctuation_allowed = punctuationState;

  if (punctuationState.canAnalyze !== true || punctuationState.canPronounce !== true) {
    throw new Error(`Allowed punctuation capabilities were not available: ${JSON.stringify(punctuationState)}`);
  }
  const advisoryAiLogStart = consoleEvents.length;
  await tap(page, ['.ai-analyze-btn'], 'validation_allowed_punctuation_tap_ai');
  await waitUntil('allowed punctuation AI request started', async () => {
    return countAiRequestSince(advisoryAiLogStart) > 0 || Boolean(await page.data('isAnalyzing'));
  }, 30000, 100);
  await waitUntil('allowed punctuation AI completed', async () => {
    return !Boolean(await page.data('isAnalyzing')) && countAiRequestSince(advisoryAiLogStart) > 0;
  }, 180000, 150);
  evidence.allowed_punctuation_ai = {
    request_events: countAiRequestSince(advisoryAiLogStart),
    state: await validationSnapshot(page),
  };

  await input(page, '.textarea-english', 'because', 'validation_prepare_word_category');
  evidence.word_category_auto_switch = await waitForValidation(
    page,
    'word category auto switch',
    (state) => state.category === '单词' && state.status === 'pass' && !hasValidationIssue(state, 'category_mismatch'),
  );

  const longMultipleWarning = Array(9).fill('I goed there yesterday.').join(' ');
  await input(page, '.textarea-english', longMultipleWarning, 'validation_enter_multiple_warnings');
  const multipleState = await waitForValidation(
    page,
    'validation multiple warnings',
    (state) => state.status === 'warning' && state.issues.length >= 3 && state.visibleIssues.length === 2 && state.hiddenCount === state.issues.length - 2,
  );
  screenshots.push(await screenshot('validation-02-multiple-warnings'));
  evidence.multiple_warnings = multipleState;

  const invalidAiLogStart = consoleEvents.length;
  await input(page, '.textarea-english', '我的', 'validation_enter_invalid_for_rapid_ai');
  await tap(page, ['.ai-analyze-btn'], 'validation_rapid_ai_tap');
  const invalidAiState = await waitForValidation(page, 'rapid AI INVALID', (state) => state.status === 'invalid');
  await new Promise((resolve) => setTimeout(resolve, 350));
  const invalidAiRequests = countAiRequestSince(invalidAiLogStart);
  if (invalidAiRequests !== 0 || Boolean(await page.data('isAnalyzing'))) {
    throw new Error(`INVALID reached AI: request_events=${invalidAiRequests}`);
  }
  evidence.invalid_ai_blocked = invalidAiState;

  await input(page, '.textarea-english', 'because', 'validation_fix_invalid');
  const immediateAfterInvalidEdit = await validationSnapshot(page);
  if (immediateAfterInvalidEdit.status !== 'idle') {
    throw new Error(`INVALID did not clear immediately after edit: ${JSON.stringify(immediateAfterInvalidEdit)}`);
  }
  evidence.invalid_cleared_to_pass = await waitForValidation(
    page,
    'INVALID changed to PASS',
    (state) => state.status === 'pass',
  );

  await input(page, '.input-source', 'Validation UX real DevTools', 'validation_preserve_source');
  await input(page, '.textarea-understanding', '保留我的理解', 'validation_preserve_understanding');
  const notesInputs = await page.$$('textarea.textarea');
  if (notesInputs.length < 3) throw new Error('Validation UX notes textarea was not found');
  await notesInputs[notesInputs.length - 1].input('保留我的备注');
  step('validation_preserve_notes', { value_length: 6 });

  const invalidSaveLogStart = consoleEvents.length;
  await input(page, '.textarea-english', '12345', 'validation_enter_invalid_for_rapid_save');
  await tap(page, ['.primary-btn.submit-btn'], 'validation_rapid_save_tap');
  const invalidSaveState = await waitForValidation(page, 'rapid save INVALID', (state) => state.status === 'invalid');
  await waitUntil('rapid invalid save settled', async () => !Boolean(await page.data('isSaving')), 10000, 100);
  const preservedForm = await page.data('form');
  const current = await currentPageSafe('validation:invalid_save_stays_on_add');
  const invalidSaveRequests = countConsoleSince(invalidSaveLogStart, /request_start.*\/api\/cards/);
  if (
    !current || normalizePagePath(current.path) !== 'pages/add/add' || invalidSaveRequests !== 0 ||
    preservedForm.whereEncountered !== 'Validation UX real DevTools' ||
    preservedForm.myUnderstanding !== '保留我的理解' ||
    preservedForm.notes !== '保留我的备注'
  ) {
    throw new Error(`INVALID save did not preserve the form: ${JSON.stringify({ invalidSaveRequests, preservedForm })}`);
  }
  screenshots.push(await screenshot('validation-03-invalid-save-blocked'));
  evidence.invalid_save_blocked = { state: invalidSaveState, preservedForm, request_events: invalidSaveRequests };

  await input(page, '.textarea-english', 'becuase', 'validation_restore_warning_for_save');
  evidence.warning_ready_for_save = await waitForValidation(
    page,
    'WARNING ready for save',
    (state) => state.status === 'warning' && hasValidationIssue(state, 'spelling'),
  );
  evidence.validation_request_events = countConsoleSince(validationLogStart, /request_start.*\/api\/validate-english/);
  if (evidence.validation_request_events < 8) {
    throw new Error(`Too few real validation requests: ${evidence.validation_request_events}`);
  }
  evidence.status = 'PASS';
  return evidence;
}

async function storageState() {
  return miniProgram.evaluate(function () {
    const accessToken = wx.getStorageSync('backendAccessToken');
    const loginAt = wx.getStorageSync('backendLoginAt');
    return {
      hasAccessToken: Boolean(accessToken),
      hasUserId: Boolean(wx.getStorageSync('backendUserId')),
      accessTokenLength: accessToken ? String(accessToken).length : 0,
      loginAtPresent: Boolean(loginAt),
    };
  });
}

async function diagnosticState() {
  return miniProgram.evaluate(function () {
    const entries = wx.getStorageSync('__level7Diagnostics');
    return Array.isArray(entries) ? entries : [];
  });
}

function writeCliEvidence(completed, command) {
  fs.writeFileSync(path.join(artifactDir, 'wechat-cli-auto-command.txt'), `${command}\n`, 'utf8');
  fs.writeFileSync(path.join(artifactDir, 'wechat-cli-auto.stdout.log'), completed.stdout || '', 'utf8');
  fs.writeFileSync(path.join(artifactDir, 'wechat-cli-auto.stderr.log'), completed.stderr || '', 'utf8');
  fs.writeFileSync(
    path.join(artifactDir, 'wechat-cli-auto-result.json'),
    `${JSON.stringify({ exit_code: completed.status, signal: completed.signal || null, error: completed.error ? String(completed.error.message || completed.error) : null }, null, 2)}\n`,
    'utf8',
  );
}

function runtimeProjectManifest() {
  const appJsonPath = path.join(runtimeProject, 'app.json');
  const projectConfigPath = path.join(runtimeProject, 'project.config.json');
  const indexJsPath = path.join(runtimeProject, 'pages', 'index', 'index.js');
  const bootstrapJsPath = path.join(runtimeProject, 'pages', 'e2e_bootstrap', 'index.js');
  const out = {
    app_json_exists: fs.existsSync(appJsonPath),
    project_config_exists: fs.existsSync(projectConfigPath),
    index_js_exists: fs.existsSync(indexJsPath),
    bootstrap_js_exists: fs.existsSync(bootstrapJsPath),
  };
  if (out.app_json_exists) {
    const appJson = JSON.parse(fs.readFileSync(appJsonPath, 'utf8'));
    out.pages = appJson.pages || [];
    out.first_page = out.pages[0] || null;
  }
  if (out.project_config_exists) {
    const projectConfig = JSON.parse(fs.readFileSync(projectConfigPath, 'utf8'));
    out.appid_present = Boolean(projectConfig.appid);
    out.libVersion = projectConfig.libVersion || null;
    out.compileType = projectConfig.compileType || null;
  }
  return out;
}

function automationPortListening() {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host: '127.0.0.1', port: automationPort });
    const finish = (value) => {
      socket.removeAllListeners();
      socket.destroy();
      resolve(value);
    };
    socket.setTimeout(1000);
    socket.once('connect', () => finish(true));
    socket.once('timeout', () => finish(false));
    socket.once('error', () => finish(false));
  });
}

function launchWechatCliAuto(phase) {
  step(`wechat_processes_before_cli_auto_${phase}`, {
    automation_port_owner: processForAutomationPort(),
    processes: listWechatProcesses(),
  });
  const command = `& "${cliPath}" auto --project "${runtimeProject}" --auto-port ${automationPort} --trust-project`;
  const launcherPath = path.join(artifactDir, 'launch-wechat-automation.ps1');
  fs.writeFileSync(
    launcherPath,
    [
      'param([string]$CliPath, [string]$ProjectPath, [int]$AutomationPort)',
      '$ErrorActionPreference = \'Continue\'',
      '& $CliPath auto --project $ProjectPath --auto-port $AutomationPort --trust-project',
      'exit $LASTEXITCODE',
      '',
    ].join('\n'),
    'utf8',
  );
  const completed = childProcess.spawnSync('powershell.exe', [
    '-NoProfile',
    '-NonInteractive',
    '-ExecutionPolicy',
    'Bypass',
    '-File',
    launcherPath,
    '-CliPath',
    cliPath,
    '-ProjectPath',
    runtimeProject,
    '-AutomationPort',
    String(automationPort),
  ], {
    cwd: runtimeProject,
    encoding: 'utf8',
    timeout: 120000,
    windowsHide: true,
  });
  writeCliEvidence(completed, command);
  step('wechat_cli_auto_finished', { phase, exit_code: completed.status, stdout_bytes: Buffer.byteLength(completed.stdout || ''), stderr_bytes: Buffer.byteLength(completed.stderr || '') });
  return completed;
}

async function waitForAutomationPortReleased(timeoutMs = 30000) {
  return waitUntil('automation TCP listener released', async () => {
    return !(await automationPortListening());
  }, timeoutMs, 500);
}

async function launchCore() {
  const runtime = prepareRuntimeProject();
  step('runtime_project_prepared', runtime);
  step('runtime_project_manifest', runtimeProjectManifest());
  launchWechatCliAuto('primary');
  await waitUntil('automation TCP listener', automationPortListening, 120000, 1000);
  step('wechat_processes_after_cli_auto_primary', {
    automation_port_owner: processForAutomationPort(),
    processes: listWechatProcesses(),
  });
  const toolInfo = await waitForAutomationSdkVersion(120000);
  step('automation_sdk_ready', toolInfo);
  if (process.env.LEVEL7_ENABLE_RAW_PAGE_DIAG === '1') {
    await rawPageControlSnapshot('after_cli_auto_before_connect');
  } else {
    await warmRuntimeCurrentPageBeforeConnect();
  }
  if (mode === 'launch') {
    step('automation_port_listening', { automation_port: automationPort });
    return runtime;
  }
  let readyPage = null;
  for (let activationAttempt = 1; activationAttempt <= 4; activationAttempt += 1) {
    if (activationAttempt > 1) {
      launchWechatCliAuto(`reopen_${activationAttempt}`);
      await waitUntil(`automation TCP listener reopen ${activationAttempt}`, automationPortListening, 120000, 1000);
      step(`wechat_processes_after_cli_auto_reopen_${activationAttempt}`, {
        automation_port_owner: processForAutomationPort(),
        processes: listWechatProcesses(),
      });
      const reopenToolInfo = await waitForAutomationSdkVersion(120000);
      step(`automation_sdk_ready_after_reopen_${activationAttempt}`, reopenToolInfo);
      await warmRuntimeCurrentPageBeforeConnect();
    }

    await connectWithRetry({ retries: 5, timeoutMs: 20000, skipSdkWait: true });
    readyPage = await currentPageSafe(
      `launch_core_runtime_ready_attempt_${activationAttempt}`,
      activationAttempt === 1 ? 15000 : 30000,
      { allowEmptyStack: true },
    );
    if (readyPage) {
      step('runtime_page_control_ready', {
        activation_attempt: activationAttempt,
        path: readyPage.path || null,
      });
      break;
    }

    step('runtime_page_control_not_ready', {
      activation_attempt: activationAttempt,
      action: activationAttempt < 4 ? 'close_and_reopen_same_runtime_project' : 'fail',
      automation_port_owner: processForAutomationPort(),
    });
    await closeAutomationBestEffort(`runtime_not_ready_attempt_${activationAttempt}`);
    await waitForAutomationPortReleased(30000);
  }
  if (!readyPage) {
    throw new Error('Runtime page control did not become ready after 4 same-runtime activation attempts');
  }
  if (process.env.LEVEL7_ENABLE_RAW_PAGE_DIAG === '1') {
    await rawPageControlSnapshot('after_connect');
  }
  return runtime;
}

async function connectRecovery() {
  await connectWithRetry({ retries: 5, timeoutMs: 20000 });
}

async function runLaunch() {
  const runtime = await launchCore();
  return { status: 'PASS', runtime };
}

async function runPrepare() {
  const runtime = prepareRuntimeProject();
  step('runtime_project_prepared', runtime);
  step('runtime_project_manifest', runtimeProjectManifest());
  return { status: 'PASS', runtime };
}

async function answerCurrentReviewCard(page, label) {
  const card = await page.data('currentCard');
  if (!card || !Array.isArray(card.options)) throw new Error(`${label}: review card/options missing`);
  const optionIndex = card.options.findIndex((option) => option.text === card.myUnderstanding);
  if (optionIndex < 0) throw new Error(`${label}: correct option not identifiable: ${JSON.stringify(card)}`);
  const options = await page.$$('.mcq-option');
  if (!options[optionIndex]) throw new Error(`${label}: rendered option missing at ${optionIndex}`);
  const previousCardId = String(card.cardId || card.id || '');
  await options[optionIndex].tap();
  step(label, { content: card.englishText, option_index: optionIndex });
  await waitUntil(`${label} feedback`, async () => {
    const current = await currentPageSafe(`${label}:feedback`);
    if (!current || normalizePagePath(current.path) !== 'pages/review/review') return false;
    const submitting = Boolean(await current.data('submittingFeedback'));
    const state = String(await current.data('pageState') || '');
    const next = await current.data('currentCard');
    const changed = state === 'completed' || !next || String(next.cardId || next.id || '') !== previousCardId;
    return !submitting && changed ? { state, next } : false;
  }, 45000, 100);
  return card;
}

async function runDiscoveryAndQuoteJourneys(page, screenshots) {
  const quote = await waitUntil('today quote loaded', async () => {
    const item = await page.data('todayQuote');
    return item && item.content && item.chinese ? item : false;
  }, 45000, 150);
  await tap(page, ['#today-quote-remember'], 'today_quote_want_to_remember');
  page = await waitForPage('pages/add/add', 30000);
  const quotePrefill = await waitUntil('today quote add prefill', async () => {
    const form = await page.data('form');
    return form && form.englishText === quote.content && form.myUnderstanding === quote.chinese
      && form.whereEncountered === '今日一句' ? form : false;
  }, 20000);
  screenshots.push(await screenshot('01a-today-quote-prefill'));
  await tap(page, ['.primary-btn.submit-btn'], 'today_quote_save_card');
  page = await waitForPage('pages/index/index', 45000);
  await waitUntil('today quote card on home', async () => {
    const cards = await page.data('cards');
    return Array.isArray(cards) && cards.some((card) => card && card.englishText === quote.content);
  }, 45000);

  await tap(page, ['#discover-entry'], 'open_discovery');
  page = await waitForPage('pages/discover/index', 30000);
  const firstItems = await waitUntil('discovery items loaded', async () => {
    const items = await page.data('items');
    return Array.isArray(items) && items.length >= 2 ? items : false;
  }, 45000, 150);
  const knownItem = firstItems[0];
  const wantedItem = firstItems[1];
  const knownButtons = await page.$$('.known-btn');
  if (!knownButtons[0]) throw new Error('Discovery known button missing');
  await knownButtons[0].tap();
  await waitUntil('known material removed', async () => {
    const items = await page.data('items');
    return Array.isArray(items) && !items.some((item) => String(item.id) === String(knownItem.id));
  }, 30000);
  step('discovery_mark_known', { item_id: knownItem.id, content: knownItem.content });

  const currentItems = await page.data('items');
  const wantedIndex = currentItems.findIndex((item) => String(item.id) === String(wantedItem.id));
  if (wantedIndex < 0) throw new Error('Discovery wanted item disappeared unexpectedly');
  const rememberButtons = await page.$$('.remember-btn');
  if (!rememberButtons[wantedIndex]) throw new Error('Discovery remember button missing');
  await rememberButtons[wantedIndex].tap();
  page = await waitForPage('pages/add/add', 30000);
  const materialPrefill = await waitUntil('discovery add prefill', async () => {
    const form = await page.data('form');
    return form && form.englishText === wantedItem.content && form.myUnderstanding === wantedItem.chinese
      && String(form.whereEncountered || '').startsWith('发现素材 · ') ? form : false;
  }, 20000);
  screenshots.push(await screenshot('01b-discovery-prefill'));
  await tap(page, ['.primary-btn.submit-btn'], 'discovery_save_card');
  // The existing add page correctly returns to its immediate caller. For a
  // discovery-origin card that caller is the discovery page, so the journey
  // must explicitly return one more level before checking the home library.
  page = await waitForPage('pages/discover/index', 45000);
  page = await navigateBackTo('pages/index/index', 30000);
  await waitUntil('discovery card on home', async () => {
    const cards = await page.data('cards');
    return Array.isArray(cards) && cards.some((card) => card && card.englishText === wantedItem.content);
  }, 45000);
  screenshots.push(await screenshot('01c-discovery-cards-saved'));

  await tap(page, ['.review-main-btn'], 'discovery_open_review');
  page = await waitForPage('pages/review/review', 45000);
  await waitUntil('discovery review active', async () => (await page.data('pageState')) === 'active', 45000);
  const reviewed = [];
  for (let index = 0; index < 6; index += 1) {
    const state = String(await page.data('pageState') || '');
    if (state === 'completed') break;
    reviewed.push(await answerCurrentReviewCard(page, `discovery_review_answer_${index + 1}`));
  }
  if (!reviewed.some((card) => card.englishText === quote.content)) {
    throw new Error('Today quote card did not enter the real review/FSRS journey');
  }
  if (!reviewed.some((card) => card.englishText === wantedItem.content)) {
    throw new Error('Discovery material card did not enter the real review/FSRS journey');
  }
  screenshots.push(await screenshot('01d-discovery-review-complete'));
  page = await safeReLaunch('pages/index/index', 60000);
  await waitForPage('pages/index/index', 30000);
  return {
    page,
    evidence: {
      quote: { id: quote.id, content: quote.content, prefill: quotePrefill, reviewed: true },
      material: { id: wantedItem.id, content: wantedItem.content, prefill: materialPrefill, reviewed: true },
      known: { id: knownItem.id, content: knownItem.content, removed_from_feed: true },
    },
  };
}

async function runCore() {
  const runtime = await launchCore();
  const screenshots = [];
  const authLogStart = consoleEvents.length;

  let page = await safeReLaunch('pages/index/index');

  const authState = await waitUntil('real backend auth storage', async () => {
    const state = await storageState();
    return state.hasAccessToken && state.hasUserId ? state : null;
  }, 45000);
  await waitUntil('empty isolated card library', async () => {
    const current = await currentPageSafe('core:empty_card_library');
    if (!current) return false;
    const total = Number(await current.data('totalCardCount'));
    return total === 0;
  }, 30000);
  screenshots.push(await screenshot('01-home-authenticated'));
  step('real_auth_ready', authState);

  const discoveryResult = await runDiscoveryAndQuoteJourneys(page, screenshots);
  page = discoveryResult.page;

  const initialAuthEvidence = {
    wx_login_start: countConsoleSince(authLogStart, /wx_login_start/),
    wx_login_success: countConsoleSince(authLogStart, /wx_login_success/),
    code2session_request_start: countConsoleSince(authLogStart, /code2session_request_start/),
    jwt_saved: countConsoleSince(authLogStart, /jwt_saved/),
    auth_refresh_start: countConsoleSince(authLogStart, /auth_refresh_start/),
    auth_refresh_join: countConsoleSince(authLogStart, /auth_refresh_join/),
  };
  if (
    initialAuthEvidence.wx_login_start < 1 ||
    initialAuthEvidence.wx_login_success < 1 ||
    initialAuthEvidence.code2session_request_start < 1 ||
    initialAuthEvidence.jwt_saved < 1
  ) {
    throw new Error(`Real auth diagnostics incomplete: ${JSON.stringify(initialAuthEvidence)}`);
  }

  await tap(page, ['.first-card-btn', '.add-main-btn'], 'open_add_page');
  page = await waitForPage('pages/add/add');
  await waitUntil('add page non-critical controls', async () => Boolean(await page.data('deferNonCriticalReady')), 15000);
  const validationEvidence = await runValidationUx(page, screenshots);
  await input(page, '.input-source', 'Level 7 WeChat DevTools E2E', 'enter_initial_source');
  await input(page, '.textarea-understanding', '短暂存在的', 'enter_initial_understanding');
  const notesInputs = await page.$$('textarea.textarea');
  if (notesInputs.length < 3) throw new Error('Notes textarea was not found');
  await notesInputs[notesInputs.length - 1].input('created through the real mini program UI');
  step('enter_initial_notes', { value_length: 48 });
  await tap(page, ['.primary-btn.submit-btn'], 'save_initial_card');
  page = await waitForPage('pages/index/index', 45000);

  await waitUntil('created card on home page', async () => {
    const cards = await page.data('cards');
    return Array.isArray(cards) && cards.some((card) => card && card.englishText === 'becuase');
  }, 45000);
  screenshots.push(await screenshot('02-card-created'));

  // `becuase` is intentionally a CONTENT_WARNING sample. The current product
  // saves it but does not start background AI, so waiting for a local AI
  // settlement would test a retired behavior. The durable Card has already
  // been proved above; capture the client state and continue to sync replay.
  const initialCards = await page.data('cards');
  const initialCard = Array.isArray(initialCards)
    ? initialCards.find((item) => item && item.englishText === 'becuase')
    : null;
  if (!initialCard) throw new Error('Created CONTENT_WARNING card disappeared from home state');
  step('initial_content_warning_card_ready', {
    analysis_status: initialCard.analysisStatus || '',
    sync_status: initialCard.backend_sync_status || initialCard.syncStatus || '',
  });

  // E2E-only setup for a real client sync replay: recreate the durable local
  // state left by an interrupted create after the backend has already committed.
  // The replay itself is triggered by leaving and returning to the real Home UI.
  const syncReplaySetup = await miniProgram.evaluate(function () {
    const userId = String(wx.getStorageSync('backendUserId') || '').trim();
    const storageKey = userId ? `cardsCache:user:${userId}` : 'cardsCache:anonymous';
    const cards = wx.getStorageSync(storageKey);
    if (!Array.isArray(cards)) return { prepared: false, reason: 'scoped cardsCache missing', storageKey };
    const index = cards.findIndex(function (card) { return card && card.englishText === 'becuase'; });
    if (index < 0) return { prepared: false, reason: 'created card missing' };
    cards[index].backend_sync_status = 'pending';
    cards[index].backend_card_id = '';
    cards[index].backend_synced_at = '';
    cards[index].backend_sync_error = 'level7_e2e_interrupted_create';
    wx.setStorageSync(storageKey, cards);
    return {
      prepared: true,
      hasLocalTempId: Boolean(cards[index].local_temp_id),
      content: cards[index].englishText,
      storageKey,
    };
  });
  if (!syncReplaySetup || !syncReplaySetup.prepared || !syncReplaySetup.hasLocalTempId) {
    throw new Error(`Could not prepare isolated client sync replay: ${JSON.stringify(syncReplaySetup)}`);
  }
  step('prepare_interrupted_create_replay_state', syncReplaySetup);
  const syncReplayLogStart = consoleEvents.length;
  await tap(page, ['.header-settings'], 'open_settings_before_sync_replay');
  await waitForPage('pages/settings/index', 20000);
  page = await navigateBackTo('pages/index/index', 30000);
  await waitUntil('real client sync replay success', async () => {
    const cards = await page.data('cards');
    const card = Array.isArray(cards) ? cards.find((item) => item && item.englishText === 'becuase') : null;
    const logs = consoleEvents.slice(syncReplayLogStart).map(consoleText).join('\n');
    return card && card.backend_sync_status === 'synced' && /sync complete synced[^0-9]*1[^0-9]+failed[^0-9]*0/.test(logs);
  }, 60000, 250);
  const syncReplayEvidence = {
    setup: syncReplaySetup,
    pending_found_events: countConsoleSince(syncReplayLogStart, /found pending cards count[^0-9]*1/),
    synced_one_events: countConsoleSince(syncReplayLogStart, /sync complete synced[^0-9]*1[^0-9]+failed[^0-9]*0/),
  };

  await tap(page, ['.home-card-item'], 'reopen_created_card');
  page = await waitForPage('pages/add/add');
  await waitUntil('edit card loaded', async () => Boolean(await page.data('isEdit')), 20000);
  const rereadBeforeEdit = await page.data('form');
  if (!rereadBeforeEdit || rereadBeforeEdit.englishText !== 'becuase') {
    throw new Error('Created card could not be re-read through the Card UI');
  }

  await input(page, '.textarea-english', 'ineffable', 'modify_card_text');
  await input(page, '.input-source', 'Level 7 edited through UI', 'modify_card_source');
  await input(page, '.textarea-understanding', '美好得难以言喻', 'modify_card_understanding');

  const cancelledStreamLogStart = consoleEvents.length;
  await tap(page, ['.ai-analyze-btn'], 'tap_ai_analyze_generation_a');
  await waitUntil('generation A produced a real delta', async () => {
    return Boolean(await page.data('isAnalyzing'))
      && countConsoleSince(cancelledStreamLogStart, /stream_delta/) > 0;
  }, 60000, 100);
  await tap(page, ['.ai-analyze-cancel'], 'cancel_ai_generation_a');
  await waitUntil('generation A cancelled', async () => !Boolean(await page.data('isAnalyzing')), 30000, 100);
  const cancellationEvidence = {
    delta_events: countConsoleSince(cancelledStreamLogStart, /stream_delta/),
    final_events_before_regenerate: countConsoleSince(cancelledStreamLogStart, /stream_final/),
    done_events_before_regenerate: countConsoleSince(cancelledStreamLogStart, /stream_done/),
  };
  if (
    cancellationEvidence.delta_events < 1
    || cancellationEvidence.final_events_before_regenerate !== 0
    || cancellationEvidence.done_events_before_regenerate !== 0
  ) {
    throw new Error(`Generation A cancellation invariant failed: ${JSON.stringify(cancellationEvidence)}`);
  }

  const streamLogStart = consoleEvents.length;
  const streamStartedAt = Date.now();
  await tap(page, ['.ai-analyze-btn'], 'tap_ai_analyze_generation_b');
  let sawAnalyzing = false;
  let progressive = null;
  let finalState = null;
  await waitUntil('streaming analysis completion', async () => {
    const state = {
      at_ms: Date.now() - streamStartedAt,
      isAnalyzing: Boolean(await page.data('isAnalyzing')),
      suggestionText: String((await page.data('suggestionText')) || ''),
      aiExampleSentence: String((await page.data('aiExampleSentence')) || ''),
      aiUsageScenario: String((await page.data('aiUsageScenario')) || ''),
      aiAnalysisModel: String((await page.data('aiAnalysisModel')) || ''),
      aiAnalysisSource: String((await page.data('aiAnalysisSource')) || ''),
    };
    if (state.isAnalyzing) sawAnalyzing = true;
    const visible = state.suggestionText || state.aiExampleSentence || state.aiUsageScenario;
    if (!progressive && state.isAnalyzing && visible) {
      progressive = state;
    }
    if (sawAnalyzing && !state.isAnalyzing) {
      finalState = state;
      return state;
    }
    return false;
  }, 180000, 100);

  const streamEvidence = {
    request_started_at: new Date(streamStartedAt).toISOString(),
    chunk_events: countConsoleSince(streamLogStart, /stream_chunk/),
    delta_events: countConsoleSince(streamLogStart, /stream_delta/),
    field_events: countConsoleSince(streamLogStart, /stream_field/),
    final_events: countConsoleSince(streamLogStart, /stream_final/),
    done_events: countConsoleSince(streamLogStart, /stream_done/),
    first_visible_events: countConsoleSince(streamLogStart, /first_visible_text/),
    progressive_before_complete: Boolean(progressive),
    progressive_snapshot: progressive,
    final_snapshot: finalState,
    cancel_regenerate: cancellationEvidence,
  };
  if (streamEvidence.chunk_events < 2) {
    throw new Error(`onChunkReceived delivered fewer than two chunks: ${JSON.stringify(streamEvidence)}`);
  }
  if (!streamEvidence.progressive_before_complete || !finalState || !finalState.suggestionText) {
    throw new Error(`Progressive UI evidence incomplete: ${JSON.stringify(streamEvidence)}`);
  }
  screenshots.push(await screenshot('03-ai-stream-complete'));

  await tap(page, ['.primary-btn.submit-btn'], 'save_modified_card');
  page = await waitForPage('pages/index/index', 45000);
  await waitUntil('modified card on home page', async () => {
    const cards = await page.data('cards');
    return Array.isArray(cards) && cards.some((card) => card && card.englishText === 'ineffable');
  }, 45000);
  const syncTriggered = await waitUntil('home sync trigger', async () => {
    return countConsoleSince(0, /phase6h-pending-sync/) > 0;
  }, 15000);
  step('home_sync_observed', { observed: Boolean(syncTriggered) });

  await tap(page, ['.home-card-item'], 'reopen_modified_card');
  page = await waitForPage('pages/add/add');
  await waitUntil('modified edit card loaded', async () => Boolean(await page.data('isEdit')), 20000);
  const rereadAfterEdit = await page.data('form');
  if (!rereadAfterEdit || rereadAfterEdit.englishText !== 'ineffable') {
    throw new Error('Modified card could not be re-read through the Card UI');
  }

  await waitUntil('TTS control ready', async () => {
    const text = String((await page.data('pronunciationText')) || '');
    const loading = Boolean(await page.data('lexicalInfoLoading'));
    return text === 'ineffable' && !loading;
  }, 30000);
  const ttsLogStart = consoleEvents.length;
  await tap(page, ['.original-pronunciation-btn'], 'tap_tts');
  let sawTtsLoading = false;
  let sawTtsPlaying = false;
  await waitUntil('TTS client completion', async () => {
    const loading = Boolean(await page.data('pronunciationLoading'));
    const playing = Boolean(await page.data('pronunciationPlaying'));
    if (loading) sawTtsLoading = true;
    if (playing) sawTtsPlaying = true;
    return sawTtsLoading && !loading ? { loading, playing } : false;
  }, 60000, 100);
  if (!sawTtsPlaying) {
    try {
      await waitUntil('InnerAudioContext onPlay', async () => {
        sawTtsPlaying = Boolean(await page.data('pronunciationPlaying'));
        return sawTtsPlaying;
      }, 5000, 100);
    } catch (_) {
      // The evidence check below records the boundary without hiding it.
    }
  }
  const ttsEvidence = {
    loading_observed: sawTtsLoading,
    playing_observed: sawTtsPlaying,
    console_errors: consoleEvents.slice(ttsLogStart).filter((entry) => /pronunciation.*(error|failed)/i.test(consoleText(entry))).length,
  };
  if (!ttsEvidence.playing_observed || ttsEvidence.console_errors > 0) {
    throw new Error(`TTS client decode/onPlay evidence failed: ${JSON.stringify(ttsEvidence)}`);
  }
  screenshots.push(await screenshot('04-tts-client'));

  await input(page, '.textarea-english', '我的', 'validation_enter_invalid_for_tts');
  const invalidTtsState = await waitForValidation(page, 'TTS INVALID', (state) => state.status === 'invalid');
  await waitUntil('invalid TTS control settled', async () => !Boolean(await page.data('lexicalInfoLoading')), 30000, 100);
  const invalidTtsLogStart = consoleEvents.length;
  await tap(page, ['.original-pronunciation-btn'], 'validation_invalid_tts_tap');
  const invalidTtsRequests = countConsoleSince(invalidTtsLogStart, /button_clicked|"event":"download_start"/);
  if (invalidTtsRequests !== 0) {
    throw new Error(`INVALID reached TTS: request_events=${invalidTtsRequests}`);
  }
  await input(page, '.textarea-english', 'ineffable', 'validation_fix_invalid_tts');
  const ttsValidationRecovered = await waitForValidation(
    page,
    'TTS INVALID changed to PASS',
    (state) => state.status === 'pass',
  );
  ttsEvidence.invalid_blocked = {
    state: invalidTtsState,
    request_events: invalidTtsRequests,
    recovered: ttsValidationRecovered,
  };

  await input(page, '.textarea-english', 'becuase', 'validation_enter_content_warning_for_tts');
  const contentWarningTtsState = await waitForValidation(
    page,
    'TTS CONTENT_WARNING',
    (state) => state.status === 'warning' && state.canPronounce === false,
  );
  const contentWarningTtsLogStart = consoleEvents.length;
  await tap(page, ['.original-pronunciation-btn'], 'validation_content_warning_tts_tap');
  const contentWarningTtsRequests = countConsoleSince(
    contentWarningTtsLogStart,
    /button_clicked|"event":"download_start"/,
  );
  if (contentWarningTtsRequests !== 0) {
    throw new Error(`CONTENT_WARNING reached TTS: request_events=${contentWarningTtsRequests}`);
  }
  ttsEvidence.content_warning_blocked = {
    state: contentWarningTtsState,
    request_events: contentWarningTtsRequests,
  };
  await input(page, '.textarea-english', 'ineffable', 'validation_recover_content_warning_tts');
  await waitForValidation(page, 'TTS CONTENT_WARNING recovered', (state) => state.status === 'pass');

  page = await navigateBackTo('pages/index/index', 30000);
  await tap(page, ['.review-main-btn'], 'open_review');
  page = await waitForPage('pages/review/review', 45000);
  await waitUntil('active review card', async () => (await page.data('pageState')) === 'active', 45000);
  screenshots.push(await screenshot('05-review-active'));
  const reviewCard = await page.data('currentCard');
  const reviewOptions = await page.$$('.mcq-option');
  if (!reviewOptions.length) {
    throw new Error('Active review card did not render any selectable options');
  }
  const correctOptionIndex = reviewCard.options.findIndex((option) => option.text === reviewCard.myUnderstanding);
  if (correctOptionIndex < 0) {
    throw new Error(`Correct review option was not identifiable from the fixed card fixture: ${JSON.stringify(reviewCard)}`);
  }
  await reviewOptions[correctOptionIndex].tap();
  step('submit_review_option', { option_index: correctOptionIndex });
  await waitUntil('review feedback completion', async () => {
    const current = await currentPageSafe('core:review_feedback_completion');
    if (!current) return false;
    const currentPath = normalizePagePath(current.path);
    if (currentPath === 'pages/today_reviewed/today_reviewed') return current;
    if (currentPath === 'pages/review/review') {
      const submitting = Boolean(await current.data('submittingFeedback'));
      const state = await current.data('pageState');
      return !submitting && (state === 'active' || state === 'completed') ? current : false;
    }
    return false;
  }, 45000);
  screenshots.push(await screenshot('06-review-submitted'));

  let current = await currentPageSafe('core:before_return_home');
  if (current && normalizePagePath(current.path) !== 'pages/index/index') {
    await safeReLaunch('pages/index/index', 60000);
  }
  await waitForPage('pages/index/index', 30000);

  miniProgram.disconnect();
  miniProgram = null;
  step('automation_disconnected_for_backend_invalidation');

  return {
    status: 'PASS',
    runtime,
    auth: initialAuthEvidence,
    card: {
      created_content: 'becuase',
      reread_before_edit: rereadBeforeEdit.englishText,
      modified_content: 'ineffable',
      reread_after_edit: rereadAfterEdit.englishText,
      sync_trigger_observed: Boolean(syncTriggered),
      sync_replay: syncReplayEvidence,
    },
    streaming: streamEvidence,
    validation: validationEvidence,
    tts: ttsEvidence,
    review: { submitted: true },
    discovery: discoveryResult.evidence,
    screenshots,
    console_event_count: consoleEvents.length,
    exception_event_count: exceptionEvents.length,
  };
}

async function runCrud() {
  const runtime = await launchCore();
  const screenshots = [];
  const authLogStart = consoleEvents.length;
  let page = await safeReLaunch('pages/index/index');

  const authState = await waitUntil('real backend auth storage', async () => {
    const state = await storageState();
    return state.hasAccessToken && state.hasUserId ? state : null;
  }, 60000);
  const authEvidence = {
    wx_login_start: countConsoleSince(authLogStart, /wx_login_start/),
    wx_login_success: countConsoleSince(authLogStart, /wx_login_success/),
    code2session_request_start: countConsoleSince(authLogStart, /code2session_request_start/),
    jwt_saved: countConsoleSince(authLogStart, /jwt_saved/),
  };
  if (
    authEvidence.wx_login_start < 1 ||
    authEvidence.wx_login_success < 1 ||
    authEvidence.code2session_request_start < 1 ||
    authEvidence.jwt_saved < 1
  ) {
    throw new Error(`Real auth diagnostics incomplete before CRUD: ${JSON.stringify(authEvidence)}`);
  }

  const rounds = [];
  for (let index = 1; index <= 3; index += 1) {
    const suffix = `${runId.replace(/[^A-Za-z0-9]/g, '').slice(-8)}r${index}`;
    const createText = `levelseven${suffix}`;
    const updateText = `levelseven${suffix}updated`;
    const createSource = `Level 7 CRUD round ${index}`;
    const updateSource = `Level 7 CRUD round ${index} updated`;
    const createUnderstanding = `created through real UI round ${index}`;
    const updateUnderstanding = `updated through real UI round ${index}`;
    const requestStart = consoleEvents.length;

    page = await currentPageOrReLaunch('pages/index/index');
    await tap(page, ['.first-card-btn', '.add-main-btn'], `crud_${index}_open_add_page`);
    page = await waitForPage('pages/add/add');
    await waitUntil(`crud ${index} add page ready`, async () => Boolean(await page.data('deferNonCriticalReady')), 20000);
    await input(page, '.textarea-english', createText, `crud_${index}_input_create_english`);
    await input(page, '.input-source', createSource, `crud_${index}_input_create_source`);
    await input(page, '.textarea-understanding', createUnderstanding, `crud_${index}_input_create_understanding`);
    await tap(page, ['.primary-btn.submit-btn'], `crud_${index}_tap_create_save`);
    page = await waitForPage('pages/index/index', 45000);
    const createdCard = await waitUntil(`crud ${index} created card in UI`, async () => {
      const cards = await page.data('cards');
      return Array.isArray(cards) ? cards.find((card) => card && card.englishText === createText) : null;
    }, 45000);
    const syncedCreate = await waitUntil(`crud ${index} create synced`, async () => {
      const cards = await miniProgram.evaluate(function () { return wx.getStorageSync('cardsCache'); });
      const card = Array.isArray(cards) ? cards.find((item) => item && item.englishText === createText) : null;
      return card && card.backend_sync_status === 'synced' && card.backend_card_id ? card : false;
    }, 45000, 250);
    if (index === 1) screenshots.push(await screenshot('crud-01-created'));

    await tapCardByText(page, createText, `crud_${index}_tap_created_card`);
    page = await waitForPage('pages/add/add', 30000);
    await waitUntil(`crud ${index} read created form`, async () => {
      const form = await page.data('form');
      return form && form.englishText === createText ? form : false;
    }, 20000);
    const readCreateForm = await page.data('form');

    await input(page, '.textarea-english', updateText, `crud_${index}_input_update_english`);
    await input(page, '.input-source', updateSource, `crud_${index}_input_update_source`);
    await input(page, '.textarea-understanding', updateUnderstanding, `crud_${index}_input_update_understanding`);
    await tap(page, ['.primary-btn.submit-btn'], `crud_${index}_tap_update_save`);
    page = await waitForPage('pages/index/index', 45000);
    const updatedCard = await waitUntil(`crud ${index} updated card in UI`, async () => {
      const cards = await page.data('cards');
      return Array.isArray(cards) ? cards.find((card) => card && card.englishText === updateText) : null;
    }, 45000);
    await waitUntil(`crud ${index} no duplicate visible after update`, async () => {
      const cards = await page.data('cards');
      if (!Array.isArray(cards)) return false;
      const matching = cards.filter((card) => card && (card.englishText === createText || card.englishText === updateText));
      return matching.length === 1 && matching[0].englishText === updateText;
    }, 15000);

    await tapCardByText(page, updateText, `crud_${index}_tap_updated_card`);
    page = await waitForPage('pages/add/add', 30000);
    await waitUntil(`crud ${index} read updated form`, async () => {
      const form = await page.data('form');
      return form && form.englishText === updateText ? form : false;
    }, 20000);
    const readUpdateForm = await page.data('form');
    if (index === 1) screenshots.push(await screenshot('crud-02-updated'));

    await tap(page, ['.danger-btn.submit-btn'], `crud_${index}_tap_delete`);
    await new Promise((resolve) => setTimeout(resolve, 350));
    await miniProgram.native().confirmModal();
    step(`crud_${index}_confirm_delete_modal`);
    page = await waitForPage('pages/index/index', 45000);
    await waitUntil(`crud ${index} deleted card gone from UI`, async () => {
      const cards = await page.data('cards');
      if (!Array.isArray(cards)) return false;
      return !cards.some((card) => card && (card.englishText === createText || card.englishText === updateText));
    }, 45000);
    if (index === 1) screenshots.push(await screenshot('crud-03-deleted'));

    rounds.push({
      round: index,
      created_text: createText,
      updated_text: updateText,
      created_card_id: createdCard.id || '',
      synced_backend_card_id: syncedCreate.backend_card_id || '',
      updated_card_id: updatedCard.id || '',
      read_create_text: readCreateForm.englishText,
      read_update_text: readUpdateForm.englishText,
      request_events: {
        starts: countConsoleSince(requestStart, /request_start/),
        finishes: countConsoleSince(requestStart, /request_finish/),
        failures: countConsoleSince(requestStart, /request_fail/),
      },
    });
  }

  await miniProgram.close();
  miniProgram = null;
  shouldCloseProject = false;
  step('crud_runtime_project_closed');

  return {
    status: 'PASS',
    runtime,
    auth: authEvidence,
    storage: authState,
    rounds,
    screenshots,
    console_event_count: consoleEvents.length,
    exception_event_count: exceptionEvents.length,
  };
}

async function runAuth() {
  await launchCore();
  let page = await currentPageOrReLaunch('pages/index/index');
  step('auth_index_page_ready', { path: page ? page.path : null });

  let testAccounts = null;
  try {
    const accounts = await withTimeout(miniProgram.testAccounts(), 15000, 'testAccounts');
    const list = Array.isArray(accounts) ? accounts : [];
    testAccounts = {
      count: list.length,
      accounts: list.map((account, index) => {
        const rawOpenid = account && (account.openid || account.openId || account.id);
        const rawName = account && (account.nickName || account.nickname || account.name);
        const digest = rawOpenid
          ? require('crypto').createHash('sha256').update(String(rawOpenid)).digest('hex').slice(0, 24)
          : null;
        return {
          index,
          nickName: rawName ? String(rawName) : null,
          openid_hash: digest,
          openid_masked: rawOpenid ? `${String(rawOpenid).slice(0, 3)}...${String(rawOpenid).slice(-3)}` : null,
        };
      }),
    };
    step('test_accounts_observed', { count: testAccounts.count, accounts: testAccounts.accounts });
  } catch (error) {
    testAccounts = {
      count: null,
      accounts: [],
      error: String(error && error.message ? error.message : error),
    };
    step('test_accounts_failed', { error: testAccounts.error });
  }

  const authState = await waitUntil('real backend auth storage', async () => {
    const state = await storageState();
    return state.hasAccessToken && state.hasUserId && state.loginAtPresent ? state : null;
  }, 60000);
  const protectedRequestEntry = await waitUntil('protected wx.request after login', async () => {
    const entries = await diagnosticState();
    return entries.find((entry) => entry && entry.event === 'request_finish' && /api\/cards/.test(String(entry.path || '')) && Number(entry.statusCode) === 200) || null;
  }, 30000);
  await page.setData({ level7E2EAuthStatus: 'WX LOGIN E2E: PASS' });
  const statusElement = await page.$('#level7-auth-status');
  const pageStatusText = statusElement ? await statusElement.text() : '';
  const screenshotEvidence = await screenshot('01-wx-login-authenticated');
  await new Promise((resolve) => setTimeout(resolve, 500));

  const diagnostics = await diagnosticState();
  const loginSuccessEntry = diagnostics.find((entry) => entry && entry.event === 'wx_login_success');
  const countDiagnostic = (event) => diagnostics.filter((entry) => entry && entry.event === event).length;
  const evidence = {
    wx_login_start: countDiagnostic('wx_login_start'),
    wx_login_success: countDiagnostic('wx_login_success'),
    wx_login_success_at: loginSuccessEntry ? loginSuccessEntry.at : null,
    code_present: Boolean(loginSuccessEntry && loginSuccessEntry.hasCode),
    code_length: loginSuccessEntry ? Number(loginSuccessEntry.codeLength || 0) : 0,
    code2session_request_start: countDiagnostic('code2session_request_start'),
    jwt_saved: countDiagnostic('jwt_saved'),
    protected_request_finished: Boolean(protectedRequestEntry),
  };
  const passed = evidence.wx_login_start >= 1
    && evidence.wx_login_success >= 1
    && evidence.code_present
    && evidence.code_length > 0
    && evidence.code2session_request_start >= 1
    && evidence.jwt_saved >= 1
    && evidence.protected_request_finished
    && authState.hasAccessToken
    && authState.hasUserId
    && pageStatusText === 'WX LOGIN E2E: PASS';
  if (!passed) throw new Error(`Real auth diagnostics incomplete: ${JSON.stringify(evidence)}`);

  miniProgram.disconnect();
  miniProgram = null;
  return {
    status: 'PASS',
    runtime: { runtime_project: runtimeProject, backend_base_url: e2eBaseUrl },
    page: { path: page.path, login_status_text: pageStatusText },
    auth: evidence,
    test_accounts: testAccounts,
    storage: authState,
    screenshot: screenshotEvidence,
  };
}

async function runPageControl() {
  const runtime = await launchCore();
  shouldCloseProject = true;
  const initial = await currentPageSafe(
    'page_control:initial',
    15000,
    { allowEmptyStack: true },
  );
  const page = await safeReLaunch('pages/index/index', 90000);
  const finalSnapshot = await rawPageControlSnapshot('page_control_after_relaunch');
  const normalized = page && normalizePagePath(page.path);
  if (normalized !== 'pages/index/index') {
    throw new Error(`Page control did not reach pages/index/index; got ${normalized || 'null'}`);
  }
  await miniProgram.close();
  miniProgram = null;
  shouldCloseProject = false;
  step('client_page_control_pass', { path: page.path });
  return {
    status: 'PASS',
    message: 'CLIENT PAGE CONTROL: PASS',
    runtime,
    initial_page: initial ? { path: initial.path, query: initial.query || null } : null,
    final_page: { path: page.path, query: page.query || null },
    final_raw_snapshot: finalSnapshot,
  };
}

async function findLogoutElement(page) {
  const items = await page.$$('.settings-item');
  for (const item of items) {
    const text = await item.text();
    if (String(text).indexOf('退出登录') >= 0) return item;
  }
  return null;
}

async function runRecovery() {
  await connectRecovery();
  shouldCloseProject = true;
  const screenshots = [];
  const recoveryLogStart = consoleEvents.length;
  let page = await currentPageOrReLaunch('pages/index/index', 90000);
  const before = await storageState();
  if (!before.hasAccessToken) throw new Error('Expected the invalidated JWT to remain in client storage');

  await tap(page, ['.header-settings'], 'open_settings_to_trigger_return');
  await waitForPage('pages/settings/index', 20000);
  page = await navigateBackTo('pages/index/index', 30000);

  await waitUntil('401 single-flight relogin recovery', async () => {
    const state = await storageState();
    const logText = consoleEvents.slice(recoveryLogStart).map(consoleText).join('\n');
    return state.hasAccessToken
      && /wx_login_success/.test(logText)
      && /request_finish.*statusCode[^0-9]*200/.test(logText)
      ? true
      : false;
  }, 60000, 150);

  const recoveryEvidence = {
    request_401_count: countConsoleSince(recoveryLogStart, /request_finish.*statusCode[^0-9]*401/),
    wx_login_start: countConsoleSince(recoveryLogStart, /wx_login_start/),
    wx_login_success: countConsoleSince(recoveryLogStart, /wx_login_success/),
    auth_refresh_start: countConsoleSince(recoveryLogStart, /auth_refresh_start/),
    auth_refresh_join: countConsoleSince(recoveryLogStart, /auth_refresh_join/),
    jwt_saved: countConsoleSince(recoveryLogStart, /jwt_saved/),
    protected_200_after_refresh: countConsoleSince(recoveryLogStart, /request_finish.*statusCode[^0-9]*200/),
  };
  if (
    recoveryEvidence.wx_login_start < 1 ||
    recoveryEvidence.auth_refresh_start < 1 ||
    recoveryEvidence.auth_refresh_join < 1 ||
    recoveryEvidence.protected_200_after_refresh < 1
  ) {
    throw new Error(`401 single-flight evidence failed: ${JSON.stringify(recoveryEvidence)}`);
  }
  screenshots.push(await screenshot('07-auth-recovered'));

  await tap(page, ['.header-settings'], 'open_settings_for_logout');
  page = await waitForPage('pages/settings/index', 20000);
  const logoutElement = await findLogoutElement(page);
  if (!logoutElement) throw new Error('Logout UI element was not found');
  await logoutElement.tap();
  step('tap_logout_ui');
  await new Promise((resolve) => setTimeout(resolve, 350));
  await miniProgram.native().confirmModal();
  step('confirm_logout_modal');
  await waitUntil('JWT cleared after logout', async () => !(await storageState()).hasAccessToken, 30000);
  screenshots.push(await screenshot('08-logout-complete'));

  const remainingNamespacedKeys = await miniProgram.evaluate(function () {
    wx.clearStorageSync();
    const prefix = wx.__level7E2EStoragePrefix || '';
    const info = wx.getStorageInfoSync();
    const keys = info && Array.isArray(info.keys) ? info.keys : [];
    return keys.filter(function (key) { return prefix && String(key).indexOf(prefix) === 0; }).length;
  });
  step('e2e_storage_cleaned', { remaining_namespaced_keys: remainingNamespacedKeys });
  if (remainingNamespacedKeys !== 0) throw new Error('E2E namespaced Storage cleanup was not proven');

  await miniProgram.close();
  miniProgram = null;
  shouldCloseProject = false;
  step('runtime_project_closed');

  return {
    status: 'PASS',
    recovery: recoveryEvidence,
    logout: { token_cleared: true },
    e2e_storage_cleanup: { remaining_namespaced_keys: remainingNamespacedKeys },
    screenshots,
    console_event_count: consoleEvents.length,
    exception_event_count: exceptionEvents.length,
  };
}

async function runSystemValidation() {
  await connectRecovery();
  const screenshots = [];
  const page = await safeReLaunch('pages/add/add', 60000);
  await input(page, '.textarea-english', 'This is a complete sentence.', 'system_warning_validation_input');
  const state = await waitForValidation(
    page,
    'Harper unavailable SYSTEM_WARNING',
    (value) => value.status === 'warning'
      && value.canSave === true
      && value.canAnalyze === true
      && value.canPronounce === true,
    30000,
  );
  screenshots.push(await screenshot('validation-04-system-warning'));
  miniProgram.disconnect();
  miniProgram = null;
  return {
    status: 'PASS',
    validation: state,
    harper_expected_unavailable: true,
    screenshots,
    console_event_count: consoleEvents.length,
    exception_event_count: exceptionEvents.length,
  };
}

async function runCleanup() {
  await connectRecovery();
  shouldCloseProject = true;
  const remainingNamespacedKeys = await miniProgram.evaluate(function () {
    wx.clearStorageSync();
    const prefix = wx.__level7E2EStoragePrefix || '';
    const info = wx.getStorageInfoSync();
    const keys = info && Array.isArray(info.keys) ? info.keys : [];
    return keys.filter(function (key) { return prefix && String(key).indexOf(prefix) === 0; }).length;
  });
  await miniProgram.close();
  miniProgram = null;
  shouldCloseProject = false;
  step('emergency_runtime_cleanup', { remaining_namespaced_keys: remainingNamespacedKeys });
  return {
    status: remainingNamespacedKeys === 0 ? 'PASS' : 'FAIL',
    e2e_storage_cleanup: { remaining_namespaced_keys: remainingNamespacedKeys },
  };
}

async function main() {
  requireValue(artifactDir, 'LEVEL7_ARTIFACT_DIR');
  requireValue(cliPath, 'LEVEL7_WECHAT_CLI');
  if (mode !== 'prepare' && mode !== 'launch' && mode !== 'auth' && mode !== 'crud' && mode !== 'core' && mode !== 'system-validation' && mode !== 'recovery' && mode !== 'cleanup' && mode !== 'page-control') {
    throw new Error('Mode must be prepare, launch, auth, crud, core, system-validation, recovery, cleanup, or page-control');
  }
  fs.mkdirSync(artifactDir, { recursive: true });
  fs.writeFileSync(consolePath, '', 'utf8');
  fs.writeFileSync(exceptionPath, '', 'utf8');

  const result = mode === 'prepare'
    ? await runPrepare()
    : (mode === 'launch' ? await runLaunch() : (mode === 'auth' ? await runAuth() : (mode === 'crud' ? await runCrud() : (mode === 'core' ? await runCore() : (mode === 'system-validation' ? await runSystemValidation() : (mode === 'recovery' ? await runRecovery() : (mode === 'page-control' ? await runPageControl() : await runCleanup())))))));
  result.mode = mode;
  result.started_at = steps.length ? steps[0].at : now();
  result.finished_at = now();
  result.steps = steps;
  saveResult(result);
  process.stdout.write(`[wechat-client] RESULT ${resultPath}\n`);
}

main().catch(async (error) => {
  const failure = {
    status: 'FAIL',
    mode,
    finished_at: now(),
    error: sanitize({ name: error && error.name, message: error && error.message, stack: error && error.stack }),
    steps,
    console_event_count: consoleEvents.length,
    exception_event_count: exceptionEvents.length,
  };
  try {
    saveResult(failure);
  } catch (_) {}
  if (miniProgram) {
    try {
      if (shouldCloseProject) await closeAutomationBestEffort('failure_cleanup');
      else miniProgram.disconnect();
    } catch (_) {}
  }
  process.stderr.write(`[wechat-client] FAIL ${redactString(error && error.stack ? error.stack : error)}\n`);
  process.exit(1);
});
