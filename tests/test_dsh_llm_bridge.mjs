import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { once } from 'node:events'
import { createConnection } from 'node:net'
import assert from 'node:assert/strict'
import { startArcLlmBridge, REQUEST_SCHEMA_VERSION } from '../plugins/arc/dsh/llm-bridge.js'

const root = mkdtempSync(join(tmpdir(), 'arc-dsh-bridge-'))
const socketPath = join(root, 'arc-llm.sock')
const tokenPath = join(root, 'arc-llm.token')
let prepareConfig
const llm = {
  async prepareCall(config) {
    prepareConfig = config
    return {
      stream(options) {
        assert.equal(options.provider, 'fake-provider')
        assert.equal(options.model, 'fake-model')
        assert.equal(options.messages[0].content[0].text, 'hello')
        return (async function* () {
          yield { type: 'text-delta', index: 0, text: 'hello ' }
          yield { type: 'reasoning-delta', index: 1, text: 'internal' }
          yield {
            type: 'usage',
            usage: { inputTokens: 3, outputTokens: 2 },
          }
          yield { type: 'finish', reason: { kind: 'stop' } }
        })()
      },
    }
  },
}

const bridge = startArcLlmBridge({ llm, socketPath, tokenPath })
try {
  await bridge.ready
  const health = await request(socketPath, {
    schema_version: REQUEST_SCHEMA_VERSION,
    token: bridge.readToken(),
    op: 'health',
  })
  assert.equal(health[0].type, 'health')
  assert.equal(health[0].ok, true)

  const events = await request(socketPath, {
    schema_version: REQUEST_SCHEMA_VERSION,
    token: bridge.readToken(),
    op: 'generate',
    provider: 'fake-provider',
    model: 'fake-model',
    prompt: 'hello',
  })
  assert.deepEqual(prepareConfig, { provider: 'fake-provider', model: 'fake-model' })
  assert.deepEqual(
    events.map((event) => event.type),
    ['started', 'text-delta', 'reasoning-delta', 'usage', 'finish'],
  )
  assert.equal(events[1].text, 'hello ')
  assert.deepEqual(events[3].usage, { input_tokens: 3, output_tokens: 2 })
  assert.equal(events[4].reason, 'stop')
} finally {
  await bridge.close()
  rmSync(root, { recursive: true, force: true })
}

async function request(path, payload) {
  const socket = createConnection(path)
  socket.setEncoding('utf8')
  await once(socket, 'connect')
  socket.write(`${JSON.stringify(payload)}\n`)
  const events = []
  let buffer = ''
  for await (const chunk of socket) {
    buffer += chunk
    while (buffer.includes('\n')) {
      const index = buffer.indexOf('\n')
      const line = buffer.slice(0, index)
      buffer = buffer.slice(index + 1)
      if (line.trim()) events.push(JSON.parse(line))
    }
  }
  return events
}
