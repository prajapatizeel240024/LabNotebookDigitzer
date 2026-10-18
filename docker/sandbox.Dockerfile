# The image every sandbox container starts from. It holds Python and nothing else:
# no pip, no extra packages, no secrets, no application code. Code arrives at run time
# through `docker exec` arguments, and the container has no network and a read-only root.
FROM python:3.12-slim

RUN pip uninstall -y pip setuptools wheel >/dev/null 2>&1 || true \
 && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin sandbox

USER 10001:10001
WORKDIR /tmp
CMD ["python3", "-I", "-c", "import signal; signal.pause()"]
