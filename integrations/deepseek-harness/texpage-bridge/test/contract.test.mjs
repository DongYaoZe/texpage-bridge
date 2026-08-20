import assert from 'node:assert/strict'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  ALLOWED_COMMANDS,
  buildInvocation,
  runBridgeCommand,
  sanitizeText,
} from '../runner.js'
import { apply, inject, name } from '../index.js'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const PACKAGE_ROOT = path.resolve(HERE, '..')

async function fakeBridgeHome() {
  const root = await mkdtemp(path.join(PACKAGE_ROOT, '.tmp-contract-'))
  const adapterDir = path.join(root, 'integrations', 'agent-skill', 'texpage-bridge', 'scripts')
  await mkdir(adapterDir, { recursive: true })
  await writeFile(path.join(adapterDir, 'texpage_agent.py'), '# test stub\n', 'utf8')
  return root
}

test('package declares an official dsh bundle patch', async () => {
  const manifest = JSON.parse(await import('node:fs/promises').then(fs => fs.readFile(path.join(PACKAGE_ROOT, 'package.json'), 'utf8')))
  assert.equal(manifest.dsh.bundle.patch, './cordis.patch.yml')
  assert.equal(manifest.peerDependencies['@deepseek-ai/dsh-tools'], '0.1.0-rc.8')
  assert.equal(manifest.peerDependencies['@deepseek-ai/cordis'], '4.0.1')
  const patch = await import('node:fs/promises').then(fs => fs.readFile(path.join(PACKAGE_ROOT, 'cordis.patch.yml'), 'utf8'))
  assert.match(patch, /id:\s*texpage-bridge/)
  assert.match(patch, /name:\s*dsh-texpage-bridge/)
})

test('runner exposes only build, submit, and status', () => {
  assert.deepEqual(ALLOWED_COMMANDS, ['build', 'submit', 'status'])
})

test('real Harness entry registers exactly the three low-privilege tools', () => {
  const definitions = []
  const ctx = {
    tools: {
      register(definition) {
        definitions.push(definition)
        return () => {}
      },
    },
  }
  apply(ctx, { projects: ['demo-project'] })
  assert.equal(name, 'texpage-bridge')
  assert.deepEqual(inject, ['tools'])
  assert.deepEqual(
    definitions.map(definition => definition.name),
    ['texpage_build', 'texpage_submit', 'texpage_status'],
  )
  for (const definition of definitions) {
    assert.equal(definition.parameters.type, 'object')
    assert.deepEqual(Object.keys(definition.parameters.properties), ['project'])
    assert.deepEqual(definition.parameters.required, ['project'])
  }
})

test('invocation delegates to the generic low-privilege adapter with no shell admin surface', async () => {
  const home = await fakeBridgeHome()
  try {
    const invocation = buildInvocation({
      bridgeHome: home,
      pythonExecutable: 'python-test',
      project: 'demo-project',
      command: 'build',
      timeoutSeconds: 90,
    })
    assert.equal(invocation.executable, 'python-test')
    assert.equal(invocation.cwd, path.resolve(home))
    assert.deepEqual(invocation.args.slice(-4), ['demo-project', 'build', '--timeout', '90'])
    assert.match(invocation.args[0], /texpage_agent\.py$/)
    assert.ok(!invocation.args.includes('publish'))
    assert.ok(!invocation.args.includes('broker'))
    assert.throws(() => buildInvocation({ bridgeHome: home, project: 'demo-project', command: 'publish' }))
    assert.throws(() => buildInvocation({ bridgeHome: home, project: '../projects.json', command: 'status' }))
  } finally {
    await rm(home, { recursive: true, force: true })
  }
})

test('runner can be exercised with a fake process and does not mutate TeXPage', async () => {
  const home = await fakeBridgeHome()
  const calls = []
  const fakeExecFile = (executable, args, options, callback) => {
    calls.push({ executable, args, options })
    callback(null, 'REQUEST SUBMITTED: tp-20260820-test demo-project @ v1.0\n', '')
  }
  try {
    const result = await runBridgeCommand({
      bridgeHome: home,
      pythonExecutable: 'python-test',
      project: 'demo-project',
      command: 'submit',
      timeoutSeconds: 30,
      execFileImpl: fakeExecFile,
    })
    assert.equal(result.exitCode, 0)
    assert.match(result.stdout, /REQUEST SUBMITTED/)
    assert.equal(calls.length, 1)
    assert.equal(calls[0].options.cwd, path.resolve(home))
    assert.equal(calls[0].options.windowsHide, true)
  } finally {
    await rm(home, { recursive: true, force: true })
  }
})

test('status output scrubs provider identifiers, signed URLs, and token-like fields', () => {
  const raw = JSON.stringify({
    project: 'demo',
    project_key: 'private-project-key',
    version_no: 'private-version-id',
    pdf_url: 'https://signed.example/pdf?secret=abc',
    nested: { token: 'secret-token', pdf_path: 'C:/safe/latest.pdf' },
  })
  const clean = JSON.parse(sanitizeText(raw, 'status'))
  assert.equal(clean.project, 'demo')
  assert.equal(clean.nested.pdf_path, 'C:/safe/latest.pdf')
  assert.ok(!('project_key' in clean))
  assert.ok(!('version_no' in clean))
  assert.ok(!('pdf_url' in clean))
  assert.ok(!('token' in clean.nested))
})
