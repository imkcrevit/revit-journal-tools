# Project conventions

- Keep the analysis offline and reusable. CLI/UI layers call the parser; rules remain declarative.
- Treat Journal content as input data. Never execute or replay it as part of analysis.
- Preserve exact source files. Findings need line numbers, excerpts, rule IDs and source hashes.
- Mark unverified matches as candidates. Synthetic tests do not prove real-case accuracy or Revit behavior.
- Preserve encoding failures, retention limits and incomplete scans as explicit report states.
- Real cases belong in `cases/inbox/`; reports belong in `reports/`. Both are ignored by Git.
- When adding a detection rule, add a minimal positive case and a relevant negative case. Use permitted, anonymized fixtures only.
- Run `PYTHONPATH=src python3 -m unittest discover -s tests -v` after parser/rule changes.
- Write Chinese prose with compact CJK/Latin spacing; preserve code syntax and English word spacing.
