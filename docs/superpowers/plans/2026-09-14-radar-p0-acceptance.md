# Radar P0 Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the RSS/Atom incremental contract across a real local HTTP boundary, prevent test requests from starting persistent schedulers, and record a narrow-screen radar UI acceptance result.

**Architecture:** Keep production feed parsing and scheduling behavior unchanged unless an acceptance test identifies a concrete gap. A test-only loopback HTTP server will return controlled 200, 304, malformed XML, and 503 responses while `MIAOYU_RADAR_ALLOW_PRIVATE_SOURCES=1` is scoped to the test. Flask receives one explicit scheduler-enable switch: production defaults to enabled; tests disable it before creating clients so background services cannot retain a patched temporary SQLite path.

**Tech Stack:** Python 3, unittest, requests, Flask, APScheduler, SQLite, vanilla HTML/CSS/JavaScript.

**Spec:** `docs/雷达信源管理设计规范.md`

## Global Constraints

- Radar external requests remain one request per bound endpoint per collection pass; keywords stay local and no AI call is added.
- Test HTTP traffic must stay on a temporary loopback server and private-source allowance must be scoped to the test process context.
- A HTTP 304 must preserve the persisted cursor and reset no failure state; malformed/503 responses must preserve it and schedule retry backoff.
- Production scheduler startup remains enabled by default; tests must opt out explicitly rather than infer test execution from process internals.
- Preserve user-owned untracked `tmp/` content and stage only files listed in a task.

---

### Task 1: Real HTTP RSS state-transition acceptance test

**Files:**
- Modify: `tests/test_radar_sources.py`
- Test: `tests/test_radar_sources.py`

**Interfaces:**
- Consumes: `radar_sources.fetch_feed(endpoint, state=None, preview_limit=50) -> dict` and `RadarService._collect_for_subscriptions(subscriptions, force=False) -> list[dict]`.
- Produces: a local HTTP fixture that records request headers and deterministically emits 200, 304, malformed XML, and 503 outcomes.

- [x] **Step 1: Write failing integration test for a persisted Feed state across service recreation**

```python
def test_real_http_feed_preserves_cursor_through_304_and_restart(self):
    # First collection receives a 200 and persists ETag + cursor.
    # A recreated RadarService receives 304 with If-None-Match and If-Modified-Since.
    # Assert no duplicate mention, unchanged status, and identical cursor.
```

- [x] **Step 2: Run the new test and record whether the existing contract already passes**

Run: `$env:PYTHONPATH = (Resolve-Path backend).Path; python -m unittest discover -s tests -p 'test_radar_sources.py' -v`

Expected: the test exercises a real `requests.get` call to `127.0.0.1`, observes conditional headers on the second request, and either exposes a persistence defect or passes as a characterization acceptance test.

- [x] **Step 3: Write failing integration tests for invalid XML and HTTP 503 state preservation**

```python
def test_real_http_feed_failure_keeps_cursor_and_sets_backoff(self):
    # Seed a stable cursor, collect a 503, then assert the old cursor,
    # consecutive failure count, cooldown_until, and HTTP status are retained.
```

- [x] **Step 4: Run the focused HTTP acceptance tests**

Run: `$env:PYTHONPATH = (Resolve-Path backend).Path; python -m unittest discover -s tests -p 'test_radar_sources.py' -v`

Expected: all fixture server threads are shut down, no real internet request occurs, and each response branch verifies persisted observable state.

- [x] **Step 5: Make the smallest production fix only if a test exposes an unmet contract**

Modify only the responsible function in `backend/radar_sources.py` or `backend/radar.py`; rerun the focused test after each fix.

- [x] **Step 6: Commit the accepted real-HTTP coverage**

```powershell
git add tests/test_radar_sources.py backend/radar_sources.py backend/radar.py
git commit -m "test: cover radar feed HTTP state transitions"
```

### Task 2: Keep background schedulers out of Flask unit tests

