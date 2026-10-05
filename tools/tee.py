"""Copy stdin to stdout and to a log file (the launchers pipe the server through this)."""
import sys

with open(sys.argv[1], "a", encoding="utf-8", errors="replace") as log:
    for line in sys.stdin:
        sys.stdout.write(line)
        sys.stdout.flush()
        log.write(line)
        log.flush()
