// Verify the @goodandready/dsh-image-gen import fix resolves through the
// generation.js <-> index.js circular ESM edge.
// Usage: node tools/_verify-imagegen-fix.mjs

const LIB = 'file:///C:/Users/wangj/.dsh/profiles/web/node_modules/@goodandready/dsh-image-gen/lib/'

const results = []

async function check(label, fn) {
  try {
    const value = await fn()
    results.push({ label, ok: true, detail: value })
  } catch (err) {
    results.push({ label, ok: false, detail: `${err.name}: ${err.message}` })
  }
}

// 1. index.js must export a callable enhancePrompt (its own definition).
await check('index.js exports enhancePrompt', async () => {
  const mod = await import(`${LIB}index.js`)
  return typeof mod.enhancePrompt === 'function' ? 'function' : `NOT A FUNCTION: ${typeof mod.enhancePrompt}`
})

// 2. generation.js imports it -> dependents must resolve without throwing.
await check('generation.js loads via its own imports', async () => {
  await import(`${LIB}tools/generation.js`)
  return 'module evaluated'
})

// 3. The circular edge: importing index.js first, then generation.js,
//    and vice versa, must both leave enhancePrompt callable.
await check('circular edge (index -> generation) preserves enhancePrompt', async () => {
  const idx = await import(`${LIB}index.js`)
  await import(`${LIB}tools/generation.js`)
  return typeof idx.enhancePrompt === 'function' ? 'still a function' : 'LOST'
})

// 4. Confirm the identifier is no longer a bare unresolved reference:
//    call it with enhancePrompt disabled -> must early-return, not ReferenceError.
await check('enhancePrompt(ctx, cfg{enhancePrompt:false}) early-returns', async () => {
  const mod = await import(`${LIB}index.js`)
  const out = await mod.enhancePrompt({}, { enhancePrompt: false }, '中文提示词测试', undefined, 'fal')
  if (!out || out.enhanced !== false || out.prompt !== '中文提示词测试') {
    throw new Error(`unexpected result: ${JSON.stringify(out)}`)
  }
  return 'returned {prompt, enhanced:false}'
})

for (const r of results) {
  console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.label}  ->  ${r.detail}`)
}
console.log(`\n${results.filter(r => r.ok).length}/${results.length} passed`)
process.exit(results.every(r => r.ok) ? 0 : 1)
