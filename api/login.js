// Vercel Serverless Function (Node runtime) — passwordless sign-in for
// non-admin portal users.
//
// Flow:
//   1. The browser POSTs { email }.
//   2. We use the Supabase service-role key (server-side ONLY) to confirm the
//      email belongs to an ACTIVE, NON-ADMIN row in portal_users.
//   3. We make sure a Supabase Auth user exists for that email (auto-provision
//      on first sign-in — admins no longer need to create it by hand).
//   4. We mint a magic-link token and return its token_hash. The browser then
//      calls supabase.auth.verifyOtp({ token_hash, type:'magiclink' }) to get a
//      real Supabase session — the SAME JWT the password flow produces, so the
//      .db middleware and the RLS policies keep working unchanged.
//
// Admins MAY use this passwordless flow too — they just get standard
// (non-admin) access for that session. To unlock the admin panel they sign in
// with their password via the "Sign in as Admin" flow. The downgrade is applied
// in the browser (see doPasswordlessLogin in index.html).
//
// Unknown emails (not in portal_users) are refused: NO Auth account is ever
// created for an email that isn't already registered in the dashboard.
//
// Required environment variables (Vercel → Project → Settings → Environment):
//   SUPABASE_URL               (already used by build.sh / middleware.js)
//   SUPABASE_SERVICE_ROLE_KEY  (NEW — keep secret, never expose to the client)

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const SUPABASE_URL = process.env.SUPABASE_URL;
  const SERVICE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!SUPABASE_URL || !SERVICE_KEY) {
    return res.status(500).json({ error: 'Server not configured.' });
  }

  // Vercel parses JSON bodies automatically; fall back to a manual parse.
  let body = req.body;
  if (typeof body === 'string') {
    try { body = JSON.parse(body); } catch { body = {}; }
  }
  const email = String(body?.email || '').trim().toLowerCase();
  if (!EMAIL_RE.test(email)) {
    return res.status(400).json({ error: 'Please enter a valid email.' });
  }

  const adminHeaders = {
    apikey: SERVICE_KEY,
    Authorization: `Bearer ${SERVICE_KEY}`,
    'Content-Type': 'application/json',
  };

  try {
    // ── 1. Resolve the portal user (by email, then by alias) ──────────
    const user = await findPortalUser(SUPABASE_URL, adminHeaders, email);
    if (!user) {
      return res.status(403).json({ error: 'Email not recognized. Contact your administrator.' });
    }
    if (user.active === false) {
      return res.status(403).json({ error: 'Account inactive. Contact your administrator.' });
    }
    // Admins are allowed through: the browser downgrades them to standard
    // access for the passwordless session (admin panel needs the password flow).

    const realEmail = String(user.email || email).toLowerCase();

    // ── 2. Make sure an Auth user exists (idempotent) ─────────────────
    await ensureAuthUser(SUPABASE_URL, adminHeaders, realEmail);

    // ── 3. Mint a magic-link token ────────────────────────────────────
    const tokenHash = await generateMagicLink(SUPABASE_URL, adminHeaders, realEmail);
    if (!tokenHash) {
      return res.status(500).json({ error: 'Could not start session. Please try again.' });
    }

    return res.status(200).json({ token_hash: tokenHash, email: realEmail });
  } catch (err) {
    return res.status(500).json({ error: 'Sign-in failed. Please try again.' });
  }
}

// Look up the portal user by email, falling back to the optional alias column.
async function findPortalUser(url, headers, email) {
  const enc = encodeURIComponent(email);
  let rows = await restSelect(url, headers,
    `portal_users?select=email,is_admin,active&email=eq.${enc}`);
  if (rows && rows.length) return rows[0];
  // Best-effort alias match (column may not exist in every deployment).
  try {
    rows = await restSelect(url, headers,
      `portal_users?select=email,is_admin,active&alias=eq.${enc}`);
    if (rows && rows.length) return rows[0];
  } catch { /* alias column absent — ignore */ }
  return null;
}

async function restSelect(url, headers, path) {
  const r = await fetch(`${url}/rest/v1/${path}`, { headers });
  if (!r.ok) throw new Error(`REST ${r.status}`);
  return r.json();
}

// Create the Auth user if it doesn't exist yet. 422 means "already registered".
async function ensureAuthUser(url, headers, email) {
  const r = await fetch(`${url}/auth/v1/admin/users`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ email, email_confirm: true }),
  });
  if (r.ok || r.status === 422) return;
  throw new Error(`createUser ${r.status}`);
}

async function generateMagicLink(url, headers, email) {
  const r = await fetch(`${url}/auth/v1/admin/generate_link`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ type: 'magiclink', email }),
  });
  if (!r.ok) throw new Error(`generate_link ${r.status}`);
  const j = await r.json();
  // GoTrue versions differ: hashed_token may be top-level, nested under
  // `properties`, or only embedded in action_link as ?token=...
  return (
    j.hashed_token ||
    j.properties?.hashed_token ||
    extractTokenFromLink(j.action_link || j.properties?.action_link)
  );
}

function extractTokenFromLink(link) {
  if (!link) return null;
  try { return new URL(link).searchParams.get('token'); } catch { return null; }
}
