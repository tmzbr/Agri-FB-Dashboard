import os
import sys
import json
import ssl
import time
import threading
import pg8000
import urllib.request as ureq
from functools import wraps
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
import jwt as pyjwt
from jwt import PyJWKClient

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DB_HOST     = os.environ.get("DB_HOST", "")
DB_USER     = os.environ.get("DB_USER", "postgres")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME     = os.environ.get("DB_NAME", "postgres")
DB_PORT     = int(os.environ.get("DB_PORT", "5432"))

# Same portal identity the Vercel middleware already verifies (see
# middleware.js) — reused here so the write endpoints check identity
# server-side instead of trusting the data-admin-only hiding in the UI.
SUPABASE_URL              = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

app = Flask(__name__)


@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


# ── Auth ────────────────────────────────────────────────────────────────────
# Mutating endpoints require a valid Supabase access token (the same one the
# portal stores after login), passed as "Authorization: Bearer <token>". The
# UI already hides admin-only buttons via data-admin-only in sector_news.html,
# but that's cosmetic — this is the actual enforcement.

_jwks_client = None

def _get_jwks_client():
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json")
    return _jwks_client


def _verify_supabase_jwt(token):
    """Returns the decoded payload for a valid, unexpired Supabase access
    token, else None. Signature is checked against Supabase's public JWKS."""
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        payload = pyjwt.decode(token, signing_key.key, algorithms=["ES256"],
                                options={"verify_aud": False})
    except Exception:
        return None
    if payload.get("exp") and time.time() > payload["exp"]:
        return None
    return payload


def _portal_user(email):
    """Looks up is_admin/active for email via the Supabase REST API, using the
    service-role key so it bypasses RLS. This call is server-to-server —
    the key never reaches the browser."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    url = f"{SUPABASE_URL}/rest/v1/portal_users?select=is_admin,active&email=eq.{email.lower()}"
    req = ureq.Request(url, headers={
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    })
    try:
        with ureq.urlopen(req, timeout=10) as resp:
            rows = json.loads(resp.read())
    except Exception:
        return None
    return rows[0] if rows else None


def _authenticated_user():
    """Verifies the bearer token on the current request and returns the
    matching active portal_users row, or None."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    payload = _verify_supabase_jwt(auth[len("Bearer "):])
    email = payload.get("email") if payload else None
    if not email:
        return None
    user = _portal_user(email)
    return user if user and user.get("active") else None


def require_user(fn):
    """Any signed-in, active portal user — for actions every client can
    trigger (e.g. Refresh), just not the general public."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not _authenticated_user():
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper


def require_admin(fn):
    """Signed-in AND is_admin — for the destructive/config actions the UI
    already hides behind data-admin-only."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = _authenticated_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        if not user.get("is_admin"):
            return jsonify({"error": "forbidden"}), 403
        return fn(*args, **kwargs)
    return wrapper


# ── Database ────────────────────────────────────────────────────────────────

def get_db():
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    return pg8000.connect(
        host=DB_HOST, user=DB_USER, password=DB_PASSWORD,
        database=DB_NAME, port=DB_PORT, ssl_context=ssl_ctx
    )


