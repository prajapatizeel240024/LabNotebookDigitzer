# Lab Notebook Digitizer

Turn photos of lab-notebook pages, and long scanned PDFs, into structured records that a person
can check and fix. It runs on your own machine. The vision model runs locally, the containers
that touch your data have no internet, and any code the model writes runs only inside a
locked-down sandbox.

```
 browser ──► 127.0.0.1:8000
               │
        ┌──────▼──────┐   enqueue    ┌───────┐  pop (lease)  ┌──────────────┐
        │     API     │─────────────►│ Redis │◄──────────────│ worker  × N  │
        │ (FastAPI)   │              │ queue │               │ (scale me)   │
        │ SSE, export │◄──┐          └───────┘               └──┬────────┬──┘
        └──────┬──────┘   │ events                              │        │
               │          │                       one gateway   │        │ "run this code
               ▼          │                       (metering)    ▼        ▼  on these rows"
        ┌─────────────┐   │                     ┌──────────────────┐  ┌─────────────────┐
        │  Postgres   │◄──┴─────────────────────│ model server     │  │ sandbox manager │
        │ pages,      │                         │ llama.cpp +      │  │ (only container │
        │ versions,   │                         │ Qwen2.5-VL GGUF  │  │ with docker.sock)│
        │ events,     │                         └──────────────────┘  └───────┬─────────┘
        │ llm_calls   │                                                       │ starts, once each
        └─────────────┘                                              ┌────────▼────────┐
                                                                     │ sandbox × pool  │
  All of this is on an internal Docker network with no route out.    │ no network,     │
  The API also joins an edge network only so its port can be         │ read-only root  │
  published on 127.0.0.1 (host port API_PORT, default 8000).         │                 │
                                                                     └─────────────────┘
```

## What it does

1. **Upload** a photo or a PDF. PDF pages are rendered with PyMuPDF, one job per page.
2. **Read**: a worker sends the page image to a local Qwen2.5-VL model (llama.cpp server) and gets
   back JSON in a fixed shape: title, date, narrative, a reagent table (name, amount, unit,
   molar mass, equivalents) and notes. The JSON is validated with Pydantic. If it is invalid the
   worker shows the model its answer and the error and asks once more; if that fails too, the
   attempt fails and the queue retries the page later, up to three attempts in all.
3. **Check**: for each reagent table the model also writes a small Python function that looks for
   inconsistencies (do the equivalents match the masses and molar masses?). That code is
   untrusted. It only ever runs in a sandbox container.
4. **Review** in the browser: page image next to the fields, edit anything, every save is a new
   version with history. Export the whole document as JSON or CSV.

## Run it

You need Docker with Compose 2.24 or newer. Everything binds to `127.0.0.1`.

```bash
make env          # writes .env with a random database password (git-ignored)
make models       # one-time download of the model weights (5.5 GB, needs internet)
make up           # build and start everything
# then open http://127.0.0.1:8000   (if port 8000 is taken: API_PORT=8001 make up)
make scale N=4    # more workers
```

On CPU the model server answers one request at a time (`-np 1`) and a page takes minutes, so
more workers do not make it faster; `make scale` pays off with a model server that serves several
requests at once. No model download? `make up-fake` swaps in a fake model server that returns a
canned page instantly; `make loadtest` uses it, and the test suite runs the same fake in-process.

Run the tests:

```bash
python3.12 -m venv .venv   # make uses .venv/bin/python when it exists, else python3
make install
make test-deps     # throwaway Postgres and Redis on 127.0.0.1:55432 / 56379
make test          # fast tests, no Docker needed beyond the two services above
make test-docker   # sandbox escape tests and the egress tests (needs a Docker daemon)
make lint
```

Stop the stack (`make down`) before `make test-docker` or `make sandbox-bench`: both start their
own sandbox pool, which first removes every sandbox container on the daemon (label
`lnd.sandbox`), including a running stack's warm pool, and two tests count those containers.

