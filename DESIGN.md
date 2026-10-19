# Design notes

What was decided, why, what it costs, and what would change in production. Every number names
the machine it was measured on; the README's Results section describes the machines. How the
system works end to end is in [docs/architecture.md](docs/architecture.md), and how it would be
deployed for real labs is in [docs/production.md](docs/production.md).

## 1. The shape of the system

One API, a Redis queue, a fleet of workers, a model server, a sandbox manager, Postgres.
Pages are the unit of work: a 300-page PDF becomes 300 small jobs, so it spreads over as many
workers as you start, and one bad page fails alone.

Postgres is the source of truth (documents, pages, versions, events, token usage). Redis only
holds "what to do next". If Redis lost everything, `POST /api/documents/{id}/requeue` puts back
every page still marked `queued` (that includes pages waiting out a retry). A page that a worker
was processing at that moment still finishes if the worker lives; if the worker died too, the
page stays `processing` and nothing puts it back yet.

## 2. The queue

A plain Redis list is not enough: a worker that dies after popping a job loses it. So the queue
is four structures changed by Lua scripts (each script is atomic):

- `ready` list: jobs waiting.
- `inflight` sorted set: jobs a worker has taken, scored by when its lease ends.
- `delayed` sorted set: jobs waiting out a retry back-off.
- `dead` list: jobs that failed 3 times.

