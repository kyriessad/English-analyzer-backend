const childProcess = require('child_process');
const fs = require('fs');
const net = require('net');
const path = require('path');

const label = process.env.E2E_COMPAT_LABEL || 'compat';
const artifactDir = process.env.E2E_COMPAT_ARTIFACT_DIR;
const moduleDir = process.env.E2E_COMPAT_MODULE_DIR;
const projectPath = process.env.E2E_COMPAT_PROJECT_PATH;
const cliPath = process.env.E2E_COMPAT_CLI_PATH;
const port = Number(process.env.E2E_COMPAT_PORT || '19420');
const trustProject = process.env.E2E_COMPAT_TRUST_PROJECT !== '0';
const closeAfter = process.env.E2E_COMPAT_CLOSE_AFTER !== '0';

if (!artifactDir || !moduleDir || !projectPath || !cliPath) {
  throw new Error('Missing required E2E_COMPAT_* environment variables');
}

fs.mkdirSync(artifactDir, { recursive: true });
const resultPath = path.join(artifactDir, `${label}.json`);

function now() {
  return new Date().toISOString();
}

function writeJson(filePath, value) {
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, 'utf8');
}

function redact(value) {
  if (typeof value !== 'string') return value;
  return value.replace(/(appid|secret|token|jwt|openid)["'=:\s]+[^"',\s}]+/gi, '$1=<redacted>');
}

function tcpProbe(timeoutMs = 1000) {
  return new Promise((resolve) => {
    const socket = new net.Socket();
    let done = false;
    const finish = (ok, error) => {
      if (done) return;
      done = true;
      socket.destroy();
      resolve({ ok, error: error ? String(error.message || error) : null });
    };
    socket.setTimeout(timeoutMs);
    socket.once('connect', () => finish(true, null));
    socket.once('timeout', () => finish(false, new Error('timeout')));
    socket.once('error', (error) => finish(false, error));
    socket.connect(port, '127.0.0.1');
  });
}

async function waitForListening(timeoutMs = 60000) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    last = await tcpProbe(1000);
    if (last.ok) return { ok: true, last };
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  return { ok: false, last };
}

function netstat19420() {
  const completed = childProcess.spawnSync(
    'powershell.exe',
    ['-NoProfile', '-Command', `netstat -ano -p tcp | Select-String '${port}' | ForEach-Object { $_.Line }`],
    { encoding: 'utf8', timeout: 15000 }
  );
  return {
    command: `netstat -ano -p tcp | findstr ${port}`,
    exit_code: completed.status,
    stdout: completed.stdout || '',
    stderr: completed.stderr || '',
  };
}

function processForPort() {
  const command = `$line = netstat -ano -p tcp | Select-String 'LISTENING' | Where-Object { $_.Line -match ':${port}\\s' } | Select-Object -First 1; if($line){ $parts = ($line.Line -split '\\s+') | Where-Object { $_ }; $pidValue = [int]$parts[4]; $p = Get-Process -Id $pidValue -ErrorAction SilentlyContinue; if($p){ [pscustomobject]@{ Pid=$pidValue; ProcessName=$p.ProcessName; Path=$p.Path } | ConvertTo-Json -Compress } }`;
  const completed = childProcess.spawnSync(
    'powershell.exe',
    ['-NoProfile', '-Command', command],
    { encoding: 'utf8', timeout: 15000 }
  );
  const raw = (completed.stdout || '').trim();
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch (error) {
    return { parse_error: String(error.message || error), raw };
  }
}

function runCli(args, timeoutMs) {
  const quotePs = (value) => `'${String(value).replace(/'/g, "''")}'`;
  const command = [
    `$CliPath = ${quotePs(cliPath)}`,
    `$ArgList = @(${args.map(quotePs).join(', ')})`,
    '& $CliPath @ArgList',
  ].join('; ');
  const completed = childProcess.spawnSync('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command], {
    encoding: 'utf8',
    timeout: timeoutMs,
    windowsHide: true,
  });
  return {
    command: `"${cliPath}" ${args.join(' ')}`,
    exit_code: completed.status,
    signal: completed.signal || null,
    error: completed.error ? String(completed.error.stack || completed.error) : null,
    stdout: redact(completed.stdout || ''),
    stderr: redact(completed.stderr || ''),
  };
}

