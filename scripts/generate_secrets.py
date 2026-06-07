#!/usr/bin/env python3
"""
generate_secrets.py

Reads .env.example and generates cryptographically secure values for all
secret placeholders, then writes the result to .env.

Usage:
    python scripts/generate_secrets.py              # generates .env from .env.example
    python scripts/generate_secrets.py --force      # overwrite existing .env
    python scripts/generate_secrets.py --output .env.generated  # write to custom path
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import string
import sys
import time


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def hex_secret(length: int = 32) -> str:
    """Return a random hex string (2*length hex chars)."""
    return secrets.token_hex(length)


def alphanum_secret(length: int = 32) -> str:
    """Return a random alphanumeric string (safe for passwords without encoding)."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def base64url_encode(data: bytes) -> str:
    """Base64url-encode without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_jwt(payload: dict, secret: str) -> str:
    """Generate a HS256 JWT token using only the standard library."""
    header = {"alg": "HS256", "typ": "JWT"}
    segments = []
    for part in (header, payload):
        segments.append(base64url_encode(json.dumps(part, separators=(",", ":")).encode()))

    signing_input = f"{segments[0]}.{segments[1]}".encode()
    signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    segments.append(base64url_encode(signature))
    return ".".join(segments)


def generate_supabase_keys(jwt_secret: str) -> tuple[str, str]:
    """Return (anon_key, service_role_key) JWTs valid until 2040."""
    iat = 1641769200  # 2022-01-10T00:00:00Z (matches Supabase demo convention)
    exp = 2224569600  # 2040-07-01T00:00:00Z

    anon_payload = {
        "role": "anon",
        "iss": "supabase",
        "iat": iat,
        "exp": exp,
    }
    service_payload = {
        "role": "service_role",
        "iss": "supabase",
        "iat": iat,
        "exp": exp,
    }
    return generate_jwt(anon_payload, jwt_secret), generate_jwt(service_payload, jwt_secret)


# ---------------------------------------------------------------------------
# Secret definitions
# ---------------------------------------------------------------------------
# Maps env variable name -> generator callable (no args).
# Order doesn't matter; they are applied to the template by name.

def build_secret_generators() -> dict:
    """Build and return secret generators. Some depend on each other (JWT)."""
    jwt_secret = hex_secret(32)
    anon_key, service_role_key = generate_supabase_keys(jwt_secret)

    postgres_password = alphanum_secret(40)
    neo4j_password = alphanum_secret(24)

    return {
        # n8n
        "N8N_ENCRYPTION_KEY": hex_secret(32),
        "N8N_USER_MANAGEMENT_JWT_SECRET": hex_secret(32),

        # Pipelines
        "PIPELINES_API_KEY": alphanum_secret(32),

        # Supabase core
        "POSTGRES_PASSWORD": postgres_password,
        "JWT_SECRET": jwt_secret,
        "ANON_KEY": anon_key,
        "SERVICE_ROLE_KEY": service_role_key,
        "DASHBOARD_PASSWORD": alphanum_secret(24),
        "POOLER_TENANT_ID": str(secrets.randbelow(9000) + 1000),

        # Supabase Storage / S3
        "S3_PROTOCOL_ACCESS_KEY_ID": hex_secret(16),
        "S3_PROTOCOL_ACCESS_KEY_SECRET": hex_secret(32),

        # Supabase Meta
        "PG_META_CRYPTO_KEY": hex_secret(32),

        # Neo4j
        "NEO4J_AUTH": f"neo4j/{neo4j_password}",

        # Langfuse / ClickHouse / MinIO
        "CLICKHOUSE_PASSWORD": alphanum_secret(32),
        "MINIO_ROOT_PASSWORD": alphanum_secret(32),
        "LANGFUSE_SALT": hex_secret(32),
        "NEXTAUTH_SECRET": hex_secret(32),
        "ENCRYPTION_KEY": hex_secret(32),

        # Supavisor
        "SECRET_KEY_BASE": hex_secret(48),
        "VAULT_ENC_KEY": alphanum_secret(32),

        # Logflare
        "LOGFLARE_PUBLIC_ACCESS_TOKEN": hex_secret(32),
        "LOGFLARE_PRIVATE_ACCESS_TOKEN": hex_secret(32),
    }


# ---------------------------------------------------------------------------
# File processing
# ---------------------------------------------------------------------------

# Default placeholder values that indicate "not yet initialised"
DEFAULT_PLACEHOLDERS = {
    "super-secret-key",
    "even-more-secret",
    "0p3n-w3bu!",
    "your-super-secret-and-long-postgres-password",
    "your-super-secret-jwt-token-with-at-least-32-characters-long",
    "this_password_is_insecure_and_should_be_updated",
    "your-tenant-id",
    "neo4j/password",
    "super-secret-key-1",
    "super-secret-key-2",
    "super-secret-key-3",
    "super-secret-key-4",
    "generate-with-openssl",
    "generate-with-openssl # generate via `openssl rand -hex 32`",
    "your-super-secret-and-long-logflare-key-public",
    "your-super-secret-and-long-logflare-key-private",
    "your-32-character-encryption-key",
    # Supabase demo defaults that should be replaced
    "625729a08b95bf1b7ff351a663f3a23c",
    "850181e4652dd023b7a98c58ae0d2d34bd487ee0cc3254aed6eda37307425907",
    "UpNVntn3cDxHJpq99YMc1T1AQgQpc8kfYTuRgBiYa15BLrx8etQoXz3gZv1/u2oq",
    "your-pg-meta-crypto-key",
    "placeholder-needs-generation",
    "your-encryption-key-32-chars-min",
}

# Also match the demo JWT keys shipped in .env.example
DEFAULT_JWT_PREFIXES = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyAgCiAgICAicm9sZSI6ICJhbm9uIi",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyAgCiAgICAicm9sZSI6ICJzZXJ2aWNlX3JvbGUi",
)


def is_placeholder(value: str) -> bool:
    """Return True if the value looks like an unchanged placeholder."""
    stripped = value.strip()
    if stripped in DEFAULT_PLACEHOLDERS:
        return True
    for prefix in DEFAULT_JWT_PREFIXES:
        if stripped.startswith(prefix):
            return True
    return False


def process_env_template(template_path: str, generators: dict) -> str:
    """Read the template, replace placeholder values, return new content."""
    with open(template_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    output_lines = []
    replaced = set()

    for line in lines:
        stripped = line.strip()
        # Skip empty / comment lines
        if not stripped or stripped.startswith("#"):
            output_lines.append(line)
            continue

        # Parse KEY=VALUE (handle commented-out vars)
        if "=" in stripped:
            key, _, value = stripped.partition("=")
            key = key.strip()

            if key in generators and is_placeholder(value):
                new_value = generators[key]
                # Preserve original indentation
                leading_ws = line[: len(line) - len(line.lstrip())]
                output_lines.append(f"{leading_ws}{key}={new_value}\n")
                replaced.add(key)
                continue

        output_lines.append(line)

    return "".join(output_lines), replaced


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate cryptographically secure secrets for the .env file."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite output file if it already exists."
    )
    parser.add_argument(
        "--output", default=".env",
        help="Output file path (default: .env)."
    )
    parser.add_argument(
        "--template", default=".env.example",
        help="Template file path (default: .env.example)."
    )
    args = parser.parse_args()

    # Resolve paths relative to project root
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    template_path = os.path.join(project_root, args.template)
    output_path = os.path.join(project_root, args.output)

    if not os.path.exists(template_path):
        print(f"Error: template file not found: {template_path}", file=sys.stderr)
        sys.exit(1)

    if os.path.exists(output_path) and not args.force:
        print(f"Error: {output_path} already exists. Use --force to overwrite.", file=sys.stderr)
        sys.exit(1)

    generators = build_secret_generators()
    content, replaced = process_env_template(template_path, generators)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"Generated {len(replaced)} secrets in {output_path}:")
    for key in sorted(replaced):
        print(f"  - {key}")

    not_replaced = set(generators.keys()) - replaced
    if not_replaced:
        print(f"\nSkipped (value already customised or key not found in template):")
        for key in sorted(not_replaced):
            print(f"  - {key}")

    print("\nDone. Review the generated .env file before starting services.")


if __name__ == "__main__":
    main()
