"""Compatibility package: the project was called jev-router before it became trirouter.

Shims that `trirouter setup` wrote before the rename (in ~/.jev-router/bin) still run
`jev_router.hooks`, `jev_router.mcp_server` and `python -m jev_router`. This package keeps them working
after `git pull`, until the user re-runs setup, which rewrites the shims to call `trirouter` directly.
Nothing else imports it. Remove it after the next release.
"""
