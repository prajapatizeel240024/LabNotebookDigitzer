# Design notes

What was decided, why, what it costs, and what would change in production. Where a number is
quoted it was measured on a 2 vCPU VM (see the README).

## 1. The shape of the system

One API, a Redis queue, a fleet of workers, a model server, a sandbox manager, Postgres.
Pages are the unit of work: a 300-page PDF becomes 300 small jobs, so it spreads over as many
workers as you start, and one bad page fails alone.

Postgres is the source of truth (documents, pages, versions, events, token usage). Redis only
holds "what to do next". If Redis lost everything, `POST /api/documents/{id}/requeue` puts the
unfinished pages back.

## 2. The queue

A plain Redis list is not enough: a worker that dies after popping a job loses it. So the queue
is four structures changed by Lua scripts (each script is atomic):

- `ready` list: jobs waiting.
- `inflight` sorted set: jobs a worker has taken, scored by when its lease ends.
- `delayed` sorted set: jobs waiting out a retry back-off.
- `dead` list: jobs that failed 3 times.

A worker pops a job and gets a lease (10 minutes by default). It acks when done. If it crashes,
a reaper (every worker runs one, once a second) finds the expired lease and puts the job back,
and that counts as an attempt. Retry delay is `2s * 2^(n-1)` with 20 percent jitter, capped at
60 s. After the third failure the job goes to `dead` and the page is marked failed, with the
error visible in the review screen; `POST /api/pages/{id}/retry` revives it.

All times come from Redis `TIME`, not from worker clocks, so skewed clocks cannot expire a lease
early.

**Idempotent jobs.** A job can run twice (a worker is slow, its lease expires, another worker
takes it). That is safe because finishing a page is one transaction that only writes if the page
is not already done, and rendered images are written under a unique temp name then renamed.

**Mapping to Azure Container Apps.** The worker already is a stateless container that reads a
queue, so it fits a Container Apps *job* (or an app with a scale rule) scaled by KEDA. The
`redis` KEDA scaler on the `ready` list length (`listName`, `listLength` = pages one replica
should hold) scales from zero. Replicas must finish or hand back their job on SIGTERM; the
worker already stops taking new jobs on SIGTERM and finishes the current one, and a lease
covers the case where it is killed anyway. Scale-in is safe for the same reason. The model
server would be a separate GPU workload profile; workers talk to it over the internal network.

## 3. The gateway and the token budget

Every model call goes through `gateway.py`; a test fails if any other module mentions the
chat-completions path. The gateway checks the tenant's budget before the call, makes it, and
writes one `llm_calls` row (tenant, job, purpose, prompt tokens, completion tokens, latency).
`GET /api/tenants/{t}/usage` sums them. Once a tenant has used its budget, the next call is
refused and the page fails without burning retries, since retrying cannot help.

Cost: the check happens before the call and the usage is added after it, so with N workers up to
N calls can start just under the limit and overshoot it. It is a soft limit. A hard limit needs
a reservation (subtract an estimate first, correct it after), which was left out to keep the
code small.

## 4. Progress that survives restarts

Events are rows in Postgres, not messages in memory. Each document has a counter; an event gets
the next number while the document row is locked, so numbers per document have no gaps and no
two events share one. The SSE endpoint just reads rows with `seq > last-seen`, polling every
0.25 s, and sends each as `id: <seq>`. A browser that reconnects sends `Last-Event-ID` and
gets exactly what it missed. The API holds no per-client state, so restarting it (or deploying a
new version) loses nothing; the test kills the API with SIGKILL in the middle of a 40-page job
and checks ids 1..N arrive once each.

The same lock makes "last page finished" happen once: whoever finishes a page locks the
document, counts pages, and emits `document_done` only if it is the one that completed it.

Trade-off: polling costs a query per open stream every 0.25 s. Postgres `LISTEN/NOTIFY` would
wake streams only when something changes. Polling was chosen because it is simple, correct after
any restart, and the load is tiny at this scale.

## 5. The sandbox

The model writes Python and we run it. That code is untrusted: it may be wrong, it may be
hostile (a scanned page could carry text that steers the model), and it must not be able to
hurt anything.

**What the code gets:** a container with no network at all, a read-only root file system, a 16 MB
`noexec` tmpfs at `/tmp`, user 10001, every Linux capability dropped, `no-new-privileges`, 64
processes, 128 MB memory with no swap, half a CPU. It receives its input (the reagent rows) as
an argument and prints one result line. The manager kills it at the timeout (5 s) or when output
passes 64 KB. Each container runs one check and is then removed. A new one replaces it in the
pool.

**Why `network_mode: none` and not the internal network.** An internal Docker network still lets
a container talk to its neighbours: the database, Redis, the model server. Code that is only
asked to do arithmetic needs none of that, so it gets no network at all.

**Warm pool.** Starting a container costs about 160 ms on the test VM (cold acquire p50 157 ms,
p95 184 ms). The manager keeps a few already-started idle containers, so a request takes one in
about 0.01 ms. Whole-run times were 237 ms warm vs 433 ms cold at p50. The remaining 237 ms is
mostly `docker exec` starting a fresh Python process; a runner that is already running and
pre-imported in the warm container would cut most of it. Not done: it needs a protocol between
manager and container, and a long-running process inside is more state to reason about.
A burst larger than the pool spills to cold starts rather than queueing (4 warm and 8 cold in
the 12-request test).

