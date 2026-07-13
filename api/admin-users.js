// Vercel Serverless Function (Node runtime) — admin-only user management.
//
// Lets a signed-in ADMIN create logins (including other admins, with a
// password), reset passwords, and delete users entirely from the dashboard —
// no manual step in the Supabase console.
//
// Security model:
//   This endpoint uses the Supabase SERVICE-ROLE key (full power), so every
//   request is authorized first:
//     1. The browser sends the caller's Supabase access token as a Bearer.
//     2. We resolve it via GoTrue /auth/v1/user (validates the signature for
//        us, ES256 or HS256) to get the caller's email.
//     3. We confirm that email is an ACTIVE admin in portal_users.
//   Only then do we touch anything with the service-role key.
//
// Actions (POST body { action, ... }):
//   create        { email, display_name, is_admin, password? }
//                 → admins: create Auth user with password; everyone: insert
//                   the portal_users row. Non-admins stay passwordless and are
//                   auto-provisioned by /api/login on first sign-in.
//   set_password  { email, password }   → create-or-update the Auth password.
//   delete        { email }             → delete the Auth user + portal_users
//                                          row (blocked for the last admin).
//
// Required environment variables (same as /api/login):
//   SUPABASE_URL
//   SUPABASE_SERVICE_ROLE_KEY   (server-side ONLY — never exposed to the client)

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const MIN_PASSWORD = 6;

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

  const adminHeaders = {
    apikey: SERVICE_KEY,
    Authorization: `Bearer ${SERVICE_KEY}`,
    'Content-Type': 'application/json',
  };

  // ── Authorize: caller must be an active admin ─────────────────────
  let callerEmail;
  try {
    callerEmail = await requireAdmin(req, SUPABASE_URL, SERVICE_KEY, adminHeaders);
  } catch (err) {
    return res.status(err.status || 401).json({ error: err.message });
  }

  // ── Parse body ────────────────────────────────────────────────────
  let body = req.body;
  if (typeof body === 'string') {
    try { body = JSON.parse(body); } catch { body = {}; }
  }
  body = body || {};
  const action = String(body.action || '');
  const email = String(body.email || '').trim().toLowerCase();

  try {
    if (action === 'create') {
      return await handleCreate(res, SUPABASE_URL, adminHeaders, body, email);
    }
    if (action === 'set_password') {
      return await handleSetPassword(res, SUPABASE_URL, adminHeaders, email, body.password);
    }
    if (action === 'delete') {
      return await handleDelete(res, SUPABASE_URL, adminHeaders, email, callerEmail);
    }
    return res.status(400).json({ error: 'Unknown action.' });
  } catch (err) {
    return res.status(err.status || 500).json({ error: err.message || 'Request failed.' });
  }
}

// ─────────────────────────────────────────────────────────────────────
// Authorization
// ─────────────────────────────────────────────────────────────────────

async function requireAdmin(req, url, serviceKey, adminHeaders) {
  const auth = req.headers?.authorization || '';
  const token = auth.startsWith('Bearer ') ? auth.slice(7).trim() : '';
  if (!token) throw httpError(401, 'Not authenticated.');

  // Resolve the token through GoTrue (validates the signature for us).
  const ur = await fetch(`${url}/auth/v1/user`, {
    headers: { apikey: serviceKey, Authorization: `Bearer ${token}` },
  });
  if (!ur.ok) throw httpError(401, 'Invalid or expired session.');
  const who = await ur.json();
  const email = String(who?.email || '').toLowerCase();
  if (!email) throw httpError(401, 'Invalid session.');

  const rows = await restSelect(url, adminHeaders,
    `portal_users?select=email,is_admin,active&email=eq.${encodeURIComponent(email)}`);
  const me = rows && rows[0];
  if (!me || me.is_admin !== true || me.active === false) {
    throw httpError(403, 'Admin privileges required.');
  }
  return email;
}

// ─────────────────────────────────────────────────────────────────────
// Actions
// ─────────────────────────────────────────────────────────────────────

async function handleCreate(res, url, headers, body, email) {
  const displayName = String(body.display_name || '').trim();
  const isAdmin = body.is_admin === true;
  const password = String(body.password || '');
  const client = String(body.client || '').trim();

  if (!EMAIL_RE.test(email)) return res.status(400).json({ error: 'Please enter a valid email.' });
  if (!displayName) return res.status(400).json({ error: 'Display name is required.' });

  // Don't duplicate an existing portal user.
  const existing = await restSelect(url, headers,
    `portal_users?select=email&email=eq.${encodeURIComponent(email)}`);
  if (existing && existing.length) {
    return res.status(409).json({ error: 'Email already registered.' });
  }

  if (isAdmin) {
    if (password.length < MIN_PASSWORD) {
      return res.status(400).json({ error: `Admin password must be at least ${MIN_PASSWORD} characters.` });
    }
    // Create (or, if the Auth account already exists, set) the password.
    await upsertAuthPassword(url, headers, email, password);
  }

  const row = { email, display_name: displayName, is_admin: isAdmin, active: true };
  if (client) row.client = client;
  let inserted;
  try {
    inserted = await restInsert(url, headers, 'portal_users', row);
  } catch (e) {
    // If the optional `client` column isn't present yet (migration not run),
    // don't block user creation — retry without it. Real errors (e.g. a
    // duplicate email → 409) still surface on the retry.
    if (client) {
      const { client: _omit, ...base } = row;
      inserted = await restInsert(url, headers, 'portal_users', base);
    } else {
      throw e;
    }
  }
  return res.status(200).json(inserted);
}

