import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const textSource = /\.(?:vue|[cm]?[jt]s|css|json|html|svg)$/i

export function checkBuildInputs(root = frontendRoot) {
  const files = ['index.html', 'vite.config.js']
  function collect(directory) {
    for (const entry of readdirSync(join(root, directory), { withFileTypes: true })) {
      const name = join(directory, entry.name)
      if (entry.isDirectory()) collect(name)
      else if (textSource.test(name)) files.push(name)
    }
  }
  collect('src')
  const invalid = files.filter(name => readFileSync(join(root, name)).includes(13))
  if (invalid.length) {
    throw new Error(`Frontend build inputs must use LF line endings:\n${invalid.map(name => `  ${name}`).join('\n')}\nSave these files as LF and rebuild. .gitattributes does not rewrite existing working files; CRLF changes Vue scope IDs and bundled asset hashes.`)
  }
}

if (process.argv[1] && relative(resolve(process.argv[1]), fileURLToPath(import.meta.url)) === '') {
  try {
    checkBuildInputs()
    console.log('Frontend build inputs use LF line endings')
  } catch (error) {
    console.error(error.message)
    process.exitCode = 1
  }
}
