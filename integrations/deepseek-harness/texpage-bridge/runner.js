import { execFile as nodeExecFile } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'

export const PROJECT_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/
export const ALLOWED_COMMANDS = Object.freeze(['build', 'submit', 'status'])

const PRIVATE_KEYS = new Set([
  'authorization',
  'cookie',
  'credential',
  'credentials',
  'log_url',
  'password',
  'pdf_url',
  'project_key',
  'secret',
  'token',
  'version_no',
])

function requireNonEmptyString(value, label) {
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw new Error(`${label} must be a non-empty string`)
  }
  return value.trim()
}

export function resolveBridgeHome(configured = '') {
  const raw = configured || process.env.TEXPAGE_BRIDGE_HOME || ''
  const bridgeHome = requireNonEmptyString(
    raw,
    'bridgeHome (or TEXPAGE_BRIDGE_HOME)',
  )
  if (!path.isAbsolute(bridgeHome)) {
    throw new Error('bridgeHome must be an absolute path')
  }
  const resolved = path.resolve(bridgeHome)
  const adapter = path.join(
    resolved,
    'integrations',
    'agent-skill',
    'texpage-bridge',
    'scripts',
    'texpage_agent.py',
  )
  if (!existsSync(adapter)) {
    throw new Error(`low-privilege texpage agent adapter not found under bridgeHome: ${adapter}`)
  }
  return { bridgeHome: resolved, adapter }
}

export function resolvePythonExecutable(configured = '') {
  const raw = configured || process.env.TEXPAGE_BRIDGE_PYTHON || ''
  return raw.trim() || (process.platform === 'win32' ? 'python' : 'python3')
}

export function buildInvocation({
  bridgeHome = '',
  pythonExecutable = '',
  project,
  command,
  timeoutSeconds = 240,
}) {
  if (!PROJECT_RE.test(project ?? '')) {
    throw new Error("project alias must contain only letters, digits, '.', '_' or '-'")
  }
  if (!ALLOWED_COMMANDS.includes(command)) {
    throw new Error(`unsupported texpage command: ${String(command)}`)
  }
  if (!Number.isInteger(timeoutSeconds) || timeoutSeconds < 1 || timeoutSeconds > 7200) {
    throw new Error('timeoutSeconds must be an integer between 1 and 7200')
  }

  const resolved = resolveBridgeHome(bridgeHome)
  const executable = resolvePythonExecutable(pythonExecutable)
  const args = [
    resolved.adapter,
    '--bridge-home',
    resolved.bridgeHome,
    project,
    command,
  ]
  if (command === 'build' || command === 'submit') {
    args.push('--timeout', String(timeoutSeconds))
  }
  return { executable, args, cwd: resolved.bridgeHome }
}

function scrubJson(value) {
  if (Array.isArray(value)) return value.map(scrubJson)
  if (typeof value === 'string') return value.replace(/https?:\/\/\S+/gi, '[redacted-url]')
  if (value === null || typeof value !== 'object') return value
  const clean = {}
  for (const [key, item] of Object.entries(value)) {
    if (PRIVATE_KEYS.has(key.toLowerCase())) continue
    clean[key] = scrubJson(item)
  }
  return clean
}

export function sanitizeText(text, command) {
  const normalized = String(text ?? '')
  if (command !== 'status') return normalized.replace(/https?:\/\/\S+/gi, '[redacted-url]')
  try {
    return `${JSON.stringify(scrubJson(JSON.parse(normalized)), null, 2)}\n`
  } catch {
    return normalized.replace(/https?:\/\/\S+/gi, '[redacted-url]')
  }
}

export function runBridgeCommand({
  bridgeHome = '',
  pythonExecutable = '',
  project,
  command,
  timeoutSeconds = 240,
  maxOutputBytes = 256 * 1024,
  signal,
  execFileImpl = nodeExecFile,
}) {
  const invocation = buildInvocation({
    bridgeHome,
    pythonExecutable,
    project,
    command,
    timeoutSeconds,
  })
  if (!Number.isInteger(maxOutputBytes) || maxOutputBytes < 4096 || maxOutputBytes > 4 * 1024 * 1024) {
    throw new Error('maxOutputBytes must be an integer between 4096 and 4194304')
  }

  return new Promise((resolve) => {
    const options = {
      cwd: invocation.cwd,
      windowsHide: true,
      maxBuffer: maxOutputBytes,
      encoding: 'utf8',
      ...(signal ? { signal } : {}),
    }
    execFileImpl(invocation.executable, invocation.args, options, (error, stdout, stderr) => {
      const numericCode = typeof error?.code === 'number' ? error.code : undefined
      const exitCode = error === null || error === undefined ? 0 : numericCode ?? 1
      resolve({
        exitCode,
        stdout: sanitizeText(stdout, command),
        stderr: sanitizeText(stderr, command),
      })
    })
  })
}