def _fetchall(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _fetchone(cur):
    if cur.description is None:
        return None
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    return dict(zip(cols, row)) if row else None


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS news (
            id                SERIAL PRIMARY KEY,
            title             TEXT    NOT NULL,
            url               TEXT    UNIQUE NOT NULL,
            source            TEXT    DEFAULT '',
            published_at      TEXT,
            fetched_at        TEXT    DEFAULT to_char(NOW(), 'YYYY-MM-DD"T"HH24:MI:SS'),
            summary           TEXT    DEFAULT '',
            group_id          INTEGER NOT NULL,
            relevance_score   REAL    DEFAULT 0.5,
            is_excluded       INTEGER DEFAULT 0,
            is_manually_added INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS learned_exclusions (
            id         SERIAL PRIMARY KEY,
            pattern    TEXT UNIQUE NOT NULL,
            reason     TEXT DEFAULT '',
            created_at TEXT DEFAULT to_char(NOW(), 'YYYY-MM-DD"T"HH24:MI:SS')
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


# ── Config ───────────────────────────────────────────────────────────────────

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


# ── Scheduled fetch ──────────────────────────────────────────────────────────

# Single-slot concurrency guard so overlapping manual triggers don't pile up.
_fetch_lock = threading.Lock()
_fetch_running = {"flag": False, "started_at": None, "last_result": None}


def _run_fetch_once():
    """Actual fetch body: called in the background thread."""
    from news_fetcher import NewsFetcher
    from relevance_filter import RelevanceFilter
    config = load_config()
    fetcher = NewsFetcher(config)
    rf = RelevanceFilter(config)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT pattern FROM learned_exclusions")
    exclusions = _fetchall(cur)
    # Commit in small batches so partial progress survives worker restarts /
    # Render proxy timeouts. Previously all inserts were committed at the very
    # end of a multi-minute fetch — killing the worker lost everything.
    BATCH = 20
    saved = 0
    blocked = 0
    try:
        items = fetcher.fetch_all()
        for i, item in enumerate(items, 1):
            # Hard-block low-quality / out-of-coverage sources at ingest.
            if rf.is_blocked_source(item):
                blocked += 1
                continue
            score = rf.score(item, exclusions)
            excluded = 1 if rf.is_branded_content(item) else 0
            try:
                cur.execute(
                    "INSERT INTO news (title,url,source,published_at,summary,group_id,relevance_score,is_excluded) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (url) DO NOTHING",
                    (item["title"], item["url"], item["source"],
                     item["published_at"], item.get("summary", ""), item["group_id"], score, excluded),
                )
                saved += 1
            except Exception:
                pass
            if i % BATCH == 0:
                conn.commit()
        conn.commit()
        return {"fetched": len(items), "saved": saved, "blocked": blocked}
    finally:
        cur.close()
        conn.close()


def scheduled_fetch():
    """Entry point for both APScheduler and the manual-trigger thread.
    Guards against overlapping runs (single-slot lock)."""
    if not _fetch_lock.acquire(blocking=False):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetch já em andamento — skip.")
        return
    try:
        _fetch_running["flag"] = True
        _fetch_running["started_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Atualizando feed...")
        result = _run_fetch_once()
        _fetch_running["last_result"] = result
        print(f"  Salvos {result['saved']} novos itens de {result['fetched']} encontrados.\n")
    except Exception as e:
        _fetch_running["last_result"] = {"error": str(e)}
        print(f"  [ERR] scheduled_fetch: {e}")
    finally:
        _fetch_running["flag"] = False
        _fetch_lock.release()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return "Feed News API running", 200


@app.route("/api/news")
def api_get_news():
    group_id = request.args.get("group_id", type=int)
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    show_excluded = request.args.get("show_excluded", "false") == "true"
    min_score = request.args.get("min_score", 0.0, type=float)

    conn = get_db()
    cur = conn.cursor()
    q = "SELECT * FROM news WHERE 1=1"
    params = []

    if group_id:
        q += " AND group_id=%s"
        params.append(group_id)
    if date_from:
        q += " AND published_at>=%s"
        params.append(date_from)
    if date_to:
        q += " AND published_at<=%s"
        params.append(date_to + "T23:59:59")
    if not show_excluded:
        q += " AND is_excluded=0"
    if min_score > 0:
        q += " AND relevance_score>=%s"
        params.append(min_score)

    q += " ORDER BY published_at DESC, id DESC"
    cur.execute(q, params)
    rows = _fetchall(cur)
    cur.close()
    conn.close()
    return jsonify(rows)


@app.route("/api/news/fetch", methods=["POST"])
@require_user
def api_fetch_news():
    """Kick off fetch in a background thread and return immediately.
    A full fetch can take 2-5 min (100+ queries + 12 direct feeds), which
    exceeds Render's proxy timeout (~100s). Running async + committing in
    batches keeps the HTTP layer responsive and persists partial progress."""
    if _fetch_running["flag"]:
        return jsonify({"success": True, "status": "already_running",
                        "started_at": _fetch_running["started_at"]}), 202
    t = threading.Thread(target=scheduled_fetch, daemon=True)
    t.start()
    return jsonify({"success": True, "status": "started"}), 202


@app.route("/api/news/fetch/status")
def api_fetch_status():
    return jsonify({
        "running": _fetch_running["flag"],
        "started_at": _fetch_running["started_at"],
        "last_result": _fetch_running["last_result"],
    })


@app.route("/api/news", methods=["POST"])
@require_admin
def api_add_news():
    data = request.json or {}
    if not data.get("title") or not data.get("url") or not data.get("group_id"):
        return jsonify({"error": "title, url e group_id são obrigatórios"}), 400
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO news (title,url,source,published_at,summary,group_id,relevance_score,is_manually_added) "
            "VALUES (%s,%s,%s,%s,%s,%s,1.0,1)",
            (data["title"], data["url"], data.get("source", "Manual"),
             data.get("published_at", (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")),
             data.get("summary", ""), data["group_id"]),
        )
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 400
    finally:
        cur.close()
        conn.close()


@app.route("/api/news/<int:nid>/exclude", methods=["POST"])
@require_admin
def api_exclude(nid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE news SET is_excluded=1 WHERE id=%s", (nid,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/news/<int:nid>/include", methods=["POST"])
@require_admin
def api_include(nid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE news SET is_excluded=0 WHERE id=%s", (nid,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/news/<int:nid>", methods=["DELETE"])
@require_admin
def api_delete_news(nid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM news WHERE id=%s", (nid,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/groups")
def api_get_groups():
    return jsonify(load_config()["groups"])


@app.route("/api/groups/<int:gid>", methods=["PUT"])
@require_admin
def api_update_group(gid):
    data = request.json or {}
    config = load_config()
    for i, g in enumerate(config["groups"]):
        if g["id"] == gid:
            for key in ("keywords", "tickers", "negative_keywords", "companies",
                        "search_queries_pt", "search_queries_en"):
                if key in data:
                    config["groups"][i][key] = data[key]
            break
    save_config(config)
    return jsonify({"success": True})


@app.route("/api/exclusions")
def api_get_exclusions():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM learned_exclusions ORDER BY created_at DESC")
    rows = _fetchall(cur)
    cur.close()
    conn.close()
    return jsonify(rows)


@app.route("/api/exclusions", methods=["POST"])
@require_admin
def api_add_exclusion():
    data = request.json or {}
    pattern = (data.get("pattern") or "").strip()
    if not pattern:
        return jsonify({"error": "pattern é obrigatório"}), 400
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO learned_exclusions (pattern,reason) VALUES (%s,%s) ON CONFLICT (pattern) DO NOTHING",
            (pattern, data.get("reason", "")),
        )
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 400
    finally:
        cur.close()
        conn.close()


@app.route("/api/exclusions/<int:eid>", methods=["DELETE"])
@require_admin
def api_delete_exclusion(eid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM learned_exclusions WHERE id=%s", (eid,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/stats")
def api_stats():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as cnt FROM news")
    total = _fetchone(cur)["cnt"]
    cur.execute("SELECT COUNT(*) as cnt FROM news WHERE is_excluded=1")
    excluded = _fetchone(cur)["cnt"]
    today_start = date.today().strftime("%Y-%m-%d")
    tomorrow_start = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    cur.execute(
        "SELECT COUNT(*) as cnt FROM news WHERE published_at >= %s AND published_at < %s",
        (today_start, tomorrow_start),
    )
    today_count = _fetchone(cur)["cnt"]
    cur.execute(
        "SELECT group_id, COUNT(*) as cnt FROM news WHERE is_excluded=0 GROUP BY group_id"
    )
    by_group = {r["group_id"]: r["cnt"] for r in _fetchall(cur)}
    cur.close()
    conn.close()
    return jsonify({"total": total, "excluded": excluded, "today": today_count, "by_group": by_group})


_rescore_lock = threading.Lock()
_rescore_running = {"flag": False, "started_at": None, "progress": 0, "total": 0,
                    "last_result": None}


def _run_rescore_once():
    """Batch-committed rescore. Survives gunicorn --timeout kill since each
    batch is persisted as it lands — a re-trigger picks up from where the
    previous run died (scores converge to current filter)."""
    from relevance_filter import RelevanceFilter
    config = load_config()
    rf = RelevanceFilter(config)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT pattern FROM learned_exclusions")
    exclusions = _fetchall(cur)
    cur.execute("SELECT id, title, url, source, summary FROM news WHERE is_manually_added=0")
    rows = _fetchall(cur)
    _rescore_running["total"] = len(rows)
    _rescore_running["progress"] = 0
    BATCH = 200
    branded = 0
    try:
        for i, row in enumerate(rows, 1):
            score = rf.score(row, exclusions)
            if rf.is_branded_content(row):
                cur.execute("UPDATE news SET relevance_score=%s, is_excluded=1 WHERE id=%s", (score, row["id"]))
                branded += 1
            else:
                cur.execute("UPDATE news SET relevance_score=%s WHERE id=%s", (score, row["id"]))
            if i % BATCH == 0:
                conn.commit()
                _rescore_running["progress"] = i
        conn.commit()
        _rescore_running["progress"] = len(rows)
        return {"rescored": len(rows), "branded_excluded": branded}
    finally:
        cur.close()
        conn.close()


def _async_rescore():
    if not _rescore_lock.acquire(blocking=False):
        print("Rescore já em andamento — skip.")
        return
    try:
        _rescore_running["flag"] = True
        _rescore_running["started_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Rescore em andamento...")
        result = _run_rescore_once()
        _rescore_running["last_result"] = result
        print(f"  Rescore completo: {result}")
    except Exception as e:
        _rescore_running["last_result"] = {"error": str(e)}
        print(f"  [ERR] rescore: {e}")
    finally:
        _rescore_running["flag"] = False
        _rescore_lock.release()


@app.route("/api/news/rescore", methods=["POST"])
@require_admin
def api_rescore():
    """Rescore all non-manually-added rows using current relevance_filter.py.
    Runs async in a background thread with batch commits (every 200 rows), so
    a full rescore over thousands of rows survives gunicorn/Render proxy
    timeouts and partial progress persists across worker restarts."""
    if _rescore_running["flag"]:
        return jsonify({"success": True, "status": "already_running",
                        "started_at": _rescore_running["started_at"],
                        "progress": _rescore_running["progress"],
                        "total": _rescore_running["total"]}), 202
    t = threading.Thread(target=_async_rescore, daemon=True)
    t.start()
    return jsonify({"success": True, "status": "started"}), 202


@app.route("/api/news/rescore/status")
def api_rescore_status():
    return jsonify({
        "running": _rescore_running["flag"],
        "started_at": _rescore_running["started_at"],
        "progress": _rescore_running["progress"],
        "total": _rescore_running["total"],
        "last_result": _rescore_running["last_result"],
    })


@app.route("/api/news/purge-blocked", methods=["POST"])
@require_admin
def api_purge_blocked():
    """Hard-delete rows whose source/url matches BLOCKED_SOURCES. One-shot
    cleanup after blocklist changes. Fetch path already skips these at insert,
    so this only reconciles historical data."""
    from relevance_filter import RelevanceFilter
    config = load_config()
    rf = RelevanceFilter(config)
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, title, url, source FROM news WHERE is_manually_added=0")
        rows = _fetchall(cur)
        blocked_ids = [r["id"] for r in rows if rf.is_blocked_source(r)]
        deleted = 0
        # Delete in chunks so commit is incremental if the list is huge.
        CHUNK = 200
        for i in range(0, len(blocked_ids), CHUNK):
            chunk = blocked_ids[i:i + CHUNK]
            # pg8000 parameter expansion for IN () list — build placeholders
            placeholders = ",".join(["%s"] * len(chunk))
            cur.execute(f"DELETE FROM news WHERE id IN ({placeholders})", chunk)
            deleted += len(chunk)
            conn.commit()
        return jsonify({"success": True, "checked": len(rows), "deleted": deleted})
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


# ── Clipping scrape — GitHub Actions architecture ─────────────────────────────
#
# Fluxo:
#   1. Frontend POST /api/scrape-trigger  → dispara GitHub Actions workflow
#   2. GitHub Actions roda scrape_worker.py com IPs limpos (sem bloqueios Globo)
#   3. scrape_worker.py POST /api/scrape-result → salva resultado no Postgres
#   4. Frontend faz polling GET /api/scrape-status?job_id=xxx até status=done
#
# Secrets necessários no Render (Environment):
#   GH_TOKEN      — GitHub Personal Access Token com permissão workflow
#   GH_OWNER      — seu usuário GitHub (ex: brunotomazetto)
#   GH_REPO       — nome do repo (ex: Agri-FB-Dashboard)
#   BACKEND_SECRET — string aleatória para autenticar callback do worker

import uuid as _uuid

GH_TOKEN  = os.environ.get("GH_TOKEN", "")
GH_OWNER  = os.environ.get("GH_OWNER", "")
GH_REPO   = os.environ.get("GH_REPO", "")
BACKEND_SECRET = os.environ.get("BACKEND_SECRET", "changeme")


def _init_scrape_table():
    """Create scrape_jobs table if not exists."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scrape_jobs (
                job_id     TEXT PRIMARY KEY,
                status     TEXT DEFAULT 'pending',
                results    TEXT DEFAULT '',
                created_at TEXT DEFAULT to_char(NOW(), 'YYYY-MM-DD"T"HH24:MI:SS'),
                updated_at TEXT DEFAULT to_char(NOW(), 'YYYY-MM-DD"T"HH24:MI:SS')
            )
        """)
        # Enable RLS with no policies: blocks all public PostgREST (anon)
        # access. This worker connects directly as `postgres`, which bypasses
        # RLS, so it is unaffected. Idempotent — safe to run on every init.
        cur.execute("ALTER TABLE scrape_jobs ENABLE ROW LEVEL SECURITY")
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[scrape] init table error: {e}", flush=True)


@app.route("/api/scrape-trigger", methods=["POST"])
@require_admin
def api_scrape_trigger():
    """Dispara o GitHub Actions workflow com os itens a scraper."""
    import traceback
    import urllib.request as ureq

    data  = request.get_json(force=True) or {}
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "items é obrigatório"}), 400

    if not GH_TOKEN or not GH_OWNER or not GH_REPO:
        # Fallback: tenta scraping local se GH não configurado
        try:
            print(f"[scrape] GH not configured, trying local scrape", flush=True)
            from scraper import scrape_batch
            results = scrape_batch(items)
            return jsonify({"mode": "local", "results": results})
        except Exception as e:
            return jsonify({"error": f"GH not configured and local scrape failed: {e}"}), 500

    job_id = str(_uuid.uuid4())

    # Save job as pending
    try:
        _init_scrape_table()
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO scrape_jobs (job_id, status) VALUES (%s, 'pending')",
            (job_id,)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[scrape] DB error: {e}", flush=True)

    # Dispatch GitHub Actions workflow
    try:
        import json as _json
        payload = _json.dumps({
            "ref": "main",
            "inputs": {
                "job_id": job_id,
                "items": _json.dumps(items),
            }
        }).encode("utf-8")

        gh_url = f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/actions/workflows/scrape.yml/dispatches"
        req = ureq.Request(
            gh_url,
            data=payload,
            headers={
                "Authorization": f"Bearer {GH_TOKEN}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="POST",
        )
        with ureq.urlopen(req, timeout=15) as resp:
            print(f"[scrape] GH dispatch status: {resp.status}, job_id={job_id}", flush=True)

        return jsonify({"job_id": job_id, "status": "pending"})

    except Exception as e:
        tb = traceback.format_exc()
        print(f"[scrape] dispatch error: {e}\n{tb}", flush=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/scrape-result", methods=["POST"])
def api_scrape_result():
    """Callback do scrape_worker.py — salva resultado no Postgres."""
    data   = request.get_json(force=True) or {}
    secret = data.get("secret", "")
    job_id = data.get("job_id", "")

    if secret != BACKEND_SECRET:
        return jsonify({"error": "unauthorized"}), 403
    if not job_id:
        return jsonify({"error": "job_id required"}), 400

    import json as _json
    results_json = _json.dumps(data.get("results", []))

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE scrape_jobs SET status='done', results=%s, "
            "updated_at=to_char(NOW(),'YYYY-MM-DD\"T\"HH24:MI:SS') WHERE job_id=%s",
            (results_json, job_id)
        )
        conn.commit()
        cur.close()
        conn.close()
        print(f"[scrape] result saved: job_id={job_id}", flush=True)
        return jsonify({"ok": True})
    except Exception as e:
        print(f"[scrape] result save error: {e}", flush=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/scrape-status")
def api_scrape_status():
    """Polling endpoint — retorna status e resultados do job."""
    import json as _json
    job_id = request.args.get("job_id", "")
    if not job_id:
        return jsonify({"error": "job_id required"}), 400

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT status, results FROM scrape_jobs WHERE job_id=%s", (job_id,))
        row = _fetchone(cur)
        cur.close()
        conn.close()

        if not row:
            return jsonify({"status": "not_found"}), 404

        result = {"status": row["status"]}
        if row["status"] == "done" and row["results"]:
            result["results"] = _json.loads(row["results"])
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Diagnostic endpoint (remover após confirmar funcionamento) ─────────────────
@app.route("/api/scrape-test")
def api_scrape_test():
    results = {}
    test_urls = {
        "globo_rural": "https://globorural.globo.com/pecuaria/",
        "g1_agro":     "https://g1.globo.com/economia/agronegocios/",
        "canal_rural": "https://www.canalrural.com.br/",
    }
    try:
        import requests as req
        for name, url in test_urls.items():
            try:
                r = req.get(url, headers={"User-Agent": "Mozilla/5.0 Chrome/124"}, timeout=8)
                results[f"tier1_{name}"] = {"status": r.status_code, "size": len(r.content)}
            except Exception as e:
                results[f"tier1_{name}"] = {"error": str(e)}
    except Exception as e:
        results["tier1_import"] = {"error": str(e)}
    try:
        from curl_cffi import requests as cffi_req
        for name, url in test_urls.items():
            try:
                r = cffi_req.get(url, impersonate="chrome124", timeout=10)
                preview = r.content.decode("utf-8", errors="replace")[:200]
                results[f"cffi_{name}"] = {"status": r.status_code, "size": len(r.content), "preview": preview}
            except Exception as e:
                results[f"cffi_{name}"] = {"error": str(e)}
    except ImportError:
        results["cffi_import"] = {"error": "curl_cffi not installed"}
    return jsonify(results)


# ── Startup ───────────────────────────────────────────────────────────────────

def _start_scheduler():
    config = load_config()
    interval_hours = config.get("refresh_interval_hours", 6)
    scheduler = BackgroundScheduler()
    scheduler.add_job(scheduled_fetch, "interval", hours=interval_hours)
    scheduler.start()
    return scheduler


if DB_HOST:
    try:
        init_db()
        _init_scrape_table()  # ensure scrape_jobs table exists for clipping
        _scheduler = _start_scheduler()
    except Exception as _e:
        print(f"[WARN] Startup error: {_e}")

# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DB_HOST:
        raise RuntimeError("DB_HOST env var não definida")
    print("=" * 60)
    print("  Feed de Noticias - Equity Research")
    print(f"  Acesse: http://localhost:5000")
    print("=" * 60)
    app.run(debug=False, port=5000, use_reloader=False)
