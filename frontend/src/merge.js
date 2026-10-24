// Compare editable fields against the version actually read by this window.
// Applying a plan only prepares a draft; the server still checks its version.
export function mergeFields(definitions, original, latest, draft) {
  const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b)
  return definitions.map(({ key, label, format = value => String(value || '（空）') }) => {
    const conflict = !equal(draft[key], original[key]) && !equal(latest[key], original[key]) && !equal(draft[key], latest[key])
    return { key, label, original: original[key], latest: latest[key], draft: draft[key], conflict,
      choice: conflict ? '' : equal(draft[key], original[key]) || equal(draft[key], latest[key]) ? 'latest' : 'draft',
      display: { original: format(original[key]), latest: format(latest[key]), draft: format(draft[key]) } }
  })
}
