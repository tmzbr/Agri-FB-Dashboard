export const config = {
  matcher: ['/(.*\\.db)'],
}

export default function middleware(request) {
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
    const payload = JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')))
    if (payload.exp && Date.now() / 1000 > payload.exp) {
      return new Response('Session expired — please log in again.', {
        status: 401,
        headers: { 'Content-Type': 'text/plain' },
      })
    }
  } catch {
    return new Response('Unauthorized', { status: 401 })
  }
  // Token present and not expired — allow the request through
}