Other targets: `make loadtest`, `make sandbox-bench`, `make eval-pages`, `make eval`
(see `make help`).

### The model

| | |
|---|---|
| Model | Qwen2.5-VL-7B-Instruct, Q4_K_M quantisation |
| Weights | `Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf`, 4,683,072,032 bytes, SHA-256 `9258bf05b12686d097ff3b6b18d968ab393649780aa2b3cd67fec43d50554392` |
| Vision part | `mmproj-Qwen2.5-VL-7B-Instruct-Q8_0.gguf`, 853,119,712 bytes, SHA-256 `2ddb555391bae966e412deab9e07b58afa18bcc06930ba0f1c78a3695ab9e506` |
| Source | https://huggingface.co/ggml-org/Qwen2.5-VL-7B-Instruct-GGUF at revision `508edd0afaa66bb9e9f40587acc2184f02daf1f6` |
| License | Apache-2.0 (as stated on that page when this was written) |
| Server | llama.cpp `llama-server` b11718 (commit 547208dca), image pinned by digest in `docker-compose.yml` |

`make models` downloads both files from that revision into a Docker volume and refuses a file
whose size or SHA-256 differs from `src/lnd/models.json` (the pins were taken from the files it
downloaded on 2026-10-18, hashed again afterwards). Apart from pulling and building the images it
is the only step that needs the internet, and it runs in its own container on its own network.
To try another GGUF, change the file names in `docker-compose.yml`, `models.json` and
`tests/test_fetch_models.py`, and the size and SHA-256 pins.

## Results

Every number below names the machine it was measured on:

| Name | Machine | Software |
|---|---|---|
| **VM** | small cloud VM: 2 vCPUs, 8 GB RAM, no GPU. The original build machine. It could not pull Docker images, so nothing that needs the stack ran there. | Python 3.13, Docker 29.8 |
| **Mac** | MacBook Air, Apple M5 (4 performance and 6 efficiency cores), 32 GB RAM, fanless | macOS 26.6.2; Docker Desktop 29.8.2 running a Linux VM with 8 vCPUs and 14.7 GB; no GPU inside containers, so llama.cpp b11718 runs on the CPU; app, sandbox and test images on `python:3.12-slim` (Python 3.12.15, Debian 13) |
| **CI** | GitHub Actions `ubuntu-24.04` runner, 4 vCPUs, x86-64 | Python 3.12.15; `.github/workflows/ci.yml` |

About the Mac: it ran on battery for part of the work and other apps were open. A fanless laptop
slows down under sustained load: llama.cpp's prompt processing went from about 28 tokens/s on a
cool machine to about 20, and generation from about 15 to about 9. Where that matters, both
numbers are given.