**Why the manager is also a risk.** Starting sibling containers needs the Docker API, and
access to the Docker socket is the same as root on the host: whoever can talk to it can start a
privileged container that mounts the host disk. So the one container that holds the socket is
the most valuable target in the system. What is done about it:

- Only the sandbox manager has the socket; a test reads the compose file and fails otherwise.
- The manager has no database or Redis credentials and no data volume. Compromising it gives the
  host, but not the data directly. (The host would give the data, so this only narrows the path.)
- Its HTTP API accepts only `code`, `rows` and `mode`, and rejects any other field. A caller
  cannot choose the image, the network, mounts or limits; those are fixed in the manager.
- The sandbox image is fixed, has no pip and no secrets.
- The manager runs read-only, non-root, with capabilities dropped. (Reaching the socket needs the
  docker group id, passed with `group_add`.)

What this does *not* fix: a bug in the manager that lets a caller control container creation is
still a host compromise, and a kernel escape from the sandbox container is a host compromise.
Containers share the host kernel; the flags shrink the attack surface, they do not remove it.

**For production**, in order of value:
1. Replace the shared kernel boundary with gVisor (`runsc`, set `SANDBOX_RUNTIME=runsc`, already
   supported) or Firecracker micro-VMs, so hostile code meets a small user-space kernel or a
   hypervisor, not the host kernel.
2. Run sandboxes on separate hosts or node pools from the data, so an escape lands on a machine
   with nothing in it.
3. Do not mount the real socket: use rootless Docker for the manager, or a socket proxy that only
   allows create/start/exec/remove on containers with the sandbox label and image.
4. On Azure, Container Apps dynamic sessions give per-session isolated code execution without
   running your own manager.

## 6. Network lockdown

Three Docker networks:

- `internal` is created with `internal: true`: Docker adds no route to the outside. Postgres,
  Redis, the model server, workers, the sandbox manager and the API all sit on it.
- `edge` exists only so Docker can publish the API's port. It has IP masquerading switched off,
  so even something attached to it cannot reach the internet. Only the API joins it, and the port
  is published on `127.0.0.1`.
- `download` is a normal bridge used only by `make models`, a one-off container.

The model server does not need the internet because the weights are in a volume. A test builds an
`internal` network and shows a container on it cannot connect to public IPs or resolve names, and
that a neighbour on the same network is still reachable (so the failure means "no route", not
"broken network").

Another test reads the compose file and fails if anything other than the API publishes a port,
if anything but `models-fetch` is on the download network, if `docker.sock` appears anywhere but
the manager, or if a password is written into the file.

## 7. Reading pages with a small model

- **Strict shape.** The reply must match a JSON schema, sent to llama.cpp as a grammar so the model
  cannot produce other text. The schema sent drops length and range keywords (large `maxLength`
  values make llama.cpp's schema-to-grammar step explode); Pydantic enforces them afterwards.
- **One retry.** If the answer is still invalid (a truncated reply, a bad date), the worker sends
  the bad answer back with the error once. After that the attempt fails and the queue's retry
  rules take over.
- **Dates** must be `YYYY-MM-DD` or null. The prompt says to convert; the validator refuses
  anything else, so "14 Mar" cannot slip in as a string.
- **PDF size.** Pages are rendered one at a time inside the worker at 150 dpi and capped at 2000
  px on the long side. The API only counts pages at upload, so a 300-page PDF is not rendered in
  the web process.
- **A check can lie about itself.** The model writes the check and reports its verdict; a wrong
  or fooled check can say "all fine". So a check result is a hint shown next to the fields, not a
  gate, and the stored code is shown to the reviewer. Edits re-run the stored code, not a new
  model call.

## 8. Choices that are simple on purpose

- **Tenant = header.** `X-Tenant` separates data and budgets. It is not authentication anyone
  could rely on. In production it comes from a verified token, set by the edge, never by the
  client.
- **Per-document lock.** Completing a page locks one document row. Pages of the same document
  finish one at a time at that step; different documents are independent. The critical section is
  a few short queries, so it did not show up in the load test.
- **Schema setup** runs at start under an advisory lock, from one `schema.sql` file with
  `IF NOT EXISTS`. There is no migration tool. A real deployment needs one (Alembic).
- **CSV export** prefixes text cells that start with `=`, `+`, `-` or `@` so a spreadsheet does
  not run them as formulas. Number cells are left alone.
- **Compose, not Kubernetes.** It matches "scale with `--scale worker=N`". The Container Apps
  mapping is in section 2.

## 9. What the measurements do and do not show

- The load test shows the queue and workers scale: 1.98x at 2 workers and 7.14x at 8 when the
  model takes 250 ms. With an instant model the 2 CPUs are the limit and the curve flattens at
  about 1.5k pages/min. That flat part says something about this VM, not about the design.
- No real model accuracy number exists in this repo yet. The harness has been checked only with a
  fake that returns the true answer, so it shows the scorer and the plumbing are right.
- The sandbox numbers come from a local Python image, plain `runc`, on a VM that is already
  virtualised. A different host, or gVisor, will give different times.
