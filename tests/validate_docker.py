#!/usr/bin/env python3
"""Static validator for the Docker assets (no Docker daemon required).

Checks the Dockerfile, .dockerignore, and docker-compose.yml for the structural
requirements RetroShelf needs. Exit 0 on success, non-zero (printing failures)
otherwise. Run: ``.venv/bin/python tests/validate_docker.py``. [SS-08 AC#1]
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def main() -> int:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    check("FROM python:3.12-slim" in dockerfile, "Dockerfile must use FROM python:3.12-slim")
    check("USER 1000" in dockerfile, "Dockerfile must run as non-root USER 1000")
    check("HEALTHCHECK" in dockerfile, "Dockerfile must define a HEALTHCHECK")
    check("/health" in dockerfile, "HEALTHCHECK must hit /health")
    check("EXPOSE 8099" in dockerfile, "Dockerfile must EXPOSE 8099")
    # The app/ COPY may carry flags (e.g. --chown=1000:1000), so match the line
    # by its `app ./app` operands rather than a fixed `COPY app` prefix.
    app_copy = re.search(r"^COPY\b.*\bapp\s+\./app\b", dockerfile, re.MULTILINE)
    check(app_copy is not None, "Dockerfile must COPY app/ into the image")
    check("requirements.txt" in dockerfile and app_copy is not None
          and dockerfile.index("requirements.txt") < app_copy.start(),
          "requirements.txt must be copied before app/ for layer caching")
    check("--no-cache-dir" in dockerfile, "pip install should use --no-cache-dir")

    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for needed in (".venv", "vault", "docs", "tests", ".git", "graphify-out", ".env"):
        check(needed in dockerignore, f".dockerignore must exclude {needed}")

    # docker-compose.yml — parse as YAML if available, else string checks.
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        compose = yaml.safe_load(compose_text)
        services = compose.get("services", {})
        check(bool(services), "compose must define services")
        svc = next(iter(services.values())) if services else {}
        ports = svc.get("ports", [])
        check(any("8099:8099" in str(p) for p in ports), "compose must publish 8099:8099")
        vols = svc.get("volumes", [])
        check(any(":/config" in str(v) for v in vols), "compose must mount /config")
        check(any(":/cache" in str(v) for v in vols), "compose must mount /cache")
        check(svc.get("restart") == "unless-stopped", "compose service should restart unless-stopped")
        env = svc.get("environment", [])
        env_str = "\n".join(env) if isinstance(env, list) else str(env)
        for key in ("KAVITA_BASE_URL", "KAVITA_OPDS_URL", "APP_PORT", "PDF_DISPOSITION",
                    "EPUB_DISPOSITION", "CACHE_FEEDS_SECONDS", "CACHE_BOOKS", "LOG_LEVEL", "TZ"):
            check(key in env_str, f"compose env must include {key}")
        networks = compose.get("networks", {})
        check(any(n.get("external") for n in networks.values() if isinstance(n, dict)),
              "compose must join an external (Kavita) network")
        # Container hardening must not regress.
        check(svc.get("read_only") is True, "compose service must set read_only: true")
        check("ALL" in [str(c) for c in svc.get("cap_drop", [])],
              "compose service must cap_drop: [ALL]")
        check(any("no-new-privileges" in str(o) for o in svc.get("security_opt", [])),
              "compose service must set no-new-privileges")
        check(any("/tmp" in str(t) for t in svc.get("tmpfs", [])),
              "compose service must provide a writable tmpfs /tmp (rootfs is read-only)")
    except ImportError:
        # No PyYAML — fall back to substring checks.
        check("8099:8099" in compose_text, "compose must publish 8099:8099")
        check(":/config" in compose_text and ":/cache" in compose_text, "compose must mount /config and /cache")
        check("external: true" in compose_text, "compose must join an external (Kavita) network")
        for key in ("KAVITA_BASE_URL", "KAVITA_OPDS_URL", "TZ"):
            check(key in compose_text, f"compose env must include {key}")
        check("read_only: true" in compose_text, "compose service must set read_only: true")
        check("no-new-privileges" in compose_text, "compose service must set no-new-privileges")
    check("kavita" in compose_text.lower(), "compose should document joining Kavita's network/service name")

    # portainer-stack.yml — the paste-into-Portainer stack that pulls the GHCR
    # image. It must carry the same container hardening and work zero-config by
    # fronting public OPDS feeds out of the box.
    stack_text = (ROOT / "portainer-stack.yml").read_text(encoding="utf-8")
    check("ghcr.io/" in stack_text, "portainer stack must pull the published GHCR image")
    check("OPDS_FEEDS" in stack_text, "portainer stack must front a default OPDS library set")
    for token in ("read_only: true", "no-new-privileges", "cap_drop"):
        check(token in stack_text, f"portainer stack must keep hardening: {token}")

    if failures:
        print("DOCKER VALIDATION FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("Docker assets OK (Dockerfile, .dockerignore, docker-compose.yml, portainer-stack.yml).")
    print("NOTE: a real `docker build` + `docker compose up` against live Kavita is")
    print("an operator step — no Docker daemon is available in this environment.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
