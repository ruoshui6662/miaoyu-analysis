# RADAR-SRC-4 Security and Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing RSS/Atom radar collection safe across competing local processes and every HTTP redirect, while preventing credential-reference disclosure and providing honest 24-hour operational evidence.

**Architecture:** Preserve the current Flask + SQLite deployment. `radar_sources` owns bounded, manually followed HTTP redirects and per-hop network validation; `db` owns atomic lease recovery and synchronization-run closure; `radar` remains the orchestrator and never treats another worker's lease as an upstream failure. API responses use explicit public projections that omit internal credential references.

**Tech Stack:** Python 3.14, standard-library `multiprocessing`, SQLite, `requests`, Flask, `unittest`, vanilla HTML/CSS/JavaScript.

**Spec:** `docs/superpowers/specs/2026-09-14-radar-src4-security-stability-design.md`

## Global Constraints

- Only the existing RSS/Atom adapter changes; do not implement web deep-crawling, RSSHub/Bridge, account login, AI, or paid Provider calls.
- Default network policy accepts only HTTP/HTTPS public targets and rejects URL credentials, loopback, private, link-local, multicast, reserved and unspecified addresses.
- Private access requires an exact `MIAOYU_RADAR_PRIVATE_SOURCE_ALLOWLIST` hostname, IP, or CIDR match; it must never become a global SSRF bypass.
- Every redirect is independently URL- and DNS-validated, redirect count is at most three, response bodies remain capped at 2 MiB, and timeouts remain `(5, 20)`.
- SQLite lease ownership is the cross-process authority; a skipped lease must not advance cursor/failure/backoff state.
- RSS/Atom endpoints reject `auth_ref`; no GET API, health response, log, run record, export, or error exposes an auth reference, URL credentials, response body, or secret.
- Tests use mocks or temporary loopback fixtures only. `tmp/` is user-owned and must not be modified or staged.
- A 24-hour operational checklist is evidence collection, not a substitute for actual 24-hour deployment observation.

---

### Task 1: Bound HTTP redirects and make private access an exact allowlist

**Files:**
- Modify: `backend/radar_sources.py`
- Modify: `tests/test_radar_sources.py`

**Interfaces:**
- Consumes: `fetch_feed(endpoint: dict, state: dict | None, *, preview_limit: int) -> dict` and existing `RadarFeedError`.
- Produces: `fetch_feed()` that requests with `allow_redirects=False`, validates each redirect target before connection, and rejects a fourth redirect with `redirect_limit`.

- [ ] **Step 1: Add red tests for redirect and allowlist policy**

Add tests using `_Response` and patched `radar_sources.requests.get`:

```python
def test_fetch_revalidates_a_redirect_before_requesting_it(self):
    endpoint = {"url": "https://public.example/feed.xml"}
    redirect = _Response(302, headers={"Location": "http://127.0.0.1/private.xml"})
    with patch("radar_sources._check_resolved_target", side_effect=[None, RadarFeedError(
        "private_address_blocked", "blocked")]), patch(
        "radar_sources.requests.get", return_value=redirect) as get:
        with self.assertRaisesRegex(RadarFeedError, "blocked"):
            fetch_feed(endpoint)
    self.assertEqual(get.call_count, 1)

def test_private_allowlist_accepts_only_exact_host_or_cidr(self):
    with patch.dict(os.environ, {"MIAOYU_RADAR_PRIVATE_SOURCE_ALLOWLIST": "10.0.0.0/24"}, clear=False):
        self.assertTrue(_private_target_allowed("10.0.0.9", ["10.0.0.9"]))
        self.assertFalse(_private_target_allowed("10.0.1.9", ["10.0.1.9"]))
```

Add a four-redirect fixture assertion for `redirect_limit` and assert each `requests.get` call used `allow_redirects=False`.

- [ ] **Step 2: Run the focused red tests**

Run: `$env:PYTHONPATH = (Resolve-Path backend).Path; python -m unittest discover -s tests -p 'test_radar_sources.py' -v`

Expected: tests fail because `fetch_feed()` currently follows redirects inside requests and `_private_target_allowed` does not exist.

- [ ] **Step 3: Add minimal checked-request helpers**

In `backend/radar_sources.py`, add:

```python
MAX_REDIRECTS = 3

def _private_target_allowed(host: str, addresses: list[str]) -> bool:
    """Match only normalized hostname/IP/CIDR entries from the allowlist env."""
    entries = {
        value.strip().lower()
        for value in os.getenv("MIAOYU_RADAR_PRIVATE_SOURCE_ALLOWLIST", "").split(",")
        if value.strip()
    }
    if host.lower() in entries:
        return True
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if any(ipaddress.ip_address(address) in network for address in addresses):
            return True
    return False

def _checked_get(url: str, headers: dict[str, str]) -> tuple[requests.Response, str]:
    """Validate URL/DNS before each no-auto-redirect request and cap redirect hops."""
    current_url = validate_endpoint_url(url)
    for hop in range(MAX_REDIRECTS + 1):
        _check_resolved_target(current_url)
        response = requests.get(current_url, headers=headers, timeout=HTTP_TIMEOUT,
                                stream=True, allow_redirects=False)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response, current_url
        if hop == MAX_REDIRECTS:
            response.close()
            raise RadarFeedError("redirect_limit", "信源重定向次数超过限制")
        location = str(response.headers.get("Location") or "").strip()
        response.close()
        if not location:
            raise RadarFeedError("invalid_redirect", "信源重定向缺少目标地址")
        current_url = validate_endpoint_url(urljoin(current_url, location))
    raise AssertionError("redirect loop must return or raise")
```

