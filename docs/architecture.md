# How it works

This page explains the Lab Notebook Digitizer as the code has it today. It is written for an
engineer joining the project. The reasons behind the choices are in [DESIGN.md](../DESIGN.md);
this page links there instead of repeating them. How to run the stack is in the
[README](../README.md).

Paths are relative to the repository root. In tables, Python files are under `src/lnd/` unless
the path says otherwise.

Every measured number names the machine it comes from:

- **The Mac**: Apple M5 MacBook Air (Mac17,3; 10 cores, 4 performance and 6 efficiency; 32 GB;
  fanless), macOS 26.6.2, Docker Desktop 29.8.2 running a Linux VM with 8 vCPUs and 14.7 GB RAM.
  Containers have no GPU, so llama.cpp b11718 runs on the CPU.
- **The VM**: the 2 vCPU, 8 GB cloud VM behind the numbers in the README's Results section.

Contents:
[1. Summary](#1-summary),
[2. Components](#2-components),
[3. The life of a page](#3-the-life-of-a-page),
[4. The queue](#4-the-queue),
[5. Data model](#5-data-model),
[6. Progress events and SSE](#6-progress-events-and-sse),
[7. The model gateway and token budgets](#7-the-model-gateway-and-token-budgets),
[8. Reading a page with the model](#8-reading-a-page-with-the-model),
[9. The sandbox](#9-the-sandbox),
[10. Networks and egress](#10-networks-and-egress),
[11. The review screen](#11-the-review-screen),
[12. Configuration](#12-configuration),
[13. Failure modes and recovery](#13-failure-modes-and-recovery),
[14. Testing and verification](#14-testing-and-verification),
[15. Where the code is](#15-where-the-code-is)

## 1. Summary

The system turns photos of lab-notebook pages and scanned PDFs into structured records that a
person checks and corrects. The API saves each upload on a shared data volume, writes a document
row and one row per page to Postgres, and pushes one job per page onto a Redis queue. Workers
take jobs under a lease, render PDF pages, and send each page image to a local vision model:
Qwen2.5-VL-7B-Instruct (Q4_K_M GGUF) served by llama.cpp's `llama-server`. Every model call goes
through one class, `LLMGateway`, which checks and meters each tenant's token budget. When a page
has a reagent table, the model also writes a small Python check, and the sandbox manager runs it
in a locked-down, single-use container with no network. The worker saves the record and a
progress event in one Postgres transaction. The browser follows progress over Server-Sent Events
and reviews records in a plain-JS screen, where every save is a new version. Postgres is the
source of truth. Redis only holds what to do next.

```mermaid
flowchart TB
    browser["Browser<br/>review screen or API client"]
    publish["Host port 127.0.0.1:API_PORT<br/>default 8000"]
    daemon["Host Docker daemon<br/>/var/run/docker.sock"]
    hf["huggingface.co<br/>and the storage host it redirects to"]

    subgraph edge_net["edge network - bridge, IP masquerade off"]
        api_edge["api, container port 8000<br/>the only member"]
    end

    subgraph internal_net["internal network - internal true, no route out"]
        api["api<br/>uvicorn lnd.api:app"]
        subgraph workers["worker x N - python -m lnd.worker"]
            run_loop["Worker.run loop<br/>worker.py"]
            pipeline["pipeline.py<br/>process_job, finalize_dead"]
            gateway["LLMGateway<br/>gateway.py"]
        end
        redis["redis<br/>job queue"]
        postgres["postgres<br/>source of truth"]
        model["model-server<br/>llama-server with Qwen2.5-VL"]
        manager["sandbox-manager<br/>python -m lnd.sandbox.manager"]
    end

    subgraph no_net["no network - network_mode none"]
        sandboxes["sandbox containers<br/>warm pool, single use"]
    end

    subgraph download_net["download network - ordinary bridge"]
        fetch["models-fetch<br/>runs only for make models"]
    end

    data_vol["data volume<br/>/data"]
    models_vol["models volume<br/>/models"]

    browser -->|"HTTP and SSE"| publish
    publish --> api_edge
    api_edge ---|"same container"| api
    api -->|"enqueue, purge dead, stats"| redis
    api -->|"documents, pages, versions, events, budgets"| postgres
    api -->|"store uploads, serve images"| data_vol
    api -->|"re-run stored check after an edit"| manager
    run_loop -->|"pop, reap"| redis
    run_loop -->|"each job, dead jobs from reap"| pipeline
    pipeline -->|"ack, fail"| redis
    pipeline -->|"page status, records, events"| postgres
    pipeline -->|"read images, write rendered PDF pages"| data_vol
    pipeline --> gateway
    gateway -->|"POST /v1/chat/completions"| model
    gateway -->|"budget check, llm_calls"| postgres
    pipeline -->|"POST /run with code and rows"| manager
    manager -->|"create, exec, remove"| daemon
    daemon -->|"starts"| sandboxes
    model -->|"reads weights, read-only"| models_vol
    fetch -->|"HTTPS, pinned revision"| hf
    fetch -->|"writes verified files"| models_vol
```

Two things the diagram makes explicit:

- The gateway is not a service. It is a class that each worker process creates once. The API
  never calls the model.
- Sandbox containers are started through the host's Docker daemon. They are siblings of the
  other containers, not children of the manager, and they have no network interface besides
  loopback.

## 2. Components

`lnd-app` is the image built from `docker/app.Dockerfile` (`python:3.12-slim`, the package
installed, user 10001). `lnd-sandbox` is built from `docker/sandbox.Dockerfile`.

| Component | Job | Command | Image | Networks and mounts | Code |
|---|---|---|---|---|---|
| `api` | Uploads, enqueueing, records, edits, exports, progress stream; serves the review screen | `uvicorn lnd.api:app --host 0.0.0.0 --port 8000` | `lnd-app` | `internal` and `edge`; publishes `127.0.0.1:${API_PORT:-8000}` to 8000; `data` volume at `/data` | `api.py`, `ingest.py`, `store.py`, `events.py`, `exports.py`, `schemas.py`, `queue.py`, `db.py`; `checks.py` and `sandbox_client.py` (re-run after an edit); `gateway.py` (`usage_summary` only) |
| `worker` x N | Runs page jobs; runs the queue reaper | `python -m lnd.worker` | `lnd-app` | `internal`; `data` volume at `/data` | `worker.py`, `pipeline.py`, `extraction.py`, `checks.py`, `queue.py`, `store.py`, `ingest.py` (PDF rendering), `sandbox_client.py` |
| LLM gateway | The only model client: budget check, call, metering | class `LLMGateway`, one per worker process | none | none of its own | `gateway.py` |
| `model-server` | Serves the vision model on an OpenAI-style API | `llama-server` with the model, `--mmproj`, `--alias $MODEL_NAME`, `-c 8192 -np 1 --cache-ram 0` | `ghcr.io/ggml-org/llama.cpp:server`, pinned by digest (b11718) | `internal`; `models` volume at `/models`, read-only | `docker-compose.yml` |
| `sandbox-manager` | Runs model-written checks in sandboxes; keeps the warm pool | `python -m lnd.sandbox.manager` | `lnd-app` | `internal`; `/var/run/docker.sock`; `group_add: ${DOCKER_GID}` | `sandbox/manager.py`, `sandbox/runner.py` |
| Sandbox containers | Run one check each, then are deleted | idles on `python3 -I -c "import signal; signal.pause()"`; work arrives by `docker exec` | `lnd-sandbox` | none (`network_mode: none`); no mounts | `sandbox/manager.py` |
| `sandbox-image` | Builds `lnd-sandbox` on the host's Docker, then exits | `python3 -c pass` | `lnd-sandbox` | `network_mode: none` | `docker/sandbox.Dockerfile` |
| `postgres` | Source of truth | image default | `postgres:16-alpine` | `internal`; `pgdata` volume | `schema.sql`, `db.py`, `store.py` |
| `redis` | Job queue | `redis-server --appendonly yes` | `redis:7-alpine` | `internal`; `redisdata` volume | `queue.py` |
| `models-fetch` | `make models` only: download and verify the weights | `python -m lnd.fetch_models /models` | `lnd-app` | `download` only; `models` volume read-write | `fetch_models.py`, `models.json` |
| Review screen | The browser UI | static files served by `api` at `/` | inside `lnd-app` | the browser reaches it on `127.0.0.1` | `static/index.html`, `static/app.js`, `static/style.css` |

Notes:

- In `docker-compose.yml`, the four services built from `lnd-app` share the `x-hardened` block:
  read-only root file system (except `models-fetch`), a tmpfs at `/tmp`, all capabilities
  dropped, `no-new-privileges`, and `restart: unless-stopped` (except `models-fetch`). They run
  as user 10001 from the image. The model server runs as the llama.cpp image's default user
  (root) with a writable root file system; the README lists this as a known limit.
- Start order (`depends_on`): the API waits for healthy Postgres and Redis. Workers also wait for
  a healthy model server; its healthcheck calls `/health` every 10 s and allows 60 failures after
  a 60 s start period, which covers loading the weights. The sandbox manager waits for
  `sandbox-image` to finish. Nothing waits for the sandbox manager: until it is up, checks come
  back as errors and pages are still saved.
- The API and every worker run `db.init_schema()` when they start (section 5).
- Volumes: `pgdata` (Postgres), `redisdata` (Redis append-only file), `data` (uploads and page
  images, shared by the API and the workers), `models` (GGUF files, written by `models-fetch`,
  read by the model server).

## 3. The life of a page

```mermaid
sequenceDiagram
    participant B as Browser
    participant A as API
    participant P as Postgres
    participant R as Redis
    participant W as Worker
    participant G as Gateway in the worker
    participant M as Model server
    participant S as Sandbox manager
    participant C as Sandbox container

    B->>A: POST /api/documents with a photo or a PDF
    A->>A: sniff the type, normalise the photo or count PDF pages, save the file
    A->>P: one transaction with document row, page rows, event 1 document_created
    A->>R: LPUSH one job per page onto ready
    A-->>B: 202 with document id and page count
    B->>A: open an EventSource on the events stream
    A->>P: read events after 0
    A-->>B: event 1 document_created
    Note over A,P: the stream polls Postgres every 0.25 s until the document is done
    W->>R: pop, a Lua script moves the job from ready to inflight with a lease
    W->>P: load page and document, mark the page processing
    opt PDF page not rendered yet
        W->>W: render with PyMuPDF, write the PNG to the data volume
    end
    W->>G: extract the page
    G->>P: check the tenant budget
    G->>M: chat completion with the image and the JSON schema
    M-->>G: JSON text and token usage
    G->>P: insert an llm_calls row, add the tokens to the tenant
    G-->>W: answer text
    W->>W: validate with Pydantic
    opt answer invalid
        W->>G: one extract_retry call with the answer and the error
    end
    opt page has reagent rows
        W->>G: check_codegen call, same budget and metering steps
        G->>M: text-only chat completion
        M-->>G: Python code in a fenced block
        G-->>W: code
        W->>S: POST /run with code and rows
        S->>C: docker exec the runner, in a warm container if one is ready
        C-->>S: one result line
        S->>S: queue the used container for removal, refill the pool
        S-->>W: status, issues, timings
    end
    W->>P: one transaction under the document row lock
    Note over W,P: page done, version 1, check result, page_done, document_done if it was the last page
    W->>R: ack, remove the job from inflight
    A->>P: the next poll finds the new events
    A-->>B: page_done and document_done, then the stream ends
    B->>A: GET the document, then the page record, image and versions
```

Step by step:

1. **Upload.** The browser sends `POST /api/documents` as multipart form data, with the tenant
   in the `X-Tenant` header. `api.upload` reads the first 16 bytes and calls
   `ingest.sniff_kind`: `%PDF-` is a PDF, the PNG or JPEG magic bytes are an image, anything else
   is a 400.
   - A PDF is streamed to `/data/docs/<doc>/source.pdf` in 1 MiB chunks and refused with 413
     past `MAX_UPLOAD_MB`. `ingest.pdf_page_count` opens it with PyMuPDF and only counts pages
     (400 if it is password-protected, unreadable or has no pages). The API renders nothing.
   - A photo may be up to 50 MB (413 above that). `ingest.normalize_image` applies the EXIF
     rotation and shrinks the long side to `MAX_IMAGE_PX`. It re-encodes only when it changed
     something: PNG stays PNG, JPEG stays JPEG (quality 92). `ingest.write_atomic` writes it to
     `/data/docs/<doc>/page.png` or `page.jpg` through a temp file and a rename.
2. **Rows and the first event, in one transaction.** `store.create_document` creates the tenant
   row if it is new, inserts the `documents` row and one `pages` row per page (status `queued`;
   a photo gets its `image_path` now, PDF pages later), and emits event 1, `document_created`. It
   returns one job payload per page: `page_id`, `document_id`, `page_no`, `tenant`. If anything
   fails before the commit, the API deletes the upload folder.
3. **Enqueue.** After the commit, `JobQueue.enqueue_many` LPUSHes the jobs onto `lnd:q:ready`,
   500 per command in one pipeline. Each job gets `attempts: 0` and a random `uid`. The API
   answers 202 with the document id, kind and page count. If the enqueue fails, the pages stay
   `queued` and `POST /api/documents/{id}/requeue` can push them later.
4. **Progress stream.** The review screen opens an `EventSource` on
   `/api/documents/{id}/events` and receives `document_created` at once (section 6).
5. **Pop with a lease.** The worker loop in `worker.Worker.run` runs the reaper at most once a
   second, then calls `JobQueue.pop(LEASE_S)`. One Lua script moves the job from `ready` to
   `inflight`, scored with "Redis time + lease". When there is no job, the worker sleeps 20 ms,
   doubling up to 250 ms.
6. **Start the page.** `pipeline.process_job` loads the page and its document
   (`store.get_page_ctx`). The tenant comes from Postgres, not from the job. A page that is
   already `done` means a duplicate delivery: the worker acks and skips it. Otherwise
   `store.mark_processing` sets the status to `processing` and `attempts` to this attempt's
   number.
7. **Page image.** `pipeline._page_image` reads the stored photo. For a PDF page it calls
   `ingest.render_pdf_page` at `PDF_DPI`, capped at `MAX_IMAGE_PX`, writes the PNG atomically to
   `/data/docs/<doc>/pages/NNNN.png`, and records `image_path`. A rerun reuses the file.
8. **Extract.** `extraction.extract_page` sends the prompt and the image (a base64 data URL)
   through `LLMGateway.chat` with purpose `extract`, temperature 0, `EXTRACT_MAX_TOKENS`, and the
   page JSON schema as `response_format`. The gateway checks the budget, calls
   `/v1/chat/completions`, writes an `llm_calls` row and adds the tokens to the tenant. The reply
   is parsed and validated against `schemas.PageExtraction`. If it is invalid, one more call
   (`extract_retry`) carries the first answer and the error. A second invalid answer raises
   `ExtractionError`, and the attempt fails (section 4).
9. **Check.** If the page has reagent rows, `checks.run_checks` asks the model for a
   `check(rows)` function (purpose `check_codegen`, `CHECK_MAX_TOKENS`, no schema), takes the
   code out of the fenced block, and posts code and rows to the sandbox manager's `POST /run`.
   The manager runs it in a warm container and returns status, issues and timings (section 9).
   Problems in this step never fail the page. They become a check result with status `error`, or
   `skipped` when the budget ran out.
10. **Finalize.** `store.finalize_page_done` runs one transaction. It locks the document row
    (`SELECT ... FOR UPDATE`), sets the page to `done` only if it is not done already, inserts
    version 1 with `source = 'model'`, upserts `page_checks` when the page had reagent rows, and
    emits `page_done` with the running counts. If every page is now `done` or `failed`, it sets
    the document to `completed` and emits `document_done`. If another worker finished this page
    first, the call changes nothing.
11. **Ack.** `JobQueue.ack` removes the job from `inflight`. The ack comes after the commit, so a
    crash between the two only causes a rerun, which step 6 skips.
12. **Browser.** The stream's next poll finds the new events and sends them. On each event the
    review screen reloads the document 300 ms later; on `document_done` it closes the stream.
    Opening a page loads `GET /api/pages/{id}/record`, the page image and the version history
    (section 11).

## 4. The queue

`src/lnd/queue.py` implements a small reliable queue on Redis. Why a plain list is not enough is
in [DESIGN.md section 2](../DESIGN.md#2-the-queue).

| Key | Type | Holds | Score |
|---|---|---|---|
| `lnd:q:ready` | list | jobs waiting; LPUSH adds, RPOP takes, so first in, first out | none |
| `lnd:q:inflight` | sorted set | jobs a worker holds | when the lease ends |
| `lnd:q:delayed` | sorted set | jobs waiting out a back-off | when the job is due |
| `lnd:q:dead` | list | jobs that used all their attempts, newest first | none |

Python writes each job as compact JSON with sorted keys. A job holds `page_id`, `document_id`,
`page_no`, `tenant`, `attempts`, a random `uid`, and `last_error` after a failure. The exact
string is the sorted-set member, so `ack` and `fail` find a job by the string the worker popped.
A job the reaper re-queues after a lease expiry is decoded and re-encoded by Redis's cjson, so
its key order can differ. That does no harm, because `ack` and `fail` always use the exact string
the worker popped. The `uid` keeps two jobs for the same page distinct.

| Operation | Called by | What it does |
|---|---|---|
| `enqueue`, `enqueue_many` | API: upload, retry, requeue | LPUSH new jobs with `attempts: 0` |
| `pop(lease_s)`, Lua `_POP` | worker | RPOP from `ready` and ZADD to `inflight` with score `TIME + lease_s` |
| `ack(job)` | worker | ZREM from `inflight` (a single command) |
| `fail(job, error)`, Lua `_RETRY` or `_DEAD` | worker | If the job is still in `inflight`, remove it and store the updated job (attempts + 1, `last_error`) in `delayed` at `TIME + back-off`, or LPUSH it to `dead` once attempts reach `MAX_ATTEMPTS`. If it is no longer in flight, do nothing. |
| `reap(limit=1000)`, Lua `_REAP` | every worker, at most once a second | Move due `delayed` jobs to `ready`. For each expired lease: attempts + 1, `last_error = 'lease expired (worker crashed or stalled)'`, then LPUSH to `ready`, or to `dead` on the last attempt. Returns the jobs that went dead. |
| `purge_dead(page_id)` | API: manual retry | Delete that page's dead letters (LRANGE, then LREM; not one script) |
| `stats`, `dead_jobs` | API: `GET /api/queue` | Sizes of the four keys and the 20 newest dead jobs |

**Atomic steps.** Redis runs each Lua script as one step: no other command runs in the middle.
So a job is never in two structures at once, and it cannot be lost between the RPOP and the ZADD.

**Leases.** Lease ends and due times are computed inside the scripts from Redis `TIME`, never from
worker clocks, so workers with skewed clocks agree. `LEASE_S` must cover a whole job: up to three
model calls (extract, extract retry, check code) plus the sandbox run. The code default is 600 s.
Compose sets 3600 s, because one call can take minutes on CPU: on the Mac an extract call took
99.8 s for a synthetic page and 216 s for a real photo.

**Reaper.** There is no separate reaper process. Each worker calls `reap()` before it pops, at
most once a second (`Worker._reap`). Jobs the reaper sends to `dead` go to
`pipeline.finalize_dead`, which marks their pages `failed` and emits `page_failed`. A job whose
lease expired goes straight back to `ready`, with no back-off.

**Back-off.** `JobQueue.backoff_s` computes

```
delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (failures - 1)) * random.uniform(0.8, 1.2)
```

The cap is applied before the jitter, so a delay can end up 20 percent above the cap: up to 72 s
with the defaults (2 s base, 60 s cap). With the default three attempts only the first two
failures wait: about 2 s (1.6 to 2.4 s), then about 4 s (3.2 to 4.8 s). The cap only matters if
`MAX_ATTEMPTS` is raised to 7 or more. Retried and reclaimed jobs are LPUSHed, so they queue
behind every job already in `ready`.

**Attempts.** `attempts` in the job counts the failed attempts so far. The page row's `attempts`
column shows the attempt in progress (job attempts + 1, set by `store.mark_processing`).
`MAX_ATTEMPTS` defaults to 3. A failed attempt is an exception inside the job's main `try` block
in `pipeline.process_job`: the model unreachable or too slow (`ModelError`), invalid output twice
(`ExtractionError`), a PDF render error, and so on. An expired lease also counts. Two things do
not count: a spent token budget (the job is acked and the page fails at once, because retrying
cannot help), and check problems (the page is saved with a check error).

**Dead letters.** After the last failed attempt the job sits in `dead`, the page is `failed` with
`gave up after 3 attempts: <error>`, `page_failed` is emitted, and the document completes if this
was its last open page. `GET /api/queue` shows the counts and the 20 newest dead jobs.

**Idempotency.** A job can run twice, for example when a slow worker's lease expires and another
worker takes the job. That is safe:

- `process_job` skips a page that is already `done`.
- `store.finalize_page_done` updates the page only `WHERE status <> 'done'` and inserts version 1
  with `ON CONFLICT DO NOTHING`. Events are emitted only when the update happened, so a second
  finish adds no version and no event.
- `store.finalize_page_failed` only touches a page that is neither `done` nor `failed`.
- `ingest.write_atomic` writes rendered images under a unique temp name and renames them, so two
  workers rendering the same page cannot leave half a file.

**Manual retry.** `POST /api/pages/{id}/retry` calls `store.reset_failed_page`. Under the document
lock it puts the page back to `queued` with `attempts` 0 and no error, sets the document back to
`processing`, and emits `page_requeued`. A page that is not `failed` gets a 409. The API then
deletes the page's dead letters (`purge_dead`) and enqueues a fresh job.

**Requeue.** `POST /api/documents/{id}/requeue` enqueues a job for every page that Postgres still
shows as `queued` (`store.queued_page_jobs`). It is for recovery after a failed enqueue or lost
Redis data. It does not look at Redis, so a page that still has a job there gets a second one.
That is safe, but it can cost extra model calls.

```mermaid
flowchart LR
    new_job["new job<br/>attempts 0"] -->|"LPUSH"| ready["ready list"]
    ready -->|"pop - RPOP then ZADD<br/>score = now + LEASE_S"| inflight["inflight sorted set"]
    inflight -->|"ack - page saved, already done,<br/>or budget refused"| removed["gone from Redis"]
    inflight -->|"fail with attempts left<br/>score = now + back-off"| delayed["delayed sorted set"]
    delayed -->|"reap when due"| ready
    inflight -->|"lease expired, reap,<br/>attempts left"| ready
    inflight -->|"fail or lease expired,<br/>no attempts left"| dead["dead list"]
    dead -->|"POST /api/pages/id/retry<br/>purge, then a new job"| new_job
```

How the job's place in Redis maps to the page status in Postgres:

| Job | Page status |
|---|---|
| in `ready`, new | `queued` |
| in `inflight` | `processing` |
| in `delayed` | `queued`, with the last error shown (`store.note_retry`) |
| back in `ready` after its lease expired | still `processing` until a worker takes it again |
| removed after the ack | `done` |
| in `dead`, or removed after a budget refusal | `failed` |

## 5. Data model

All tables are in `src/lnd/schema.sql`.

```mermaid
flowchart LR
    tenants["tenants"] -->|"owns"| documents["documents"]
    documents -->|"has"| pages["pages"]
    documents -->|"progress log"| events["events"]
    pages -->|"versions"| page_versions["page_versions"]
    pages -->|"0 or 1 check row"| page_checks["page_checks"]
    tenants -.->|"tenant_id, not a foreign key"| llm_calls["llm_calls"]
```

| Table | Purpose | Key columns |
|---|---|---|
| `tenants` | One row per tenant name, created on first use by `db.ensure_tenant` | `id` (the `X-Tenant` value), `token_budget` (NULL means unlimited; starts as `DEFAULT_TOKEN_BUDGET`), `tokens_used` |
| `documents` | One upload | `id` (UUID), `tenant_id`, `kind` (`pdf` or `image`), `source_path` (relative to `DATA_DIR`), `page_count`, `status` (`processing` or `completed`), `last_seq` (the last event number handed out), `completed_at` |
| `pages` | One page of a document; the unit of work | `id` (UUID), `document_id`, `page_no` (unique per document), `status` (`queued`, `processing`, `done`, `failed`), `attempts`, `error`, `image_path` |
| `page_versions` | Every version of a page record | primary key (`page_id`, `version`), `source` (`model` for version 1, `user` for edits), `data` (JSONB in the `PageExtraction` shape), `editor`, `note` |
| `page_checks` | The model-written check code for a page and its latest result; no row when the page had no reagents | `page_id` (primary key), `code` (NULL when the check call failed, was skipped or gave no usable code), `result` (JSONB) |
| `events` | Progress log of a document | primary key (`document_id`, `seq`), `type`, `data` (JSONB) |
| `llm_calls` | One row per model call or refused call, written by the gateway | `tenant_id`, `job_id` (`<document_id>:<page_no>`), `purpose`, `model`, `status` (`ok`, `error`, `rejected`), `prompt_tokens`, `completion_tokens`, `latency_ms`, `error` |

- **Versions are append-only.** The code only ever INSERTs into `page_versions`. Version 1 is the
  model's answer, written once. Each save adds `max(version) + 1` while holding a lock on the page
  row (`store.add_user_version`). The current record is the highest version. Going back to an old
  version means loading it into the form and saving it as a new one.
- **Checks keep only the latest result.** `page_checks.result` is overwritten when an edit re-runs
  the stored code (`store.save_check_result`).
- **Events have gap-free numbers per document.** See section 6.
- **`llm_calls` is the metering ledger.** `tenants.tokens_used` is a running total that the
  gateway raises after each successful call. `GET /api/tenants/{t}/usage` reads both (section 7).
- **Files are not in the database.** `source_path` and `image_path` point into the `data` volume:
  `docs/<doc>/source.pdf`, `docs/<doc>/page.png` or `page.jpg`, and `docs/<doc>/pages/NNNN.png`.
- **Schema setup.** `db.init_schema()` runs the whole of `schema.sql` (only `CREATE ... IF NOT
  EXISTS`) under a Postgres advisory lock, so containers that start together do not race. There
  is no migration tool ([DESIGN.md section 8](../DESIGN.md#8-choices-that-are-simple-on-purpose)).
- **Connections.** Each process has one psycopg pool of 1 to 10 connections (`db.get_pool`).
  `db.connection()` is one transaction: it commits on success and rolls back on an exception.
- Deleting a document would cascade to its pages, versions, checks and events, but no code path
  deletes anything today.

## 6. Progress events and SSE

| Event | Emitted by | Data |
|---|---|---|
| `document_created` | `store.create_document`, at upload | `filename`, `page_count` |
| `page_done` | `store.finalize_page_done` | `page_no`, `title`, `reagents` (row count), `check` (`none`, `passed`, `issues`, `error` or `skipped`), `total`, `done`, `failed` |
| `page_failed` | `store.finalize_page_failed` | `page_no`, `error` (first 200 characters), `total`, `done`, `failed` |
| `page_requeued` | `store.reset_failed_page`, on manual retry | `page_no` |
| `document_done` | `store._maybe_complete` | `total`, `done`, `failed` |

A page that starts, or an attempt that will be retried, emits no event. Those changes show in
`GET /api/documents/{id}` (page status, attempts, error).

**Numbering.** `events.emit` runs inside the caller's transaction. It first runs
`UPDATE documents SET last_seq = last_seq + 1 ... RETURNING last_seq`, which locks the document
row until the commit, and then inserts the event with that number. A second writer for the same
document waits on the lock, so the events of one document commit in order. A transaction that
rolls back takes its number back with it. So the numbers have no gaps, and a reader that asks for
`seq > N` never skips an event. (A global sequence could commit 6 before 5; the docstring of
`src/lnd/events.py` explains.) The functions that finish or reset a page also lock the document
row first, with `SELECT ... FOR UPDATE`. That makes the counts in the event and the "was this the
last page?" decision consistent, so exactly one `document_done` is emitted even when workers
finish together.

**The stream.** `GET /api/documents/{id}/events` (`api.stream_events`):

- returns 404 for a document of another tenant;
- starts after the `Last-Event-ID` header if there is one (a value that is not a number counts
  as 0), otherwise after `?after=N`, otherwise from the beginning;
- first sends `retry: 1000`, so a browser waits 1 s before it reconnects;
- then loops: stop if the client has gone; read up to 500 events with `seq > cursor`, plus the
  document's `last_seq` and status; send each event as `id: <seq>`, `event: <type>`,
  `data: <json>`; read again at once if events came, otherwise sleep 0.25 s; after 15 s without
  events send a `: keepalive` comment;
- ends the response when the document is `completed` and the client has every event up to
  `last_seq`. A client that connects after that gets the whole log, then the stream closes.

The API keeps no state per client: the cursor lives in the client. Restarting the API loses
nothing; `tests/test_sse.py` kills it with SIGKILL in the middle of a 40-page job and checks that
ids 1..N arrive exactly once. The cost is one short pair of queries per open stream every 0.25 s
([DESIGN.md section 4](../DESIGN.md#4-progress-that-survives-restarts) compares this with
`LISTEN/NOTIFY`). The response sets `Cache-Control: no-cache` and `X-Accel-Buffering: no` so
proxies do not buffer it.

**The browser.** `EventSource` reconnects on its own and sends `Last-Event-ID`. It also reconnects
after a stream ends normally, so `static/app.js` closes the stream itself on `document_done`.
After a manual retry it opens a new stream with `?after=<last id seen>`, because a new
`EventSource` does not carry the old `Last-Event-ID`.

## 7. The model gateway and token budgets

`gateway.LLMGateway.chat()` is the only code that calls the model server. A test fails if any
other module in `src/lnd/` mentions `/v1/chat/completions`, `CHAT_PATH` or `model_base_url` (the
fake model server is exempt, and `config.py` may define `model_base_url`):
`test_only_the_gateway_talks_to_the_model` in `tests/test_gateway.py`. Each worker process
creates one gateway; the API creates none.

What one call does:

1. **Budget check** (`_check_budget`). Create the tenant row if needed. If `token_budget` is set
   and `tokens_used >= token_budget`, write an `llm_calls` row with status `rejected`, commit it,
   and raise `BudgetExceeded`. The model is not called.
2. **Request.** `POST {MODEL_BASE_URL}/v1/chat/completions` with `model` set to `MODEL_NAME`
   (compose passes the same name to llama-server as `--alias`, so the name the server reports
   matches), the messages, `max_tokens`, `temperature` 0 and `stream: false`. For extraction it
   adds `response_format = {type: json_schema, json_schema: {name: page, strict: true, schema: ...}}`.
   The httpx client's timeout is `MODEL_TIMEOUT_S`.
3. **Failure.** An HTTP error, a timeout or a malformed reply writes an `llm_calls` row with
   status `error`, the latency, the error text and zero tokens, then raises `ModelError`.
4. **Success.** An `llm_calls` row with status `ok`, the prompt and completion tokens from the
   reply's `usage`, and the latency in milliseconds. `tenants.tokens_used` grows by both token
   counts. If the server sent no `usage`, completion tokens are estimated as characters / 4, and
   the row's `error` column says so.

Purposes are `extract`, `extract_retry` and `check_codegen`. `job_id` is
`<document_id>:<page_no>`, so the ledger shows the cost of each page.

**Budgets.**

- `PUT /api/tenants/{t}/budget` with `{"token_budget": N}` sets a limit; `null` removes it. A new
  tenant starts with `DEFAULT_TOKEN_BUDGET` (empty means unlimited). Changing that variable later
  does not touch existing tenants.
- `GET /api/tenants/{t}/usage` (`gateway.usage_summary`) returns the budget, tokens used and
  remaining, and call counts by status. For successful calls it also returns calls, tokens and
  average latency per purpose, and the overall average and maximum latency.
- **The limit is soft.** The check runs before the call and the tokens are added after it, so
  calls that started just under the limit all finish and can push usage past it. The overshoot is
  at most (calls in flight) x (tokens per call). For scale, one extract call on the Mac used 2,292
  prompt tokens and 282 output tokens. Why there is no reservation is in
  [DESIGN.md section 3](../DESIGN.md#3-the-gateway-and-the-token-budget).
- **A refusal during extraction** fails the page at once with `budget_exceeded: ...`; the job is
  acked, with no retries (`pipeline.process_job`). **A refusal during the check call** still saves
  the page, with the check result `skipped`. After raising the budget, retry the failed pages.
- The usage, budget and queue endpoints take the tenant from the path or not at all, not from
  `X-Tenant`: any caller can read or change any tenant's budget. The tenant header is a label,
  not authentication ([DESIGN.md section 8](../DESIGN.md#8-choices-that-are-simple-on-purpose)).

## 8. Reading a page with the model

**Prompt.** `extraction.EXTRACT_PROMPT` asks for one JSON object with `title`, `date`
(`YYYY-MM-DD` or null), `narrative`, `reagents` (`name`, `amount`, `unit`, `molar_mass_g_mol`,
`equivalents`; null for a blank or unreadable cell, and "never guess a value"), and `notes`. It
goes out as one user message holding the text and the image as a base64 data URL, with
temperature 0 and up to `EXTRACT_MAX_TOKENS` (2048) output tokens.

**Schema as grammar.** `schemas.llm_json_schema()` starts from
`PageExtraction.model_json_schema()`, inlines the `$ref`s, and drops `$defs`, `title`,
`default`, `pattern`, `description`, `minLength`, `maxLength`, `minItems`, `maxItems`, `minimum`
and `maximum`. Property names such as `title` are kept. It then marks every key as required, at
the top level and in each reagent; a value can still be null. What is left (types, nullable
`anyOf`, `additionalProperties: false` and the full `required` lists) is turned into a grammar by
llama-server, so the model can only produce JSON of that shape, and it writes every key in schema
order: `title`, `date`, `narrative`, `reagents`, `notes`. (With only `title` and `narrative`
required, the real model left `date` out; the docstring of `llm_json_schema` has the details.)
The limits are dropped because large `maxLength` values make llama.cpp's schema-to-grammar step
build huge grammars; Pydantic enforces them afterwards.

**Validation.** `extraction.parse_extraction` strips a Markdown code fence if there is one, parses
the JSON, and validates `PageExtraction`:

- no extra keys, at the top level or in a reagent;
- `title` up to 300 characters, `narrative` up to 20,000, `notes` up to 5,000;
- at most 60 reagents; each `name` 1 to 200 characters, `unit` up to 30 characters,
  `molar_mass_g_mol` and `equivalents` not negative;
- `date` must look like `YYYY-MM-DD`; an empty string becomes null.

**One retry.** On a JSON or validation error the worker makes one more call (`extract_retry`). It
sends the original message (with the image again), the first answer (its first 4,000 characters)
as the assistant turn, and a user turn that names the problem (up to five validation errors, or
the JSON error). If that answer is invalid too, `ExtractionError` fails the attempt and the
queue's rules take over: up to three attempts, so at most six extract calls for one page.

**Image size.**

- Photos are shrunk at upload to `MAX_IMAGE_PX` (2000) on the long side, after the EXIF rotation.
  Smaller photos without a rotation flag are stored byte for byte.
- PDF pages are rendered by the worker at `PDF_DPI` (150) and capped at `MAX_IMAGE_PX`. At 150 dpi
  a US Letter page is 1275 x 1650 px and an A4 page about 1240 x 1754 px, so the cap only applies
  to larger pages or a higher `PDF_DPI`.
- The stored image is what the model sees and what the review screen shows.

**Image tokens.** Qwen2.5-VL cuts an image into 14 x 14 px patches and merges each 2 x 2 group
into one token, so one image token covers 28 x 28 px. llama.cpp b11718 rounds each side to the
nearest multiple of 28, so the image costs about round(width / 28) x round(height / 28) tokens.
That build also gives this model a default limit of 4,096 image tokens and shrinks larger images.

| Image | Image tokens by that rule | Measured prompt tokens |
|---|---|---|
| synthetic page, 1100 x 1500 px | 39 x 54 = 2,106 | 2,292 on the Mac |
| camera photo downscaled to 2000 x 1333 px | 71 x 48 = 3,408 | 3,594 on the Mac |
| US Letter PDF page at 150 dpi, 1275 x 1650 px | 46 x 59 = 2,714 | 2,900 on the Mac (the 10-page PDF test) |

The measured prompts differ by exactly what the rule predicts, and in all three the rest of the
prompt (instructions, chat template and image markers) is the same 186 tokens. Image
size is the main cost knob: on the Mac, reading the prompt took most of the extract call's time.

**Model server settings** (`docker-compose.yml`):

- `-c 8192`: one context of 8,192 tokens. The image, the prompt and up to 2,048 output tokens must
  fit in it. The retry call is longer, because it repeats the image and adds the first answer.
- `-np 1`: one request at a time. Requests from other workers wait inside llama-server, and that
  wait counts against the caller's `MODEL_TIMEOUT_S`. On CPU, more workers do not make pages
  faster.
- `--cache-ram 0`: no prompt cache in RAM. Every page brings a new image, so cached prompts were
  never reused while the cache grew; the measurement is in
  [DESIGN.md section 7](../DESIGN.md#7-reading-pages-with-a-small-model). With the cache off, the
  model server held about 6.5 GiB of anonymous memory on the Mac while reading pages, flat from page
  to page; `docker stats` shows about 11 GiB because it also counts the memory-mapped model files,
  which the kernel can reclaim.

**Timeouts.**

| Setting | Code default | Compose | What it limits |
|---|---|---|---|
| `MODEL_TIMEOUT_S` | 300 s | 900 s | One model call. It is the httpx timeout; llama-server replies only when the answer is complete, so it bounds the whole call, including time spent waiting behind other requests. |
| `LEASE_S` | 600 s | 3600 s | One job, before the reaper hands it to another worker. Keep it above 3 x `MODEL_TIMEOUT_S` plus the sandbox run. |
| `SANDBOX_CLIENT_TIMEOUT_S` | 20 s | not set | The HTTP call to the sandbox manager |
| `SANDBOX_TIMEOUT_S` | 5 s | 5 s | One check inside a sandbox container |

The code defaults suit the fake model. They do not satisfy the 3 x rule (600 s is less than
3 x 300 s), so set both values when you run a worker outside compose against a slow model.

**Measured on the Mac** (one synthetic 1100 x 1500 page with 3 reagents, and one real photo). The
first numbers are from a cool machine; once the fanless MacBook Air has been under full load for a
few minutes, prompt processing drops to about 20 tokens/s and generation to about 9, and the same
page took 181 s (extract 135.9 s, check code 44.7 s).

| Step | Time | Tokens |
|---|---|---|
| whole page, end to end | 131.5 s | |
| extract call | 99.8 s | 2,292 prompt tokens, including the image, at 28.5 tokens/s; 282 output tokens at 14.6 tokens/s |
| check-code call | 30.7 s | 385 prompt tokens at 54 tokens/s; 355 output tokens at 15 tokens/s |
| sandbox check, warm container | 0.04 s | |
| extract call for a real 5184 x 3456 camera photo, downscaled to 2000 x 1333 | 216 s | 3,594 prompt tokens |

## 9. The sandbox

**Why it exists.** The model writes Python, and the system runs it. That code is untrusted: it
can be wrong, and a page can carry text that steers the model into writing hostile code. It only
ever runs inside a sandbox container. No service runs it in-process: a test fails if `exec(` or
`eval(` appears anywhere in `src/lnd/` outside `sandbox/runner.py` and `sandbox/manager.py`
(`tests/test_architecture.py`). The threat model is in
[DESIGN.md section 5](../DESIGN.md#5-the-sandbox).

**Container flags.** `SandboxPool._create` in `src/lnd/sandbox/manager.py` creates every sandbox
with:

| Setting | Value | Variable |
|---|---|---|
| network | `network_mode: none` | |
| root file system | read-only | |
| `/tmp` | tmpfs `rw,noexec,nosuid,nodev,size=16m,mode=1777` | `SANDBOX_TMPFS_SIZE` |
| user | `10001:10001` | `SANDBOX_USER` |
| capabilities | all dropped | |
| privilege escalation | `no-new-privileges` | |
| processes | `pids_limit` 64 | `SANDBOX_PIDS` |
| memory | 128 MB, with the swap limit equal to it (no swap) | `SANDBOX_MEMORY` |
| CPU | half a CPU (`nano_cpus`) | `SANDBOX_CPUS` |
| `/dev/shm` | 1 MB | |
| ulimits | 256 open files, no core dumps | |
| environment | nothing added by the manager beyond the image's own defaults (such as `PATH`); each exec adds only `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED` and `HOME=/tmp` | |
| mounts | none | |
| runtime | Docker's default (runc), or another such as `runsc` for gVisor | `SANDBOX_RUNTIME` |
| label | `lnd.sandbox=1` | |

**Image.** `docker/sandbox.Dockerfile` is `python:3.12-slim` with pip, setuptools and wheel
removed and a non-root user 10001. It holds no application code and no secrets. Its command,
`python3 -I -c "import signal; signal.pause()"`, does nothing but wait.

**One run** (`SandboxPool.run` and `_exec`):

1. Take a free slot. At most `SANDBOX_MAX_CONCURRENT` (4) runs go at once. If no slot frees up
   within 15 s, the request gets a 503.
2. Take a warm container from the pool. If the pool is empty, or the request says `mode: cold`,
   create and start one ("cold"). `mode: warm` behaves like `auto`: it also falls back to a cold
   start.
3. `docker exec`, as user 10001 in `/tmp`:
   `python3 -I -c <runner> <base64 code> <base64 rows>`. The runner (`sandbox/runner.py`) decodes
   both, runs the code in a fresh namespace, calls `check(rows)`, requires a list of strings, and
   prints one line: `@@LND_RESULT@@` followed by `{"ok": true, "issues": [...]}` (at most 100
   issues) or `{"ok": false, "error": "..."}`. The runner is not a security boundary; the
   container is.
4. A timer kills the container after `SANDBOX_TIMEOUT_S` (5 s). The container is also killed when
   stdout and stderr together pass `SANDBOX_OUTPUT_LIMIT` (64 KiB). During a kill the manager
   keeps reading and discarding output, so the Docker daemon does not block.
5. The answer has `status` (`ok`, `error`, `timeout` or `output_limit`), `result`, `stdout` and
   `stderr` (each clipped to 2,000 characters, without the result line), `exit_code`, `start`
   (`warm` or `cold`) and `timings_ms` (`acquire`, `exec`, `total`).
6. The container is always deleted afterwards, in a background thread. It is never reused. If a
   warm container turns out to have died while idle, the run falls back to a cold one.

**Warm pool.** A background thread keeps `SANDBOX_POOL_SIZE` (4) started, idle containers ready.
It refills when a container is taken, and checks every 2 s. On start the manager removes every
container on the daemon that has the `lnd.sandbox` label. That is why `make test-docker` and
`make sandbox-bench`, which start their own pool, also remove a running stack's pool. A burst
bigger than the pool spills to cold starts instead of waiting, up to the concurrency limit. On
the VM a warm acquire took 0.01 ms at p50 and a whole check run 237 ms warm against 433 ms cold;
on the Mac the check in the single-page run took 0.04 s on a warm container.

**The HTTP API** (`create_app` in `sandbox/manager.py`, port 8090; not published, so it is
reachable from containers on `internal` and, on a Linux host, from the host itself):

| Endpoint | Request and answer |
|---|---|
| `POST /run` | `{"code": ..., "rows": [...], "mode": "auto"}`. `code` is 1 to 16,000 characters; `rows` is up to 200 objects and at most 64,000 bytes as JSON (413 above); `mode` is `auto`, `warm` or `cold`. Any other field is a 422. No free slot gives 503, a Docker error 502. |
| `GET /pool` | image, pool size, ready count, containers created, runs, warm hits, cold starts, create errors, runtime |
| `GET /health` | pings the Docker daemon |

Callers are the workers, after the `check_codegen` call, and the API, which re-runs stored code
after an edit. Both use `sandbox_client.SandboxClient`. Only `sandbox/manager.py` imports the
Docker SDK (`tests/test_architecture.py`). Before sending, `checks.extract_code` refuses code
without `def check` or longer than 16,000 bytes. `checks.run_code` turns an unreachable or failing
manager into a check result `sandbox unavailable: ...`.

**What the manager can and cannot do.**

- It can do anything the Docker socket allows, which is the same as root on the host. That makes
  it the most valuable target in the stack.
- Its code only creates containers from `SANDBOX_IMAGE` with the flags above, runs the runner in
  them, and removes containers labelled `lnd.sandbox`. A caller cannot choose the image, network,
  mounts, limits or timeout: `RunRequest` rejects extra fields
  (`test_manager_only_accepts_code_and_rows` in `tests/test_sandbox_escape.py`).
- It has no database password, no Redis URL and no data volume (`tests/test_compose.py`), and it
  does not import the database or queue modules (`tests/test_architecture.py`). Redis and the
  model server have no authentication of their own, so on the `internal` network only the
  manager's code keeps it away from them.
- It runs read-only, as user 10001, with all capabilities dropped and `no-new-privileges`. It
  reaches the socket through `group_add: ${DOCKER_GID}` (0 on Docker Desktop, where the socket
  inside containers belongs to root's group).
- Not covered: a bug that lets a caller steer container creation, or a kernel escape from a
  sandbox, is a host compromise. The flags shrink the attack surface; the kernel is still shared.
  DESIGN.md lists the next steps (gVisor through `SANDBOX_RUNTIME=runsc`, separate hosts, a socket
  proxy).
- A check result is a hint, not a gate. A wrong or fooled check can report "all fine", so the
  review screen shows the code next to the verdict.

## 10. Networks and egress

| Network | Created as | Members | Way out |
|---|---|---|---|
| `internal` | `internal: true` | postgres, redis, model-server, api, worker, sandbox-manager | none: Docker adds no route out |
| `edge` | bridge with `com.docker.network.bridge.enable_ip_masquerade: "false"` | api only | none on a Linux host: no NAT. On Docker Desktop for Mac a container on such a bridge did reach the internet (measured), so there the API is not cut off. It exists so Docker can publish the API's port, which needs a network that is not internal. |
| `download` | ordinary bridge | models-fetch only | yes |
| none | `network_mode: none` | sandbox containers, sandbox-image | no network at all |

Only the API publishes a port, `127.0.0.1:${API_PORT:-8000}:8000`. No other service publishes
one. On Docker Desktop that keeps them out of reach from the Mac. On a Linux host, Docker still
lets the host itself connect to container IPs on an internal network, so a local process can
reach Postgres, Redis, the model server and the sandbox manager by container IP. Of these, only
Postgres asks for a password.

| From | To | How |
|---|---|---|
| browser or another local client | api | `http://127.0.0.1:API_PORT`, through `edge` |
| api | postgres, redis | `internal` |
| api | sandbox-manager | `internal`, `POST /run` (re-run of stored check code after an edit) |
| worker | postgres, redis | `internal` |
| worker (the gateway) | model-server | `internal`, `POST /v1/chat/completions` |
| worker | sandbox-manager | `internal`, `POST /run` |
| sandbox-manager | host Docker daemon | the mounted socket `/var/run/docker.sock` |
| sandbox containers | anything | nothing; results come back only through the `docker exec` stream |
| models-fetch | huggingface.co, plus the storage host its download links redirect to | `download`, HTTPS, only while `make models` runs; the GET follows redirects |
| any other container | the internet | nothing: `internal` has no route; `edge` has no NAT on Linux, but not on Docker Desktop (above) |

**Why sandboxes get no network.** An internal network still lets a container reach its
neighbours: Postgres, Redis (which has no password), the model server and the manager. Code that
only does arithmetic needs none of them, so it gets no network at all. The escape test tries
1.1.1.1, 8.8.8.8, the Docker bridge gateway, port 8090 on loopback and a DNS lookup, and all must
fail. More in [DESIGN.md section 6](../DESIGN.md#6-network-lockdown).

**Tests.** `tests/test_compose.py` reads the compose file and fails if anything but the API
publishes a port, if a service is on the wrong network, if the socket is mounted anywhere but the
manager, or if a password is written into the file. `tests/test_egress.py` (needs Docker) builds
an `internal: true` network and shows a container on it reaches a neighbour (the control) but not
1.1.1.1, 8.8.8.8 or example.com, and builds a no-masquerade bridge like `edge` and shows it cannot
reach 1.1.1.1 while an ordinary bridge can. All of that passes on Linux (CI). On Docker Desktop for
Mac the `internal` tests pass too, but the no-masquerade bridge reaches 1.1.1.1, which the test
records as an expected failure there.

**What `make models` does.**

1. It runs `make env` first: compose refuses to start without `POSTGRES_PASSWORD` and
   `DOCKER_GID`, which `.env` provides.
2. `docker compose --profile models run --rm --build models-fetch` builds `lnd-app` and runs
   `python -m lnd.fetch_models /models` on the `download` network, with the `models` volume
   mounted read-write.
3. For each file in `src/lnd/models.json`, `fetch_models` asks Hugging Face for the size and
   SHA-256 at the pinned revision (a `HEAD` request, redirects off, that reads the
   `x-linked-size` and `x-linked-etag` headers), and refuses to go on if they differ from the pins.
4. It downloads to `<file>.part`, following the redirect to Hugging Face's storage host and
   resuming with an HTTP Range request after an interruption, then checks size and SHA-256 and
   renames the file. A file that is already there and passes the check is skipped.

The files are `Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf` and `mmproj-Qwen2.5-VL-7B-Instruct-Q8_0.gguf`
from `ggml-org/Qwen2.5-VL-7B-Instruct-GGUF` at revision
`508edd0afaa66bb9e9f40587acc2184f02daf1f6`. Apart from pulling and building images, this is the
only step that uses the internet. After it, the model server reads the files from the volume.

## 11. The review screen

![The review screen: page image on the left, extracted fields on the right](review-screen.png)

`src/lnd/static/` holds three files that the API serves at `/` (`StaticFiles` with
`html=True`): `index.html`, `app.js` and `style.css`. There is no framework and no build step.

**Tenant.** A text box in the header, remembered in `localStorage` as `lnd-tenant`. Calls made
through the `api()` helper send it as the `X-Tenant` header; the usage call puts it in the path.
Requests the browser makes on its own (the page image, the export links and the `EventSource`)
cannot send headers, so their URLs carry `?tenant=`. The API accepts either (`api.tenant_dep`);
the default is `default`, and a name is 1 to 64 letters, digits, `_`, `.` or `-`.

| What the user does | Requests |
|---|---|
| opens the screen | `GET /api/documents`, `GET /api/tenants/{t}/usage` |
| uploads a file | `POST /api/documents` (multipart), then opens the new document |
| opens a document | `GET /api/documents/{id}` for the progress bar and page chips; an `EventSource` on `/api/documents/{id}/events?tenant=`; the export links become `/api/documents/{id}/export.json?tenant=` and `.../export.csv?tenant=`; a one-page document opens its page at once |
| (a progress event arrives) | 300 ms later, `GET /api/documents/{id}` and the usage again; on `document_done` the stream is closed and the list refreshed; if the open page changed status, its record is reloaded, and its image too if it had not loaded yet |
| opens a page | the image element loads `/api/pages/{pid}/image?tenant=`; `GET /api/pages/{pid}/record`; then, if there is a record, `GET /api/pages/{pid}/versions` |
| retries a failed page | `POST /api/pages/{pid}/retry`, then a new stream with `?after=<last id>` |
| loads an old version | `GET /api/pages/{pid}/versions/{v}` fills the form; saving makes it current as a new version |
| saves | `PUT /api/pages/{pid}/record` with `{"data": ..., "editor": ..., "note": ...}` |
| exports | the links download JSON or CSV |

**Saving.** The browser first checks the date format, the numbers and that every reagent has a
name. The API validates the body as `RecordUpdate` (a full `PageExtraction`; extra keys give a
422), answers 409 if the page has no model record yet, and adds a `user` version
(`store.add_user_version`). If the page has stored check code and the edited data has reagent
rows, `api.put_record` runs that stored code on the edited rows through the sandbox manager, with
no new model call, and saves the result over the old one. The response holds the new version
number and the check result. If the model wrote no check code (for example, the page had no
reagents at first), edits run no check. There is no conflict check: two saves both become
versions, and the later one is current.

**Exports** (`src/lnd/exports.py`). JSON holds the document and, for each page, the latest
version (`page`, `status`, `version`, `source`, `data`). CSV has one line per reagent row; a page
with no reagents or no record still gets one line. Text cells that start with `=`, `+`, `-`, `@`,
a tab or a carriage return get a leading `'`, so a spreadsheet does not run them as formulas.
Numbers stay numbers.

**Model output is never parsed as HTML.** Page text comes from the model and may contain
anything, including markup. `app.js` puts server data on the page only through `textContent`,
input values, or a `title` attribute set with `setAttribute`. It never uses `innerHTML`. The
check code is shown in a `pre` element through `textContent`.

## 12. Configuration

Compose passes a container only the variables written in `docker-compose.yml`. The `.env` file
fills in the `${...}` references there; it is not handed to the containers as a whole. To change,
say, `PDF_DPI` in the stack, add it to the `x-app-env` block. Settings are read once per process
(`config.get_settings` is cached).

**Read by `src/lnd/config.py`** (API and workers):

| Variable | Code default | Compose sets | Meaning |
|---|---|---|---|
| `DATABASE_URL` | `postgresql://lnd@127.0.0.1:55432/lnd_test` | `postgresql://lnd:${POSTGRES_PASSWORD}@postgres:5432/lnd` | Postgres |
| `REDIS_URL` | `redis://127.0.0.1:56379/0` | `redis://redis:6379/0` | the queue |
| `DATA_DIR` | `./data` | `/data` | uploads and page images |
| `MODEL_BASE_URL` | `http://127.0.0.1:8081` | `http://model-server:8080` | the model server |
| `MODEL_NAME` | `qwen2.5-vl-7b-instruct-q4_k_m` | `${MODEL_NAME:-qwen2.5-vl-7b-instruct-q4_k_m}`, also llama-server's `--alias` | the `model` field and `llm_calls.model` |
| `MODEL_TIMEOUT_S` | `300` | `${MODEL_TIMEOUT_S:-900}` | timeout of one model call |
| `EXTRACT_MAX_TOKENS` | `2048` | not set | output limit of extract calls |
| `CHECK_MAX_TOKENS` | `600` | not set | output limit of check-code calls |
| `SANDBOX_URL` | `http://127.0.0.1:8090` | `http://sandbox-manager:8090` | the sandbox manager |
| `SANDBOX_CLIENT_TIMEOUT_S` | `20` | not set | HTTP timeout to the manager |
| `MAX_ATTEMPTS` | `3` | not set | attempts per job |
| `BACKOFF_BASE_S` | `2.0` | not set | first retry delay |
| `BACKOFF_CAP_S` | `60.0` | not set | delay cap, applied before the jitter |
| `LEASE_S` | `600` | `${LEASE_S:-3600}` | job lease |
| `PDF_DPI` | `150` | not set | PDF render resolution |
| `MAX_IMAGE_PX` | `2000` | not set | long-side cap for photos and PDF pages |
| `MAX_UPLOAD_MB` | `500` | not set | PDF size limit; photos have a fixed 50 MB limit in `api.py` |
| `DEFAULT_TOKEN_BUDGET` | empty, meaning unlimited | `${DEFAULT_TOKEN_BUDGET:-}` | budget of new tenants |

`LOG_LEVEL` (default `INFO`) sets the log level of the worker (`worker.py`) and the sandbox
manager.

**Read by `src/lnd/sandbox/manager.py`:**

| Variable | Code default | Compose sets |
|---|---|---|
| `SANDBOX_IMAGE` | `lnd-sandbox:latest` | `lnd-sandbox:latest` |
| `SANDBOX_POOL_SIZE` | `4` | `${SANDBOX_POOL_SIZE:-4}` |
| `SANDBOX_TIMEOUT_S` | `5` | `${SANDBOX_TIMEOUT_S:-5}` |
| `SANDBOX_RUNTIME` | empty (Docker's default, runc) | `${SANDBOX_RUNTIME:-}` |
| `SANDBOX_MEMORY` | `128m` | not set |
| `SANDBOX_CPUS` | `0.5` | not set |
| `SANDBOX_PIDS` | `64` | not set |
| `SANDBOX_TMPFS_SIZE` | `16m` | not set |
| `SANDBOX_OUTPUT_LIMIT` | `65536` | not set |
| `SANDBOX_MAX_CONCURRENT` | `4` | not set |
| `SANDBOX_USER` | `10001:10001` | not set |
| `BIND_HOST` | `127.0.0.1` | `0.0.0.0` |
| `PORT` | `8090` | `8090` |

The 15 s wait for a free sandbox slot is fixed in the code (`SandboxConfig.queue_wait_s`).

**Used only by compose:**

| Variable | Default | Used for |
|---|---|---|
| `POSTGRES_PASSWORD` | none: compose refuses to start without it; `make env` generates one | Postgres and `DATABASE_URL` |
| `DOCKER_GID` | none: required; `make env` writes the socket's group id, or 0 | `group_add` of the sandbox manager |
| `API_PORT` | `8000` | host port of the API (the Makefile reads it too) |
| `FAKE_LATENCY_MS` | `0` | `docker-compose.fake.yml`: delay of the fake model server |

The fake model server also reads `FAKE_ERROR_RATE`, `FAKE_ORACLE_DIR`, `BIND_HOST` and `PORT`
(default 8081). The tests read `LND_TEST_DATABASE_URL`, `LND_TEST_REDIS_URL`, `SANDBOX_IMAGE` and
`CI`.

## 13. Failure modes and recovery

| What fails | What happens | Where in the code |
|---|---|---|
| A worker crashes mid-job | Its job stays in `inflight` until the lease ends (3600 s in compose). Then any worker's reaper puts it back on `ready` with one attempt counted, or on `dead` (page `failed`) if it was the last attempt. Committed work is kept. A crash after the commit but before the ack leads to a rerun that is skipped. | `queue.py` (`_REAP`), `worker.Worker._reap`, `pipeline.finalize_dead`, `pipeline.process_job` |
| A worker is stopped (SIGTERM) | It stops taking new jobs and finishes the current one if it can. Compose sets no `stop_grace_period`, so Docker sends SIGKILL after 10 s. With the real model a job takes minutes, so a worker that holds a job when `docker compose stop` or `down` runs, or when `make up` recreates it after a rebuild, is usually killed mid-job. Its job stays in `inflight` until the lease ends (3600 s in compose) and then counts as one failed attempt. | `worker.main`, `Worker.stop`, `docker-compose.yml` |
| The model server is down or restarting | The call fails at once (`ModelError`, an `llm_calls` row with status `error`). The attempt fails and is retried after about 2 s and about 4 s; after three attempts the page is `failed`. So a model server that restarts under running workers can fail pages within seconds; retry them by hand. At stack start, compose holds the workers back until the model server is healthy. | `gateway.LLMGateway.chat`, `pipeline.process_job`, `JobQueue.fail`, `docker-compose.yml` |
| The model server is slow | A call that passes `MODEL_TIMEOUT_S` fails the same way. With `-np 1`, calls from several workers queue inside llama-server and the waiting counts. If a whole job outlives `LEASE_S`, the reaper gives it to another worker while the first is still busy: both call the model, the first to finish writes the record, and the second finish changes nothing. | the same, plus `store.finalize_page_done` |
| The model returns invalid output | One retry call with the error. A second invalid answer fails the attempt (`ExtractionError`), and the queue retries the page, up to three attempts. | `extraction.extract_page` |
| Check code is unusable, crashes, loops or floods output | The page is saved anyway. The check result has status `error`, with the sandbox status (`timeout`, `output_limit`) when there is one. The review screen shows "Check did not run" and the reason. | `checks.run_checks`, `checks.run_code`, `sandbox/manager.py` |
| The sandbox manager is down or busy | The client raises `SandboxUnavailable`, which becomes the check result `sandbox unavailable: ...`; the page is still saved. Beyond `SANDBOX_MAX_CONCURRENT` runs, a request waits up to 15 s and then gets a 503, with the same result. An edit made while the manager is down saves the version and stores the error as the check result. | `sandbox_client.py`, `checks.run_code`, `api.put_record` |
| The API restarts mid-stream | Workers carry on; they do not use the API. The browser's `EventSource` reconnects with `Last-Event-ID` and gets exactly the events it missed, from Postgres. An upload that was in flight fails and has to be sent again. | `api.stream_events`, `events.py`, `tests/test_sse.py` |
| The enqueue fails after the upload committed (Redis down) | The upload gets a 500, but the document and its `queued` pages exist. `POST /api/documents/{id}/requeue` enqueues them. A manual retry that fails the same way leaves the page `queued`, and requeue fixes that too. | `api.upload`, `api.retry_page`, `api.requeue` |
| Redis restarts | The queue survives a restart: Redis keeps an append-only file on the `redisdata` volume. Redis syncs that file to disk once a second (its default), so a crash can lose about the last second of queue changes; `requeue` and the leases recover from that. | `docker-compose.yml` |
| Redis loses its data | Postgres still has every page's status. `POST /api/documents/{id}/requeue` re-enqueues the pages marked `queued`, including those waiting out a back-off. A worker that held a job finishes it; its ack finds nothing to remove. A page whose worker also died stays `processing`; nothing puts it back today. | `store.queued_page_jobs`, [DESIGN.md section 1](../DESIGN.md#1-the-shape-of-the-system) |
| Redis is unreachable | Uploads fail after the commit (see above). Workers exit at their next Redis call (reap, pop, ack or fail), and Docker restarts them (`restart: unless-stopped`). `/healthz` fails. | `worker.Worker.run`, `pipeline.process_job`, `api.healthz` |
| The token budget is spent | The gateway refuses before calling the model (a `rejected` row). During extraction the page fails at once with `budget_exceeded` and no retries; during the check call the page is saved with the check `skipped`. Raise the budget, then retry the failed pages. Calls already running can overshoot. | `gateway.LLMGateway._check_budget`, `pipeline.process_job` |
| Postgres is down | API requests fail with 500 and `/healthz` fails. A worker that cannot reach Postgres exits. If the error comes while it loads the page, the job stays in `inflight` until its lease ends and then counts one attempt. If it comes later in the job (budget check, metering, saving), `queue.fail` has already moved the job to `delayed` (back after the back-off), or to `dead` on the last attempt, with the page left `processing` (see the known gaps). Docker restarts the worker, which keeps exiting at `init_schema()` until Postgres is back. Nothing committed is lost. | `db.py`, `pipeline.process_job`, `worker.main` |
| Two reviewers save the same page | Both saves become versions; the later one is current. | `store.add_user_version` |

Known gaps in the current code:

- `JobQueue.fail()` returns `retry` or `dead` from the attempt count alone, even when its Lua
  script found that the job was no longer in flight (its lease had expired and the reaper had
  passed it on). `process_job` acts on that answer: it sets the page back to `queued`, or marks it
  `failed` and may emit `document_done`, while another worker still has the job. When that worker
  finishes, the page becomes `done`, possibly after `document_done` was sent. This needs a job to
  outlive `LEASE_S`, which the compose values make unlikely.
- If Postgres fails at the moment a worker records a failed attempt, or marks the pages of jobs
  the reaper sent to `dead`, the queue has already moved the job. If it went to `dead`, the page
  stays `processing`.
- Requeue can create duplicate jobs (section 4).

## 14. Testing and verification

| Behaviour | Proved by | Needs |
|---|---|---|
| Queue: first in first out and ack; back-off, then a retry; three failures go to `dead`; back-off growth and cap; an expired lease returns the job with an attempt counted; expiry on the last attempt is reported as dead; a late `fail` after the lease was lost does not schedule the job twice; 8 threads never get the same job; `purge_dead` | `tests/test_queue.py` | Redis |
| Pipeline end to end: a photo; a PDF (one job per page, rendered in the worker, gap-free ids); a duplicate job and a double finish change nothing; retry then success; dead after three failures; manual retry; invalid JSON once; a spent budget fails pages without retries; the sandbox down keeps the extraction; check issues are stored; 8 workers produce one `document_done`; a vanished worker's job is recovered | `tests/test_pipeline.py` | Postgres, Redis, the fake model in-process, `StubSandbox` |
| API: health; upload validation, including a password-protected PDF; PDF pages counted, not rendered; 404s; tenant isolation, also through `?tenant=`; edits create versions and keep history; edit validation (422) and 409; edits re-run the stored check; JSON and CSV export with formula defusing; EXIF rotation; PNG stored byte for byte; queue endpoint; UI served | `tests/test_api.py` | Postgres, Redis, the fake model in-process for the tests that finish pages |
| SSE: full replay, then the end; `Last-Event-ID`; `?after=`; a bad id; another tenant gets 404; the API killed with SIGKILL mid-job, ids 1..N arrive exactly once | `tests/test_sse.py` | Postgres, Redis, the fake model; the restart test spawns the API, two workers and the fake model server, and runs the sandbox stub in a thread |
| Gateway: the metering row; the usage endpoint; usage per tenant; a refusal without a model call; raising the budget; model errors cost no tokens; only the gateway knows the endpoint | `tests/test_gateway.py` | Postgres, Redis (the `clean` fixture checks and flushes it), the fake model |
| Extraction: one call when valid, one retry when not, never two retries | `tests/test_extraction.py` | Postgres, Redis (the `clean` fixture checks and flushes it), the fake model |
| Output schema: extra keys, dates, nulls, ranges, the flattened LLM schema with every key required in page order, fenced and truncated JSON | `tests/test_schemas.py` | nothing |
| Module rules: only the manager imports the Docker SDK; API and worker modules never import docker or subprocess; no `exec(` or `eval(` outside `sandbox/runner.py` and `sandbox/manager.py`; the manager imports no database or queue code; no hard-coded secrets | `tests/test_architecture.py` | nothing |
| Compose layout: only the API publishes, on loopback; network membership; `internal` and the no-NAT `edge`; the socket only in the manager; the manager has no credentials or data volume; nothing privileged or on the host network; app containers hardened; no passwords in compose files; `docker compose config` accepts the files | `tests/test_compose.py` | nothing (the Docker CLI for the last check) |
| Model download: verify, skip, resume, reject a bad hash; the manifest pins revision, sizes and hashes | `tests/test_fetch_models.py` | nothing |
| Accuracy harness: the answer key, the planted errors, reproducible page renders, scoring rules, an oracle run scores 100 percent, unfinished pages count as failed, damaged answers score lower | `tests/test_evaluation.py` | Postgres, Redis and the oracle fake model in-process for the end-to-end parts |
| Sandbox escapes, 15 tests: network, writes outside `/tmp`, the small `/tmp`, secrets and host files, fork bomb, endless loop, endless output, one giant print, memory bomb, bad code, the container's flags, non-root without capabilities, single use, warm pool, parallel runs, the narrow HTTP API | `tests/test_sandbox_escape.py` | Docker and the sandbox image (`docker` marker) |
| Egress: an internal network and a no-NAT bridge have no way out | `tests/test_egress.py` | Docker and the sandbox image (`docker` marker) |

**Running them** (targets in the `Makefile`):

- `make install` installs the package and the dev tools (into `.venv` if it exists).
- `make test-deps` starts a throwaway Postgres on `127.0.0.1:55432` and Redis on
  `127.0.0.1:56379` from `docker-compose.test.yml`. `make test` runs `pytest -m "not docker"`.
  Without the two services, the integration tests skip locally; with `CI=true` they fail.
- `make test-docker` builds the sandbox image and runs `pytest -m docker`. Stop the stack first:
  these tests remove every `lnd.sandbox` container, and two of them count those containers.
- `make lint` runs ruff.
- CI (`.github/workflows/ci.yml`) has four jobs: `lint`; `test` (Postgres and Redis as services,
  `CI=true`); `sandbox` (builds the real sandbox image, runs the `docker` tests, then the sandbox
  benchmark for information); `images` (builds both images, runs `docker compose config -q` on
  the three compose files, and starts the app image read-only with the fake model server as a
  smoke test).

**The fake model server** (`src/lnd/fakes/model_server.py`) speaks the same
`/v1/chat/completions` shape. A request with an image gets a canned aspirin page; a request
without one gets a working `check()` function. Token counts are made up, so metering has numbers
to count. `POST /_control` sets latency, injected HTTP 500s, broken JSON, an error rate and the
check code; `GET /_stats` counts requests. The tests run it in a thread (`tests/conftest.py`),
`make up-fake` swaps it in for the model server (`docker-compose.fake.yml` replaces only that
service), `make loadtest` uses it, and CI starts it as the image smoke test.

**The sandbox stub** (`src/lnd/fakes/sandbox_stub.py`) answers `/run` with "ok, no issues" and
starts nothing. The local load test and the SSE restart test use it. Most other tests use the
`StubSandbox` class in `tests/conftest.py`.

**The oracle evaluation.** `make eval-pages` renders 20 synthetic pages from
`evaluation/facts.json` (`evaluation/render_pages.py`) and writes `evaluation/pages/index.json`,
which maps each image's SHA-256 to its page id. With `FAKE_ORACLE_DIR` set, the fake model answers
each known image with its true page, optionally damaged by an error rate. That tests the scorer
and the plumbing, not a model (`tests/test_evaluation.py`). `make eval` sends the same 20 pages
through a running stack with the real model (`evaluation/run_eval.py`, total timeout
`EVAL_TIMEOUT`, 7200 s by default), scores every field (`evaluation/score.py`), writes
`evaluation/results/latest.json`, and exits with an error if pages did not finish.

**The load test** (`bench/load_test.py`). `python -m bench.load_test` starts the API, the fake
model, the sandbox stub and 1, 2, 4 and 8 worker processes on this machine, against the Postgres
and Redis in `DATABASE_URL` and `REDIS_URL`. `make loadtest` instead brings up the compose stack
with the fake model (and the real sandbox manager), scales `worker` to each of `WORKER_SETS`, and
each time uploads a 10-page warm-up PDF, then a `PAGES`-page PDF. The VM results are in the
README.

**The sandbox benchmark** (`bench/sandbox_bench.py`). `make sandbox-bench` starts its own manager
(or uses one given with `--url`), measures warm and cold acquire and total times, and sends a
burst bigger than the pool. The VM results are in the README.

## 15. Where the code is

```
docker-compose.yml           the stack: services, networks, volumes
docker-compose.fake.yml      swaps in the fake model server
docker-compose.test.yml      throwaway Postgres and Redis for the tests
docker/app.Dockerfile        one image for api, worker, sandbox manager, models-fetch and fakes
docker/sandbox.Dockerfile    the sandbox image: Python only, no pip
Makefile                     the make targets this document names
src/lnd/api.py               HTTP API, progress stream, exports, static files
src/lnd/ingest.py            type sniffing, photo normalising, PDF counting and rendering, atomic writes
src/lnd/store.py             queries for documents, pages, versions and checks; events are written
                             through events.emit, and the SSE endpoint in api.py reads events itself
src/lnd/events.py            event numbering, event reads and SSE frames
src/lnd/db.py                connection pool, schema setup, tenant rows
src/lnd/schema.sql           the tables
src/lnd/queue.py             the Redis queue and its Lua scripts
src/lnd/worker.py            worker loop, reaper calls, signal handling
src/lnd/pipeline.py          one page job, start to finish
src/lnd/extraction.py        prompt, parsing, validation, one retry
src/lnd/schemas.py           PageExtraction, API bodies, the reduced schema sent to llama.cpp
src/lnd/gateway.py           the only model client: budgets and metering
src/lnd/checks.py            check-code prompt, code extraction, result shape
src/lnd/sandbox_client.py    HTTP client for the sandbox manager
src/lnd/sandbox/manager.py   sandbox manager: pool, container flags, HTTP API
src/lnd/sandbox/runner.py    the program that runs inside a sandbox
src/lnd/exports.py           JSON and CSV export
src/lnd/config.py            settings from environment variables
src/lnd/fetch_models.py      make models: pinned, verified download
src/lnd/models.json          model files, revision, sizes and SHA-256
src/lnd/fakes/               fake model server and sandbox stub
src/lnd/static/              the review screen: index.html, app.js, style.css
tests/                       pytest (map in section 14)
bench/                       load test and sandbox benchmark
evaluation/                  answer key, page renderer, scorer, eval runner
.github/workflows/ci.yml     CI jobs
```
