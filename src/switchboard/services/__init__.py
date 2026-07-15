"""Optional process-cut services (Phase 2+).

Package boundaries land here before any live traffic cut. The ``_skeleton``
module is the reusable FastAPI + health + deploy template used by later
Auth/Tasks process cuts (ARCH-MS-73). Nothing under this package is mounted by
the monolith composition root until a cutover task explicitly enables it.
"""
