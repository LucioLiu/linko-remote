"""Frozen desktop service entry point. Both platforms use the same UI/API."""
from lanpilot.peer_client import main

if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == '--apply-update':
        from lanpilot.updater import main as apply_update
        apply_update()
    else:
        main()
