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
  All of this is on one Docker network with no route out.            │ no network,     │
  Only the API is also published, on 127.0.0.1.                      │ read-only root  │
                                                                     └─────────────────┘
```

## What it does

1. **Upload** a photo or a PDF. PDF pages are rendered with PyMuPDF, one job per page.
2. **Read**: a worker sends the page image to a local Qwen2.5-VL model (llama.cpp server) and gets
   back JSON in a fixed shape: title, date, narrative, a reagent table (name, amount, unit,
   molar mass, equivalents) and notes. The JSON is validated with Pydantic. If it is invalid the
   worker asks once more, then fails the page.
3. **Check**: for each reagent table the model also writes a small Python function that looks for
   inconsistencies (do the equivalents match the masses and molar masses?). That code is
   untrusted. It only ever runs in a sandbox container.
4. **Review** in the browser: page image next to the fields, edit anything, every save is a new
   version with history. Export the whole document as JSON or CSV.

## Run it

You need Docker with Compose 2.24 or newer. Everything binds to `127.0.0.1`.

```bash
make env          # writes .env with a random database password (git-ignored)
make models       # one-time download of the model weights (about 5.5 GB, needs internet)
make up           # build and start everything
# then open http://127.0.0.1:8000
make scale N=4    # more workers
```

No model download? `make up-fake` swaps in a fake model server that returns a canned page
instantly. It is how the load test and CI run.

Run the tests:

```bash
make install
make test-deps     # throwaway Postgres and Redis on 127.0.0.1:55432 / 56379
make test          # fast tests, no Docker needed beyond the two services above
make test-docker   # sandbox escape tests and the egress test (needs a Docker daemon)
make lint
```

Other targets: `make loadtest`, `make sandbox-bench`, `make eval-pages`, `make eval`
(see `make help`).

### The model

| | |
|---|---|
| Model | Qwen2.5-VL-7B-Instruct, Q4_K_M quantisation |
| Files | `Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf` (about 4.7 GB) and `mmproj-Qwen2.5-VL-7B-Instruct-Q8_0.gguf` (about 850 MB, the vision part) |
| Source | https://huggingface.co/ggml-org/Qwen2.5-VL-7B-Instruct-GGUF |
| License | Apache-2.0 (as stated on that page when this was written) |

`make models` downloads both files into a Docker volume, checks them against the SHA-256 that
Hugging Face reports, and prints the hash. Paste the hashes into `src/lnd/models.json` if you
want them pinned. This is the only step that needs the internet; it runs in its own container
on its own network. A 7B model at 4-bit fits in about 8 GB of memory. To try another GGUF, change the file names
in `docker-compose.yml` and `models.json`.

## Results

All numbers below were measured on one small cloud VM: **2 vCPUs, 8 GB RAM, no GPU**,
Python 3.13, Docker 29.8. Read them as "what this code does on a tiny box", not as a
benchmark of a real deployment. Anything marked *not measured* could not be run here.

| What | Result |
|---|---|
| Extraction accuracy, real model | **not measured** (no GPU or model download here). Run `make models && make up && make eval`. |
| Scoring harness, with an oracle fake model | 100.0% of fields on 20 pages (this tests the scorer and the plumbing, not a model) |
| Sandbox escape tests passed | **15 of 15** tests passed: every attack failed as required (list below) |
| Warm sandbox start | acquire p50 0.01 ms, p95 0.04 ms; whole check run p50 237 ms, p95 282 ms |
| Cold sandbox start | acquire p50 157 ms, p95 184 ms; whole check run p50 433 ms, p95 495 ms |
| Burst of 12 requests vs a pool of 4 | 4 served warm, 8 cold, all done in 2.5 s |
| Egress test | a container on an `internal` network cannot reach `1.1.1.1`, `8.8.8.8` or a DNS name; a control container on the same network can reach its neighbour |
| SSE resume | API killed with SIGKILL mid-job (40 pages); reconnect with `Last-Event-ID` gets event ids 1..N exactly once |

### Pages per minute by worker count

300-page synthetic PDF, one job per page, fake model server, local processes
(`python -m bench.load_test`). Page rendering, Postgres writes and the fake model all share the
same 2 CPUs with the workers.

| Workers | Instant model: pages/min | Instant model: time | Model takes 250 ms: pages/min | Speed-up |
|---:|---:|---:|---:|---:|
| 1 | 789 | 22.8 s | 104 | 1.00x |
| 2 | 1306 | 13.8 s | 205 | 1.98x |
| 4 | 1448 | 12.4 s | 402 | 3.87x |
| 8 | 1489 | 12.1 s | 741 | 7.14x |

What this says: with an instant model, the 2 CPUs are the limit, so adding workers past 2 does
little. With a model that takes 250 ms per call (closer to waiting on a GPU), the workers mostly
wait, and the line is nearly straight up to 8 workers. The 250 ms rows used 120 pages, not 300,
to keep the run short. The compose version of this test is `make loadtest`; it was not run
here because Docker could not pull images.

### Sandbox escape tests

Each one runs hostile code in a real sandbox container and must fail: network call (IP and DNS),
write outside `/tmp`, fill the tiny `/tmp`, read environment variables and host files, fork
bomb, infinite loop, endless output, one giant print, memory bomb. Then it inspects the running
container and checks the flags below, and that a container is used once and replaced.

The sandbox container has: no network (`network_mode: none`), read-only root, 16 MB `noexec`
tmpfs, user 10001, all capabilities dropped, `no-new-privileges`, 64 processes, 128 MB memory
(no swap), half a CPU, a 5 s wall-clock timeout enforced by the manager, and a 64 KB output cap.

What was not verified: these ran against a local Python 3.13 root image, not the
`python:3.12-slim` image the Dockerfile builds. CI builds the real image and runs the same tests.

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
```

## Accuracy test

`evaluation/facts.json` is the answer key for 20 made-up pages. `make eval-pages` draws them
with the Caveat handwriting font (SIL Open Font License, in `evaluation/fonts/`): each word is
placed slightly off the line, the page is rotated up to 1.5 degrees, blurred, tinted and
sprinkled with noise. `make eval` uploads them through the API and scores every field (title,
date, narrative, notes, row count, and each cell of each reagent row). Three pages have a planted
mistake in the numbers so the sandbox check has something to find. To add real pages, put the
images in `evaluation/pages/` and add entries to `facts.json` with the same fields.

## Known limits

- The tenant is a header (`X-Tenant`), not a login. It separates data and token budgets, it is
  not authentication. See DESIGN.md.
- Token budgets are checked before a call, so a few in-flight calls can overshoot a little.
- The review screen was only syntax-checked, never opened in a real browser here.
- `docker compose up` for the whole stack and the image builds could not run in the build
  environment (no access to image registries). `docker compose config` passes; CI builds the images.
- Tested on Python 3.13 locally; the project targets 3.12 and CI uses 3.12.

See [DESIGN.md](DESIGN.md) for the decisions and what would change in production.
