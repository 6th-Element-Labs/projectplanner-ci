"""Switchboard platform package (ARCH-MS / ADR-0009).

Modular monolith target: protocol adapters call application services; application
services call domain + storage repositories. Extraction to microservices happens
at repository boundaries in later phases.
"""

__version__ = "0.1.0"
