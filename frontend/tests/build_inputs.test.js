import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, expect, test } from 'vitest'
import { checkBuildInputs } from '../scripts/check-build-inputs.mjs'

let root
function fixture() {
  root = mkdtempSync(join(tmpdir(), 'magpie-build-inputs-'))
  mkdirSync(join(root, 'src', 'components'), { recursive: true })
  writeFileSync(join(root, 'index.html'), '<div id="app"></div>\n')
  writeFileSync(join(root, 'vite.config.js'), 'export default {}\n')
  return join(root, 'src', 'components', 'Example.vue')
}
afterEach(() => { if (root) rmSync(root, { recursive: true, force: true }) })

test('accepts canonical LF sources, including nested Vue styles', () => {
  writeFileSync(fixture(), '<template>示例</template>\n<style scoped>div { color: red; }</style>\n')
  expect(() => checkBuildInputs(root)).not.toThrow()
})

test.each(['\r\n', '\n\r\n'])('rejects noncanonical Vue input before building without rewriting it (%j)', ending => {
  const file = fixture()
  const source = `<template>示例</template>${ending}<style scoped>div { color: red; }</style>\n`
  writeFileSync(file, source)
  expect(() => checkBuildInputs(root)).toThrow(/Example\.vue/)
  expect(readFileSync(file, 'utf8')).toBe(source)
})

test('reports all affected inputs, including HTML and Vite config', () => {
  writeFileSync(fixture(), '<template>ok</template>\n')
  writeFileSync(join(root, 'index.html'), '<div/>\r\n')
  writeFileSync(join(root, 'vite.config.js'), 'export default {}\r\n')
  expect(() => checkBuildInputs(root)).toThrow(/index\.html[\s\S]*vite\.config\.js/)
})
