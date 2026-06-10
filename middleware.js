export const config = {
  matcher: ['/(.*\\.db)'],
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

export default async function middleware(request) {
  const cookieHeader = request.headers.get('cookie') || ''
  const match = cookieHeader.match(/(?:^|;\s*)ibba_auth=([^;]+)/)
  const token = match ? decodeURIComponent(match[1]) : null

  if (!token || !token.startsWith('eyJ')) {
    return new Response('Unauthorized — please log in at the portal.', {
      status: 401,
      headers: { 'Content-Type': 'text/plain' },
    })
  }

  try {
    const payload = await verifyJWT(token)
    if (!payload) {
      return new Response('Unauthorized — invalid token.', {
        status: 401,
        headers: { 'Content-Type': 'text/plain' },
      })
    }
    if (payload.exp && Date.now() / 1000 > payload.exp) {
      return new Response('Session expired — please log in again.', {
        status: 401,
        headers: { 'Content-Type': 'text/plain' },
      })
    }
  } catch {
    return new Response('Unauthorized', { status: 401 })
  }
}