| What | VM | Mac | CI |
|---|---|---|---|
| Full stack with the real model (`make models`, `make up`) | could not run | runs, after the fixes listed below | images build; compose files valid |
| Extraction accuracy, real model, 20 synthetic pages (`make eval`) | not measured | 100.0% of 400 fields; 20 of 20 pages fully correct (table below) | not run |
| One page, real model on CPU | not measured | 131.5 s on a cool machine; 181 s after a few minutes of load | not run |
| A real camera photo of a notebook page | not run | a valid record; the extract call took 216 s | not run |
| A 10-page scanned PDF | not run | 10 of 10 pages came back as valid records from the real model | not run |
| Review screen in a real browser | syntax check only | headless Chromium: every step passes after three fixes (below), plus a fourth found by reading the code | not run |
| Oracle run: a fake model that returns the answer key (tests the scorer and the plumbing, not a model) | 100.0% of 400 fields | 100.0% | 100.0% |
| Full test suite | not stated | 135 passed, 1 skipped, 1 expected failure, on `python:3.12-slim` with the real sandbox image (the skip: a pixel-reproduction test that passes on x86-64 but not on arm64; the expected failure: next row but one) | all pass: the fast tests and the 21 Docker tests |
| Sandbox tests (15) | 15 of 15, on a local Python 3.13 image | 15 of 15, on the real `python:3.12-slim` sandbox image | 15 of 15, on the real image |
| Egress tests (6) | passed | 5 of 6. The sixth fails as expected on Docker Desktop: a bridge with masquerading off, like `edge`, still reached the internet there, so on a Mac the API container is not cut off from the internet. The containers that touch data are on `internal`, which did block it | 6 of 6 |
| Warm sandbox: acquire p50 / p95; whole check p50 / p95 | 0.01 / 0.04 ms; 237 / 282 ms | 0.01 / 0.02 ms; 38 / 44 ms | 0.01 / 0.02 ms; 64 / 71 ms |
| Cold sandbox: acquire p50 / p95; whole check p50 / p95 | 157 / 184 ms; 433 / 495 ms | 93 / 106 ms; 127 / 144 ms | 83 / 90 ms; 157 / 183 ms |
| Burst against a pool of 4, in the benchmark (which lets every request of the burst run at once; the stack allows 4 at a time) | 12 requests: 4 warm, 8 cold, 2.5 s | 12 requests: 4 warm, 8 cold, 0.41 s | 8 requests: 4 warm, 4 cold, 0.53 s |
| SSE resume: API killed with SIGKILL mid-job (40 pages), reconnect gets ids 1..N exactly once | passed | passed | passed |

### Accuracy with the real model (Mac)