async function withTimeout(promise, timeoutMs, name) {
  let timer = null;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${name} timed out after ${timeoutMs}ms`)), timeoutMs);
  });
  try {
    return await Promise.race([promise, timeout]);
  } finally {
    clearTimeout(timer);
  }
}

async function rawSend(method) {
  const wsPath = path.join(moduleDir, 'node_modules', 'ws');
  const WebSocket = require(wsPath);
  return await new Promise((resolve) => {
    const ws = new WebSocket(`ws://127.0.0.1:${port}`);
    const id = `probe-${Date.now()}`;
    const result = { ok: false, response: null, error: null };
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      try {
        ws.close();
      } catch (error) {
        // Best-effort close only.
      }
      resolve(result);
    };
    const timer = setTimeout(() => {
      result.error = `${method} timed out after 10000ms`;
      finish();
    }, 10000);
    ws.on('open', () => {
      ws.send(JSON.stringify({ id, method, params: {} }));
    });
    ws.on('message', (data) => {
      try {
        result.response = JSON.parse(data.toString());
        result.ok = true;
      } catch (error) {
        result.error = String(error.message || error);
        result.raw = data.toString();
      }
      finish();
    });
    ws.on('error', (error) => {
      result.error = String(error.message || error);
      finish();
    });
    ws.on('close', () => {
      if (!result.ok && !result.error) result.error = 'closed before response';
      finish();
    });
  });
}

async function rawToolGetInfo() {
  return await rawSend('Tool.getInfo');
}

async function rawCurrentPage() {
  return await rawSend('App.getCurrentPage');
}

async function main() {
  const result = {
    label,
    started_at: now(),
    cli_path: cliPath,
    project_path: projectPath,
    automation_port: port,
    module_dir: moduleDir,
    steps: [],
    pass: false,
  };

  const pkg = require(path.join(moduleDir, 'node_modules', 'miniprogram-automator', 'package.json'));
  result.miniprogram_automator_version = pkg.version;

  const launchArgs = ['auto', '--project', projectPath, '--auto-port', String(port)];
  if (trustProject) launchArgs.push('--trust-project');
  result.launch = runCli(launchArgs, 120000);
  result.steps.push({ at: now(), name: 'cli_auto_finished', exit_code: result.launch.exit_code });

  result.listen_wait = await waitForListening(60000);
  result.netstat = netstat19420();
  result.port_process = processForPort();
  result.steps.push({ at: now(), name: 'listen_checked', listen_ok: result.listen_wait.ok, process: result.port_process });

  if (result.listen_wait.ok) {
    result.raw_tool_get_info = await rawToolGetInfo();
    result.steps.push({ at: now(), name: 'raw_tool_get_info_finished', ok: result.raw_tool_get_info.ok });
    result.raw_current_page = await rawCurrentPage();
    result.steps.push({ at: now(), name: 'raw_current_page_finished', ok: result.raw_current_page.ok });

    try {
      const automator = require(path.join(moduleDir, 'node_modules', 'miniprogram-automator'));
      const miniProgram = await withTimeout(
        automator.connect({ wsEndpoint: `ws://127.0.0.1:${port}` }),
        15000,
        'miniprogram-automator.connect'
      );
      result.connect = { ok: true };
      const page = await withTimeout(miniProgram.currentPage(), 15000, 'currentPage');
      result.current_page = {
        ok: true,
        path: page && page.path ? page.path : null,
        route: page && page.route ? page.route : null,
        query: page && page.query ? page.query : null,
      };
      result.pass = true;
      if (miniProgram && typeof miniProgram.disconnect === 'function') {
        miniProgram.disconnect();
      }
    } catch (error) {
      result.connect = {
        ok: false,
        type: error && error.name ? error.name : 'Error',
        message: String(error && error.message ? error.message : error),
        stack: String(error && error.stack ? error.stack : error),
      };
      result.current_page = { ok: false };
    }
  }

  if (closeAfter) {
    result.close = runCli(['close', '--project', projectPath], 45000);
    await new Promise((resolve) => setTimeout(resolve, 3000));
    result.netstat_after_close = netstat19420();
  }

  result.finished_at = now();
  writeJson(resultPath, result);
  console.log(JSON.stringify({
    label: result.label,
    version: result.miniprogram_automator_version,
    listen: result.listen_wait.ok,
    connect: result.connect ? result.connect.ok : false,
    currentPage: result.current_page ? result.current_page.ok : false,
    resultPath,
  }, null, 2));

  process.exit(result.pass ? 0 : 1);
}

main().catch((error) => {
  const failure = {
    label,
    pass: false,
    fatal: {
      type: error && error.name ? error.name : 'Error',
      message: String(error && error.message ? error.message : error),
      stack: String(error && error.stack ? error.stack : error),
    },
    finished_at: now(),
  };
  writeJson(resultPath, failure);
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