A worker pops a job and gets a lease (10 minutes by default in the code; the compose file sets 60
minutes, because a CPU model can spend minutes on each of a job's up to three calls). It acks when
done. If it crashes, a reaper (every worker runs one between jobs, at most once a second) finds the
expired lease and puts the job straight back on `ready`, with no back-off; that counts as an
attempt. A job that fails while its worker is alive waits out a back-off first:
`2 s * 2^(n-1)`, capped at 60 s, then multiplied by a random 0.8 to 1.2, so with three attempts the
two waits are about 2 s and 4 s. After the third failure the job goes to `dead` and the page is
marked failed, with the error visible in the review screen; `POST /api/pages/{id}/retry` revives it.

All times come from Redis `TIME`, not from worker clocks, so skewed clocks cannot expire a lease
early.

**Idempotent jobs.** A job can run twice (a worker is slow, its lease expires, another worker
takes it). That is safe because finishing a page is one transaction that only writes if the page
is not already done, and rendered images are written under a unique temp name then renamed.

**Mapping to Azure Container Apps.** The worker already is a stateless container that reads a
queue, so it fits a Container Apps *job* (or an app with a scale rule) scaled by KEDA. The
`redis` KEDA scaler on the `ready` list length (`listName`, `listLength` = pages one replica
should hold) can scale from zero, with one change first: today the reaper runs only inside
workers, so with zero replicas nothing moves due retries from `delayed` back to `ready` or
reclaims expired leases, and the scaler (which watches `ready`) would not wake for them. Keep a
minimum of one replica, or run the reaper elsewhere. Replicas must finish or hand back their job
on SIGTERM; the worker already stops taking new jobs on SIGTERM and finishes the current one, and
a lease covers the case where it is killed anyway. Scale-in is safe for the same reason. The model
server would be a separate GPU workload profile; workers talk to it over the internal network.

## 3. The gateway and the token budget

Every model call goes through `gateway.py`; a test fails if any other module mentions the
chat-completions path. The gateway checks the tenant's budget before the call, makes it, and
writes one `llm_calls` row (tenant, job, purpose, prompt tokens, completion tokens, latency).
`GET /api/tenants/{t}/usage` sums them. Once a tenant has used its budget, the next call is
refused. If that is the extract call, the page fails without burning retries, since retrying
cannot help; if it is the check-code call, the page is saved with its check marked skipped.

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

Trade-off: polling costs two short queries per open stream every 0.25 s (the new events, then
the document's last number and status). Postgres `LISTEN/NOTIFY` would
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

**Warm pool.** A cold start adds a container start to every check: a cold acquire took 157 ms at
p50 on the VM, 83 ms on CI's runner and 93 ms on the Mac (the README has p95s). The manager keeps a few
already-started idle containers, so a request takes one in about 0.01 ms on all three. A whole
check run took 237 ms warm against 433 ms cold at p50 on the VM, 64 against 157 ms on CI and 38
against 127 ms on the Mac. Most of a warm run is the `docker exec` that starts a fresh Python
process (not measured on its own); a runner that is already running and pre-imported in the warm
container would cut most of it. Not done: it needs a protocol between manager and container, and
a long-running process inside is more state to reason about. In the benchmark, which lets every
request of a burst run at once, a burst bigger than the pool spills to cold starts (4 warm and 8
cold out of 12 on the VM and on the Mac). The stack itself runs 4 checks at a time
(`SANDBOX_MAX_CONCURRENT`), so there extra requests also wait up to 15 s for a slot; that does
not avoid cold starts, because the pool refills one container at a time.

**Why the manager is also a risk.** Starting sibling containers needs the Docker API, and
access to the Docker socket is the same as root on the host: whoever can talk to it can start a
privileged container that mounts the host disk. So the one container that holds the socket is
the most valuable target in the system. What is done about it:

- Only the sandbox manager has the socket; a test reads the compose file and fails otherwise.
- The manager has no database password and no data volume. Compromising it gives the host, but
  not the data directly. (The host would give the data, so this only narrows the path.) It does
  sit on `internal`, where Redis and the model server have no password at all, so it could reach
  them; only Postgres is protected by a credential.
- Its HTTP API accepts only `code`, `rows` and `mode`, and rejects any other field. A caller
  cannot choose the image, the network, mounts or limits; those are fixed in the manager.
- The sandbox image is fixed, has no pip and no secrets.
- The manager runs read-only, non-root, with capabilities dropped. (Reaching the socket needs the
  docker group id, passed with `group_add`. On Docker Desktop the socket inside containers is
  `root:root` with mode 660, so that group is 0, root's group.)

What this does *not* fix: a bug in the manager that lets a caller control container creation is
still a host compromise, and a kernel escape from the sandbox container is a host compromise.
Containers share the host kernel; the flags shrink the attack surface, they do not remove it.

**For production**, in order of value:
1. Replace the shared kernel boundary with gVisor (`runsc`; `SANDBOX_RUNTIME=runsc` is passed
   through to Docker, but nothing here has run it) or Firecracker micro-VMs, so hostile code meets a small user-space kernel or a
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
  so on a Linux host even something attached to it cannot reach the internet (tested in CI). On
  Docker Desktop for Mac that does not hold: a container on such a bridge reached `1.1.1.1`, so
  there the API, the one container on `edge`, is not cut off. Only the API joins it, and the port
  is published on `127.0.0.1`.
- `download` is a normal bridge used only by `make models`, a one-off container.

The model server does not need the internet because the weights are in a volume. A test builds an
`internal` network and shows a container on it cannot connect to `1.1.1.1`, `8.8.8.8` or
`example.com` (by name), and that a neighbour on the same network is still reachable (so the
failure means "no route", not "broken network"). Another builds a bridge with masquerading off,
like `edge`, and shows it cannot reach `1.1.1.1` while an ordinary bridge can. That holds on
Linux (CI). On Docker Desktop for Mac the `internal` tests pass, but the masquerade-off bridge
does reach `1.1.1.1`, so the test records an expected failure there.

Another test reads the compose file and fails if anything other than the API publishes a port,
if anything but `models-fetch` is on the download network, if `docker.sock` is in any service's
volumes but the manager's, or if a password is written into the file.

## 7. Reading pages with a small model

- **Strict shape.** The reply must match a JSON schema, sent to llama.cpp as a grammar so the model
  cannot produce other text. The schema sent drops length and range keywords (large `maxLength`
  values make llama.cpp's schema-to-grammar step explode); Pydantic enforces them afterwards.
- **One retry.** If the answer is still invalid (a truncated reply, a bad date), the worker sends
  the bad answer back with the error once. After that the attempt fails and the queue's retry
  rules take over.
- **Dates** must be `YYYY-MM-DD` or null. The prompt says to convert; the validator refuses
  anything else, so "14 Mar" cannot slip in as a string.
- **Every key is required in the grammar.** A value may be null, but the key must be written. With
  only `title` and `narrative` required, llama.cpp's grammar let the real model leave `date` out,
  and it did on every page (Pydantic then filled in null), even on a page dated 2025-02-03.
  Requiring every key makes it write them in page order, and dates came back right on all 20 eval
  pages.
- **PDF size.** Pages are rendered one at a time inside the worker at 150 dpi and capped at 2000
  px on the long side. The API only counts pages at upload, so a 300-page PDF is not rendered in
  the web process.
- **CPU speed sets the limits.** On CPU one page takes minutes (see the README), so the compose
  file gives each model call 15 minutes (`MODEL_TIMEOUT_S`) and a job a 60-minute lease
  (`LEASE_S`). A worker takes no job until the model server reports healthy: compose orders the
  start, and the worker also checks itself, because after a Docker restart the restart policy
  brings every container back at once (seen on the Mac: 16 calls got a 503 before that check
  existed). Pages taken during model load would otherwise burn their attempts in seconds.
  llama.cpp's in-RAM prompt cache is off (`--cache-ram 0`): every page brings a new image, so no
  page reuses another's prompt, and the cache (up to 8 GiB by default) only takes memory; with it
  on, the model server's memory in `docker stats` rose from 7.9 to 8.8 GiB over five pages on the
  Mac, in a 14.7 GB VM. With
  `-np 1` the server answers one request at a time, so more workers do not help on CPU.
- **A check can lie about itself.** The model writes the check and reports its verdict; a wrong
  or fooled check can say "all fine". So a check result is a hint shown next to the fields, not a
  gate, and the stored code is shown to the reviewer. Edits re-run the stored code, not a new
  model call.

## 8. Choices that are simple on purpose

- **Tenant = header.** `X-Tenant` (or `?tenant=` for links and the event stream; `default` when
  neither is sent) separates data. The usage and budget endpoints take the tenant from the path,
  so anyone can read or change any tenant's budget. It is not authentication anyone could rely
  on. In production it comes from a verified token, set by the edge, never by the
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

- The local load test (`python -m bench.load_test`) shows the queue and workers scale when the
  model is the wait: with a fake model that takes 250 ms, 1.98x at 2 workers and 7.14x at 8 on the
  VM, 2.01x and 7.58x on the Mac. With an instant model the VM's 2 CPUs flatten the curve at about
  1.5k pages/min; on the Mac it was still rising at 8 workers (9.4k pages/min). The compose
  version (`make loadtest`) runs real sandbox checks and flattened at about 750 pages/min from 2
  workers on the Mac. None of it applies to the real model on CPU, which answers one request at a
  time.
- Real-model accuracy (Mac, 20 synthetic pages): 100% of 400 fields, after the date fix. The pages
  are synthetic (one handwriting font, one layout), so real notebooks will score lower; the one
  real photo tried got a valid record but has no answer key. The oracle run (100% on any machine)
  checks the scorer and the plumbing, not a model.
- The model-written checks caught all 3 planted problems, but none of the 17 clean pages passed:
  16 got false alarms and on one the check did not run. That is why a check is a hint and not a
  gate.
- The sandbox numbers come from plain `runc` on machines that are themselves virtualised (the VM,
  CI's runner, Docker Desktop's VM on the Mac). The CI and Mac numbers use the real
  `python:3.12-slim` sandbox image; the VM's used a local Python 3.13 image. A bare-metal host, or
  gVisor, will give different times.