async function handleSetPassword(res, url, headers, email, passwordRaw) {
  const password = String(passwordRaw || '');
  if (!EMAIL_RE.test(email)) return res.status(400).json({ error: 'Please enter a valid email.' });
  if (password.length < MIN_PASSWORD) {
    return res.status(400).json({ error: `Password must be at least ${MIN_PASSWORD} characters.` });
  }
  // Must be a known portal user.
  const rows = await restSelect(url, headers,
    `portal_users?select=email&email=eq.${encodeURIComponent(email)}`);
  if (!rows || !rows.length) return res.status(404).json({ error: 'User not found in the portal.' });

  await upsertAuthPassword(url, headers, email, password);
  return res.status(200).json({ ok: true });
}

async function handleDelete(res, url, headers, email, callerEmail) {
  if (!EMAIL_RE.test(email)) return res.status(400).json({ error: 'Please enter a valid email.' });

  // Guard: never delete the last active admin (would lock everyone out).
  const target = await restSelect(url, headers,
    `portal_users?select=is_admin,active&email=eq.${encodeURIComponent(email)}`);
  const t = target && target[0];
  if (t && t.is_admin === true && t.active !== false) {
    const admins = await restSelect(url, headers,
      `portal_users?select=email&is_admin=eq.true&active=eq.true`);
    if (admins && admins.length <= 1) {
      return res.status(409).json({ error: 'Cannot delete the last active admin.' });
    }
  }

  // Delete the Auth account (if any), then the portal row.
  const authUser = await findAuthUserByEmail(url, headers, email);
  if (authUser?.id) {
    const dr = await fetch(`${url}/auth/v1/admin/users/${authUser.id}`, {
      method: 'DELETE', headers,
    });
    if (!dr.ok && dr.status !== 404) throw httpError(502, `Could not delete login (${dr.status}).`);
  }
  await restDelete(url, headers, `portal_users?email=eq.${encodeURIComponent(email)}`);
  return res.status(200).json({ ok: true });
}

// ─────────────────────────────────────────────────────────────────────
// Supabase helpers
// ─────────────────────────────────────────────────────────────────────

// Create the Auth user with this password, or update the password if the
// account already exists. email_confirm:true so they can sign in immediately.
async function upsertAuthPassword(url, headers, email, password) {
  const cr = await fetch(`${url}/auth/v1/admin/users`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ email, password, email_confirm: true }),
  });
  if (cr.ok) return;
  if (cr.status !== 422) throw httpError(502, `Could not create login (${cr.status}).`);

  // 422 → already registered: update the existing user's password.
  const user = await findAuthUserByEmail(url, headers, email);
  if (!user?.id) throw httpError(502, 'Login exists but could not be located.');
  const ur = await fetch(`${url}/auth/v1/admin/users/${user.id}`, {
    method: 'PUT',
    headers,
    body: JSON.stringify({ password, email_confirm: true }),
  });
  if (!ur.ok) throw httpError(502, `Could not update password (${ur.status}).`);
}

// Page through the admin user list to find a user by email (case-insensitive).
// GoTrue may cap per_page below what we ask, so we stop only on an empty page
// (not on a short one) to avoid missing later pages.
async function findAuthUserByEmail(url, headers, email) {
  const target = email.toLowerCase();
  for (let page = 1; page <= 100; page++) {
    const r = await fetch(`${url}/auth/v1/admin/users?page=${page}&per_page=1000`, { headers });
    if (!r.ok) throw httpError(502, `Could not list logins (${r.status}).`);
    const j = await r.json();
    const users = Array.isArray(j) ? j : (j.users || []);
    if (!users.length) break; // past the last page
    const hit = users.find(u => String(u.email || '').toLowerCase() === target);
    if (hit) return hit;
  }
  return null;
}

async function restSelect(url, headers, path) {
  const r = await fetch(`${url}/rest/v1/${path}`, { headers });
  if (!r.ok) throw httpError(502, `Database read failed (${r.status}).`);
  return r.json();
}

async function restInsert(url, headers, table, row) {
  const r = await fetch(`${url}/rest/v1/${table}`, {
    method: 'POST',
    headers: { ...headers, Prefer: 'return=representation' },
    body: JSON.stringify(row),
  });
  if (!r.ok) {
    if (r.status === 409) throw httpError(409, 'Email already registered.');
    throw httpError(502, `Database write failed (${r.status}).`);
  }
  const rows = await r.json();
  return Array.isArray(rows) ? rows[0] : rows;
}

async function restDelete(url, headers, path) {
  const r = await fetch(`${url}/rest/v1/${path}`, { method: 'DELETE', headers });
  if (!r.ok) throw httpError(502, `Database delete failed (${r.status}).`);
}

function httpError(status, message) {
  const e = new Error(message);
  e.status = status;
  return e;
}
