import Schema from '@deepseek-ai/schemastery'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { PROJECT_RE, runBridgeCommand } from './runner.js'

export const name = 'texpage-bridge'
export const inject = ['tools']

export const Config = Schema.object({
  bridgeHome: Schema.string().default(''),
  pythonExecutable: Schema.string().default(''),
  projects: Schema.array(String).default([]),
  timeoutSeconds: Schema.number().min(1).max(7200).default(240),
  maxOutputBytes: Schema.number().min(4096).max(4 * 1024 * 1024).default(256 * 1024),
})

function allowedProjects(config) {
  const allowed = new Set()
  for (const project of config.projects ?? []) {
    if (!PROJECT_RE.test(project)) {
      throw new Error(`texpage-bridge: invalid configured project alias ${JSON.stringify(project)}`)
    }
    allowed.add(project)
  }
  return allowed
}

function failureMessage(command, project, result) {
  const detail = (result.stderr || result.stdout || `exit code ${result.exitCode}`).trim()
  const bounded = detail.length > 4000 ? `${detail.slice(0, 4000)}\n[truncated]` : detail
  return `texpage_${command} failed for ${project}: ${bounded}`
}

function outputContract() {
  return {
    schema: {
      type: 'object',
      additionalProperties: false,
      properties: {
        project: { type: 'string', required: true },
        output: { type: 'string', required: true },
      },
    },
    render: (_args, value) => [{ type: 'text', text: value.output }],
  }
}

function makeTool(command, description, config, allowed) {
  return defineTool({
    name: `texpage_${command}`,
    description,
    parameters: {
      project: {
        type: 'string',
        required: true,
        description: 'A project alias explicitly allow-listed in this plugin configuration.',
      },
    },
    output: outputContract(),
    async execute(args, exec) {
      if (!PROJECT_RE.test(args.project) || !allowed.has(args.project)) {
        throw new Error(`texpage-bridge: project alias is not allow-listed: ${JSON.stringify(args.project)}`)
      }
      const result = await runBridgeCommand({
        bridgeHome: config.bridgeHome,
        pythonExecutable: config.pythonExecutable,
        project: args.project,
        command,
        timeoutSeconds: config.timeoutSeconds,
        maxOutputBytes: config.maxOutputBytes,
        signal: exec.signal,
      })
      if (result.exitCode !== 0) throw new Error(failureMessage(command, args.project, result))
      return {
        project: args.project,
        output: result.stdout.trim() || `${command} completed successfully`,
      }
    },
  })
}

export function apply(ctx, config = {}) {
  const resolved = {
    bridgeHome: config.bridgeHome ?? '',
    pythonExecutable: config.pythonExecutable ?? '',
    projects: config.projects ?? [],
    timeoutSeconds: config.timeoutSeconds ?? 240,
    maxOutputBytes: config.maxOutputBytes ?? 256 * 1024,
  }
  const allowed = allowedProjects(resolved)

  ctx.tools.register(makeTool(
    'build',
    'Freeze the current local worktree, submit it through the existing TeXPage broker, wait for compilation, and return only sanitized build output. No version or broker administration is exposed.',
    resolved,
    allowed,
  ))
  ctx.tools.register(makeTool(
    'submit',
    'Freeze the current local worktree and queue an asynchronous build through the existing TeXPage broker. Returns the sanitized request submission output only.',
    resolved,
    allowed,
  ))
  ctx.tools.register(makeTool(
    'status',
    'Read the last local TeXPage build record for an allow-listed project without starting a build. Provider identifiers and URL-like values are scrubbed from the returned text.',
    resolved,
    allowed,
  ))
}