Use `urljoin()` for relative locations; call `_check_resolved_target()` before every `requests.get(..., allow_redirects=False)`; close intermediate responses; reject unsupported/missing locations and a fourth redirect with `RadarFeedError` codes. Keep `MIAOYU_RADAR_ALLOW_PRIVATE_SOURCES` only as a test-compatibility branch and document that production uses the exact allowlist.

- [ ] **Step 4: Run focused adapter tests green**

Run: `$env:PYTHONPATH = (Resolve-Path backend).Path; python -m unittest discover -s tests -p 'test_radar_sources.py' -v`

Expected: new redirect/allowlist cases and existing 200/304/503/cursor cases pass.

- [ ] **Step 5: Commit the HTTP-boundary change**

```powershell
git add backend/radar_sources.py tests/test_radar_sources.py
git commit -m "feat: harden radar feed redirects"
```

### Task 2: Recover expired leases without leaving running records behind

**Files:**
- Modify: `backend/db.py`
- Modify: `tests/test_radar_sources.py`

**Interfaces:**
- Consumes: `radar_endpoint_lease_acquire(endpoint_id: int, owner: str, now: str, lease_until: str) -> bool`.
- Produces: the same boolean interface, atomic closure of expired endpoint `radar_sync_runs` as `abandoned` / `lease_expired`, and `radar_sync_runs_for_endpoint(endpoint_id: int) -> list[dict]` for operational inspection.

- [ ] **Step 1: Add red multiprocess and recovery tests**

Create a module-level child target (required by Windows spawn) that sets `db.SETTINGS_DB`, waits on an `Event`, calls `radar_endpoint_lease_acquire`, and sends its boolean result through a `multiprocessing.Queue`. Add tests:

```python
def test_two_processes_acquire_exactly_one_endpoint_lease(self):
    context = multiprocessing.get_context("spawn")
    start, results = context.Event(), context.Queue()
    workers = [context.Process(target=_acquire_endpoint_lease_in_child,
        args=(str(db_path), endpoint_id, owner, start, results)) for owner in ("a", "b")]
    for worker in workers:
        worker.start()
    start.set()
    self.assertEqual(sorted(results.get(timeout=10) for _ in workers), [False, True])
    for worker in workers:
        worker.join(timeout=10)
        self.assertEqual(worker.exitcode, 0)

def test_expired_lease_is_recovered_and_old_running_run_is_abandoned(self):
    self.assertTrue(db.radar_endpoint_lease_acquire(endpoint_id, "worker-a", now, expires))
    run_id = db.radar_sync_run_create(endpoint_id, now)
    self.assertTrue(db.radar_endpoint_lease_acquire(endpoint_id, "worker-b", after_expiry, later))
    self.assertEqual(db.radar_endpoint_state(endpoint_id)["lease_owner"], "worker-b")
    old_run = next(run for run in db.radar_sync_runs_for_endpoint(endpoint_id) if run["id"] == run_id)
    self.assertEqual((old_run["status"], old_run["error_code"]), ("abandoned", "lease_expired"))
```

Add a regression assertion that an unavailable lease causes no `fetch_feed` call, does not increment `consecutive_failures`, and leaves `cursor_value` unchanged.

- [ ] **Step 2: Run the focused red tests**

Run: `$env:PYTHONPATH = (Resolve-Path backend).Path; python -m unittest discover -s tests -p 'test_radar_sources.py' -v`

Expected: existing acquire logic permits expiry but does not close the old `running` synchronization record; the new assertion fails on that record state.

- [ ] **Step 3: Make expiry recovery atomic in the lease transaction**

In `backend/db.py`, while `BEGIN IMMEDIATE` is held and an existing lease is recognized as expired, execute:

```sql
UPDATE radar_sync_runs
SET finished_at=?, status='abandoned', error_code='lease_expired',
    error_message='采集租约已过期，由新任务接管'
WHERE endpoint_id=? AND status='running';
```

Then update lease owner/until and commit. Add `radar_sync_runs_for_endpoint(endpoint_id: int, limit: int = 100) -> list[dict]`, selecting `id`, `started_at`, `finished_at`, `status`, `item_count`, `new_count`, `http_status`, `error_code`, and `error_message`, ordered by descending id. Do not change the public boolean return contract.

- [ ] **Step 4: Run focused lease tests green**

Run: `$env:PYTHONPATH = (Resolve-Path backend).Path; python -m unittest discover -s tests -p 'test_radar_sources.py' -v`

Expected: exactly one spawned process holds the lease, expiry recovers it, old running work is visibly abandoned, and existing cursor/backoff semantics still pass.

- [ ] **Step 5: Commit the lease-recovery change**

