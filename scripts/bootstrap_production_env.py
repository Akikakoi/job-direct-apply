"""Bootstrap a production env file without printing or committing secrets.

Usage (from repository root):
    .venv/Scripts/python.exe scripts/bootstrap_production_env.py

By default this reads backend/.env as the source for non-provider settings,
copies backend/.env.example as the template, and writes backend/.env.production.
Provider secrets (LLM/OCR/SMTP/IM) are intentionally left blank unless
--copy-provider-secrets is passed, so development credentials are not reused in
production.

The generated file is ignored by git. AUTH_SECRET, UPLOADS_KEY and METRICS_TOKEN
are generated locally and are never written to the terminal.
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
sys.path.insert(0, str(BACKEND))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import Settings  # noqa: E402
from app.services.auth import new_secret  # noqa: E402
from app.services.crypto import new_key  # noqa: E402
from app.services.prodcheck import audit_config  # noqa: E402

ENV_RE = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$")
PROVIDER_SECRET_FIELDS = (
    "LLM_API_KEY",
    "OCR_API_KEY",
    "SMTP_PASSWORD",
    "IM_WEBHOOK_SECRET",
)
GENERATED_SECRET_FIELDS = ("AUTH_SECRET", "UPLOADS_KEY", "METRICS_TOKEN")


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = ENV_RE.match(line)
        if not match:
            continue
        value = match.group(4).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[match.group(2)] = value
    return values


def env_value(value: str) -> str:
    if value == "":
        return ""
    if any(ch in value for ch in (" ", "#", '"')):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def render_env(template: str, values: dict[str, str]) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for line in template.splitlines():
        match = ENV_RE.match(line)
        if match and match.group(2) in values:
            key = match.group(2)
            lines.append(f"{match.group(1)}{key}={env_value(values[key])}")
            seen.add(key)
        else:
            lines.append(line)
    for key, value in values.items():
        if key not in seen:
            lines.append(f"{key}={env_value(value)}")
    return "\n".join(lines).rstrip() + "\n"


def build_values(
    template_text: str,
    source_text: str,
    existing_text: str,
    *,
    copy_provider_secrets: bool,
    rotate_secrets: bool,
    enable_async: bool,
    ua: str | None,
) -> dict[str, str]:
    values = parse_env(template_text)
    values.update(parse_env(source_text))
    existing = parse_env(existing_text)

    if not copy_provider_secrets:
        for field in PROVIDER_SECRET_FIELDS:
            # Re-running the bootstrap updates configuration without wiping provider
            # secrets already placed in the ignored production file.
            values[field] = existing.get(field, "")

    generated = {
        "AUTH_SECRET": new_secret,
        "UPLOADS_KEY": new_key,
        "METRICS_TOKEN": lambda: secrets.token_urlsafe(32),
    }
    for field, generator in generated.items():
        keep_existing = bool(existing.get(field)) and not rotate_secrets
        values[field] = existing[field] if keep_existing else generator()

    values["AUTH_REQUIRED"] = "true"
    values["LOG_JSON"] = "true"
    values["METRICS_ENABLED"] = "true"
    values["SENTRY_ENVIRONMENT"] = "production"
    values["UA"] = ua or values.get("UA") or "job-direct-apply-bot/1.0 (+https://github.com/Akikakoi/job-direct-apply)"
    if "dev@example.com" in values["UA"]:
        values["UA"] = "job-direct-apply-bot/1.0 (+https://github.com/Akikakoi/job-direct-apply)"
    if enable_async:
        values["RESUME_PARSE_ASYNC"] = "true"
        values["MATCH_ASYNC"] = "true"
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate backend/.env.production")
    parser.add_argument("--source", type=Path, default=BACKEND / ".env")
    parser.add_argument("--template", type=Path, default=BACKEND / ".env.example")
    parser.add_argument("--output", type=Path, default=BACKEND / ".env.production")
    parser.add_argument("--force", action="store_true", help="overwrite an existing production file")
    parser.add_argument(
        "--rotate-secrets",
        action="store_true",
        help="regenerate AUTH_SECRET/UPLOADS_KEY/METRICS_TOKEN (requires --force)",
    )
    parser.add_argument(
        "--copy-provider-secrets",
        action="store_true",
        help="copy LLM/OCR/SMTP/IM secrets from the source env (not recommended for dev-to-prod)",
    )
    parser.add_argument(
        "--enable-async",
        action="store_true",
        help="set RESUME_PARSE_ASYNC/MATCH_ASYNC=true (only when Celery workers are deployed)",
    )
    parser.add_argument("--ua", default=None, help="crawler User-Agent; add a real contact URL/email")
    args = parser.parse_args()

    source = args.source if args.source.exists() else args.template
    if not args.template.exists():
        print(f"template not found: {args.template}", file=sys.stderr)
        return 2
    if not source.exists():
        print(f"source not found: {source}", file=sys.stderr)
        return 2
    if args.output.exists() and not args.force:
        print(f"{args.output} already exists; use --force to update it", file=sys.stderr)
        return 2
    if args.rotate_secrets and not args.force:
        print("--rotate-secrets requires --force", file=sys.stderr)
        return 2

    template_text = args.template.read_text(encoding="utf-8")
    source_text = source.read_text(encoding="utf-8")
    existing_text = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
    values = build_values(
        template_text,
        source_text,
        existing_text,
        copy_provider_secrets=args.copy_provider_secrets,
        rotate_secrets=args.rotate_secrets,
        enable_async=args.enable_async,
        ua=args.ua,
    )
    args.output.write_text(render_env(template_text, values), encoding="utf-8", newline="\n")

    prod_settings = Settings(_env_file=args.output)
    report = audit_config(prod_settings, env_tracked=False, env_example_text=template_text)
    counts = report["counts"]
    print(f"wrote {args.output}")
    print("generated locally (values hidden): " + ", ".join(GENERATED_SECRET_FIELDS))
    configured_provider = [field for field in PROVIDER_SECRET_FIELDS if values.get(field)]
    missing_provider = [field for field in PROVIDER_SECRET_FIELDS if not values.get(field)]
    if configured_provider:
        print("provider secrets configured: " + ", ".join(configured_provider))
    if missing_provider:
        print("provider secrets left blank: " + ", ".join(missing_provider))
    print(f"production audit: {report['status']} (fail={counts['fail']}, warn={counts['warn']})")
    for check in report["checks"]:
        print(f"  [{check['level']}] {check['message']}")
    profile = values.get("DEPLOYMENT_PROFILE", "local").lower()
    if profile == "hosted" and (
        "localhost" in values.get("DATABASE_URL", "") or "localhost" in values.get("REDIS_URL", "")
    ):
        print("manual action: replace localhost DATABASE_URL/REDIS_URL with hosted endpoints")
    elif profile == "local":
        print("deployment profile: local; localhost endpoints are expected")
    if values.get("RESUME_PARSE_ASYNC") != "true":
        print("manual action: enable async only after Celery worker/beat are deployed")
    return 1 if counts["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())