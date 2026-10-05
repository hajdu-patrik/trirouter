"""Old hook shims run `jev_router.hooks`: this forwards to trirouter.hooks. A hook must never block or
crash the host tool, so even a broken import ends quietly with exit code 0."""
import sys


def main(argv=None):
    try:
        from trirouter.hooks import main as real_main
        return real_main(argv)
    except Exception:  # noqa: BLE001 - always exit 0
        return 0


if __name__ == "__main__":
    sys.exit(main())
