#!/usr/bin/env python3
"""Regression checks for the public IXP package docs.

Run:
    python3 test_public_protocol_package.py
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def require(text, needle, label):
    if needle not in text:
        raise AssertionError(f"{label} missing {needle!r}")


def main():
    package = read("docs/IXP-PUBLIC-PACKAGE.md")
    conformance = read("docs/IXP-CONFORMANCE.md")
    readme = read("README.md")
    adapters = read("adapters/README.md")
    spec = read("docs/IXP-SPEC.md")

    for needle in (
        "Apache-2.0",
        "hosted/commercial",
        "IXP-CONFORMANCE.md",
        "LICENSE",
        "Open the language. Sell the governed workplace.",
    ):
        require(package, needle, "public package doc")

    for needle in (
        "IXP-core conformant",
        "IXP-core + TXP/OXP tested",
        "Switchboard-compatible adapter",
        "python3 adapters/conformance.py --json",
        "mid-token interrupt",
        "hard process kill",
    ):
        require(conformance, needle, "conformance doc")

    require(readme, "docs/IXP-PUBLIC-PACKAGE.md", "README docs table")
    require(readme, "docs/IXP-CONFORMANCE.md", "README docs table")
    require(adapters, "../docs/IXP-CONFORMANCE.md", "adapter README")
    require(adapters, "IXP-core conformant", "adapter README")
    require(spec, "IXP-PUBLIC-PACKAGE.md", "IXP spec")
    require(spec, "IXP-CONFORMANCE.md", "IXP spec")


if __name__ == "__main__":
    main()