`make eval` on the Mac: 20 synthetic pages, Qwen2.5-VL-7B Q4_K_M on CPU, one page at a time.
The run was interrupted when Docker Desktop was quit at page 11; after a restart the other pages
finished from the queue, and all 20 were scored with the same code (`evaluation/score.py`). Right
after that restart, 16 model calls got a 503 because Docker's restart policy brought the worker
back while the model was still loading (compose's start order only applies to `make up`); the
queue retried them and no page failed, but one page was on its last attempt. The pages are
synthetic (one handwriting font, one layout, light blur and noise), so treat this as an
upper bound: real notebooks will score lower. The one real photo tried has no answer key.

| Field | Cells | Accuracy |
|---|---:|---:|
| title | 20 | 100.0% |
| date | 20 | 100.0% |
| narrative (90% character similarity) | 20 | 100.0% |
| notes (90% character similarity) | 20 | 100.0% |
| reagent count | 20 | 100.0% |
| reagent name | 60 | 100.0% |
| reagent amount (within 1%) | 60 | 100.0% |
| reagent unit | 60 | 100.0% |
| reagent molar mass (within 1%) | 60 | 100.0% |
| reagent equivalents (within 1%) | 60 | 100.0% |
| **all fields** | **400** | **100.0%** (20 of 20 pages fully correct) |

Before the date fix (see "What was fixed"), dates were never filled in: 0 of 3 in the first,
stopped run of `make eval` (whose other 62 fields were all right) and 0 of 10 on the 10-page PDF.
The model left the `date` key out, and Pydantic filled in null.

The model also writes the consistency check for each reagent table. Those checks are weak: in the
same run they caught all 3 planted problems, but also raised false alarms on 16 of the 17 clean
pages, and on one page the check did not run. In the 10-page PDF run, one generated check crashed
with a `NameError` in its own code; the sandbox reported it and the page was still saved. This is
why a check is shown as a hint next to the fields, not used as a gate (DESIGN.md section 7).

### Time per page (Mac, CPU)

| Run | Prompt tokens (image + text) | Extract call | Check-code call | Whole page |
|---|---:|---:|---:|---:|
| page01 (1100 x 1500), cool machine | 2,292 | 99.8 s | 30.7 s | 131.5 s |
| page01 again, after minutes of full load | 2,292 | 135.9 s | 44.7 s | 181.1 s |
| real photo, 5184 x 3456 downscaled to 2000 x 1333 | 3,594 | 216.4 s | no reagent table | 216 s of model time |
| 20 eval pages, back to back | 2,292 each | median 143 s (100-250 s) | median 40 s | median 181 s (134-321 s) |

On a cool machine llama.cpp read the prompt at 28.5 tokens/s and generated at 14.6 tokens/s; after
a few minutes of full load the fanless Air settled at about 20 and 9. So on this Mac a page takes
two to three minutes: about 20 to 27 pages an hour, and a 300-page PDF is an overnight job. GPU
serving is the fix for speed (docs/production.md); the model was not changed.

### Review screen (Mac, headless Chromium)

Driven with Playwright 1.64 against the real stack and the real model: open the screen, switch
tenant, upload a page, see the page image load, wait for the model, see the fields fill in by
themselves, edit the title and a reagent amount, save (version 2, with editor and note), see the
history (v2 and v1), load v1 back into the form, export JSON and CSV (both carry version 2 and the
edits), open a 10-page PDF and pick a page, fail a page on purpose (token budget 0), retry it and
see it finish by itself. All steps pass, with no errors in the browser console. The screenshot is
`docs/review-screen.png`.

### Pages per minute by worker count

A 300-page synthetic PDF, one job per page, with the fake model server (instant, or 250 ms per
call) and the sandbox stub, all as local processes (`python -m bench.load_test`). This measures
everything around the model: page rendering, the queue, Postgres writes, the gateway. It says
nothing about real model speed: with the real model on CPU, one model server serves one request at
a time, so extra workers wait. The 250 ms rows used 120 pages to keep the run short.

VM (2 vCPUs, all processes on the VM):

| Workers | Instant model: pages/min | Instant model: time | Model takes 250 ms: pages/min | Speed-up at 250 ms |
|---:|---:|---:|---:|---:|
| 1 | 789 | 22.8 s | 104 | 1.00x |
| 2 | 1306 | 13.8 s | 205 | 1.98x |
| 4 | 1448 | 12.4 s | 402 | 3.87x |
| 8 | 1489 | 12.1 s | 741 | 7.14x |

Mac (inside Docker Desktop's Linux VM, 8 vCPUs, `python:3.12-slim`, with the test Postgres and
Redis on the same Docker network; no page failed):

| Workers | Instant model: pages/min | Instant model: time | Model takes 250 ms: pages/min | Speed-up at 250 ms |
|---:|---:|---:|---:|---:|
| 1 | 1986 | 9.07 s | 107 | 1.00x |
| 2 | 3735 | 4.82 s | 215 | 2.01x |
| 4 | 6596 | 2.73 s | 432 | 4.04x |
| 8 | 9398 | 1.92 s | 810 | 7.58x |

What this says: with an instant model the CPUs are the limit (2 on the VM, 8 here), and adding
workers helps until they run out. With a model that takes 250 ms per call, the workers mostly wait,
and the line is nearly straight up to 8 workers on both machines.

`make loadtest` on the Mac: the whole compose stack in Docker Desktop's VM, scaled to 1, 2, 4 and
8 workers, with the fake model (instant) and real sandbox checks through the sandbox manager:

| Workers | Pages/min | Time for 300 pages |
|---:|---:|---:|
| 1 | 580 | 31.0 s |
| 2 | 787 | 22.9 s |
| 4 | 716 | 25.2 s |
| 8 | 745 | 24.2 s |

It flattens at about 750 pages/min from 2 workers on. Here every page also runs its check in a real
sandbox container, and the manager runs 4 at a time while it replaces each used container, so the
sandbox, not the queue, sets the ceiling once the model is fast.

### Sandbox tests

Each one runs hostile code in a real sandbox container and must fail: network calls (IP and DNS),
writes outside `/tmp`, filling the tiny `/tmp`, reading environment variables and host files, a
fork bomb, an endless loop, endless output, one giant print, a memory bomb, and becoming root.
Others check that bad code is just an error, the container's flags, that a container is used once
and replaced, the warm pool, parallel requests, and that the manager accepts only code and rows.

The sandbox container has: no network (`network_mode: none`), a read-only root, a 16 MB `noexec`
tmpfs, user 10001, all capabilities dropped, `no-new-privileges`, 64 processes, 128 MB memory (no
swap), half a CPU, a 5 s wall-clock timeout enforced by the manager, and a 64 KB output cap.

One test that reads host files proves something only when pytest runs on the Docker host itself,
as in CI; on the Mac the tests ran inside a container.

### What was fixed while getting it to run

| Problem found | Fix | How it was checked |
|---|---|---|
| `make models` failed: `PermissionError` writing `/models` (the new volume was owned by root; the downloader runs as uid 10001) | the app image creates `/models` owned by the app user, like `/data` | `make models` then downloaded and verified both files |
| `make models` kept using a stale image after a Dockerfile change | it rebuilds the image first (`run --build`) | the same run |
| `make up` could not start the API: port 8000 was taken on the Mac | host port is `API_PORT` (default 8000) | the stack ran on `API_PORT=8001` |
| Model files and server image were not pinned | `models.json` pins the Hugging Face revision, exact sizes and SHA-256, and the fetcher refuses anything else; the llama.cpp image is pinned by digest | hashes recomputed from the downloaded files; tests |
| **Dates were never extracted**: the schema sent to llama.cpp required only title and narrative, and the model skipped the optional `date` key on every page, even one dated 2025-02-03 | every key is required in the schema sent to the model (values can still be null), so it writes them in page order | the same request with both schemas, on the real model; then the eval above |
| The scorer gave a missing page credit where the truth is empty (page13 has no date) | a missing page now scores every field wrong, as documented | new test |
| llama.cpp's in-RAM prompt cache grew the model server about 0.2 GiB a page | `--cache-ram 0` | anonymous memory stayed at 6.3-6.6 GiB over the 20-page eval |
| Workers started before the model had loaded; pages taken then fail all attempts in seconds | workers wait for the model server's health check | start order in `make up` |
| One model call can take minutes on CPU | compose sets `MODEL_TIMEOUT_S=900` and `LEASE_S=3600` (overridable in `.env`) | container environment |
| `make eval` gave 20 CPU pages 3600 s and threw away every result on a timeout | `EVAL_TIMEOUT` (default 7200 s); unfinished pages are scored as failed and listed | new test |
| Review screen: the form never appeared when the page finished; it needed a click | reload the page on screen when its status changes | headless Chromium: failed before the fix, passes after |
| Review screen: `.review { display: grid }` beat the `hidden` attribute, so stale fields stayed visible | a global `[hidden] { display: none !important }` | failed before, passes after |
| Review screen: after Retry the page never updated (the event stream had closed at `document_done`) | resume the stream with `?after=<last id>` | passes in headless Chromium on the real stack (not run before the fix) |
| Review screen: a late answer could put one page's fields next to another page's image, and Save would write them to the wrong page | ignore answers for a page that is no longer selected | code review only (a race) |
| A sandbox test failed on the real `python:3.12-slim` image: it took Debian's empty `/mnt` for a host mount | only a mount point or something inside counts | CI and the Mac, on the real image |
| The escape tests needed the docker CLI | they use the Docker SDK | the `python:3.12-slim` run |
| `make test-docker` failed without `.env` | it runs `make env` first | |
| GPU compose override that nothing here could test | removed | |

### Not verified

- GPU serving. Nothing here has a GPU; the override is gone. Production notes are in
  [docs/production.md](docs/production.md).
- The stack with the real model on x86-64 (CI builds the x86-64 images and runs the tests, but the
  model only ran on the Mac's arm64).
- Accuracy on real handwriting. The eval pages are synthetic; the real photo got a valid record,
  but there is no answer key for it.
- More than one worker with the real model (with `-np 1` they would queue at the model server).
- gVisor (`SANDBOX_RUNTIME=runsc`): not available on Docker Desktop, not tested anywhere.
- Browsers other than Chromium.
- The review screen's guard against late answers (a race; reviewed, not reproduced).
- `make env` on Linux: only the macOS fallback (`DOCKER_GID=0`, which is right for Docker Desktop)
  ran here.
- Everything in docs/production.md, which is a design proposal.

## Where things are

```
src/lnd/api.py            HTTP API, SSE, exports
src/lnd/queue.py          Redis queue: leases, backoff, dead-letter list (Lua scripts)
src/lnd/worker.py         worker loop        pipeline.py   one page, start to finish
src/lnd/gateway.py        the only place that talks to the model; counts tokens
src/lnd/extraction.py     prompt, JSON schema, validate, retry once
src/lnd/checks.py         ask the model for a check, run it in the sandbox
src/lnd/sandbox/          sandbox manager (the one service with the Docker socket)
src/lnd/events.py         per-document event log, SSE format
src/lnd/fakes/            fake model server and sandbox stub
bench/                    load test and sandbox benchmark
evaluation/               answer key (facts.json), page renderer, scorer
tests/                    pytest
docs/                     architecture.md (how it works), production.md (how it would be deployed),
                          review-screen.png
```

## Accuracy test

`evaluation/facts.json` is the answer key for 20 made-up pages. `make eval-pages` draws them
with the Caveat handwriting font (SIL Open Font License, in `evaluation/fonts/`): each word is
placed slightly off the line, the page is rotated up to 1.5 degrees, blurred, tinted and
sprinkled with noise. `make eval` uploads them through the API and scores every field (title,
date, narrative, notes, row count, and each cell of each reagent row). Three pages have a planted
mistake in the numbers so the sandbox check has something to find. Scoring rules: text fields
must match after normalising case, spacing and punctuation; narrative and notes need 90 percent
character similarity; numbers must be within 1 percent; reagent rows are compared in the order
written. `facts.json` and `pages/` are generated together by `render_pages.py` (re-rendering
replaces the pages) and the tests check them against each other, so the harness cannot score
real pages yet: that needs a separate answer key and page directory, which `run_eval.py` does
not take today.

## Known limits

- The tenant is a header (`X-Tenant`), not a login. It separates data and token budgets, it is
  not authentication. See DESIGN.md.
- Token budgets are checked before a call, so a few in-flight calls can overshoot a little.
- On CPU the model is slow: about two minutes a page at best on the Mac, one page at a time.
  A 300-page PDF is an overnight job there.
- Edits have no conflict check: two people saving the same page at once both get a new version,
  and the later one is current.
- On Docker Desktop for Mac the API container can reach the internet: there, a bridge with
  masquerading off (`edge`) is not sealed (measured). On a Linux host it is (CI). The containers
  that touch data are on `internal` and blocked on both.
- After a Docker or host restart, Docker's restart policy can bring workers back before the model
  has loaded; their first calls get a 503 and use up attempts (seen once here: no page was lost,
  one was on its last attempt). A worker that waits for the model's health check before its first
  job would fix it.
- Stopping or recreating a worker in the middle of a page (`make down`, or `make up` after a
  rebuild) kills it after Docker's 10 s grace period, since a page takes minutes on CPU. Its job
  then waits out the lease (60 minutes in compose) before another worker takes it, as one used
  attempt. Stop the stack when the queue is empty (`GET /api/queue`).
- The model server container runs as the llama.cpp image's default user (root) with a writable
  root file system; the other containers that touch data are read-only and non-root.

See [DESIGN.md](DESIGN.md) for the decisions, [docs/architecture.md](docs/architecture.md) for
how the system works, and [docs/production.md](docs/production.md) for running it for real.
