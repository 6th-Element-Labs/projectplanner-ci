#!/usr/bin/env python3
"""Unit tests for github_app_auth: installation tokens off the App's own rate bucket.

Offline — the GitHub calls are injected. The real prod failure this guards is the
whole fleet sharing ONE user PAT's 5,000 req/hr, so every read 403s once the batch
timers have spent it (2026-07-26 incident). An App installation token is billed to
the installation, not to a human account.
"""
import json
import os
import sys
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import github_app_auth as ga  # noqa: E402

passed = failed = 0

# A throwaway RS256 key generated per-run; never a real credential.
_KEY_CACHE = {}


def test_private_key_pem():
    if "pem" not in _KEY_CACHE:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _KEY_CACHE["pem"] = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
    return _KEY_CACHE["pem"]


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    if condition:
        passed += 1
    else:
        failed += 1


def clear_env():
    for name in (ga.APP_ID_ENV, ga.INSTALLATION_ID_ENV, ga.PRIVATE_KEY_PATH_ENV,
                 ga.PRIVATE_KEY_ENV, ga.INSTALLATION_MAP_ENV,
                 "PM_GITHUB_TOKEN", "GITHUB_TOKEN", "SWITCHBOARD_CI_GITHUB_TOKEN"):
        os.environ.pop(name, None)
    ga.reset_cache()


def configure_app(pem=None):
    os.environ[ga.APP_ID_ENV] = "1234567"
    os.environ[ga.PRIVATE_KEY_ENV] = pem if pem is not None else test_private_key_pem()
    ga.reset_cache()


class FakeGitHub:
    """Records requests and answers the two endpoints the minter uses."""

    def __init__(self, *, expires_in=3600, mint_error=None, installation_id=99):
        self.calls = []
        self.expires_in = expires_in
        self.mint_error = mint_error
        self.installation_id = installation_id
        self.mint_count = 0

    def __call__(self, method, url, *, token="", now=None):
        self.calls.append((method, url, token))
        if url.endswith("/installation"):
            return {"id": self.installation_id}
        if url.endswith("/access_tokens"):
            if self.mint_error is not None:
                raise self.mint_error
            self.mint_count += 1
            expiry = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime((now or time.time()) + self.expires_in))
            return {"token": f"ghs_minted{self.mint_count}", "expires_at": expiry}
        raise AssertionError(f"unexpected url {url}")


print("\n-- falls back to the PAT chain when no App is configured --")
clear_env()
os.environ["SWITCHBOARD_CI_GITHUB_TOKEN"] = "tok-ci"
ok(ga.app_configured() is False, "app_configured() false with no App id/key")
ok(ga.resolve_token(repo="o/r") == "tok-ci", "no App -> falls back to the env PAT")
os.environ["PM_GITHUB_TOKEN"] = "tok-pm"
ok(ga.resolve_token(repo="o/r") == "tok-pm", "default env order prefers PM_GITHUB_TOKEN")
ok(ga.resolve_token(repo="o/r", env_order=("SWITCHBOARD_CI_GITHUB_TOKEN", "PM_GITHUB_TOKEN"))
   == "tok-ci", "caller-supplied env order is honoured (pr_gate prefers the CI token)")
ok(ga.resolve_token(repo="o/r", explicit="tok-explicit") == "tok-explicit",
   "an explicit token always wins")
ga.resolve_token(repo="o/r")   # re-resolve: the explicit call above set source="explicit"
ok(ga.token_source() == "env:PM_GITHUB_TOKEN", "token_source() names the env var used")

print("\n-- mints an installation token when the App IS configured --")
clear_env()
configure_app()
os.environ["PM_GITHUB_TOKEN"] = "tok-pat"
gh = FakeGitHub()
tok = ga.resolve_token(repo="6th-Element-Labs/projectplanner", request_fn=gh)
ok(tok == "ghs_minted1", "App configured -> returns the minted installation token, not the PAT")
ok(ga.token_source() == "github_app", "token_source() reports github_app")
methods = [c[0] for c in gh.calls]
urls = [c[1] for c in gh.calls]
ok(any(u.endswith("/repos/6th-Element-Labs/projectplanner/installation") for u in urls),
   "resolves the repo's installation id from the repo itself")
ok(any(u == "https://api.github.com/app/installations/99/access_tokens" for u in urls),
   "mints against that installation id")
ok(methods[-1] == "POST", "the mint call is a POST")

print("\n-- the minted token is cached, so polling does not re-mint every call --")
before = gh.mint_count
for _ in range(5):
    ga.resolve_token(repo="6th-Element-Labs/projectplanner", request_fn=gh)
ok(gh.mint_count == before, "repeat calls inside the TTL reuse the cached token")
ok(not any(u.endswith("/installation") for u in [c[1] for c in gh.calls[len(urls):]]),
   "the installation-id lookup is cached too")