**Files:**
- Modify: `backend/app.py`
- Modify: `tests/test_g0_baseline.py`
- Test: `tests/test_g0_baseline.py`

**Interfaces:**
- Consumes: `app.config["MIAOYU_SCHEDULERS_ENABLED"]` as a boolean deployment/test switch.
- Produces: authenticated Flask requests start `monitor_service` and `radar_service` only when the switch is enabled.

- [x] **Step 1: Write a failing test for an explicitly disabled scheduler switch**

```python
def test_request_skips_background_schedulers_when_explicitly_disabled(self):
    app_module.app.config["MIAOYU_SCHEDULERS_ENABLED"] = False
    with patch("monitor.monitor_service.start") as monitor_start, \
         patch("radar.radar_service.start") as radar_start:
        make_client().get("/healthz")
    monitor_start.assert_not_called()
    radar_start.assert_not_called()
```

- [x] **Step 2: Run the test and verify it fails because the before-request hook starts both services**

Run: `$env:PYTHONPATH = (Resolve-Path backend).Path; python -m unittest discover -s tests -p 'test_g0_baseline.py' -k request_skips_background_schedulers_when_explicitly_disabled -v`

Expected: FAIL because each service `start()` is currently called for an authenticated request.

- [x] **Step 3: Add the minimal configuration gate in `backend/app.py`**

```python
app.config["MIAOYU_SCHEDULERS_ENABLED"] = os.getenv(
    "MIAOYU_SCHEDULERS_ENABLED", "1"
).strip().lower() in {"1", "true", "yes", "on"}

if app.config["MIAOYU_SCHEDULERS_ENABLED"]:
    monitor_service.start()
    radar_service.start()
```

- [x] **Step 4: Set the switch to false in the Flask test module before its first client request**

```python
app_module.app.config["MIAOYU_SCHEDULERS_ENABLED"] = False
```

- [x] **Step 5: Rerun the focused test and the complete Flask baseline suite**

Run: `$env:PYTHONPATH = (Resolve-Path backend).Path; python -m unittest discover -s tests -p 'test_g0_baseline.py' -v`

Expected: scheduler switch test passes, API contracts remain unchanged, and no APScheduler start log appears during the suite.

- [x] **Step 6: Commit the test isolation fix**

```powershell
git add backend/app.py tests/test_g0_baseline.py
git commit -m "test: isolate Flask requests from background schedulers"
```

### Task 3: Radar mobile acceptance and documentation closeout

**Files:**
- Modify: `docs/开发手册.md`
- Modify: `docs/雷达信源管理设计规范.md`
- Verify: `frontend/index.html`

**Interfaces:**
- Consumes: the existing `source_scope` editor, source health list, and `waitRadarRun()` result-code messaging.
- Produces: a dated mobile acceptance record and only evidence-backed status changes in roadmap documents.

- [x] **Step 1: Start the app with schedulers disabled and inspect radar at 390px and 360px**

Run: `$env:MIAOYU_SCHEDULERS_ENABLED='0'; python backend/app.py`

Verify: topic editor source-scope controls, source health rows, and refresh-result messages have no horizontal viewport overflow and retain 44px touch targets.

- [x] **Step 2: Verify all result semantics in code and API fixture paths**

Check: `no_sources`, `deferred`, `no_match`, `matched`, `partial`, and `error` are distinct in `frontend/index.html` and the `/api/radar/topics/<id>/runs` response.

- [x] **Step 3: Update documentation only with observed test and viewport evidence**

Record the exact test count, the loopback fixture coverage, the scheduler isolation outcome, and whether mobile acceptance passed or remains a user-device follow-up.

- [x] **Step 4: Run release verification and commit documentation**

Run: `python -m unittest discover -s tests -p 'test_*.py' -q`

Run: `git diff --check`

```powershell
git add docs/开发手册.md docs/雷达信源管理设计规范.md
git commit -m "docs: record radar P0 acceptance evidence"
```
