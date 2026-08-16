import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { startArcLlmBridge } from './llm-bridge.js'

export const name = 'arc-dsh'
export const inject = ['skills', 'llm']

const adapterDir = dirname(fileURLToPath(import.meta.url))
const skillDir = join(adapterDir, '..', 'skills', 'arc')
const skillPath = join(skillDir, 'SKILL.md')

function readRequiredFrontmatterField(frontmatter, key) {
  const value = new RegExp(`^${key}:\\s*(.+)$`, 'm')
    .exec(frontmatter)?.[1]
    ?.trim()
  if (value === undefined || value === '') {
    throw new Error(`ARC SKILL.md is missing ${key}`)
  }
  return value
}

function readArcSkill() {
  const source = readFileSync(skillPath, 'utf8')
  const match = /^---\r?\n([\s\S]*?)\r?\n---\r?\n([\s\S]*)$/.exec(source)
  if (match === null) {
    throw new Error('ARC SKILL.md is missing YAML frontmatter')
  }

  const skill = {
    name: readRequiredFrontmatterField(match[1], 'name'),
    description: readRequiredFrontmatterField(match[1], 'description'),
    content: match[2],
  }
  if (skill.name !== 'arc') {
    throw new Error(`ARC SKILL.md declares unexpected name: ${skill.name}`)
  }
  return skill
}

export function apply(ctx) {
  ctx.skills.register({
    ...readArcSkill(),
    source: 'bundled',
    path: skillPath,
    resourceBase: { kind: 'directory', path: skillDir },
  })

  const shellEnv = typeof ctx.get === 'function' ? ctx.get('shellEnv') : undefined
  const bridge = startArcLlmBridge({ llm: ctx.llm })
  let shellEnvDispose
  if (shellEnv !== undefined) {
    shellEnvDispose = shellEnv.register({
      name: 'arc-dsh',
      variables: {
        DSH_ARC_LLM_SOCKET: {
          description: 'Unix socket used by ARC to call the native DSH model runtime.',
        },
        DSH_ARC_LLM_TOKEN_FILE: {
          description: '0600 token file used to authenticate ARC to the DSH model bridge.',
        },
        DSH_ARC_RUNTIME: {
          description: 'Absolute ARC runtime launcher path for DSH model shell commands.',
        },
        DSH_ARC_BIN_DIR: {
          description: 'Absolute directory containing ARC command wrappers.',
        },
      },
      resolve() {
        return {
          DSH_ARC_LLM_SOCKET: bridge.socketPath,
          DSH_ARC_LLM_TOKEN_FILE: bridge.tokenPath,
          DSH_ARC_RUNTIME: join(skillDir, 'scripts', 'arc-runtime'),
          DSH_ARC_BIN_DIR: join(adapterDir, '..', 'bin'),
        }
      },
    })
  }
  ctx.effect(
    () => () => {
      shellEnvDispose?.()
      return bridge.close()
    },
    'arc-dsh:llm-bridge',
  )
}
