// Protected paths:
//   • every .db file (the datasets)
//   • every .html inside a subdirectory (the dashboard modules)
// The root index.html (the portal shell + login screen) stays public — it is
// what an unauthenticated visitor must reach in order to sign in. Modules live
// one or more folders deep, so requiring a directory segment separates them.
export const config = {
  matcher: ['/(.*\\.db)', '/(.*)/(.*\\.html)'],
}

// Module-level JWKS cache — survives across requests on the same Edge instance
let jwksCache = null
let jwksCacheTime = 0
const JWKS_TTL = 3600 * 1000 // 1 hour

async function getJWKS() {
  const now = Date.now()
  if (jwksCache && now - jwksCacheTime < JWKS_TTL) return jwksCache
  const url = `${process.env.SUPABASE_URL}/auth/v1/.well-known/jwks.json`
  const res = await fetch(url)
  if (!res.ok) throw new Error('Failed to fetch JWKS')
  const { keys } = await res.json()
  jwksCache = keys
  jwksCacheTime = now
  return keys
}

function b64urlDecode(str) {
  return Uint8Array.from(atob(str.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0))
}

async function verifyES256(headerB64, payloadB64, sigB64, jwk) {
  const key = await crypto.subtle.importKey(
    'jwk', jwk,
    { name: 'ECDSA', namedCurve: 'P-256' },
    false, ['verify']
  )
  const data = new TextEncoder().encode(`${headerB64}.${payloadB64}`)
  const sig = b64urlDecode(sigB64)
  return crypto.subtle.verify({ name: 'ECDSA', hash: 'SHA-256' }, key, sig, data)
}

async function verifyHS256(headerB64, payloadB64, sigB64, secret) {
  const key = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(secret),
    { name: 'HMAC', hash: 'SHA-256' }, false, ['verify']
  )
  const data = new TextEncoder().encode(`${headerB64}.${payloadB64}`)
  const sig = b64urlDecode(sigB64)
  return crypto.subtle.verify('HMAC', key, sig, data)
}

async function verifyJWT(token) {
  const parts = token.split('.')
  if (parts.length !== 3) return null
  const [headerB64, payloadB64, sigB64] = parts

  const header = JSON.parse(atob(headerB64.replace(/-/g, '+').replace(/_/g, '/')))
  const payload = JSON.parse(atob(payloadB64.replace(/-/g, '+').replace(/_/g, '/')))

  let valid = false

  if (header.alg === 'ES256') {
    const keys = await getJWKS()
    const jwk = keys.find(k => k.kid === header.kid && k.alg === 'ES256') || keys.find(k => k.alg === 'ES256')
    if (!jwk) return null
    valid = await verifyES256(headerB64, payloadB64, sigB64, jwk)
  } else if (header.alg === 'HS256') {
    const secret = process.env.SUPABASE_JWT_SECRET
    if (!secret) return null
    valid = await verifyHS256(headerB64, payloadB64, sigB64, secret)
  }

  if (!valid) return null
  return payload
}

// A blocked .db is consumed by fetch() inside a module, so plain text is right.
// A blocked .html is something a person is looking at — send them a page that
// explains the situation and links back to the portal.
function denied(request, message) {
  const isHtml = new URL(request.url).pathname.endsWith('.html')
  if (!isHtml) {
    return new Response(message, {
      status: 401,
      headers: { 'Content-Type': 'text/plain' },
    })
  }
  const body = `<!doctype html><meta charset="utf-8">
<title>Sign in required — Agribusiness, F&B DataHouse</title>
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       background:#fafafa;color:#1a1a1a;text-align:center}
  .box{max-width:420px;padding:32px}
  h1{font-size:20px;margin:0 0 12px}
  p{margin:0 0 24px;color:#555}
  a{display:inline-block;padding:10px 20px;border-radius:6px;
    background:#ec7000;color:#fff;text-decoration:none;font-weight:600}
</style>
<div class="box">
  <h1>Sign in required</h1>
  <p>${message}</p>
  <p>Dashboards open from inside the portal — direct links no longer work.</p>
  <a href="/" target="_top">Go to the portal</a>
</div>`
  return new Response(body, {
    status: 401,
    headers: { 'Content-Type': 'text/html; charset=utf-8' },
  })
}

export default async function middleware(request) {
  const cookieHeader = request.headers.get('cookie') || ''
  const match = cookieHeader.match(/(?:^|;\s*)agri_auth=([^;]+)/)
  const token = match ? decodeURIComponent(match[1]) : null

  if (!token || !token.startsWith('eyJ')) {
    return denied(request, 'Please log in at the portal.')
  }

  try {
    const payload = await verifyJWT(token)
    if (!payload) {
      return denied(request, 'Your session token is not valid.')
    }
    if (payload.exp && Date.now() / 1000 > payload.exp) {
      return denied(request, 'Your session expired. Please log in again.')
    }
  } catch {
    return denied(request, 'Could not verify your session.')
  }
}
