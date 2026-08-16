import { randomBytes, randomUUID } from 'node:crypto'
import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs'
import { dirname, join } from 'node:path'
import { homedir } from 'node:os'
import { createServer } from 'node:net'

export const REQUEST_SCHEMA_VERSION = 'arc.dsh-llm.request.v1'
export const EVENT_SCHEMA_VERSION = 'arc.dsh-llm.event.v1'

const MAX_REQUEST_BYTES = 16 * 1024 * 1024

function defaultDshHome() {
  return process.env.DSH_HOME || join(homedir(), '.dsh')
}

export function resolveBridgePaths({ socketPath, tokenPath } = {}) {
  const resolvedSocketPath =
    socketPath ||
    process.env.DSH_ARC_LLM_SOCKET ||
    join(defaultDshHome(), 'runtime', 'arc-llm.sock')
  return {
    socketPath: resolvedSocketPath,
    tokenPath:
      tokenPath ||
      process.env.DSH_ARC_LLM_TOKEN_FILE ||
      `${resolvedSocketPath}.token`,
  }
}

function removeSocketIfOwned(path) {
  if (!existsSync(path)) return
  const stat = lstatSync(path)
  if (!stat.isSocket()) {
    throw new Error(`Refusing to replace non-socket path: ${path}`)
  }
  unlinkSync(path)
}

function send(socket, event) {
  if (socket.destroyed || !socket.writable) return false
  socket.write(`${JSON.stringify(event)}\n`)
  return true
}

function normalizeUsage(usage) {
  if (!usage || typeof usage !== 'object') return null
  const value = {}
  if (Number.isInteger(usage.inputTokens)) value.input_tokens = usage.inputTokens
  if (Number.isInteger(usage.outputTokens)) value.output_tokens = usage.outputTokens
  if (Number.isInteger(usage.cacheReadTokens)) {
    value.cached_input_tokens = usage.cacheReadTokens
  }
  return Object.keys(value).length === 0 ? null : value
}

function normalizeChunk(chunk) {
  if (!chunk || typeof chunk !== 'object') return null
  if (chunk.type === 'text-delta' && typeof chunk.text === 'string') {
    return { type: 'text-delta', text: chunk.text }
  }
  if (chunk.type === 'reasoning-delta' && typeof chunk.text === 'string') {
    return { type: 'reasoning-delta', text: chunk.text }
  }
  if (chunk.type === 'usage') {
    const usage = normalizeUsage(chunk.usage)
    return usage === null ? null : { type: 'usage', usage }
  }
  if (chunk.type === 'finish') {
    const reason = chunk.reason?.kind
    if (typeof reason !== 'string') return null
    const event = { type: 'finish', reason }
    if (chunk.reason?.failure && typeof chunk.reason.failure === 'object') {
      event.failure = {
        code: String(chunk.reason.failure.code || 'provider_error'),
        message: String(chunk.reason.failure.message || 'Provider failed.'),
      }
    }
    return event
  }
  return null
}

function validateRequest(value, token) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Bridge request must be a JSON object.')
  }
  if (value.schema_version !== REQUEST_SCHEMA_VERSION) {
    throw new Error(`Unsupported bridge schema: ${String(value.schema_version)}`)
  }
  if (value.token !== token) throw new Error('Bridge authentication failed.')
  if (value.op !== 'health' && value.op !== 'generate') {
    throw new Error('Bridge request op must be health or generate.')
  }
  if (value.op === 'generate') {
    if (typeof value.prompt !== 'string' || value.prompt.length === 0) {
      throw new Error('Bridge generate.prompt must be a non-empty string.')
    }
    if (typeof value.model !== 'string' || value.model.length === 0) {
      throw new Error('Bridge generate.model must be a non-empty string.')
    }
  }
  return value
}

function messageForPrompt(prompt) {
  return {
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text: prompt }],
    source: { kind: 'user' },
  }
}