print("\n-- refreshes before expiry, not after --")
clear_env()
configure_app()
gh = FakeGitHub(expires_in=3600)
t0 = 1_000_000.0
ga.resolve_token(repo="o/r", request_fn=gh, now=t0)
ga.resolve_token(repo="o/r", request_fn=gh, now=t0 + 3000)
ok(gh.mint_count == 1, "still cached 10 min before expiry")
ga.resolve_token(repo="o/r", request_fn=gh, now=t0 + 3400)
ok(gh.mint_count == 2, "re-mints inside the refresh skew, before the token actually expires")

print("\n-- separate installations get separate tokens (Helm is a personal-account install) --")
clear_env()
configure_app()


class TwoInstalls(FakeGitHub):
    def __call__(self, method, url, *, token="", now=None):
        if url.endswith("/installation"):
            self.calls.append((method, url, token))
            return {"id": 11 if "6th-Element-Labs" in url else 22}
        return super().__call__(method, url, token=token, now=now)


gh = TwoInstalls()
a = ga.resolve_token(repo="6th-Element-Labs/projectplanner", request_fn=gh)
b = ga.resolve_token(repo="StevenRidder/Helm", request_fn=gh)
ok(a != b, "each installation mints its own token")
minted = [c[1] for c in gh.calls if c[1].endswith("/access_tokens")]
ok("https://api.github.com/app/installations/11/access_tokens" in minted
   and "https://api.github.com/app/installations/22/access_tokens" in minted,
   "each repo mints against its own installation id")

print("\n-- an explicit installation map skips the discovery call --")
clear_env()
configure_app()
os.environ[ga.INSTALLATION_MAP_ENV] = json.dumps({"6th-Element-Labs": 77})
gh = FakeGitHub()
ga.resolve_token(repo="6th-Element-Labs/projectplanner", request_fn=gh)
ok(not any(u.endswith("/installation") for u in [c[1] for c in gh.calls]),
   "configured owner -> no installation discovery request")
ok(any(u == "https://api.github.com/app/installations/77/access_tokens"
       for u in [c[1] for c in gh.calls]), "uses the mapped installation id")

print("\n-- a single-installation deployment can pin the id outright --")
clear_env()
configure_app()
os.environ[ga.INSTALLATION_ID_ENV] = "55"
gh = FakeGitHub()
ga.resolve_token(repo="any/repo", request_fn=gh)
ok(any(u == "https://api.github.com/app/installations/55/access_tokens"
       for u in [c[1] for c in gh.calls]), "pinned installation id is used for every repo")

print("\n-- degrades to the PAT instead of taking the read path down --")
clear_env()
configure_app()
os.environ["PM_GITHUB_TOKEN"] = "tok-pat"
gh = FakeGitHub(mint_error=urllib.error.HTTPError(
    "https://api.github.com/app/installations/99/access_tokens", 401, "Bad credentials", {}, None))
tok = ga.resolve_token(repo="o/r", request_fn=gh)
ok(tok == "tok-pat", "mint failure falls back to the PAT rather than raising")
ok(ga.token_source() == "env:PM_GITHUB_TOKEN", "token_source() reports the fallback that was used")
ok("401" in (ga.last_error() or ""), "the real mint failure is kept, not discarded")

print("\n-- a broken key with no PAT reports the reason instead of returning junk --")
clear_env()
configure_app(pem="-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----\n")
gh = FakeGitHub()
tok = ga.resolve_token(repo="o/r", request_fn=gh)
ok(tok == "", "unusable key + no PAT -> empty token (callers surface no_github_token)")
ok(bool(ga.last_error()), "the key failure is recorded for diagnosis")

print("\n-- the App JWT is well-formed --")
clear_env()
configure_app()
pem = test_private_key_pem()
now = 1_700_000_000.0
encoded = ga.app_jwt(now=now)
import jwt as _jwt  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
pub = serialization.load_pem_private_key(pem.encode(), password=None).public_key()
# `now` is a fixed past timestamp, so verify the signature but not the clock.
claims = _jwt.decode(encoded, pub, algorithms=["RS256"],
                     options={"verify_aud": False, "verify_exp": False})
ok(claims["iss"] == "1234567", "iss is the App id")
ok(claims["iat"] <= int(now), "iat is backdated for clock skew")
ok(0 < claims["exp"] - int(now) <= 600, "exp is inside GitHub's 10-minute ceiling")
ok(_jwt.get_unverified_header(encoded)["alg"] == "RS256", "signed RS256")

print("\n-- the private key can come from a file, and is never echoed --")
clear_env()
import tempfile  # noqa: E402
with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as fh:
    fh.write(test_private_key_pem())
    key_path = fh.name
os.environ[ga.APP_ID_ENV] = "1234567"
os.environ[ga.PRIVATE_KEY_PATH_ENV] = key_path
ga.reset_cache()
ok(ga.app_configured() is True, "app_configured() true from a key file")
gh = FakeGitHub()
ok(ga.resolve_token(repo="o/r", request_fn=gh) == "ghs_minted1", "mints using the key file")
status = ga.status()
blob = json.dumps(status)
ok("PRIVATE KEY" not in blob and "ghs_" not in blob,
   "status() leaks neither the PEM nor the minted token")
ok(status.get("configured") is True and status.get("source") == "github_app",
   "status() reports configuration for the health surface")
os.unlink(key_path)

clear_env()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
