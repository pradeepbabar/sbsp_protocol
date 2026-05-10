# Contributing to SBSP

Thanks for your interest — this is an early research project and every contribution helps.

## Ways to contribute

- **Bug reports** — open an issue with the error, your OS, Docker version, and ContainerLab version
- **Code** — pick an open issue or one from the roadmap below
- **Tests** — more test coverage is always needed
- **Documentation** — protocol spec, architecture docs, diagrams
- **Discussion** — open an issue to discuss design decisions or the algorithm

## Getting started

```bash
git clone https://github.com/pradeepbabar/sbsp_protocol.git
cd sbsp_protocol
pip install -e ".[dev]"
pytest sbsp/tests/ -v
```

## Good first issues

These are well-scoped and don't require deep protocol knowledge:

- Add `exchange.py` skeleton with DBD send/receive stubs
- Write tests for `advertise.py` PrefixLsa encode/decode edge cases
- Add a `Makefile` with `make test`, `make build`, `make deploy` targets
- Add GitHub Actions CI (run pytest on every PR)
- Improve the `sbsp-show` CLI to display neighbour state and prefix table

## Larger contributions (discuss first)

- Full DBD/LSR/LSU/LSAck exchange (Phase 6)
- Parallel wave computation using `multiprocessing.Pool` (Phase 8)
- FRRouting OSPF benchmark topology (Phase 10)

## Pull request process

1. Fork the repo and create a branch from `main`
2. Write tests for any new behaviour
3. Make sure `pytest` passes
4. Open a PR with a clear description of what you changed and why

## Code style

- Python: follow PEP 8, type hints encouraged, docstrings for public functions
- Keep functions small and focused
- No hardcoded IPs or credentials anywhere

## Questions?

Open a GitHub issue with the `question` label.