```powershell
git add backend/db.py tests/test_radar_sources.py
git commit -m "feat: recover expired radar endpoint leases"
```

### Task 3: Reject credential references and project only public endpoint fields

**Files:**
- Modify: `backend/app.py`
- Modify: `tests/test_g0_baseline.py`

**Interfaces:**
- Consumes: `POST /api/radar/endpoints`, `GET /api/radar/endpoints`, and database endpoint records containing an internal `auth_ref` field.
- Produces: RSS/Atom create/update paths that reject non-empty `auth_ref`, and every endpoint API dictionary (list/create/update) that omits `auth_ref` before JSON serialization.

- [ ] **Step 1: Add red authenticated API tests**

In `tests/test_g0_baseline.py`, use the existing authenticated client helper to assert:

```python
created = client.post("/api/radar/endpoints", json={
    "name": "测试源", "url": "https://example.test/feed.xml", "auth_ref": "secret-ref",
})
self.assertEqual(created.status_code, 400)

source_id = db.radar_source_identity_get_or_create("内部引用源", "example.test")
db.radar_endpoint_create(source_id, "rss", "https://example.test/private.xml", auth_ref="secret-ref")
listed = client.get("/api/radar/endpoints")
self.assertNotIn("auth_ref", listed.get_data(as_text=True))
self.assertNotIn("secret-ref", listed.get_data(as_text=True))
```

Use a temporary `db.SETTINGS_DB`; do not require an external Feed request.

- [ ] **Step 2: Run the focused red API tests**

Run: `$env:PYTHONPATH = (Resolve-Path backend).Path; python -m unittest discover -s tests -p 'test_g0_baseline.py' -v`

Expected: the creation request currently ignores/accepts `auth_ref`, and list serialization exposes a seeded internal reference.

- [ ] **Step 3: Enforce public projection and request rejection**

In `backend/app.py`, add the following helper near the Radar routes:

```python
def _public_radar_endpoint(endpoint: dict) -> dict:
    public = dict(endpoint or {})
    public.pop("auth_ref", None)
    return public
```

Use it for `GET /api/radar/endpoints`, POST create responses, and PATCH update responses. In both POST and PATCH, reject a supplied non-empty `auth_ref` with a 400 explanation that RSS/Atom does not support credentials. Do not modify existing database values or return them in error messages.

- [ ] **Step 4: Run focused API tests green**

Run: `$env:PYTHONPATH = (Resolve-Path backend).Path; python -m unittest discover -s tests -p 'test_g0_baseline.py' -v`

Expected: seeded internal data stays in SQLite but never crosses the HTTP response boundary; credential references are rejected at input.

- [ ] **Step 5: Commit the credential-boundary change**

```powershell
git add backend/app.py tests/test_g0_baseline.py
git commit -m "fix: keep radar credential references internal"
```

### Task 4: Document 24-hour deployment observation and close regression verification

**Files:**
- Modify: `docs/雷达信源管理设计规范.md`
- Modify: `docs/开发手册.md`
- Modify: `docs/开发计划-验收清单.md`
- Test: `tests/test_radar_sources.py`
- Test: `tests/test_g0_baseline.py`

**Interfaces:**
- Consumes: source health fields and `radar_sync_runs` status/error fields.
- Produces: a dated 24-hour observation checklist with pass/fail thresholds and an honest `🟡` stage status pending real deployment evidence.

- [ ] **Step 1: Add a failing regression test for lease skip state preservation**

Add a test that seeds `cursor_value`, `consecutive_failures`, `next_fetch_at`, then makes `radar_endpoint_lease_acquire` return `False`; assert no fetch and an identical persisted state after collection.

- [ ] **Step 2: Run the red test and implement only if it exposes a gap**

Run: `$env:PYTHONPATH = (Resolve-Path backend).Path; python -m unittest discover -s tests -p 'test_radar_sources.py' -v`

Expected: either the new test fails because a state write occurs on lease skip, then minimize the responsible `backend/radar.py` change; or it passes as a characterization test and no production change is made.

- [ ] **Step 3: Add the 24-hour checklist and status evidence**

Document exactly: two independently started app processes, one configured public Feed, 15-minute interval, 96 expected checks, no simultaneous duplicate `radar_sync_runs`, no `running` record beyond 120 seconds, controlled recovery after one process stop, and no secret/auth-ref text in source-health/API/log samples. Keep RADAR-SRC-4 at 🟡 until the checklist is actually observed.

- [ ] **Step 4: Run all release checks**

Run: `$env:PYTHONPATH = (Resolve-Path backend).Path; python -m unittest discover -s tests -p 'test_*.py' -q`

Run: `python -m compileall -q backend tests`

Run: `git diff --check`

Expected: complete suite passes, Python sources compile, and no whitespace errors are introduced.

- [ ] **Step 5: Commit documentation and completed plan evidence**

```powershell
git add docs/雷达信源管理设计规范.md docs/开发手册.md docs/开发计划-验收清单.md \
  docs/superpowers/plans/2026-09-14-radar-src4-security-stability.md tests/test_radar_sources.py
git commit -m "docs: record radar source stability acceptance"
```
