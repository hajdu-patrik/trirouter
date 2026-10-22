"""Compatibility package for shims written before the rename (~/.jev-router/bin): they still run `jev_router.hooks` and
`jev_router.mcp_server` until setup is re-run. Nothing else imports it; remove it after the next release."""