async function handleRequest(socket, value, token, llm) {
  const request = validateRequest(value, token)
  if (request.op === 'health') {
    send(socket, {
      schema_version: EVENT_SCHEMA_VERSION,
      type: 'health',
      ok: true,
    })
    socket.end()
    return
  }

  const controller = new AbortController()
  const abort = () => controller.abort()
  socket.once('close', abort)
  try {
    const provider =
      typeof request.provider === 'string' && request.provider.length > 0
        ? request.provider
        : 'deepseek-official'
    const prepared = await llm.prepareCall({ provider, model: request.model })
    send(socket, {
      schema_version: EVENT_SCHEMA_VERSION,
      type: 'started',
      provider,
      model: request.model,
    })
    const options = {
      provider,
      model: request.model,
      messages: [messageForPrompt(request.prompt)],
      signal: controller.signal,
    }
    if (typeof request.system === 'string' && request.system.length > 0) {
      options.system = request.system
    }
    if (typeof request.temperature === 'number') {
      options.temperature = request.temperature
    }
    if (Number.isInteger(request.max_tokens) && request.max_tokens > 0) {
      options.maxTokens = request.max_tokens
    }
    if (typeof request.reasoning_effort === 'string') {
      options.reasoningEffort = request.reasoning_effort
    }

    let finished = false
    for await (const chunk of prepared.stream(options)) {
      const event = normalizeChunk(chunk)
      if (event === null) continue
      if (!send(socket, { schema_version: EVENT_SCHEMA_VERSION, ...event })) {
        return
      }
      if (event.type === 'finish') finished = true
    }
    if (!finished && !socket.destroyed) {
      send(socket, {
        schema_version: EVENT_SCHEMA_VERSION,
        type: 'finish',
        reason: 'error',
        failure: {
          code: 'incomplete_stream',
          message: 'DSH ended the native stream without a finish event.',
        },
      })
    }
  } catch (error) {
    if (!socket.destroyed) {
      const message = error instanceof Error ? error.message : String(error)
      send(socket, {
        schema_version: EVENT_SCHEMA_VERSION,
        type: 'finish',
        reason: 'error',
        failure: { code: 'bridge_error', message },
      })
    }
  } finally {
    socket.removeListener('close', abort)
    if (!socket.destroyed) socket.end()
  }
}

export function startArcLlmBridge({ llm, socketPath, tokenPath } = {}) {
  if (!llm || typeof llm.prepareCall !== 'function') {
    throw new Error('ARC DSH bridge requires the native DSH llm service.')
  }

  const paths = resolveBridgePaths({ socketPath, tokenPath })
  mkdirSync(dirname(paths.socketPath), { recursive: true, mode: 0o700 })
  chmodSync(dirname(paths.socketPath), 0o700)
  removeSocketIfOwned(paths.socketPath)
  const token = randomBytes(32).toString('hex')
  mkdirSync(dirname(paths.tokenPath), { recursive: true, mode: 0o700 })
  writeFileSync(paths.tokenPath, `${token}\n`, { mode: 0o600 })
  chmodSync(paths.tokenPath, 0o600)

  const server = createServer((socket) => {
    socket.setEncoding('utf8')
    let buffer = ''
    let handled = false
    socket.on('data', (chunk) => {
      if (handled) return
      buffer += chunk
      if (Buffer.byteLength(buffer, 'utf8') > MAX_REQUEST_BYTES) {
        socket.destroy(new Error('Bridge request is too large.'))
        return
      }
      const newline = buffer.indexOf('\n')
      if (newline < 0) return
      handled = true
      const line = buffer.slice(0, newline)
      let value
      try {
        value = JSON.parse(line)
      } catch {
        socket.destroy(new Error('Bridge request is not valid JSON.'))
        return
      }
      void handleRequest(socket, value, token, llm)
    })
  })
  let readyResolve
  let readyReject
  const ready = new Promise((resolve, reject) => {
    readyResolve = resolve
    readyReject = reject
  })
  server.on('error', (error) => {
    console.error(`[arc-dsh] LLM bridge error: ${error.message}`)
    readyReject(error)
  })
  let listening = false
  server.once('listening', () => {
    listening = true
    chmodSync(paths.socketPath, 0o600)
    readyResolve()
  })
  server.listen(paths.socketPath)

  let closed = false
  return {
    ...paths,
    ready,
    close() {
      if (closed) return Promise.resolve()
      closed = true
      return new Promise((resolve) => {
        const cleanup = () => {
          if (existsSync(paths.socketPath) && lstatSync(paths.socketPath).isSocket()) {
            unlinkSync(paths.socketPath)
          }
          if (existsSync(paths.tokenPath)) unlinkSync(paths.tokenPath)
          resolve()
        }
        if (listening && server.listening) {
          server.close(cleanup)
          return
        }
        server.once('listening', () => server.close(cleanup))
      })
    },
    readToken() {
      return readFileSync(paths.tokenPath, 'utf8').trim()
    },
  }
}
